"""Folder compression — background zip with progress callbacks.

WebSocket broadcast replaced with listener callbacks.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

ProgressListener = Callable[["CompressTask"], Awaitable[None]]


class CompressStatus(str, Enum):
    COMPRESSING = "compressing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class CompressTask:
    id: int
    folder_path: str
    folder_name: str
    status: CompressStatus = CompressStatus.COMPRESSING
    progress: float = 0.0
    total_files: int = 0
    processed_files: int = 0
    zip_path: Optional[str] = None
    zip_size: int = 0
    error: Optional[str] = None
    created_at: Optional[datetime] = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "folder_name": self.folder_name,
            "status": self.status.value,
            "progress": self.progress,
            "total_files": self.total_files,
            "processed_files": self.processed_files,
            "zip_size": self.zip_size,
            "error": self.error,
        }
        return d


class Compressor:
    """Manages folder compression tasks with progress updates via callbacks."""

    def __init__(self):
        self.tasks: Dict[int, CompressTask] = {}
        self._next_id: int = 1
        self._listeners: List[ProgressListener] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._cancelled: set = set()

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def add_listener(self, cb: ProgressListener) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: ProgressListener) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    async def start_compress(self, folder_path: str, folder_name: str) -> int:
        task_id = self._next_id
        self._next_id += 1
        task = CompressTask(id=task_id, folder_path=folder_path, folder_name=folder_name)
        self.tasks[task_id] = task
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, self._compress, task)
        return task_id

    def _compress(self, task: CompressTask) -> None:
        try:
            tmp_dir = tempfile.mkdtemp()
            zip_name = f"{task.folder_name}.zip"
            zip_path = os.path.join(tmp_dir, zip_name)

            # Count files first
            file_list: List[str] = []
            for root, _dirs, files in os.walk(task.folder_path):
                for f in files:
                    file_list.append(os.path.join(root, f))
            task.total_files = len(file_list)

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, file_path in enumerate(file_list):
                    if task.id in self._cancelled:
                        task.status = CompressStatus.CANCELLED
                        self._broadcast_sync(task)
                        return
                    arcname = os.path.relpath(
                        file_path, os.path.dirname(task.folder_path)
                    )
                    zf.write(file_path, arcname)
                    task.processed_files = i + 1
                    if task.total_files > 0:
                        task.progress = (
                            task.processed_files / task.total_files * 100
                        )
                    if task.processed_files % 5 == 0 or task.processed_files == task.total_files:
                        self._broadcast_sync(task)

            task.zip_path = zip_path
            task.zip_size = os.path.getsize(zip_path)
            task.status = CompressStatus.COMPLETED
            task.completed_at = datetime.now()
            task.progress = 100.0
            self._broadcast_sync(task)
        except Exception as exc:
            task.status = CompressStatus.FAILED
            task.error = str(exc)
            self._broadcast_sync(task)

    def cancel(self, task_id: int) -> bool:
        task = self.tasks.get(task_id)
        if task and task.status == CompressStatus.COMPRESSING:
            self._cancelled.add(task_id)
            return True
        return False

    def get_task(self, task_id: int) -> Optional[CompressTask]:
        return self.tasks.get(task_id)

    def cleanup_task(self, task_id: int) -> None:
        task = self.tasks.get(task_id)
        if task and task.zip_path:
            tmp_dir = os.path.dirname(task.zip_path)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            task.zip_path = None
        self.tasks.pop(task_id, None)
        self._cancelled.discard(task_id)

    def _broadcast_sync(self, task: CompressTask) -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._broadcast(task), self._loop)

    async def _broadcast(self, task: CompressTask) -> None:
        for cb in list(self._listeners):
            try:
                await cb(task)
            except Exception as exc:
                print(f"[compress] listener error: {exc}")


# Global singleton
compressor = Compressor()