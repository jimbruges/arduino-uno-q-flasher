"""Per-device flashing workflow.

Translates the `process_device()` function from the original bash script into an
async state machine that emits structured events for the UI.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shlex
import time
from pathlib import Path
from typing import Awaitable, Callable

from . import adb
from .events import (
    ALL_STAGES,
    DeviceFinishedEvent,
    DeviceRetryEvent,
    DeviceStartedEvent,
    Event,
    LogEvent,
    OPTIONAL_STAGES,
    SetupSummaryEvent,
    Stage,
    StageEvent,
)
from .parser import SetupOutputParser
from .package_cache import CACHE_PORT

EmitFn = Callable[[Event], Awaitable[None]]
AfterSuccessFn = Callable[[str, EmitFn], Awaitable[tuple[bool, str | None]]]

SETUP_SCRIPT_NAME = "unoq-setup.sh"
PROPERTIES_FILE_NAME = "properties.msgpack"
PROPERTIES_TARGET_PATHS = (
    "/var/lib/arduino-app-cli/properties.msgpack",
    "/home/arduino/.local/share/arduino-app-cli/properties.msgpack",
    "/tmp/properties.msgpack",
)
APPS_TARGET_DIR = "/home/arduino/ArduinoApps/"
REMOTE_SETUP_SCRIPT_PATH = f"/home/arduino/.{SETUP_SCRIPT_NAME}"
REMOTE_ENV_PATH = "/home/arduino/.env"
MODEL_BUNDLES_DIR_NAME = "model-bundles"
REMOTE_MODELS_DIR = "/var/lib/arduino-app-cli/models"
REMOTE_MODEL_STAGING_DIR = "/home/arduino/.unoq-model-bundles"
DOCKER_IMAGE_ARCHIVE = Path(".cache/workshop/docker-images.tar")
ARDUINO_DATA_ARCHIVE = Path(".cache/workshop/arduino-data.tar.gz")
APP_RUNTIME_ARCHIVE = Path(".cache/workshop/app-runtime.tar.gz")

PASSWORD_SUCCESS_RE = re.compile(
    r"password updated successfully"
    r"|all authentication tokens updated successfully"
    r"|passwd: password changed",
    re.IGNORECASE,
)
PASSWORD_NEEDS_CURRENT_RE = re.compile(
    r"current password|authentication failure|password unchanged",
    re.IGNORECASE,
)
PASSWORD_TOKEN_ERROR_RE = re.compile(
    r"authentication token manipulation error", re.IGNORECASE
)

# Set the green + red user LEDs to a solid on/off state at the end of a flash.
# Two shell vars ($GREEN, $RED) are prepended when invoked so the script itself
# stays a constant. Probes the same node-name variants that IDENTIFY_BLINK_CMD
# does — kernel/image versions differ slightly (unoq:user-red1 vs. red:user).
END_STATE_LED_CMD = r"""
set +e
find_led() {
  local color="$1"
  for c in \
      /sys/class/leds/unoq:user-${color}1 \
      /sys/class/leds/unoq:user-${color} \
      /sys/class/leds/${color}:user \
      /sys/class/leds/user:${color}; do
    if [ -e "$c/brightness" ]; then
      echo "$c"; return 0
    fi
  done
  for g in /sys/class/leds/*${color}* /sys/class/unoq:*${color}*; do
    [ -e "$g/brightness" ] && echo "$g" && return 0
  done
  return 1
}

write_sysfs() {
  local file="$1" value="$2"
  if printf '%s' "$value" > "$file" 2>/dev/null; then return 0; fi
  if printf '%s\n' "$value" | tee "$file" >/dev/null 2>&1; then return 0; fi
  if printf '%s\n' "$value" | sudo -n tee "$file" >/dev/null 2>&1; then return 0; fi
  return 1
}

set_led() {
  local color="$1" state="$2"
  local led
  led=$(find_led "$color")
  if [ -z "$led" ]; then
    echo "[led] no $color LED found (wanted state=$state)"
    return 0
  fi
  local max val
  max=$(cat "$led/max_brightness" 2>/dev/null || echo 255)
  write_sysfs "$led/trigger" none || true
  if [ "$state" = "on" ]; then val="$max"; else val="0"; fi
  if write_sysfs "$led/brightness" "$val"; then
    echo "[led] $color=$state at $led"
  else
    echo "[led] failed to write $led/brightness"
  fi
}

set_led "green" "$GREEN"
set_led "red" "$RED"
exit 0
"""


def _end_state_led_cmd(*, success: bool) -> str:
    green = "on" if success else "off"
    red = "off" if success else "on"
    return f"GREEN={green}\nRED={red}\n{END_STATE_LED_CMD}"


class FlasherContext:
    """Per-run configuration shared across all devices."""

    def __init__(
        self,
        app_folder: Path | None,
        setup_script: Path,
        env_file: Path | None,
        unoq_default_password: str | None,
        project_root: Path,
        post_update_cmd: str | None = None,
        prune_docker_before_post_update: bool = False,
        use_package_cache: bool = True,
        max_parallel_updates: int = 4,
        prepare_uploaded_app: bool = False,
        example_apps: list[str] | None = None,
    ) -> None:
        self.app_folder = app_folder
        self.setup_script = setup_script
        self.env_file = env_file
        self.unoq_default_password = unoq_default_password
        self.project_root = project_root
        self.post_update_cmd = post_update_cmd
        self.prune_docker_before_post_update = prune_docker_before_post_update
        self.use_package_cache = use_package_cache
        self.update_semaphore = asyncio.Semaphore(max_parallel_updates)
        self.artifact_semaphore = asyncio.Semaphore(2)
        self.prepare_uploaded_app = prepare_uploaded_app
        self.example_apps = tuple(example_apps or ())

    @property
    def properties_file(self) -> Path | None:
        if self.app_folder is not None:
            candidate = self.app_folder / PROPERTIES_FILE_NAME
            if candidate.is_file():
                return candidate
        candidate2 = self.project_root / PROPERTIES_FILE_NAME
        if candidate2.is_file():
            return candidate2
        return None

    @property
    def model_bundle_dir(self) -> Path | None:
        candidate = self.project_root / MODEL_BUNDLES_DIR_NAME
        if candidate.is_dir() and any(candidate.iterdir()):
            return candidate
        return None

    @property
    def docker_image_archive(self) -> Path | None:
        candidate = self.project_root / DOCKER_IMAGE_ARCHIVE
        return candidate if candidate.is_file() else None

    @property
    def arduino_data_archive(self) -> Path | None:
        candidate = self.project_root / ARDUINO_DATA_ARCHIVE
        return candidate if candidate.is_file() else None

    @property
    def app_runtime_archive(self) -> Path | None:
        candidate = self.project_root / APP_RUNTIME_ARCHIVE
        return candidate if candidate.is_file() else None


async def flash_device(
    serial: str,
    ctx: FlasherContext,
    skip_stages: set[Stage],
    emit: EmitFn,
    after_success: AfterSuccessFn | None = None,
) -> bool:
    """Run the full 7-stage workflow for one device.

    Returns True on success, False on failure. Errors in optional stages do not
    fail the run; errors in required stages do.

    On a WiFi failure ("No network with SSID X found", missing on-device
    UNOQ_WIFI_SSID / UNOQ_WIFI_PASSWORD, or a missing /home/arduino/.env), all
    stages are re-run once and push_env is force-included on the retry — the
    on-device .env may be missing, empty, or hold stale credentials that don't
    match the local file.
    """
    start_time = time.monotonic()

    await emit(DeviceStartedEvent(device=serial))

    async def log(line: str, stream: str = "info", stage: Stage | None = None) -> None:
        await emit(LogEvent(device=serial, stage=stage, line=line, stream=stream))  # type: ignore[arg-type]

    max_attempts = 2
    attempt = 0
    last_hint = None
    last_reason: str | None = None
    current_skip = set(skip_stages)

    while attempt < max_attempts:
        attempt += 1
        parser = SetupOutputParser()

        ok, reason = await _run_stages(serial, ctx, current_skip, emit, parser, log)
        _, hint = parser.finish()

        if ok:
            await _log_arduino_cli_version(serial, log=log)
            if after_success is not None:
                hook_ok, hook_reason = await after_success(serial, emit)
                if not hook_ok:
                    await _set_end_state_led(serial, success=False, log=log)
                    await emit(
                        DeviceFinishedEvent(
                            device=serial,
                            result="failed",
                            elapsed_seconds=round(time.monotonic() - start_time, 1),
                            failure_reason=hook_reason,
                        )
                    )
                    return False
            await _set_end_state_led(serial, success=True, log=log)
            await emit(
                DeviceFinishedEvent(
                    device=serial,
                    result="success",
                    elapsed_seconds=round(time.monotonic() - start_time, 1),
                )
            )
            return True

        last_hint = hint
        last_reason = reason

        # On WiFi failure: force-push local .env (even if the user originally
        # skipped Step 1) and re-run all stages. The device's on-device .env
        # may be missing, empty, or hold stale credentials that don't match
        # what's in the local file.
        wifi_retry_possible = (
            attempt < max_attempts
            and hint is not None
            and hint.code == "wifi_failed"
            and ctx.env_file is not None
        )
        if wifi_retry_possible:
            if "push_env" in current_skip:
                await log(
                    "WiFi failure detected and push_env was skipped. "
                    "Overriding skip so local .env is pushed on retry.",
                )
                current_skip = current_skip - {"push_env"}
            await log(
                f"WiFi failure detected. Re-pushing local .env and retrying "
                f"all stages (attempt {attempt + 1}/{max_attempts})...",
            )
            await emit(
                DeviceRetryEvent(
                    device=serial,
                    attempt=attempt + 1,
                    max_attempts=max_attempts,
                    reason="WiFi failure — re-pushing .env and retrying.",
                )
            )
            continue
        break

    await _set_end_state_led(serial, success=False, log=log)
    await emit(
        DeviceFinishedEvent(
            device=serial,
            result="failed",
            elapsed_seconds=round(time.monotonic() - start_time, 1),
            failure_reason=(last_hint.message if last_hint else last_reason),
        )
    )
    return False


async def _run_stages(
    serial: str,
    ctx: FlasherContext,
    skip_stages: set[Stage],
    emit: EmitFn,
    parser: SetupOutputParser,
    log: Callable[..., Awaitable[None]],
) -> tuple[bool, str | None]:
    """Run all stages once. Returns (success, failure_reason).

    Failure reason is a short string used if the parser didn't produce a hint.
    Emits SetupSummaryEvent on terminal paths.
    """

    # Keep request-provided skips and optionally force-skip prune when disabled.
    local_skip_stages = set(skip_stages)
    if not ctx.prune_docker_before_post_update:
        local_skip_stages.add("prune_docker_images")

    async def line_cb_for(stage: Stage):
        async def cb(line: str, stream: str) -> None:
            await emit(LogEvent(device=serial, stage=stage, line=line, stream=stream))  # type: ignore[arg-type]

        return cb

    async def line_cb_run_setup(line: str, stream: str) -> None:
        parser.feed(line)
        await emit(LogEvent(device=serial, stage="run_setup", line=line, stream=stream))  # type: ignore[arg-type]

    async def run_stage(
        stage: Stage,
        action: Callable[[], Awaitable[bool]],
        *,
        required: bool,
    ) -> bool:
        if stage in local_skip_stages:
            if stage not in OPTIONAL_STAGES:
                await log(
                    f"Cannot skip required stage '{stage}'. Running anyway.",
                    stream="info",
                    stage=stage,
                )
            else:
                await emit(StageEvent(device=serial, stage=stage, status="skipped"))
                return True

        await emit(StageEvent(device=serial, stage=stage, status="started"))
        try:
            ok = await action()
        except Exception as exc:  # noqa: BLE001
            await log(f"Exception in stage {stage}: {exc}", stream="stderr", stage=stage)
            ok = False

        if ok:
            await emit(StageEvent(device=serial, stage=stage, status="completed"))
            return True
        await emit(StageEvent(device=serial, stage=stage, status="failed"))
        return not required

    # 1. push setup script
    async def stage_push_script() -> bool:
        cb = await line_cb_for("push_setup_script")
        rc, _ = await adb.push(serial, ctx.setup_script, REMOTE_SETUP_SCRIPT_PATH, cb)
        return rc == 0

    if not await run_stage("push_setup_script", stage_push_script, required=True):
        await _emit_summary(serial, parser, emit)
        return False, "Failed to push setup script."

    # 2. push .env (skip silently if not present)
    async def stage_push_env() -> bool:
        if ctx.env_file is None:
            await log(".env not present locally; skipping.", stage="push_env")
            return True
        cb = await line_cb_for("push_env")
        rc, _ = await adb.push(serial, ctx.env_file, REMOTE_ENV_PATH, cb)
        return rc == 0

    if not await run_stage("push_env", stage_push_env, required=True):
        await _emit_summary(serial, parser, emit)
        return False, "Failed to push .env."

    # 3. chmod the setup script
    async def stage_chmod() -> bool:
        cb = await line_cb_for("chmod_script")
        rc, _ = await adb.shell(
            serial, f"chmod +x {REMOTE_SETUP_SCRIPT_PATH}", cb
        )
        return rc == 0

    if not await run_stage("chmod_script", stage_chmod, required=True):
        await _emit_summary(serial, parser, emit)
        return False, "Failed to chmod setup script."

    # 4. change password (optional, can be skipped)
    async def stage_password() -> bool:
        return await _change_password(serial, ctx, line_cb_for, log)

    # password failure does NOT fail the device, matching the bash script
    await run_stage("change_password", stage_password, required=False)

    # 5. push properties (optional) — must happen BEFORE run_setup so the wizard
    # sees the "done" markers and doesn't block.
    async def stage_push_properties() -> bool:
        props = ctx.properties_file
        if props is None:
            await log(
                f"{PROPERTIES_FILE_NAME} not found locally. Skipping.",
                stage="push_properties",
            )
            return True
        cb = await line_cb_for("push_properties")
        temp_target = "/tmp/properties.msgpack"
        rc, _ = await adb.push(serial, props, temp_target, cb)
        if rc != 0:
            return False

        user_target = "/home/arduino/.local/share/arduino-app-cli/properties.msgpack"
        rc, _ = await adb.shell(
            serial,
            "mkdir -p /home/arduino/.local/share/arduino-app-cli && "
            f"install -m 664 {shlex.quote(temp_target)} {shlex.quote(user_target)}",
            cb,
        )
        if rc != 0:
            return False

        system_target = "/var/lib/arduino-app-cli/properties.msgpack"
        install_cmd = (
            "mkdir -p /var/lib/arduino-app-cli && "
            f"install -o arduino -g arduino -m 664 {shlex.quote(temp_target)} "
            f"{shlex.quote(system_target)}"
        )
        rc, _ = await adb.shell(
            serial,
            f"sudo -n bash -lc {shlex.quote(install_cmd)}",
            cb,
        )
        if rc != 0:
            for password in dict.fromkeys(
                candidate
                for candidate in (ctx.unoq_default_password, "arduino")
                if candidate
            ):
                rc, _ = await adb.shell(
                    serial,
                    f"printf '%s\\n' {shlex.quote(password)} | "
                    f"sudo -S -k -p '' bash -lc {shlex.quote(install_cmd)}",
                    cb,
                )
                if rc == 0:
                    break
        if rc != 0:
            return False

        expected_digest = hashlib.sha256(props.read_bytes()).hexdigest()
        checksum_cmd = "sha256sum " + " ".join(
            shlex.quote(target) for target in PROPERTIES_TARGET_PATHS
        )
        rc, output = await adb.shell(serial, checksum_cmd, cb)
        digests = [
            line.split()[0]
            for line in output.splitlines()
            if line.strip()
        ]
        if rc != 0 or digests != [expected_digest] * len(PROPERTIES_TARGET_PATHS):
            await log(
                "properties.msgpack verification failed on one or more destinations.",
                stage="push_properties",
                stream="stderr",
            )
            return False
        return True

    await run_stage("push_properties", stage_push_properties, required=False)

    async def restore_workshop_images(stage: Stage) -> bool:
        archive = ctx.docker_image_archive
        if archive is None:
            await log(
                "No Docker image bundle captured; skipping image restore.",
                stage=stage,
            )
            return True
        cb = await line_cb_for(stage)
        manifest_path = archive.with_name("manifest.json")
        images: list[str] = []
        if manifest_path.is_file():
            try:
                import json

                images = json.loads(manifest_path.read_text(encoding="utf-8")).get(
                    "images", []
                )
            except (OSError, ValueError):
                images = []
        if images:
            inspect_cmd = " && ".join(
                f"docker image inspect {shlex.quote(image)} >/dev/null"
                for image in images
            )
            rc, _ = await adb.shell(serial, inspect_cmd, cb)
            if rc == 0:
                await log(
                    f"All {len(images)} cached Docker images are already present; skipping transfer.",
                    stage=stage,
                )
                return True
        required_free_kb = archive.stat().st_size // 1024 + 1024 * 1024
        rc, free_output = await adb.shell(
            serial,
            "df -Pk / | awk 'NR==2 {print $4}'",
            cb,
        )
        try:
            free_kb = int(free_output.strip().splitlines()[-1]) if rc == 0 else 0
        except (IndexError, ValueError):
            free_kb = 0
        if free_kb and free_kb < required_free_kb:
            await log(
                "Reclaiming unused Docker data before workshop image restore...",
                stage=stage,
            )
            rc, _ = await adb.shell(
                serial,
                "docker container prune -f && docker image prune -a -f",
                cb,
            )
            if rc != 0:
                return False
            rc, free_output = await adb.shell(
                serial,
                "df -Pk / | awk 'NR==2 {print $4}'",
                cb,
            )
            try:
                free_kb = int(free_output.strip().splitlines()[-1]) if rc == 0 else 0
            except (IndexError, ValueError):
                free_kb = 0
            if free_kb and free_kb < required_free_kb:
                await log(
                    "Insufficient disk space for workshop image restore after cleanup.",
                    stage=stage,
                    stream="stderr",
                )
                return False
        await log(
            f"Loading {archive.stat().st_size // (1024 * 1024)} MB Docker image bundle over USB...",
            stage=stage,
        )
        async with ctx.artifact_semaphore:
            rc, _ = await adb.stream_file_to_shell(
                serial,
                archive,
                "docker load",
                cb,
            )
        return rc == 0

    # 6. run remote setup script (parsed for summary + failure hints)
    async def stage_run_setup_inner() -> bool:
        arduino_archive = ctx.arduino_data_archive
        if arduino_archive is not None:
            import hashlib
            import json

            manifest_path = arduino_archive.with_name("manifest.json")
            digest = ""
            try:
                digest = json.loads(manifest_path.read_text(encoding="utf-8")).get(
                    "arduino_sha256", ""
                )
            except (OSError, ValueError):
                pass
            if not digest:
                digest = hashlib.sha256(arduino_archive.read_bytes()).hexdigest()
            marker = f"/home/arduino/.arduino15/.unoq-cache-{digest}"
            rc, _ = await adb.shell(serial, f"test -f {shlex.quote(marker)}")
            if rc != 0:
                await log(
                    f"Loading {arduino_archive.stat().st_size // (1024 * 1024)} MB Arduino toolchain cache over USB...",
                    stage="run_setup",
                )
                async with ctx.artifact_semaphore:
                    rc, _ = await adb.stream_file_to_shell(
                        serial,
                        arduino_archive,
                        "tar --touch -xzf - -C /home/arduino",
                        line_cb_run_setup,
                    )
                if rc != 0:
                    return False
                rc, _ = await adb.shell(
                    serial,
                    f"touch {shlex.quote(marker)}",
                    line_cb_run_setup,
                )
                if rc != 0:
                    return False
            else:
                await log(
                    "Arduino toolchain cache is already present; skipping transfer.",
                    stage="run_setup",
                )
        if not await restore_workshop_images("run_setup"):
            await log(
                "Could not preload workshop images before App Lab initialization.",
                stage="run_setup",
                stream="stderr",
            )
            return False
        cache_remote = f"tcp:{CACHE_PORT}"
        cache_configured = False
        if ctx.use_package_cache:
            rc, _ = await adb.reverse(
                serial,
                cache_remote,
                cache_remote,
                line_cb_run_setup,
            )
            if rc != 0:
                await log(
                    "Package cache tunnel failed; refusing an uncached update.",
                    stage="run_setup",
                    stream="stderr",
                )
                return False
            cache_configured = True
            await log(
                f"Package cache connected over USB on localhost:{CACHE_PORT}.",
                stage="run_setup",
            )

        setup_command = f"source /etc/profile; bash {REMOTE_SETUP_SCRIPT_PATH}"
        if cache_configured:
            setup_command = (
                f"export UNOQ_APT_CACHE_URL=http://127.0.0.1:{CACHE_PORT}; "
                f"{setup_command}"
            )
        try:
            rc, _ = await adb.shell(
                serial,
                setup_command,
                line_cb_run_setup,
            )
        finally:
            if cache_configured:
                await adb.remove_reverse(serial, cache_remote)
        if rc != 0:
            return False

        setup_result, _ = parser.finish()
        if setup_result is not None and setup_result.status == "FAILED":
            return False

        model_bundle = ctx.model_bundle_dir
        if model_bundle is None:
            await log(
                "No local model-bundles directory; skipping model restore.",
                stage="run_setup",
            )
            return True

        await log(
            f"Restoring offline model bundle to {REMOTE_MODELS_DIR}...",
            stage="run_setup",
        )
        expected_model = (
            f"{REMOTE_MODELS_DIR}/llamacpp/unsloth/"
            "Qwen3.5-0.8B-GGUF/Qwen3.5-0.8B-Q4_0.gguf"
        )
        rc, _ = await adb.shell(
            serial,
            f"test $(stat -c %s {expected_model} 2>/dev/null || echo 0) -eq 507154688",
            line_cb_run_setup,
        )
        if rc == 0:
            await log(
                "Qwen 3.5 0.8B model is already present; skipping 507 MB transfer.",
                stage="run_setup",
            )
            return True
        rc, _ = await adb.shell(
            serial,
            f"rm -rf {REMOTE_MODEL_STAGING_DIR} && "
            f"mkdir -p {REMOTE_MODEL_STAGING_DIR}",
            line_cb_run_setup,
        )
        if rc != 0:
            return False
        for model_family in model_bundle.iterdir():
            if not model_family.is_dir():
                continue
            rc, _ = await adb.push(
                serial,
                model_family,
                f"{REMOTE_MODEL_STAGING_DIR}/",
                line_cb_run_setup,
            )
            if rc != 0:
                return False

        install_cmd = (
            f"mkdir -p {REMOTE_MODELS_DIR} && "
            f"cp -a {REMOTE_MODEL_STAGING_DIR}/. {REMOTE_MODELS_DIR}/ && "
            f"chown -R arduino:arduino {REMOTE_MODELS_DIR} && "
            f"rm -rf {REMOTE_MODEL_STAGING_DIR}"
        )
        sudo_candidates = [ctx.unoq_default_password, "arduino"]
        rc, _ = await adb.shell(
            serial,
            f"sudo -n bash -lc {shlex.quote(install_cmd)}",
            line_cb_run_setup,
        )
        if rc == 0:
            return True
        for password in dict.fromkeys(p for p in sudo_candidates if p):
            rc, _ = await adb.shell(
                serial,
                f"printf '%s\\n' {shlex.quote(password)} | "
                f"sudo -S -k -p '' bash -lc {shlex.quote(install_cmd)}",
                line_cb_run_setup,
            )
            if rc == 0:
                return True
        return False

    async def stage_run_setup() -> bool:
        async with ctx.update_semaphore:
            return await stage_run_setup_inner()

    if not await run_stage("run_setup", stage_run_setup, required=True):
        await _emit_summary(serial, parser, emit)
        return False, "Remote setup script failed."

    # If the on-device summary itself reported FAILED, treat as failure even
    # though adb's exit code was 0 (script's `trap print_summary EXIT` always
    # prints the summary).
    await _emit_summary(serial, parser, emit)
    result, hint = parser.finish()
    if result is not None and result.status == "FAILED":
        return False, (hint.message if hint else "Device-side setup reported FAILED.")

    # 7. prune unused docker images/containers before post-update (optional).
    async def stage_prune_docker_images() -> bool:
        cb = await line_cb_for("prune_docker_images")
        # Best-effort cleanup for stale artifacts from previous app versions.
        # Does not remove running containers/images in use.
        prune_cmd = (
            "set -e; "
            "if command -v docker >/dev/null 2>&1; then "
            "echo '[prune] docker before:'; docker system df || true; "
            "docker container prune -f; "
            "docker image prune -a -f; "
            "echo '[prune] docker after:'; docker system df || true; "
            "elif command -v podman >/dev/null 2>&1; then "
            "echo '[prune] podman before:'; podman system df || true; "
            "podman container prune -f; "
            "podman image prune -a -f; "
            "echo '[prune] podman after:'; podman system df || true; "
            "else "
            "echo '[prune] no docker/podman found; skipping'; "
            "fi"
        )
        rc, _ = await adb.shell(serial, prune_cmd, cb)
        return rc == 0

    await run_stage(
        "prune_docker_images",
        stage_prune_docker_images,
        required=False,
    )

    # 8. Stream cached workshop images straight into Docker. No second copy of
    # the archive is written to the board, which matters on the small rootfs.
    async def stage_restore_workshop_cache() -> bool:
        if not await restore_workshop_images("restore_workshop_cache"):
            return False
        runtime_archive = ctx.app_runtime_archive
        if runtime_archive is None:
            return True
        import hashlib
        import json

        cb = await line_cb_for("restore_workshop_cache")
        manifest_path = runtime_archive.with_name("manifest.json")
        digest = ""
        try:
            digest = json.loads(manifest_path.read_text(encoding="utf-8")).get(
                "app_runtime_sha256", ""
            )
        except (OSError, ValueError):
            pass
        if not digest:
            digest = hashlib.sha256(runtime_archive.read_bytes()).hexdigest()
        marker = f"/var/lib/arduino-app-cli/.unoq-app-cache-{digest}"
        rc, _ = await adb.shell(serial, f"test -f {shlex.quote(marker)}")
        if rc == 0:
            await log(
                "Prepared app runtime cache is already present; skipping transfer.",
                stage="restore_workshop_cache",
            )
        else:
            await log(
                f"Restoring {runtime_archive.stat().st_size // (1024 * 1024)} MB prepared app runtime cache...",
                stage="restore_workshop_cache",
            )
            async with ctx.artifact_semaphore:
                rc, _ = await adb.stream_file_to_shell(
                    serial,
                    runtime_archive,
                    "tar --touch -xzf - -C /",
                    cb,
                )
            if rc != 0:
                return False
            rc, _ = await adb.shell(
                serial,
                f"touch {shlex.quote(marker)}",
                cb,
            )
            if rc != 0:
                return False
        return True

    if not await run_stage(
        "restore_workshop_cache",
        stage_restore_workshop_cache,
        required=True,
    ):
        return False, "Failed to restore cached workshop artifacts."

    # 9. Push the app immediately before optionally preparing it.
    if ctx.app_folder is None:
        await emit(StageEvent(device=serial, stage="push_app", status="skipped"))
        await log(
            "No app folder selected; skipping app push.",
            stage="push_app",
        )
    else:
        async def stage_push_app() -> bool:
            cb = await line_cb_for("push_app")
            rc, _ = await adb.push(serial, ctx.app_folder, APPS_TARGET_DIR, cb)
            return rc == 0

        if not await run_stage("push_app", stage_push_app, required=True):
            await _emit_summary(serial, parser, emit)
            return False, "Failed to push app folder."

    # 10. Materialize the uploaded app on the warm board before cache capture.
    async def stage_prepare_uploaded_app() -> bool:
        if not ctx.prepare_uploaded_app:
            await log(
                "Uploaded app cache preparation was not selected; skipping.",
                stage="prepare_uploaded_app",
            )
            return True
        if ctx.app_folder is None:
            await log(
                "No app folder was uploaded.",
                stage="prepare_uploaded_app",
                stream="stderr",
            )
            return False
        cb = await line_cb_for("prepare_uploaded_app")
        remote_app = f"{APPS_TARGET_DIR}{ctx.app_folder.name}"
        await log(
            f"Starting and stopping uploaded app to populate its cache: {remote_app}",
            stage="prepare_uploaded_app",
        )
        rc, _ = await adb.shell(
            serial,
            f"TMPDIR=/tmp arduino-app-cli app start {shlex.quote(remote_app)}",
            cb,
        )
        if rc != 0:
            await adb.shell(
                serial,
                f"TMPDIR=/tmp arduino-app-cli app stop {shlex.quote(remote_app)}",
                cb,
            )
            return False
        rc, _ = await adb.shell(
            serial,
            f"TMPDIR=/tmp arduino-app-cli app stop {shlex.quote(remote_app)}",
            cb,
        )
        return rc == 0

    if not await run_stage(
        "prepare_uploaded_app",
        stage_prepare_uploaded_app,
        required=True,
    ):
        return False, "The uploaded app failed to prepare."

    # 11. Materialize selected examples before arbitrary post-update commands.
    async def stage_prepare_examples() -> bool:
        if not ctx.example_apps:
            await log("No examples selected; skipping.", stage="prepare_examples")
            return True
        cb = await line_cb_for("prepare_examples")
        total = len(ctx.example_apps)
        for index, app_id in enumerate(ctx.example_apps, start=1):
            await log(
                f"Preparing example {index}/{total}: {app_id}",
                stage="prepare_examples",
            )
            start_cmd = f"TMPDIR=/tmp arduino-app-cli app start {shlex.quote(app_id)}"
            rc, _ = await adb.shell(serial, start_cmd, cb)
            if rc != 0:
                await adb.shell(
                    serial,
                    f"TMPDIR=/tmp arduino-app-cli app stop {shlex.quote(app_id)}",
                    cb,
                )
                return False
            rc, _ = await adb.shell(
                serial,
                f"TMPDIR=/tmp arduino-app-cli app stop {shlex.quote(app_id)}",
                cb,
            )
            if rc != 0:
                return False
        return True

    if not await run_stage("prepare_examples", stage_prepare_examples, required=True):
        return False, "A selected example failed to prepare."

    # 12. post-update commands prepare workshop apps and must all succeed.
    async def stage_post_update() -> bool:
        if not ctx.post_update_cmd:
            await log(
                "No post-update command configured; skipping.",
                stage="post_update",
            )
            return True
        cb = await line_cb_for("post_update")
        commands = [
            line.strip()
            for line in ctx.post_update_cmd.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not commands:
            await log(
                "No runnable post-update commands found (empty/comment-only). Skipping.",
                stage="post_update",
            )
            return True

        total = len(commands)
        for idx, cmd in enumerate(commands, start=1):
            await log(
                f"Running post-update command {idx}/{total}: {cmd}",
                stage="post_update",
            )
            # Run each command inside a login shell so `app`, `arduino-app-cli`,
            # and any PATH entries from shell init files are available.
            rc, _ = await adb.shell(
                serial,
                f"TMPDIR=/tmp bash -lc {shlex.quote(cmd)}",
                cb,
            )
            if rc != 0:
                await log(
                    f"Command {idx}/{total} failed with exit code {rc}.",
                    stage="post_update",
                    stream="stderr",
                )
                return False
        return True

    if not await run_stage("post_update", stage_post_update, required=True):
        return False, "A workshop post-update command failed."

    # 12. Verify the board is actually usable, not merely package-updated.
    async def stage_verify_ready() -> bool:
        cb = await line_cb_for("verify_ready")
        checks = [
            "command -v arduino-app-cli >/dev/null",
            "arduino-app-cli app list >/dev/null",
            "docker info >/dev/null",
            "find /var/lib/arduino-app-cli/assets /home/arduino/.local/share/arduino-app-cli/assets -mindepth 1 -maxdepth 1 -type d -print -quit 2>/dev/null | grep -q .",
            "test $(df -Pk / | awk 'NR==2 {print $4}') -ge 204800",
        ]
        if ctx.model_bundle_dir is not None:
            checks.append(
                f"test $(stat -c %s {REMOTE_MODELS_DIR}/llamacpp/unsloth/"
                "Qwen3.5-0.8B-GGUF/Qwen3.5-0.8B-Q4_0.gguf 2>/dev/null || echo 0) "
                "-eq 507154688"
            )
        rc, _ = await adb.shell(
            serial,
            "set -e; " + "; ".join(checks) + "; echo '[verify] App Lab ready'",
            cb,
        )
        return rc == 0

    if not await run_stage("verify_ready", stage_verify_ready, required=True):
        return False, "Final App Lab readiness verification failed."
    return True, None


async def _set_end_state_led(
    serial: str,
    *,
    success: bool,
    log: Callable[..., Awaitable[None]],
) -> None:
    """Set the on-device LEDs to reflect the terminal flash result.
    Success: green ON solid, red OFF. Failure: red ON solid, green OFF.
    Best-effort — never raises. LED failures don't affect the flash result."""
    try:
        rc, out = await adb.shell(serial, _end_state_led_cmd(success=success))
        if rc != 0:
            await log(
                f"LED end-state command exited {rc}: {out}",
                stream="stderr",
            )
    except Exception as exc:  # noqa: BLE001
        await log(f"LED end-state command raised: {exc}", stream="stderr")


async def _log_arduino_cli_version(
    serial: str,
    log: Callable[..., Awaitable[None]],
) -> None:
    """After a successful flash, capture `arduino-cli version --json` from the
    device and stream it to the device log. Best-effort — never raises, never
    changes the device result."""

    async def cb(line: str, stream: str) -> None:
        await log(f"[arduino-cli version] {line}", stream=stream)  # type: ignore[arg-type]

    try:
        rc, _ = await adb.shell(
            serial,
            "source /etc/profile; arduino-cli version --json",
            cb,
        )
        if rc != 0:
            await log(
                f"[arduino-cli version] exited {rc}",
                stream="stderr",
            )
    except Exception as exc:  # noqa: BLE001
        await log(
            f"[arduino-cli version] command raised: {exc}",
            stream="stderr",
        )


async def _emit_summary(serial: str, parser: SetupOutputParser, emit: EmitFn) -> None:
    """Emit a SetupSummaryEvent if the parser captured one. Idempotent-ish: only
    called from terminal paths."""
    result, _ = parser.finish()
    if result is None or result.status is None:
        return
    await emit(
        SetupSummaryEvent(
            device=serial,
            status=result.status,  # type: ignore[arg-type]
            elapsed_seconds=result.elapsed_seconds,
            errors=list(result.errors),
        )
    )


async def _change_password(
    serial: str,
    ctx: FlasherContext,
    line_cb_for: Callable[[Stage], Awaitable[Callable[[str, str], Awaitable[None]]]],
    log: Callable[..., Awaitable[None]],
) -> bool:
    new_pw = ctx.unoq_default_password
    if not new_pw:
        await log(
            "UNOQ_DEFAULT_PASSWORD is not set. Skipping password change.",
            stage="change_password",
        )
        return True

    cb = await line_cb_for("change_password")

    async def attempt(cmd: str) -> tuple[int, str]:
        return await adb.shell(serial, cmd, cb)

    pw_q = shlex.quote(new_pw)

    # Attempt 1: no current password
    await log("Changing password (attempt 1: no current password)...", stage="change_password")
    rc, out = await attempt(
        f"printf '%s\\n%s\\n' {pw_q} {pw_q} | passwd arduino"
    )
    combined = out

    if PASSWORD_SUCCESS_RE.search(combined):
        await log("Password changed successfully (no current password required).", stage="change_password")
        return True

    if PASSWORD_NEEDS_CURRENT_RE.search(combined):
        # When passwd asks for the current password, first try the configured
        # default (many boards are already on that value), then fall back to
        # the factory default used on older images.
        current_candidates: list[tuple[str, str]] = []
        if new_pw:
            current_candidates.append((new_pw, "configured default password"))
        if new_pw != "arduino":
            current_candidates.append(("arduino", "factory default password 'arduino'"))

        for current_pw, label in current_candidates:
            await log(
                f"Retrying password change with current password candidate: {label}...",
                stage="change_password",
            )
            current_q = shlex.quote(current_pw)
            rc2, out2 = await attempt(
                f"printf '%s\\n%s\\n%s\\n' {current_q} {pw_q} {pw_q} | passwd arduino"
            )
            combined = out2
            if PASSWORD_SUCCESS_RE.search(combined):
                await log(
                    f"Password changed successfully using {label}.",
                    stage="change_password",
                )
                return True
            if PASSWORD_TOKEN_ERROR_RE.search(combined):
                await log("Token manipulation error; password may already be changed.", stage="change_password")
                return True
            if re.search(r"password unchanged", combined, re.IGNORECASE):
                # Try the next candidate before concluding it's already non-default.
                continue

        await log(
            "Password unchanged; none of the known current-password candidates worked.",
            stage="change_password",
        )
        return True

    if PASSWORD_TOKEN_ERROR_RE.search(combined):
        await log("Token manipulation error; password may already be changed.", stage="change_password")
        return True

    await log(
        f"Password change may have already happened or failed. Output: {combined}",
        stage="change_password",
    )
    # Treat as non-fatal, matching original script
    return True
