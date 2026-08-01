"""
Folder Compressor - Background zip compression with real-time progress via WebSocket
"""
import asyncio
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List

from fastapi import WebSocket

from .ws_broadcast import broadcast_sync, build_message


class CompressStatus(str, Enum):
    COMPRESSING = "compressing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
        try:
            # Collect the file list with a single walk (reused for counting and zipping)
            file_paths: List[str] = []
            for root, dirs, files in os.walk(task.folder_path):
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
                        self._broadcast_sync(task)
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                        return

                    arcname = os.path.relpath(file_path, os.path.dirname(task.folder_path))
                    try:
                        zf.write(file_path, arcname)
                    except (PermissionError, OSError):
                        # Skip unreadable files
                        pass

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
            task.status = CompressStatus.FAILED
            task.error = str(e)
            self._broadcast_sync(task)

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
        if task and task.zip_path and os.path.exists(task.zip_path):
            try:
                tmp_dir = os.path.dirname(task.zip_path)
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except OSError:
                pass
        self._cancelled.discard(task_id)

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
        broadcast_sync(
            self.websockets,
            self._loop,
            build_message("compress_progress", **self._progress_fields(task)),
        )


# Global compressor instance
compressor = Compressor()
