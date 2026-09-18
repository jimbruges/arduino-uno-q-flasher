"""In-memory registry of active runs + WebSocket event broker.

Each run has:
  - a FlasherContext (paths, password)
  - one DeviceState per device
  - a list of subscriber queues; events are fanned out to every subscriber
  - a replay buffer so a freshly-connected WS gets prior events
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .events import (
    DeviceFinishedEvent,
    DeviceState,
    Event,
    LogEvent,
    RunFinishedEvent,
    Stage,
    StageEvent,
)
from .flasher import AfterSuccessFn, FlasherContext, flash_device


@dataclass
class Upload:
    upload_id: str
    folder: Path  # path to staged folder on disk
    name: str  # original folder name


@dataclass
class Run:
    run_id: str
    ctx: FlasherContext
    devices: dict[str, DeviceState] = field(default_factory=dict)
    subscribers: list[asyncio.Queue[Event]] = field(default_factory=list)
    event_log: list[Event] = field(default_factory=list)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    upload: Upload | None = None
    _tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    after_success: AfterSuccessFn | None = None


class Registry:
    def __init__(self, uploads_dir: Path) -> None:
        self.uploads_dir = uploads_dir
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self._uploads: dict[str, Upload] = {}
        self._runs: dict[str, Run] = {}

    # ---------- uploads ----------

    def new_upload_dir(self) -> tuple[str, Path]:
        upload_id = uuid.uuid4().hex[:12]
        target = self.uploads_dir / upload_id
        target.mkdir(parents=True, exist_ok=True)
        return upload_id, target

    def register_upload(self, upload_id: str, folder: Path, name: str) -> Upload:
        upload = Upload(upload_id=upload_id, folder=folder, name=name)
        self._uploads[upload_id] = upload
        metadata = {
            "upload_id": upload_id,
            "folder_name": name,
            "folder": folder.name,
        }
        (folder.parent / "upload.json").write_text(
            json.dumps(metadata, indent=2) + "\n",
            encoding="utf-8",
        )
        return upload

    def get_upload(self, upload_id: str) -> Upload | None:
        if not upload_id.isalnum() or len(upload_id) > 64:
            return None
        base = self.uploads_dir / upload_id
        metadata_path = base / "upload.json"
        try:
            if time.time() - metadata_path.stat().st_mtime > 24 * 60 * 60:
                self.cleanup_upload(upload_id)
                return None
        except OSError:
            return None
        upload = self._uploads.get(upload_id)
        if upload is not None and upload.folder.is_dir():
            return upload
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            folder = base / metadata["folder"]
            name = str(metadata["folder_name"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if not folder.is_dir() or folder.parent != base:
            return None
        upload = Upload(upload_id=upload_id, folder=folder, name=name)
        self._uploads[upload_id] = upload
        return upload

    def cleanup_upload(self, upload_id: str) -> None:
        upload = self._uploads.pop(upload_id, None)
        target = upload.folder.parent if upload else self.uploads_dir / upload_id
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)

    # ---------- runs ----------

    def create_run(self, ctx: FlasherContext, upload: Upload | None) -> Run:
        run_id = uuid.uuid4().hex[:12]
        run = Run(run_id=run_id, ctx=ctx, upload=upload)
        self._runs[run_id] = run
        return run

    def get_run(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    # ---------- pub/sub ----------

    async def emit(self, run: Run, event: Event) -> None:
        run.event_log.append(event)
        self._apply_to_device_state(run, event)
        for q in list(run.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def subscribe(self, run: Run) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=10_000)
        # replay
        for ev in run.event_log:
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                break
        run.subscribers.append(q)
        return q

    def unsubscribe(self, run: Run, q: asyncio.Queue[Event]) -> None:
        if q in run.subscribers:
            run.subscribers.remove(q)

    def _apply_to_device_state(self, run: Run, event: Event) -> None:
        if isinstance(event, StageEvent):
            d = run.devices.get(event.device)
            if not d:
                return
            d.stages[event.stage] = event.status
            if event.status == "started":
                d.current_stage = event.stage
                d.status = "running"
        elif isinstance(event, DeviceFinishedEvent):
            d = run.devices.get(event.device)
            if not d:
                return
            d.status = event.result  # type: ignore[assignment]
            d.current_stage = None
            d.elapsed_seconds = event.elapsed_seconds
            d.failure_reason = event.failure_reason

    # ---------- run lifecycle ----------

    async def run_devices(
        self,
        run: Run,
        device_configs: list[tuple[str, set[Stage]]],
        after_success: AfterSuccessFn | None = None,
    ) -> None:
        """Kick off all devices in parallel and wait for completion."""
        run.after_success = after_success
        for serial, skip in device_configs:
            run.devices[serial] = DeviceState(
                serial=serial,
                status="idle",
                skip_stages=list(skip),
            )

        async def run_one(serial: str, skip: set[Stage]) -> str:
            ok = await flash_device(
                serial,
                run.ctx,
                skip,
                lambda ev: self.emit(run, ev),
                after_success,
            )
            return "success" if ok else "failed"

        tasks: dict[str, asyncio.Task] = {}
        for serial, skip in device_configs:
            t = asyncio.create_task(run_one(serial, skip))
            tasks[serial] = t
            run._tasks[serial] = t

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        successful: list[str] = []
        failed: list[str] = []
        for serial, res in zip(tasks.keys(), results):
            if isinstance(res, Exception):
                failed.append(serial)
                await self.emit(
                    run,
                    LogEvent(
                        device=serial,
                        line=f"Unhandled exception: {res!r}",
                        stream="stderr",
                    ),
                )
                await self.emit(
                    run,
                    DeviceFinishedEvent(device=serial, result="failed"),
                )
            elif res == "success":
                successful.append(serial)
            else:
                failed.append(serial)

        await self.emit(
            run,
            RunFinishedEvent(successful=successful, failed=failed),
        )

        run.finished.set()

        # Uploads remain reusable for 24 hours so browser reloads and later
        # board batches do not require selecting and transferring the folder again.

    async def retry_device(
        self, run: Run, serial: str, skip: set[Stage]
    ) -> None:
        """Re-run a single device using the same context."""
        if serial in run._tasks and not run._tasks[serial].done():
            return  # already running
        run.devices[serial] = DeviceState(
            serial=serial,
            status="idle",
            skip_stages=list(skip),
        )

        async def run_one() -> None:
            await flash_device(
                serial,
                run.ctx,
                skip,
                lambda ev: self.emit(run, ev),
                run.after_success,
            )

        t = asyncio.create_task(run_one())
        run._tasks[serial] = t
