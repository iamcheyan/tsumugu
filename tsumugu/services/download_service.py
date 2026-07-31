"""Download manager — yt-dlp + wget queue, no WebSocket.

Replaces the FastAPI WebSocket broadcast with a listener-callback pattern:
TUI widgets register as listeners and receive progress updates.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

import yt_dlp

from .audio_splitter import audio_splitter


class DownloadStatus(str, Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    CONVERTING = "converting"
    SPLITTING = "splitting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class DownloadTask:
    id: int
    url: str
    title: str = ""
    format: str = "mp3"
    split_mode: Optional[str] = None
    keep_original: bool = False
    save_path: str = "/"
    status: DownloadStatus = DownloadStatus.PENDING
    progress: float = 0.0
    speed: str = ""
    eta: str = ""
    current_file: str = ""
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    download_type: str = "youtube"  # "youtube" or "direct"

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["created_at"] = self.created_at.isoformat() if self.created_at else None
        d["completed_at"] = (
            self.completed_at.isoformat() if self.completed_at else None
        )
        return d


ProgressListener = Callable[[DownloadTask], Awaitable[None]]


class DownloadManager:
    """Manages a download queue with concurrent task execution."""

    def __init__(self, max_concurrent: int = 3):
        self.max_concurrent = max_concurrent
        self.tasks: Dict[int, DownloadTask] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self.active_tasks: int = 0
        self._listeners: List[ProgressListener] = []
        self._worker_task: Optional[asyncio.Task] = None
        self._started = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self._started:
            self._started = True
            self._loop = asyncio.get_running_loop()
            self._worker_task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        self._started = False

    # ── listeners ──────────────────────────────────────────────────────────

    def add_listener(self, cb: ProgressListener) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: ProgressListener) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    # ── queue management ───────────────────────────────────────────────────

    async def add_download(self, task: DownloadTask) -> int:
        self.tasks[task.id] = task
        await self.queue.put(task.id)
        await self._broadcast(task)
        return task.id

    async def cancel_download(self, task_id: int) -> bool:
        task = self.tasks.get(task_id)
        if task and task.status in (DownloadStatus.PENDING, DownloadStatus.DOWNLOADING):
            task.status = DownloadStatus.CANCELLED
            await self._broadcast(task)
            return True
        return False

    def get_task(self, task_id: int) -> Optional[DownloadTask]:
        return self.tasks.get(task_id)

    def get_all_tasks(self) -> List[DownloadTask]:
        return list(self.tasks.values())

    def get_queue_status(self) -> dict[str, Any]:
        return {
            "total": len(self.tasks),
            "pending": sum(1 for t in self.tasks.values() if t.status == DownloadStatus.PENDING),
            "downloading": sum(1 for t in self.tasks.values() if t.status == DownloadStatus.DOWNLOADING),
            "converting": sum(1 for t in self.tasks.values() if t.status == DownloadStatus.CONVERTING),
            "splitting": sum(1 for t in self.tasks.values() if t.status == DownloadStatus.SPLITTING),
            "completed": sum(1 for t in self.tasks.values() if t.status == DownloadStatus.COMPLETED),
            "failed": sum(1 for t in self.tasks.values() if t.status == DownloadStatus.FAILED),
            "active_tasks": self.active_tasks,
            "max_concurrent": self.max_concurrent,
        }

    def clear_completed(self) -> None:
        """Remove finished tasks from memory."""
        done = {k: v for k, v in self.tasks.items()
                if v.status in (DownloadStatus.COMPLETED, DownloadStatus.FAILED,
                                DownloadStatus.CANCELLED)}
        for k in done:
            del self.tasks[k]

    # ── worker ─────────────────────────────────────────────────────────────

    async def _worker(self) -> None:
        while True:
            try:
                task_id = await self.queue.get()
                task = self.tasks.get(task_id)
                if not task or task.status == DownloadStatus.CANCELLED:
                    self.queue.task_done()
                    continue
                while self.active_tasks >= self.max_concurrent:
                    await asyncio.sleep(0.5)
                self.active_tasks += 1
                try:
                    await self._process_download(task)
                finally:
                    self.active_tasks -= 1
                    self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                print(f"Worker error: {exc}")
                await asyncio.sleep(1)

    async def _process_download(self, task: DownloadTask) -> None:
        try:
            # Ensure save dir exists and is writable
            try:
                os.makedirs(task.save_path, exist_ok=True)
                test_file = os.path.join(task.save_path, ".write_test")
                with open(test_file, "w") as f:
                    f.write("test")
                os.remove(test_file)
            except PermissionError:
                task.status = DownloadStatus.FAILED
                task.error = f"Permission denied: Cannot write to '{task.save_path}'"
                await self._broadcast(task)
                return
            except OSError as exc:
                task.status = DownloadStatus.FAILED
                task.error = f"Cannot create directory: {exc}"
                await self._broadcast(task)
                return

            task.status = DownloadStatus.DOWNLOADING
            await self._broadcast(task)

            if task.download_type == "direct":
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._download_with_wget, task)
            else:
                format_ext = task.format.lower()
                outputtmpl = os.path.join(task.save_path, "%(title)s.%(ext)s")
                ydl_opts = {
                    "outtmpl": outputtmpl,
                    "format": "bestaudio/best",
                    "postprocessors": [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": format_ext,
                        "preferredquality": "192",
                    }],
                    "progress_hooks": [lambda d: self._progress_hook(task, d)],
                    "quiet": True,
                    "no_warnings": True,
                }
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._download_with_ytdlp, task, ydl_opts)

            if task.status != DownloadStatus.CANCELLED:
                if task.split_mode and task.download_type == "youtube":
                    await self._split_audio(task)
                task.status = DownloadStatus.COMPLETED
                task.progress = 100.0
                task.completed_at = datetime.now()
                await self._broadcast(task)
        except Exception as exc:
            task.status = DownloadStatus.FAILED
            task.error = str(exc)
            await self._broadcast(task)

    # ── download methods ───────────────────────────────────────────────────

    def _download_with_ytdlp(self, task: DownloadTask, ydl_opts: dict) -> None:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([task.url])
        except Exception as exc:
            raise Exception(f"Download failed: {exc}")

    def _download_with_wget(self, task: DownloadTask) -> None:
        filename = task.current_file
        if not filename:
            from urllib.parse import urlparse, unquote
            parsed = urlparse(task.url)
            filename = unquote(os.path.basename(parsed.path)) or "download"
            task.current_file = filename
        output_path = os.path.join(task.save_path, filename)
        cmd = [
            "wget", "-c", "--show-progress", "-q",
            "--timeout=30", "--tries=3", "-O", output_path, task.url,
        ]
        try:
            proc = subprocess.Popen(
                cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
                text=True, bufsize=1,
            )
            progress_re = re.compile(
                r"^\s*(\d+)%\s+([\d.]+\s*[KMG]i?B/s)\s+(?:eta\s+)?(.+)?$"
            )
            for line in proc.stderr:
                line = line.rstrip("\n\r")
                m = progress_re.match(line)
                if m:
                    task.progress = float(m.group(1))
                    task.speed = m.group(2).strip()
                    eta_str = (m.group(3) or "").strip()
                    if eta_str and eta_str != "in":
                        task.eta = eta_str
                    if self._loop and self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self._broadcast(task), self._loop
                        )
            proc.wait()
            if proc.returncode != 0 and task.status != DownloadStatus.CANCELLED:
                raise Exception(f"wget exited with code {proc.returncode}")
        except Exception as exc:
            if task.status != DownloadStatus.CANCELLED:
                raise Exception(f"Download failed: {exc}")

    def _progress_hook(self, task: DownloadTask, d: dict) -> None:
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                task.progress = (downloaded / total) * 100
            speed = d.get("speed")
            if speed:
                if speed > 1024 * 1024:
                    task.speed = f"{speed / (1024 * 1024):.1f} MB/s"
                elif speed > 1024:
                    task.speed = f"{speed / 1024:.1f} KB/s"
                else:
                    task.speed = f"{speed:.0f} B/s"
            eta = d.get("eta")
            if eta:
                if eta > 3600:
                    task.eta = f"{eta // 3600}h {(eta % 3600) // 60}m"
                elif eta > 60:
                    task.eta = f"{eta // 60}m {eta % 60}s"
                else:
                    task.eta = f"{eta}s"
            filename = d.get("filename", "")
            if filename:
                task.current_file = os.path.basename(filename)
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._broadcast(task), self._loop
                )
        elif d["status"] == "finished":
            task.status = DownloadStatus.CONVERTING
            task.progress = 100.0
            task.speed = ""
            task.eta = ""
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._broadcast(task), self._loop
                )

    async def _split_audio(self, task: DownloadTask) -> None:
        try:
            task.status = DownloadStatus.SPLITTING
            await self._broadcast(task)
            audio_file = self._find_downloaded_file(task)
            if not audio_file:
                print(f"Warning: Could not find downloaded file for task {task.id}")
                return
            result = await audio_splitter.split_audio(
                audio_file_path=audio_file,
                split_mode=task.split_mode or "chapter_info",
                keep_original=task.keep_original,
                output_dir=task.save_path,
            )
            if result.success:
                print(f"Successfully split {len(result.files)} tracks from {audio_file}")
            else:
                print(f"Warning: Split failed: {result.error}")
        except Exception as exc:
            print(f"Error during audio splitting: {exc}")

    def _find_downloaded_file(self, task: DownloadTask) -> Optional[str]:
        if os.path.exists(task.save_path):
            for file in os.listdir(task.save_path):
                if file.startswith(task.title) and file.endswith(f".{task.format}"):
                    return os.path.join(task.save_path, file)
        audio_exts = [f".{task.format}", ".mp3", ".m4a", ".flac"]
        if os.path.exists(task.save_path):
            for file in os.listdir(task.save_path):
                if any(file.endswith(ext) for ext in audio_exts):
                    return os.path.join(task.save_path, file)
        return None

    # ── broadcast ──────────────────────────────────────────────────────────

    async def _broadcast(self, task: DownloadTask) -> None:
        for cb in list(self._listeners):
            try:
                await cb(task)
            except Exception as exc:
                print(f"[download] listener error: {exc}")


# Global singleton
download_manager = DownloadManager(max_concurrent=3)