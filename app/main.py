"""FastAPI app: HTTP routes + WebSocket endpoint.

Routes:
  GET  /                       -> serves index.html
  GET  /api/health             -> simple liveness + adb status
  GET  /api/devices            -> list online devices via adb
  POST /api/upload             -> receive folder upload (multipart, many files)
  POST /api/runs               -> start a new run for selected devices
  POST /api/runs/{id}/devices/{serial}/retry  -> retry a device
  WS   /ws/runs/{id}           -> stream events for a run
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import time
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import adb
from .env_file import MANAGED_KEYS, read_env, write_env
from .events import LogEvent, OPTIONAL_STAGES, Stage, StageEvent, StartRunRequest
from .flasher import FlasherContext, SETUP_SCRIPT_NAME
from .package_cache import PackageCache
from .runs import Registry
from .workshop_cache import WorkshopCache

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
UPLOADS_DIR = PROJECT_ROOT / ".uploads"
CACHE_DIR = PROJECT_ROOT / ".cache" / "packages"
WORKSHOP_CACHE_DIR = PROJECT_ROOT / ".cache" / "workshop"
FLASH_IMAGE_CACHE_DIR = PROJECT_ROOT / ".cache" / "flasher-images"

registry = Registry(uploads_dir=UPLOADS_DIR)
package_cache = PackageCache(cache_dir=CACHE_DIR)
workshop_cache = WorkshopCache(root=WORKSHOP_CACHE_DIR)
flash_lock = asyncio.Lock()
flash_state: dict = {
    "status": "idle",
    "logs": [],
    "exit_code": None,
    "preserve_user": False,
}
flash_task: asyncio.Task | None = None
flash_process: asyncio.subprocess.Process | None = None
LOCAL_FLASHER_PATH = PROJECT_ROOT / "tools" / "arduino-flasher-cli" / "arduino-flasher-cli"
latest_image_cache: dict = {"checked_at": 0.0, "info": None, "error": None}
image_digest_cache: dict = {"signature": None, "expected": None, "matches": False}
MIN_FLASH_FREE_BYTES = 8 * 1024 * 1024 * 1024
FLASH_PROCESS_TIMEOUT_SECONDS = 45 * 60


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load .env from project root so UNOQ_DEFAULT_PASSWORD is available.
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    _sweep_old_uploads(UPLOADS_DIR, max_age_hours=24)
    package_cache.start()
    try:
        yield
    finally:
        package_cache.stop()


def _sweep_old_uploads(uploads_dir: Path, max_age_hours: int) -> None:
    """Delete any staged upload directories older than max_age_hours."""
    import shutil
    import time

    if not uploads_dir.exists():
        return
    cutoff = time.time() - max_age_hours * 3600
    for child in uploads_dir.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass


app = FastAPI(title="Arduino UNO Q Flasher", lifespan=lifespan)


# ----- static -----

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ----- API -----


@app.get("/api/health")
async def health() -> dict:
    try:
        adb_bin = adb.adb_path()
        adb_ok = True
        adb_error = None
    except adb.AdbNotFoundError as e:
        adb_bin = None
        adb_ok = False
        adb_error = str(e)
    return {
        "ok": True,
        "adb_available": adb_ok,
        "adb_path": adb_bin,
        "adb_error": adb_error,
        "password_configured": bool(os.environ.get("UNOQ_DEFAULT_PASSWORD")),
        "wifi_ssid_configured": bool(os.environ.get("UNOQ_WIFI_SSID")),
        "wifi_password_configured": bool(os.environ.get("UNOQ_WIFI_PASSWORD")),
        "setup_script_available": (PROJECT_ROOT / SETUP_SCRIPT_NAME).is_file(),
        "properties_available": (PROJECT_ROOT / "properties.msgpack").is_file(),
        "package_cache": package_cache.status(),
        "workshop_cache": workshop_cache.status(),
    }


class SettingsBody(BaseModel):
    UNOQ_WIFI_SSID: str | None = None
    UNOQ_WIFI_PASSWORD: str | None = None
    UNOQ_DEFAULT_PASSWORD: str | None = None


class FlashImageRequest(BaseModel):
    edl_pins_confirmed: bool = False
    preserve_user: bool = False


class CopilotDiagnosisRequest(BaseModel):
    prompt: str


@app.post("/api/copilot/diagnose")
async def diagnose_with_copilot(request: CopilotDiagnosisRequest) -> dict:
    prompt = request.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="diagnostic prompt is empty")
    if len(prompt) > 20_000:
        raise HTTPException(status_code=400, detail="diagnostic prompt is too large")

    code_cli = shutil.which("code")
    if code_cli is None and sys.platform == "darwin":
        bundled_cli = Path("/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code")
        if bundled_cli.is_file():
            code_cli = str(bundled_cli)
    if code_cli is None:
        raise HTTPException(status_code=503, detail="VS Code command-line tool was not found")

    process = await asyncio.create_subprocess_exec(
        code_cli,
        "chat",
        "--mode",
        "agent",
        "--reuse-window",
        prompt,
        cwd=PROJECT_ROOT,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise HTTPException(status_code=504, detail="VS Code did not accept the Copilot request")
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise HTTPException(status_code=503, detail=detail or "VS Code failed to open Copilot Chat")
    return {"ok": True}


def _flasher_path() -> str | None:
    local_binary_matches_host = (
        sys.platform == "darwin" and platform.machine().lower() in {"arm64", "aarch64"}
    )
    if (
        local_binary_matches_host
        and LOCAL_FLASHER_PATH.is_file()
        and os.access(LOCAL_FLASHER_PATH, os.X_OK)
    ):
        return str(LOCAL_FLASHER_PATH)
    return shutil.which("arduino-flasher-cli")


async def _edl_device_count() -> int:
    if sys.platform == "darwin":
        command = ["system_profiler", "SPUSBDataType", "-json", "-detailLevel", "mini"]
    elif sys.platform.startswith("linux"):
        command = ["lsusb"]
    else:
        return 0
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
    except (OSError, asyncio.TimeoutError):
        return 0
    output = stdout.decode("utf-8", errors="replace")
    if sys.platform == "darwin":
        try:
            usb_data = json.loads(output)
        except json.JSONDecodeError:
            return 0

        def count_edl_devices(value: object) -> int:
            if isinstance(value, dict):
                vendor = str(value.get("vendor_id", "")).lower()
                product = str(value.get("product_id", "")).lower()
                own_match = int("0x05c6" in vendor and "0x9008" in product)
                return own_match + sum(count_edl_devices(item) for item in value.values())
            if isinstance(value, list):
                return sum(count_edl_devices(item) for item in value)
            return 0

        return count_edl_devices(usb_data)
    return sum("05c6:9008" in line.lower() for line in output.splitlines())


async def _run_image_flash(preserve_user: bool) -> None:
    tool = _flasher_path()
    if tool is None:
        flash_state.update(
            status="failed",
            logs=["Arduino Flasher CLI disappeared before flashing could start."],
            exit_code=-1,
        )
        return
    flash_state.update(status="running", logs=[], exit_code=None, preserve_user=preserve_user)
    try:
        image_info = await _latest_image_info(tool, force=True)
        image_path = FLASH_IMAGE_CACHE_DIR / Path(urlparse(image_info["url"]).path).name
        if not await _image_matches(image_path, image_info["sha256"]):
            image_path.unlink(missing_ok=True)
            FLASH_IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            flash_state["logs"].append(
                f"Downloading UNO Q image {image_info['version']} once for this Mac..."
            )
            exit_code = await asyncio.wait_for(
                _stream_flasher(
                    tool,
                    "download",
                    image_info["version"],
                    "--dest-dir",
                    str(FLASH_IMAGE_CACHE_DIR),
                ),
                timeout=FLASH_PROCESS_TIMEOUT_SECONDS,
            )
            if exit_code != 0 or not await _image_matches(image_path, image_info["sha256"]):
                raise RuntimeError("latest image download or checksum verification failed")
            for old_image in FLASH_IMAGE_CACHE_DIR.iterdir():
                if old_image.is_file() and old_image != image_path:
                    old_image.unlink(missing_ok=True)
        else:
            flash_state["logs"].append(
                f"Using verified cached UNO Q image {image_info['version']}."
            )

        if await _edl_device_count() != 1:
            raise RuntimeError("exactly one EDL board must remain connected")
        command = [tool, "flash", str(image_path), "--yes"]
        if preserve_user:
            command.append("--preserve-user")
        flash_state["logs"].append("Starting official Arduino Flasher CLI...")
        exit_code = await asyncio.wait_for(
            _stream_flasher(*command),
            timeout=FLASH_PROCESS_TIMEOUT_SECONDS,
        )
        flash_state["exit_code"] = exit_code
        flash_state["status"] = "succeeded" if exit_code == 0 else "failed"
    except asyncio.CancelledError:
        flash_state["logs"].append("Operation canceled by the operator.")
        flash_state["status"] = "canceled"
        flash_state["exit_code"] = -2
        raise
    except Exception as exc:  # noqa: BLE001
        flash_state["logs"].append(f"Image operation failed: {exc}")
        flash_state["status"] = "failed"
        flash_state["exit_code"] = -1


async def _stream_flasher(*command: str) -> int:
    global flash_process
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    flash_process = process
    try:
        assert process.stdout is not None
        while line := await process.stdout.readline():
            flash_state["logs"].append(line.decode("utf-8", errors="replace").rstrip())
            flash_state["logs"] = flash_state["logs"][-500:]
        return await process.wait()
    except asyncio.CancelledError:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        raise
    finally:
        flash_process = None


async def _latest_image_info(tool: str, force: bool = False) -> dict:
    age = time.monotonic() - latest_image_cache["checked_at"]
    if not force and latest_image_cache["info"] and age < 300:
        return latest_image_cache["info"]
    process = await asyncio.create_subprocess_exec(
        tool,
        "list",
        "--format",
        "json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    if process.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", errors="replace").strip() or "image catalog failed")
    catalog = json.loads(stdout)
    info = catalog["latest"]
    if not all(info.get(key) for key in ("version", "url", "sha256")):
        raise RuntimeError("latest image catalog entry is incomplete")
    latest_image_cache.update(checked_at=time.monotonic(), info=info, error=None)
    return info


async def _image_matches(path: Path, expected_sha256: str) -> bool:
    if not path.is_file():
        return False
    stat = path.stat()
    signature = (str(path), stat.st_size, stat.st_mtime_ns)
    if (
        image_digest_cache["signature"] == signature
        and image_digest_cache["expected"] == expected_sha256
    ):
        return image_digest_cache["matches"]

    def digest() -> str:
        result = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(4 * 1024 * 1024):
                result.update(chunk)
        return result.hexdigest()

    matches = await asyncio.to_thread(digest) == expected_sha256
    image_digest_cache.update(
        signature=signature,
        expected=expected_sha256,
        matches=matches,
    )
    return matches


@app.get("/api/flashing/status")
async def flashing_status() -> dict:
    image: dict = {"available": False, "cached": False}
    tool = _flasher_path()
    if tool is not None:
        try:
            info = await _latest_image_info(tool)
            path = FLASH_IMAGE_CACHE_DIR / Path(urlparse(info["url"]).path).name
            image = {
                "available": True,
                "cached": await _image_matches(path, info["sha256"]),
                "version": info["version"],
                "path": str(path),
            }
        except (OSError, ValueError, KeyError, asyncio.TimeoutError, RuntimeError) as exc:
            image = {"available": False, "cached": False, "error": str(exc)}
    return {
        **flash_state,
        "tool_available": _flasher_path() is not None,
        "tool_path": _flasher_path(),
        "edl_devices": await _edl_device_count(),
        "platform_supported": sys.platform == "darwin" or sys.platform.startswith("linux"),
        "image": image,
        "image_cache_dir": str(FLASH_IMAGE_CACHE_DIR),
        "free_bytes": shutil.disk_usage(PROJECT_ROOT).free,
        "required_free_bytes": MIN_FLASH_FREE_BYTES,
    }


@app.post("/api/flashing/start")
async def start_image_flash(request: FlashImageRequest) -> dict:
    global flash_task
    if not request.edl_pins_confirmed:
        raise HTTPException(status_code=400, detail="confirm the EDL pins are bridged")
    if _flasher_path() is None:
        raise HTTPException(status_code=503, detail="arduino-flasher-cli is not available")
    if shutil.disk_usage(PROJECT_ROOT).free < MIN_FLASH_FREE_BYTES:
        raise HTTPException(
            status_code=507,
            detail="at least 8 GB of free space is required to download and extract the image",
        )
    async with flash_lock:
        if flash_state["status"] in {"starting", "running"}:
            raise HTTPException(status_code=409, detail="an image flash is already running")
        edl_devices = await _edl_device_count()
        if edl_devices != 1:
            raise HTTPException(
                status_code=400,
                detail=f"connect exactly one board in EDL mode; detected {edl_devices}",
            )
        flash_task = asyncio.create_task(_run_image_flash(request.preserve_user))
        flash_state.update(
            status="starting",
            logs=["Validated one EDL board; starting flasher..."],
            exit_code=None,
            preserve_user=request.preserve_user,
        )
    return {"ok": True}


@app.post("/api/flashing/cancel")
async def cancel_image_flash() -> dict:
    if flash_task is None or flash_task.done():
        raise HTTPException(status_code=409, detail="no image operation is running")
    flash_task.cancel()
    try:
        await flash_task
    except asyncio.CancelledError:
        pass
    return {"ok": True, "status": flash_state["status"]}


@app.get("/api/settings")
async def get_settings() -> dict:
    """Return current managed-key values. Passwords are returned in plaintext
    so the UI can render a show/hide reveal button — the .env file itself is
    plaintext on disk, so exposing it over localhost is not an additional risk.
    Also returns the absolute path of the .env file so the UI can show it."""
    env_path = PROJECT_ROOT / ".env"
    on_disk = read_env(env_path)

    def _val(key: str) -> str:
        return on_disk.get(key) or os.environ.get(key) or ""

    return {
        "UNOQ_WIFI_SSID": _val("UNOQ_WIFI_SSID"),
        "UNOQ_WIFI_PASSWORD": _val("UNOQ_WIFI_PASSWORD"),
        "UNOQ_DEFAULT_PASSWORD": _val("UNOQ_DEFAULT_PASSWORD"),
        # Kept for backwards-compat with older frontends and health checks.
        "UNOQ_WIFI_PASSWORD_set": bool(_val("UNOQ_WIFI_PASSWORD")),
        "UNOQ_DEFAULT_PASSWORD_set": bool(_val("UNOQ_DEFAULT_PASSWORD")),
        "env_file_path": str(env_path),
        "env_file_exists": env_path.exists(),
    }


@app.post("/api/settings")
async def update_settings(body: SettingsBody) -> dict:
    """Write any provided keys into the project .env (preserves other keys)."""
    updates: dict[str, str] = {}
    for key in MANAGED_KEYS:
        val = getattr(body, key, None)
        if val is not None:  # empty string is a valid "clear"
            updates[key] = val
    if not updates:
        raise HTTPException(status_code=400, detail="no settings provided")
    env_path = PROJECT_ROOT / ".env"
    write_env(env_path, updates)
    return {"ok": True, "updated": list(updates.keys())}


@app.get("/api/devices")
async def devices() -> dict:
    try:
        ds = await adb.list_devices()
    except adb.AdbNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"devices": [{"serial": d.serial, "state": d.state} for d in ds]}


@app.get("/api/devices/{serial}/examples")
async def device_examples(serial: str) -> dict:
    online = {device.serial for device in await adb.list_devices()}
    if serial not in online:
        raise HTTPException(status_code=404, detail="device is not online")
    rc, output = await adb.shell(serial, "arduino-app-cli app list --format json")
    if rc != 0:
        raise HTTPException(status_code=502, detail=output or "could not list examples")
    try:
        apps = json.loads(output).get("apps", [])
        examples = []
        for app_entry in apps:
            if not app_entry.get("example"):
                continue
            encoded = str(app_entry.get("id", ""))
            padding = "=" * (-len(encoded) % 4)
            app_id = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
            if not app_id.startswith("examples:"):
                continue
            examples.append(
                {
                    "id": app_id,
                    "name": app_entry.get("name") or app_id,
                    "description": app_entry.get("description") or "",
                    "status": app_entry.get("status") or "unknown",
                    "inspirational": app_id.startswith("examples:inspirational/"),
                }
            )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=502, detail=f"invalid App CLI response: {exc}") from exc
    examples.sort(key=lambda item: (not item["inspirational"], item["name"].lower()))
    return {"device": serial, "examples": examples}


@app.get("/api/cache")
async def cache_status() -> dict:
    return {
        "packages": package_cache.status(),
        "workshop": workshop_cache.status(),
    }


@app.post("/api/cache/verify")
async def verify_cache() -> dict:
    return await workshop_cache.verify()


@app.post("/api/cache/images/{serial}")
async def capture_image_cache(serial: str) -> dict:
    online = {device.serial for device in await adb.list_devices()}
    if serial not in online:
        raise HTTPException(status_code=404, detail="device is not online")
    try:
        return await workshop_cache.capture_images(serial)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/cache/workshop/{serial}")
async def capture_workshop_cache(serial: str) -> dict:
    online = {device.serial for device in await adb.list_devices()}
    if serial not in online:
        raise HTTPException(status_code=404, detail="device is not online")
    try:
        return await workshop_cache.capture_all(serial)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# Blink the red user LED ~5s to physically identify the board on a bench full
# of UNO Qs. Runs over `adb shell` which on UNO Q is the `arduino` user.
#
# UNO Q kernel registers user LEDs at /sys/class/leds/unoq:user-red1 (verified
# on current image). We probe that path first, then a few fallbacks, and dump
# diagnostics if nothing matches.
#
# Gotchas this command handles:
#  - Active kernel `trigger` overrides manual brightness writes; we set it to
#    `none` first.
#  - Brightness must be `max_brightness` (usually 255), not literal 1.
#  - Direct shell `>` redirection sometimes fails on sysfs; fall through to
#    `tee`, then `sudo -n tee` if both fail.
#  - Verify the write actually changed the file before running the blink loop,
#    so a permission failure surfaces clearly instead of silently no-op'ing.
IDENTIFY_BLINK_CMD = r"""
set +e
echo "[identify] whoami: $(whoami)"

