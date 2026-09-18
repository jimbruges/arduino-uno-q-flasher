"""Capture reusable workshop artifacts from a prepared UNO Q."""
from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import tarfile
import time
from pathlib import Path

from . import adb

IMAGE_ARCHIVE_NAME = "docker-images.tar"
ARDUINO_ARCHIVE_NAME = "arduino-data.tar.gz"
APP_RUNTIME_ARCHIVE_NAME = "app-runtime.tar.gz"
MANIFEST_NAME = "manifest.json"


class WorkshopCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._capture_lock = asyncio.Lock()
        self._capture_all_lock = asyncio.Lock()
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

    def status(self) -> dict:
        manifest: dict = {}
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
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
            for path in (self.image_archive, self.arduino_archive, self.app_runtime_archive)
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

    async def capture_all(self, serial: str) -> dict:
        async with self._capture_all_lock:
            paths = (
                self.image_archive,
                self.arduino_archive,
                self.app_runtime_archive,
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
                await self.capture_arduino_data(serial)
                await self.capture_app_runtime(serial)
                result = await self.capture_images(serial)
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

    def _read_manifest(self) -> dict:
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_manifest(self, manifest: dict) -> None:
        temp = self.manifest_path.with_suffix(".json.part")
        temp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        temp.replace(self.manifest_path)


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
        raise ValueError(f"invalid tar archive: {exc}") from exc


def _validate_docker_archive(path: Path) -> None:
    _validate_tar_archive(path, "manifest.json")