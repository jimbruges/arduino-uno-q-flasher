"""Capture reusable workshop artifacts from a prepared UNO Q."""
from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import shutil
import sys
import tarfile
import time
from pathlib import Path
from typing import Awaitable, Callable, Iterable

from . import adb

IMAGE_ARCHIVE_NAME = "docker-images.tar"
ARDUINO_ARCHIVE_NAME = "arduino-data.tar.gz"
APP_RUNTIME_ARCHIVE_NAME = "app-runtime.tar.gz"
APPS_ARCHIVE_NAME = "apps.tar.gz"
MODELS_DIR_NAME = "models"
MANIFEST_NAME = "manifest.json"
REMOTE_MODELS_ROOT = "/var/lib/arduino-app-cli/models"
REMOTE_APPS_PARENT = "/home/arduino"
REMOTE_APPS_DIR_NAME = "ArduinoApps"

LogFn = Callable[[str], Awaitable[None]]

# Lists every model file as "<relative path>\t<size>" so the host can compare a
# board's model tree against the cached copy by exact file sizes.
REMOTE_MODEL_LISTING_CMD = (
    f"cd {REMOTE_MODELS_ROOT} 2>/dev/null && "
    "find . -mindepth 1 -type f -printf '%P\\t%s\\n'"
)


class WorkshopCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._capture_lock = asyncio.Lock()
        self._capture_all_lock = asyncio.Lock()
        self._models_lock = asyncio.Lock()
        self._verified_signature: tuple | None = None
        self._verification: dict = {"integrity": "unchecked", "integrity_errors": []}

    @property
    def image_archive(self) -> Path:
        return self.root / IMAGE_ARCHIVE_NAME

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    @property
    def arduino_archive(self) -> Path:
        return self.root / ARDUINO_ARCHIVE_NAME

    @property
    def app_runtime_archive(self) -> Path:
        return self.root / APP_RUNTIME_ARCHIVE_NAME

    @property
    def apps_archive(self) -> Path:
        return self.root / APPS_ARCHIVE_NAME

    @property
    def models_dir(self) -> Path:
        return self.root / MODELS_DIR_NAME

    @property
    def models_manifest_path(self) -> Path:
        # Kept apart from the archive manifest: models are captured
        # incrementally and must survive an archive capture rollback.
        return self.models_dir / MANIFEST_NAME

    def models_manifest(self) -> dict:
        try:
            return json.loads(self.models_manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def status(self) -> dict:
        manifest: dict = {}
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        models = self.models_manifest()
        signature = self._archive_signature()
        verification = self._verification
        if signature != self._verified_signature:
            verification = {"integrity": "unchecked", "integrity_errors": []}
        return {
            "images_ready": self.image_archive.is_file(),
            "image_archive_bytes": (
                self.image_archive.stat().st_size
                if self.image_archive.is_file()
                else 0
            ),
            "images": manifest.get("images", []),
            "source_device": manifest.get("source_device"),
            "captured_at": manifest.get("captured_at"),
            "sha256": manifest.get("sha256"),
            "arduino_ready": self.arduino_archive.is_file(),
            "arduino_archive_bytes": (
                self.arduino_archive.stat().st_size
                if self.arduino_archive.is_file()
                else 0
            ),
            "arduino_sha256": manifest.get("arduino_sha256"),
            "app_runtime_ready": self.app_runtime_archive.is_file(),
            "app_runtime_archive_bytes": (
                self.app_runtime_archive.stat().st_size
                if self.app_runtime_archive.is_file()
                else 0
            ),
            "app_runtime_sha256": manifest.get("app_runtime_sha256"),
            "apps_ready": self.apps_archive.is_file(),
            "apps_archive_bytes": (
                self.apps_archive.stat().st_size if self.apps_archive.is_file() else 0
            ),
            "apps": manifest.get("apps", []),
            "apps_sha256": manifest.get("apps_sha256"),
            "models_ready": bool(models.get("units")),
            "models": [
                {"path": unit["path"], "bytes": unit["bytes"], "files": len(unit["files"])}
                for unit in models.get("units", [])
            ],
            "models_bytes": sum(unit["bytes"] for unit in models.get("units", [])),
            "models_source_device": models.get("source_device"),
            **verification,
        }

    async def verify(self) -> dict:
        signature = self._archive_signature()
        if signature == self._verified_signature:
            return self.status()
        async with self._capture_lock:
            signature = self._archive_signature()
            if signature != self._verified_signature:
                errors = await asyncio.to_thread(self._verify_archives)
                self._verified_signature = signature
                self._verification = {
                    "integrity": "invalid" if errors else "verified",
                    "integrity_errors": errors,
                    "verified_at": int(time.time()),
                }
        return self.status()

    def _archive_signature(self) -> tuple:
        return tuple(
            (path.name, path.stat().st_size, path.stat().st_mtime_ns)
            for path in (
                self.image_archive,
                self.arduino_archive,
                self.app_runtime_archive,
                self.apps_archive,
                self.models_manifest_path,
            )
            if path.is_file()
        )

    def _verify_archives(self) -> list[str]:
        manifest = self._read_manifest()
        checks = (
            (self.image_archive, "sha256", _validate_docker_archive),
            (
                self.arduino_archive,
                "arduino_sha256",
                lambda path: _validate_tar_archive(path, ".arduino15/"),
            ),
            (
                self.app_runtime_archive,
                "app_runtime_sha256",
                lambda path: _validate_tar_archive(path, "/.cache/"),
            ),
            (
                self.apps_archive,
                "apps_sha256",
                lambda path: _validate_tar_archive(path, f"{REMOTE_APPS_DIR_NAME}/"),
            ),
        )
        errors: list[str] = []
        for archive, digest_key, validator in checks:
            if not archive.is_file():
                continue
            expected = manifest.get(digest_key)
            if not expected:
                errors.append(f"{archive.name}: checksum is missing from manifest")
                continue
            actual = _sha256(archive)
            if actual != expected:
                errors.append(f"{archive.name}: checksum does not match")
                continue
            try:
                validator(archive)
            except (OSError, ValueError) as exc:
                errors.append(f"{archive.name}: {exc}")
        for unit in self.models_manifest().get("units", []):
            mismatch = _model_unit_mismatch(self.models_dir / unit["path"], unit["files"])
            if mismatch:
                errors.append(f"model {unit['path']}: {mismatch}")
        return errors

    async def capture_images(self, serial: str) -> dict:
        async with self._capture_lock:
            rc, output = await adb.shell(
                serial,
                "docker images --format '{{.Repository}}:{{.Tag}}'",
            )
            if rc != 0:
                raise RuntimeError(output or "could not list Docker images")
            images = sorted(
                {
                    line.strip()
                    for line in output.splitlines()
                    if line.strip() and "<none>" not in line
                }
            )
            if not images:
                raise RuntimeError("the reference board has no tagged Docker images")

            rc, error = await adb.exec_out_to_file(
                serial,
                [
                    "sh",
                    "-c",
                    f"docker save {' '.join(shlex.quote(image) for image in images)} | cat",
                ],
                self.image_archive,
                _validate_docker_archive,
            )
            if rc != 0:
                raise RuntimeError(error or "docker save failed")

            digest = await asyncio.to_thread(_sha256, self.image_archive)
            manifest = self._read_manifest()
            manifest.update({
                "version": 1,
                "source_device": serial,
                "captured_at": int(time.time()),
                "images": images,
                "size_bytes": self.image_archive.stat().st_size,
                "sha256": digest,
            })
            self._write_manifest(manifest)
            return self.status()

    async def capture_arduino_data(self, serial: str) -> dict:
        async with self._capture_lock:
            rc, error = await adb.exec_out_to_file(
                serial,
                [
                    "sh",
                    "-c",
                    "tar -C /home/arduino --exclude=.arduino15/tmp "
                    "-czf - .arduino15 | cat",
                ],
                self.arduino_archive,
                lambda path: _validate_tar_archive(path, ".arduino15/"),
            )
            if rc != 0:
                raise RuntimeError(error or "Arduino data export failed")

            manifest = self._read_manifest()
            manifest.update(
                {
                    "version": 1,
                    "source_device": serial,
                    "captured_at": int(time.time()),
                    "arduino_size_bytes": self.arduino_archive.stat().st_size,
                    "arduino_sha256": await asyncio.to_thread(
                        _sha256, self.arduino_archive
                    ),
                }
            )
            self._write_manifest(manifest)
            return self.status()

    async def capture_all(
        self,
        serial: str,
        log: LogFn | None = None,
        seed_dirs: Iterable[Path] = (),
    ) -> dict:
        async with self._capture_all_lock:
            paths = (
                self.image_archive,
                self.arduino_archive,
                self.app_runtime_archive,
                self.apps_archive,
                self.manifest_path,
            )
            backups: dict[Path, Path] = {}
            for path in paths:
                backup = path.with_name(path.name + ".backup")
                backup.unlink(missing_ok=True)
                if path.exists():
                    path.replace(backup)
                    backups[path] = backup
            try:
                # adb exec-out occasionally returns rc=0 with a truncated/empty
                # stream right after the board reboots from a sketch upload;
                # retry each transfer a couple of times before giving up.
                await _retry_capture(lambda: self.capture_arduino_data(serial))
                await _retry_capture(lambda: self.capture_app_runtime(serial))
                await _retry_capture(lambda: self.capture_apps(serial))
                await _retry_capture(lambda: self.capture_images(serial))
                # Models are captured last and incrementally (unit by unit), so
                # a failure here keeps every model already cached.
                result = await self.capture_models(serial, log=log, seed_dirs=seed_dirs)
            except BaseException:
                for path in paths:
                    path.unlink(missing_ok=True)
                for path, backup in backups.items():
                    backup.replace(path)
                raise
            else:
                for backup in backups.values():
                    backup.unlink(missing_ok=True)
                return result

    async def capture_app_runtime(self, serial: str) -> dict:
        async with self._capture_lock:
            command = (
                "cd / && "
                "find var/lib/arduino-app-cli/examples "
                "home/arduino/.local/share/arduino-app-cli/examples "
                "home/arduino/ArduinoApps -type d -name .cache -prune -print0 "
                "2>/dev/null | tar --null -T - -czf - | cat"
            )
            rc, error = await adb.exec_out_to_file(
                serial,
                ["sh", "-c", command],
                self.app_runtime_archive,
                lambda path: _validate_tar_archive(path, "/.cache/"),
            )
            if rc != 0:
                raise RuntimeError(error or "App runtime cache export failed")
            manifest = self._read_manifest()
            manifest.update(
                {
                    "version": 1,
                    "source_device": serial,
                    "captured_at": int(time.time()),
                    "app_runtime_size_bytes": self.app_runtime_archive.stat().st_size,
                    "app_runtime_sha256": await asyncio.to_thread(
                        _sha256, self.app_runtime_archive
                    ),
                }
            )
            self._write_manifest(manifest)
            return self.status()

    async def capture_apps(self, serial: str) -> dict:
        """Archive the board's own App Lab apps (sources only; `.cache` dirs go
        into the app runtime archive)."""
        async with self._capture_lock:
            rc, output = await adb.shell(
                serial,
                f"cd {REMOTE_APPS_PARENT}/{REMOTE_APPS_DIR_NAME} 2>/dev/null && "
                "find . -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '%P\\n'",
            )
            apps = sorted(line.strip() for line in output.splitlines() if line.strip())
            manifest = self._read_manifest()
            if rc != 0 or not apps:
                self.apps_archive.unlink(missing_ok=True)
                manifest.update({"apps": [], "apps_sha256": None, "apps_size_bytes": 0})
                self._write_manifest(manifest)
                return self.status()
            rc, error = await adb.exec_out_to_file(
                serial,
                [
                    "sh",
                    "-c",
                    f"tar -C {REMOTE_APPS_PARENT} --exclude=.cache -czf - "
                    f"{REMOTE_APPS_DIR_NAME} | cat",
                ],
                self.apps_archive,
                lambda path: _validate_tar_archive(path, f"{REMOTE_APPS_DIR_NAME}/"),
            )
            if rc != 0:
                raise RuntimeError(error or "App Lab apps export failed")
            manifest.update(
                {
                    "version": 1,
                    "source_device": serial,
                    "captured_at": int(time.time()),
                    "apps": apps,
                    "apps_size_bytes": self.apps_archive.stat().st_size,
                    "apps_sha256": await asyncio.to_thread(_sha256, self.apps_archive),
                }
            )
            self._write_manifest(manifest)
            return self.status()

    async def capture_models(
        self,
        serial: str,
        log: LogFn | None = None,
        seed_dirs: Iterable[Path] = (),
    ) -> dict:
        """Mirror the board's model tree (REMOTE_MODELS_ROOT) into the cache.

        Units (`<family>/<name>`, e.g. `genai/qwen3_vl_8b_...`) that are already
        cached with identical file sizes are skipped. Missing units are seeded
        from `seed_dirs` (e.g. a pull-models.sh download) when a matching copy
        exists there, otherwise pulled over USB.
        """

        async def note(line: str) -> None:
            if log is not None:
                await log(line)

        async with self._models_lock:
            rc, output = await adb.shell(serial, REMOTE_MODEL_LISTING_CMD)
            units = group_model_files(output) if rc == 0 else {}
            if not units:
                await note(
                    f"No models found under {REMOTE_MODELS_ROOT} on {serial}; "
                    "keeping the existing model cache."
                )
                return self.status()

            self.models_dir.mkdir(parents=True, exist_ok=True)
            seeds = [Path(seed) for seed in seed_dirs]
            for unit_path, files in sorted(units.items()):
                local = self.models_dir / unit_path
                total = sum(files.values())
                if _model_unit_mismatch(local, files) is None:
                    await note(f"Model {unit_path} is already cached ({_mb(total)} MB).")
                    continue
                partial = local.with_name(f".{local.name}.part")
                _remove_path(partial)
                local.parent.mkdir(parents=True, exist_ok=True)

                seeded = False
                for seed in seeds:
                    candidate = seed / local.name
                    if _model_unit_mismatch(candidate, files) is None:
                        await note(f"Copying model {unit_path} from {candidate}...")
                        await _clone_path(candidate, partial)
                        seeded = True
                        break
                if not seeded:
                    await note(
                        f"Pulling model {unit_path} ({_mb(total)} MB) from {serial}; "
                        "this can take several minutes..."
                    )
                    rc, error = await adb.pull(
                        serial, f"{REMOTE_MODELS_ROOT}/{unit_path}", partial
                    )
                    if rc != 0:
                        _remove_path(partial)
                        raise RuntimeError(f"pulling model {unit_path} failed: {error}")
                mismatch = _model_unit_mismatch(partial, files)
                if mismatch:
                    _remove_path(partial)
                    raise RuntimeError(f"cached model {unit_path} is incomplete: {mismatch}")
                _remove_path(local)
                partial.replace(local)
                await note(f"Cached model {unit_path} ({_mb(total)} MB).")

            # Mirror the reference board: drop cached units it no longer has.
            for family in self.models_dir.iterdir():
                if not family.is_dir():
                    continue
                for entry in family.iterdir():
                    if f"{family.name}/{entry.name}" not in units:
                        _remove_path(entry)
                if not any(family.iterdir()):
                    family.rmdir()

            manifest = {
                "version": 1,
                "source_device": serial,
                "captured_at": int(time.time()),
                "units": [
                    {"path": path, "bytes": sum(files.values()), "files": files}
                    for path, files in sorted(units.items())
                ],
            }
            temp = self.models_manifest_path.with_suffix(".json.part")
            temp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            temp.replace(self.models_manifest_path)
            return self.status()

    def _read_manifest(self) -> dict:
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_manifest(self, manifest: dict) -> None:
        temp = self.manifest_path.with_suffix(".json.part")
        temp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        temp.replace(self.manifest_path)


async def _retry_capture(action, attempts: int = 3, delay: float = 2.0):
    """Retry a single capture step; adb exec-out can flake right after reboot."""
    last_error: RuntimeError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await action()
        except RuntimeError as exc:
            last_error = exc
            if attempt < attempts:
                await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error


def group_model_files(listing: str) -> dict[str, dict[str, int]]:
    """Group REMOTE_MODEL_LISTING_CMD output into model units.

    A unit is the first two path components (`genai/<model>`, or a file such
    as `llamacpp/models.ini`); its files map relative path -> size, with ""
    standing for a unit that is itself a single file.
    """
    units: dict[str, dict[str, int]] = {}
    for raw in listing.splitlines():
        path, sep, size = raw.rstrip("\r").rpartition("\t")
        if not sep or not size.isdigit() or not path:
            continue
        parts = path.split("/")
        if any(part.startswith(".") and part.endswith(".part") for part in parts[:2]):
            continue
        unit = "/".join(parts[:2])
        relative = "/".join(parts[2:])
        units.setdefault(unit, {})[relative] = int(size)
    return units


def _model_unit_mismatch(path: Path, files: dict[str, int]) -> str | None:
    """Return why `path` differs from the expected file sizes, or None."""
    for relative, size in files.items():
        candidate = path / relative if relative else path
        try:
            actual = candidate.stat().st_size if candidate.is_file() else None
        except OSError:
            actual = None
        if actual is None:
            return f"{relative or path.name} is missing"
        if actual != size:
            return f"{relative or path.name} is {actual} bytes, expected {size}"
    return None


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


async def _clone_path(source: Path, target: Path) -> None:
    # On APFS `cp -c` makes copy-on-write clones, so seeding takes seconds and no
    # extra disk space; fall back to a normal copy elsewhere.
    if sys.platform == "darwin":
        proc = await asyncio.create_subprocess_exec("cp", "-c", "-R", str(source), str(target))
        if await proc.wait() == 0:
            return
        _remove_path(target)
    if source.is_dir():
        await asyncio.to_thread(shutil.copytree, source, target)
    else:
        await asyncio.to_thread(shutil.copy2, source, target)


def _mb(size: int) -> int:
    return size // (1024 * 1024)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_tar_archive(path: Path, required_name: str) -> None:
    try:
        with tarfile.open(path, "r:*") as archive:
            if not any(required_name in member.name for member in archive):
                raise ValueError(f"archive does not contain {required_name}")
    except tarfile.TarError as exc:
        remote_error = _sniff_remote_error(path)
        if remote_error:
            raise ValueError(f"remote command failed: {remote_error}") from exc
        raise ValueError(f"invalid tar archive: {exc}") from exc


def _sniff_remote_error(path: Path, max_bytes: int = 4096) -> str | None:
    """adb exec-out can't propagate the remote shell's exit code, so a failing
    command (e.g. 'docker save' hitting ENOSPC) silently writes its plain-text
    error message into what should have been the archive. Detect that case so
    the real cause is surfaced instead of a confusing tar-parsing error.
    """
    try:
        size = path.stat().st_size
        if size == 0 or size > max_bytes:
            return None
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    text = text.strip()
    if not text or any(not ch.isprintable() and ch not in "\n\r\t" for ch in text):
        return None
    return text


def _validate_docker_archive(path: Path) -> None:
    _validate_tar_archive(path, "manifest.json")