LED=""
for c in \
    /sys/class/leds/unoq:user-red1 \
    /sys/class/leds/unoq:user-red \
    /sys/class/unoq:user-red1 \
    /sys/class/unoq:user-red \
    /sys/class/leds/red:user \
    /sys/class/leds/user:red; do
  if [ -e "$c/brightness" ]; then
    LED="$c"
    break
  fi
done

# Glob fallback: any /sys/class/leds/*red* or /sys/class/unoq:*red* with
# a brightness file.
if [ -z "$LED" ]; then
  for g in /sys/class/leds/*red* /sys/class/unoq:*red*; do
    [ -e "$g/brightness" ] && LED="$g" && break
  done
fi

if [ -z "$LED" ]; then
  echo "[identify] no known LED node matched. Diagnostics:"
  echo "[identify] /sys/class/leds/ contents:"
  ls -la /sys/class/leds/ 2>&1
  echo "[identify] /sys/class/ entries matching unoq|red|led:"
  ls /sys/class/ 2>&1 | grep -Ei 'unoq|red|led' || true
  exit 1
fi

echo "[identify] using $LED"

write_sysfs() {
  local file="$1" value="$2"
  if printf '%s' "$value" 2>/dev/null > "$file" 2>/dev/null; then return 0; fi
  if printf '%s\n' "$value" | tee "$file" >/dev/null 2>&1; then return 0; fi
  if printf '%s\n' "$value" | sudo -n tee "$file" >/dev/null 2>&1; then return 0; fi
  return 1
}

write_sysfs "$LED/trigger" none \
  || echo "[identify] could not set trigger to none (continuing anyway)"
MAX=$(cat "$LED/max_brightness" 2>/dev/null || echo 255)
echo "[identify] max_brightness=$MAX"

if ! write_sysfs "$LED/brightness" "$MAX"; then
  echo "[identify] cannot write to $LED/brightness:"
  ls -la "$LED/brightness" 2>&1
  exit 1
fi

# Verify the write actually took (sysfs may accept writes that the driver
# silently discards if the trigger is still active).
READBACK=$(cat "$LED/brightness" 2>/dev/null)
echo "[identify] brightness after first write: $READBACK (expected ~$MAX)"

# Blink loop: 8 toggles @ 0.3s on/off = 4.8s + 0.3s tail = ~5s total.
for i in 1 2 3 4 5 6 7 8; do
  sleep 0.3
  write_sysfs "$LED/brightness" 0
  sleep 0.3
  write_sysfs "$LED/brightness" "$MAX"
done
sleep 0.3
write_sysfs "$LED/brightness" 0
exit 0
"""

WIFI_CHECK_CMD = r"""
set +e
echo "[wifi-check] whoami: $(whoami)"
echo "[wifi-check] timestamp: $(date -Is 2>/dev/null || date)"

echo "[wifi-check] nmcli device status:"
nmcli device status 2>&1

SSID=$(nmcli -t -f active,ssid dev wifi 2>/dev/null | awk -F: '$1=="yes"{print $2; exit}')
if [ -n "$SSID" ]; then
    echo "[wifi-check] connected_ssid: $SSID"
else
    echo "[wifi-check] connected_ssid: (none)"
fi

echo "[wifi-check] ip route default:"
ip route 2>/dev/null | grep '^default' || echo "(no default route)"

echo "[wifi-check] dns lookup downloads.arduino.cc:"
if nslookup downloads.arduino.cc >/dev/null 2>&1; then
    echo "ok"
else
    echo "failed"
fi

echo "[wifi-check] http reachability https://downloads.arduino.cc:"
if curl -s --max-time 5 --head https://downloads.arduino.cc >/dev/null 2>&1; then
    echo "ok"
    exit 0
fi
echo "failed"
exit 1
"""


@app.post("/api/devices/{serial}/identify")
async def identify_device(serial: str) -> dict:
    try:
        rc, out = await adb.shell(serial, IDENTIFY_BLINK_CMD)
    except adb.AdbNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if rc != 0:
        # Surface the FULL output (not truncated) so the user can see the
        # actual LED node names on their image.
        raise HTTPException(
            status_code=502,
            detail=f"adb shell exited {rc}.\n{out}",
        )
    return {"ok": True, "output": out}


@app.post("/api/devices/{serial}/wifi-check")
async def wifi_check_device(serial: str) -> dict:
    try:
        rc, out = await adb.shell(serial, WIFI_CHECK_CMD)
    except adb.AdbNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {
        "ok": rc == 0,
        "exit_code": rc,
        "output": out,
    }


@app.post("/api/upload")
async def upload(
    folder_name: str = Form(...),
    paths: list[str] = Form(...),
    files: list[UploadFile] = File(...),
) -> dict:
    """Receive an entire folder.

    The frontend sends, in order, one `files` entry and one `paths` entry per file,
    where `paths[i]` is the file's `webkitRelativePath` (folder/subdir/file.ext).
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded.")
    if len(paths) != len(files):
        raise HTTPException(
            status_code=400,
            detail=f"paths/files length mismatch: {len(paths)} vs {len(files)}",
        )

    upload_id, base = registry.new_upload_dir()
    folder_root = base / _sanitize(folder_name)
    folder_root.mkdir(parents=True, exist_ok=True)

    for f, rel in zip(files, paths):
        rel = (rel or "").replace("\\", "/").lstrip("/")
        # webkitRelativePath includes the top folder name. Strip it so we don't
        # double-nest under folder_root.
        parts = rel.split("/")
        if parts and parts[0] == folder_name:
            parts = parts[1:]
        rel_clean = "/".join(parts)
        if not rel_clean or ".." in Path(rel_clean).parts:
            continue
        dest = folder_root / rel_clean
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as out:
            while True:
                chunk = await f.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)

    registry.register_upload(upload_id, folder_root, folder_name)
    file_count = 0
    eim_files: list[dict] = []
    for p in folder_root.rglob("*"):
        if not p.is_file():
            continue
        file_count += 1
        if p.suffix.lower() == ".eim":
            eim_files.append(
                {
                    "path": p.relative_to(folder_root).as_posix(),
                    "size_bytes": p.stat().st_size,
                }
            )
    eim_files.sort(key=lambda e: e["path"])
    return {
        "upload_id": upload_id,
        "folder_name": folder_name,
        "file_count": file_count,
        "eim_files": eim_files,
        "properties_available": (folder_root / "properties.msgpack").is_file(),
    }


@app.get("/api/uploads/{upload_id}")
async def get_upload(upload_id: str) -> dict:
    upload = registry.get_upload(upload_id)
    if upload is None:
        raise HTTPException(status_code=404, detail="staged app folder expired or is unavailable")
    files = [path for path in upload.folder.rglob("*") if path.is_file()]
    eim_files = [
        {
            "path": path.relative_to(upload.folder).as_posix(),
            "size_bytes": path.stat().st_size,
        }
        for path in files
        if path.suffix.lower() == ".eim"
    ]
    return {
        "upload_id": upload.upload_id,
        "folder_name": upload.name,
        "file_count": len(files),
        "eim_files": sorted(eim_files, key=lambda item: item["path"]),
        "properties_available": (upload.folder / "properties.msgpack").is_file(),
    }


@app.post("/api/runs")
async def start_run(req: StartRunRequest) -> dict:
    upload = None
    app_folder: Path | None = None
    if req.upload_id:
        upload = registry.get_upload(req.upload_id)
        if upload is None:
            raise HTTPException(status_code=404, detail="upload_id not found")
        app_folder = upload.folder
    if not req.devices:
        raise HTTPException(status_code=400, detail="no devices selected")
    if req.warm_cache and len(req.devices) != 1:
        raise HTTPException(
            status_code=400,
            detail="warm-cache mode requires exactly one device",
        )
    if req.prepare_uploaded_app and (not req.warm_cache or upload is None):
        raise HTTPException(
            status_code=400,
            detail="uploaded app preparation requires warm-cache mode and an upload",
        )
    invalid_examples = [
        app_id
        for app_id in req.example_apps
        if not re.fullmatch(r"examples:[A-Za-z0-9._/-]+", app_id)
    ]
    if invalid_examples:
        raise HTTPException(status_code=400, detail="invalid example app ID")

    app_push_requested = any("push_app" not in device.skip_stages for device in req.devices)
    if app_push_requested and upload is None:
        raise HTTPException(status_code=400, detail="app-folder push requires a staged folder")
    post_update_requested = any(
        "post_update" not in device.skip_stages for device in req.devices
    )
    if post_update_requested and not (req.post_update_cmd or "").strip():
        raise HTTPException(status_code=400, detail="post-update stage requires a command")
    properties_requested = any(
        "push_properties" not in device.skip_stages for device in req.devices
    )
    properties_available = (PROJECT_ROOT / "properties.msgpack").is_file() or (
        app_folder is not None and (app_folder / "properties.msgpack").is_file()
    )
    if properties_requested and not properties_available:
        raise HTTPException(status_code=400, detail="properties.msgpack is required but missing")

    cache = await workshop_cache.verify()
    workshop_restore_requested = any(
        "restore_workshop_cache" not in device.skip_stages for device in req.devices
    )
    if workshop_restore_requested and cache["integrity"] == "invalid":
        raise HTTPException(
            status_code=409,
            detail="Saved workshop files are damaged; warm the cache again before setup.",
        )

    setup_script = PROJECT_ROOT / SETUP_SCRIPT_NAME
    setup_script_requested = any(
        not {"push_setup_script", "chmod_script", "run_setup"}.issubset(device.skip_stages)
        for device in req.devices
    )
    if setup_script_requested and not setup_script.is_file():
        raise HTTPException(
            status_code=500,
            detail=f"{SETUP_SCRIPT_NAME} not found at project root: {setup_script}",
        )

    env_file = PROJECT_ROOT / ".env"
    ctx = FlasherContext(
        app_folder=app_folder,
        setup_script=setup_script,
        env_file=env_file if env_file.is_file() else None,
        unoq_default_password=os.environ.get("UNOQ_DEFAULT_PASSWORD"),
        project_root=PROJECT_ROOT,
        post_update_cmd=(req.post_update_cmd or "").strip() or None,
        prune_docker_before_post_update=req.prune_docker_before_post_update,
        use_package_cache=req.use_package_cache,
        max_parallel_updates=len(req.devices),
        prepare_uploaded_app=req.warm_cache and req.prepare_uploaded_app,
        example_apps=req.example_apps if req.warm_cache else [],
    )
    run = registry.create_run(ctx, upload)
    previous_images = set(workshop_cache.status().get("images", []))
    previous_packages = package_cache.cached_urls()

    device_configs: list[tuple[str, set[Stage]]] = [
        (d.serial, {s for s in d.skip_stages if s in OPTIONAL_STAGES})
        for d in req.devices
    ]

    async def capture_after_success(serial: str, emit) -> tuple[bool, str | None]:
        await emit(StageEvent(device=serial, stage="capture_cache", status="started"))
        try:
            result = await workshop_cache.capture_all(serial)
        except RuntimeError as exc:
            await emit(
                LogEvent(
                    device=serial,
                    stage="capture_cache",
                    line=f"Cache capture failed: {exc}",
                    stream="stderr",
                )
            )
            await emit(StageEvent(device=serial, stage="capture_cache", status="failed"))
            return False, "Failed to capture the warmed board cache."

        new_images = sorted(set(result.get("images", [])) - previous_images)
        new_packages = sorted(package_cache.cached_urls() - previous_packages)
        await emit(
            LogEvent(
                device=serial,
                stage="capture_cache",
                line=(
                    f"Captured {len(result.get('images', []))} Docker images, "
                    f"{result.get('arduino_archive_bytes', 0)} bytes of Arduino data, "
                    f"and {result.get('app_runtime_archive_bytes', 0)} bytes of app caches."
                ),
            )
        )
        await emit(
            LogEvent(
                device=serial,
                stage="capture_cache",
                line=(
                    f"New cache entries: {len(new_images)} Docker image(s), "
                    f"{len(new_packages)} package URL(s)."
                ),
            )
        )
        for image in new_images:
            await emit(LogEvent(device=serial, stage="capture_cache", line=f"New image: {image}"))
        for url in new_packages:
            await emit(LogEvent(device=serial, stage="capture_cache", line=f"New package: {url}"))
        await emit(StageEvent(device=serial, stage="capture_cache", status="completed"))
        return True, None

    asyncio.create_task(
        registry.run_devices(
            run,
            device_configs,
            capture_after_success if req.warm_cache else None,
        )
    )
    return {"run_id": run.run_id}


class RetryBody(BaseModel):
    skip_stages: list[Stage] = []


@app.post("/api/runs/{run_id}/devices/{serial}/retry")
async def retry_device(
    run_id: str, serial: str, body: RetryBody | None = None
) -> dict:
    run = registry.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    # If the run originally had a folder but its staged copy was cleaned up,
    # we can't retry the push_app step. Folderless runs (app_folder is None)
    # are always retryable — push_app will just be skipped again.
    if (
        run.ctx.app_folder is not None
        and run.upload is None
        and not run.ctx.app_folder.exists()
    ):
        raise HTTPException(
            status_code=410,
            detail="staged upload was cleaned up; please re-upload the folder.",
        )
    skip: set[Stage] = set()
    if body is not None:
        skip = {s for s in body.skip_stages if s in OPTIONAL_STAGES}
    await registry.retry_device(run, serial, skip)
    return {"ok": True}


# ----- WebSocket -----


@app.websocket("/ws/runs/{run_id}")
async def ws_run(ws: WebSocket, run_id: str) -> None:
    await ws.accept()
    run = registry.get_run(run_id)
    if run is None:
        await ws.send_json({"type": "error", "message": "run not found"})
        await ws.close()
        return

    q = registry.subscribe(run)
    try:
        # Stream events until the run finishes AND the queue is drained.
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if run.finished.is_set() and q.empty():
                    break
                continue
            await ws.send_json(ev.model_dump())
    except WebSocketDisconnect:
        pass
    finally:
        registry.unsubscribe(run, q)
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


def _sanitize(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in name)
    return safe or "app"
