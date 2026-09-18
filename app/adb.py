"""Async wrappers around the `adb` CLI.

All functions stream stdout/stderr line-by-line via an async callback so the
caller can forward log lines to a WebSocket without buffering.
"""
from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

LineCallback = Callable[[str, str], Awaitable[None]]  # (line, stream)


class AdbNotFoundError(RuntimeError):
    pass


def adb_path() -> str:
    path = shutil.which("adb")
    if not path:
        raise AdbNotFoundError(
            "adb was not found on PATH. Install Android platform-tools and "
            "ensure `adb` is available."
        )
    return path


@dataclass(frozen=True)
class AdbDevice:
    serial: str
    state: str  # "device", "offline", "unauthorized", ...


async def list_devices() -> list[AdbDevice]:
    """Return only devices in the 'device' (online & authorized) state."""
    proc = await asyncio.create_subprocess_exec(
        adb_path(),
        "devices",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, _ = await proc.communicate()
    if proc.returncode != 0:
        return []
    devices: list[AdbDevice] = []
    for raw in stdout_b.decode(errors="replace").splitlines()[1:]:
        parts = raw.strip().split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(AdbDevice(serial=parts[0], state=parts[1]))
    return devices


async def _run_streaming(
    args: list[str],
    on_line: LineCallback | None,
) -> tuple[int, str]:
    """Run `adb` with the given args. Stream stdout/stderr to `on_line`.

    Returns (returncode, combined_output).
    """
    proc = await asyncio.create_subprocess_exec(
        adb_path(),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    collected: list[str] = []

    async def pump(stream: asyncio.StreamReader, label: str) -> None:
        while True:
            chunk = await stream.readline()
            if not chunk:
                return
            line = chunk.decode(errors="replace").rstrip("\r\n")
            if not line:
                continue
            collected.append(line)
            if on_line is not None:
                await on_line(line, label)

    assert proc.stdout is not None and proc.stderr is not None
    await asyncio.gather(
        pump(proc.stdout, "stdout"),
        pump(proc.stderr, "stderr"),
    )
    rc = await proc.wait()
    return rc, "\n".join(collected)


async def push(
    serial: str,
    local: Path,
    remote: str,
    on_line: LineCallback | None = None,
) -> tuple[int, str]:
    return await _run_streaming(
        ["-s", serial, "push", str(local), remote], on_line
    )


async def shell(
    serial: str,
    command: str,
    on_line: LineCallback | None = None,
) -> tuple[int, str]:
    return await _run_streaming(
        ["-s", serial, "shell", command], on_line
    )


async def reverse(
    serial: str,
    remote: str,
    local: str,
    on_line: LineCallback | None = None,
) -> tuple[int, str]:
    return await _run_streaming(
        ["-s", serial, "reverse", remote, local], on_line
    )


async def remove_reverse(
    serial: str,
    remote: str,
    on_line: LineCallback | None = None,
) -> tuple[int, str]:
    return await _run_streaming(
        ["-s", serial, "reverse", "--remove", remote], on_line
    )


async def exec_out_to_file(
    serial: str,
    remote_args: list[str],
    target: Path,
    validate: Callable[[Path], None] | None = None,
) -> tuple[int, str]:
    """Stream binary stdout from `adb exec-out` into a local file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".part")
    with temp.open("wb") as output:
        proc = await asyncio.create_subprocess_exec(
            adb_path(),
            "-s",
            serial,
            "exec-out",
            *remote_args,
            stdout=output,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_b = await proc.communicate()
    stderr = stderr_b.decode(errors="replace").strip()
    if proc.returncode != 0:
        temp.unlink(missing_ok=True)
        return proc.returncode, stderr
    try:
        if validate is not None:
            validate(temp)
        temp.replace(target)
    except (OSError, ValueError) as exc:
        temp.unlink(missing_ok=True)
        return 1, str(exc)
    return 0, stderr


async def stream_file_to_shell(
    serial: str,
    source: Path,
    command: str,
    on_line: LineCallback | None = None,
) -> tuple[int, str]:
    """Stream a local binary file to a remote command's stdin."""
    with source.open("rb") as input_file:
        proc = await asyncio.create_subprocess_exec(
            adb_path(),
            "-s",
            serial,
            "shell",
            command,
            stdin=input_file,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
    output: list[str] = []
    for label, data in (("stdout", stdout_b), ("stderr", stderr_b)):
        for line in data.decode(errors="replace").splitlines():
            output.append(line)
            if on_line is not None:
                await on_line(line, label)
    return proc.returncode, "\n".join(output)
