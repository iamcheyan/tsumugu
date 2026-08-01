"""
Folder Compressor - Background zip compression with real-time progress via WebSocket
"""
import asyncio
import logging
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Dict, List

from fastapi import WebSocket

from .ws_broadcast import broadcast_sync, build_message

logger = logging.getLogger(__name__)


class CompressStatus(str, Enum):
    COMPRESSING = "compressing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Bounded retention for finished tasks so the manager cannot grow unbounded.
TASK_RETENTION = timedelta(hours=1)
MAX_TASKS = 100
_TERMINAL_STATUSES = (CompressStatus.COMPLETED, CompressStatus.FAILED, CompressStatus.CANCELLED)


@dataclass
class CompressTask:
    id: int
    folder_path: str  # Full filesystem path
    folder_name: str  # Display name
    status: CompressStatus = CompressStatus.COMPRESSING
    progress: float = 0.0
    total_files: int = 0
    processed_files: int = 0
    zip_path: Optional[str] = None
    zip_size: int = 0
    error: Optional[str] = None
    created_at: Optional[datetime] = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None


class Compressor:
    """Manages folder compression tasks with progress updates via WebSocket"""

    def __init__(self):
        self.tasks: Dict[int, CompressTask] = {}
        self._next_id: int = 1
        self.websockets: List[WebSocket] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._cancelled: set = set()

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def add_websocket(self, ws: WebSocket):
        self.websockets.append(ws)

    def remove_websocket(self, ws: WebSocket):
        if ws in self.websockets:
            self.websockets.remove(ws)

    async def start_compress(self, folder_path: str, folder_name: str) -> int:
        """Start compressing a folder. Returns task_id."""
        self._evict_expired_tasks()

        task_id = self._next_id
        self._next_id += 1

        task = CompressTask(
            id=task_id,
            folder_path=folder_path,
            folder_name=folder_name,
        )
        self.tasks[task_id] = task

        # Capture the running loop so the worker thread can schedule broadcasts.
        loop = asyncio.get_running_loop()
        self._loop = loop
        # Run compression in thread pool
        loop.run_in_executor(None, self._compress, task)

        return task_id

    def _compress(self, task: CompressTask):
        """Compress a folder to zip (runs in thread pool)."""
        tmp_dir: Optional[str] = None
        try:
            # Collect the file list with a single walk (reused for counting and zipping)
            file_paths: List[str] = []
            for root, dirs, files in os.walk(task.folder_path, onerror=self._on_walk_error):
                file_paths.extend(os.path.join(root, fname) for fname in files)
            task.total_files = max(len(file_paths), 1)

            # Create temp directory for the zip
            tmp_dir = tempfile.mkdtemp(prefix="nas_compress_")
            zip_name = f"{task.folder_name}.zip"
            zip_path = os.path.join(tmp_dir, zip_name)

            processed = 0
            last_broadcast = 0

            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for file_path in file_paths:
                    # Check cancellation
                    if task.id in self._cancelled:
                        task.status = CompressStatus.CANCELLED
                        task.completed_at = datetime.now()
                        self._broadcast_sync(task)
                        return

                    arcname = os.path.relpath(file_path, os.path.dirname(task.folder_path))
                    try:
                        zf.write(file_path, arcname)
                    except (PermissionError, OSError) as e:
                        # Skip unreadable files but surface them in the logs.
                        logger.warning("Skipping unreadable file %s: %s", file_path, e)

                    processed += 1
                    task.processed_files = processed
                    task.progress = min(processed / task.total_files * 100, 99.9)

                    # Broadcast every ~5 files to avoid flooding
                    if processed - last_broadcast >= 5:
                        last_broadcast = processed
                        self._broadcast_sync(task)

            # Compression done
            task.zip_path = zip_path
            task.zip_size = os.path.getsize(zip_path)
            task.progress = 100
            task.status = CompressStatus.COMPLETED
            task.completed_at = datetime.now()
            self._broadcast_sync(task)

        except Exception as e:
            logger.exception("Compression failed for task %s (%s)", task.id, task.folder_name)
            task.status = CompressStatus.FAILED
            task.completed_at = datetime.now()
            task.error = str(e)
            self._broadcast_sync(task)
        finally:
            # Remove the temp dir on every exit path except success, where the
            # zip archive must stay on disk until it is downloaded.
            if tmp_dir and task.status != CompressStatus.COMPLETED:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def cancel(self, task_id: int) -> bool:
        """Cancel a compression task."""
        if task_id in self.tasks:
            task = self.tasks[task_id]
            if task.status == CompressStatus.COMPRESSING:
                self._cancelled.add(task_id)
                return True
        return False

    def get_task(self, task_id: int) -> Optional[CompressTask]:
        return self.tasks.get(task_id)

    def cleanup_task(self, task_id: int):
        """Remove temp zip file and task record."""
        task = self.tasks.pop(task_id, None)
        if task:
            self._remove_task_files(task)
        self._cancelled.discard(task_id)

    def _on_walk_error(self, err: OSError):
        """os.walk onerror: log unreadable subtrees, keep walking the rest."""
        logger.warning("Skipping unreadable directory while compressing: %s", err)

    def _remove_task_files(self, task: CompressTask):
        """Remove the temp dir holding a finished task's zip archive."""
        if task.zip_path:
            tmp_dir = os.path.dirname(task.zip_path)
            if tmp_dir:
                try:
                    shutil.rmtree(tmp_dir)
                except OSError as e:
                    logger.warning("Failed to remove temp dir %s: %s", tmp_dir, e)

    def _evict_expired_tasks(self):
        """Drop terminal tasks older than the retention window; cap the dict."""
        now = datetime.now()
        for task in list(self.tasks.values()):
            if task.status not in _TERMINAL_STATUSES:
                continue
            ended = task.completed_at or task.created_at or now
            if now - ended > TASK_RETENTION:
                self.tasks.pop(task.id, None)
                self._remove_task_files(task)

        # Hard cap: evict the oldest terminal tasks if the dict is over MAX_TASKS.
        over = len(self.tasks) - MAX_TASKS
        if over > 0:
            oldest_terminal = sorted(
                (t for t in self.tasks.values() if t.status in _TERMINAL_STATUSES),
                key=lambda t: t.completed_at or t.created_at or datetime.min,
            )
            for task in oldest_terminal[:over]:
                self.tasks.pop(task.id, None)
                self._remove_task_files(task)

    def _progress_fields(self, task: CompressTask) -> dict:
        """Message payload matching what the frontend consumes for compress_progress."""
        return {
            "task_id": task.id,
            "status": task.status.value,
            "progress": task.progress,
            "folder_name": task.folder_name,
            "total_files": task.total_files,
            "processed_files": task.processed_files,
            "zip_size": task.zip_size,
            "error": task.error,
        }

    def _broadcast_sync(self, task: CompressTask):
        """Broadcast progress (thread-safe via loop)."""
        loop = self._loop
        if loop is None:
            return
        broadcast_sync(
            self.websockets,
            loop,
            build_message("compress_progress", **self._progress_fields(task)),
        )


# Global compressor instance
compressor = Compressor()
