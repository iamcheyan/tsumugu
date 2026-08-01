"""
Download Manager - Handles yt-dlp + wget download queue with real-time progress via WebSocket
"""
import asyncio
import logging
import os
import re
import subprocess
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from enum import Enum
from datetime import datetime, timedelta
import yt_dlp
from fastapi import WebSocket
from .audio_splitter import audio_splitter
from .ws_broadcast import build_message, broadcast_serialized, broadcast_sync

logger = logging.getLogger(__name__)


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
    process: Optional[subprocess.Popen] = None
    file_path: Optional[str] = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()


class DownloadManager:
    """Manages download queue with concurrent task execution"""

    _TERMINAL_RETENTION = timedelta(hours=1)
    _MAX_TERMINAL_TASKS = 100

    def __init__(self, max_concurrent: int = 3):
        self.max_concurrent = max_concurrent
        self.tasks: Dict[int, DownloadTask] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self.active_tasks: int = 0
        self.websockets: List[WebSocket] = []
        self._worker_task: Optional[asyncio.Task] = None
        self._started = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._terminal_at: Dict[int, datetime] = {}

    def _prune_tasks(self) -> None:
        """Evict terminal tasks past the retention window or beyond the cap."""
        now = datetime.now()
        # Age-based: drop terminal tasks older than the retention window.
        for tid in list(self._terminal_at):
            if now - self._terminal_at[tid] > self._TERMINAL_RETENTION:
                self.tasks.pop(tid, None)
                self._terminal_at.pop(tid, None)
        # Count-based: keep only the newest MAX_TERMINAL_TASKS terminal entries.
        terminal_ids = sorted(self._terminal_at, key=lambda tid: self._terminal_at[tid])
        overflow = len(terminal_ids) - self._MAX_TERMINAL_TASKS
        if overflow > 0:
            for tid in terminal_ids[:overflow]:
                self.tasks.pop(tid, None)
                self._terminal_at.pop(tid, None)

    async def start(self):
        """Start the download worker"""
        if not self._started:
            self._started = True
            self._loop = asyncio.get_running_loop()
            self._worker_task = asyncio.create_task(self._worker())
    
    async def stop(self):
        """Stop the download worker"""
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        self._started = False
    
    def add_websocket(self, ws: WebSocket):
        """Register a WebSocket for progress updates"""
        self.websockets.append(ws)
    
    def remove_websocket(self, ws: WebSocket):
        """Unregister a WebSocket"""
        if ws in self.websockets:
            self.websockets.remove(ws)
    
    async def add_download(self, task: DownloadTask) -> int:
        """Add a download task to the queue"""
        self._prune_tasks()
        self.tasks[task.id] = task
        await self.queue.put(task.id)
        await self._broadcast_progress(task)
        return task.id
    
    async def cancel_download(self, task_id: int) -> bool:
        """Cancel a download task (kills the subprocess if one is running)"""
        if task_id not in self.tasks:
            return False
        task = self.tasks[task_id]
        if task.status not in [DownloadStatus.PENDING, DownloadStatus.DOWNLOADING]:
            return False
        task.status = DownloadStatus.CANCELLED
        task.completed_at = datetime.now()
        self._terminal_at[task.id] = task.completed_at
        await self._broadcast_progress(task)
        self._update_history(task, DownloadStatus.CANCELLED.value)
        if task.process is not None and task.process.poll() is None:
            await asyncio.to_thread(self._terminate_process, task)
        return True

    def _terminate_process(self, task: DownloadTask) -> None:
        """Terminate the wget subprocess, escalating to kill (worker thread)."""
        proc = task.process
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.error("Subprocess for task %s did not exit after kill", task.id)
    
    def get_task(self, task_id: int) -> Optional[DownloadTask]:
        """Get a download task by ID"""
        return self.tasks.get(task_id)
    
    def get_all_tasks(self) -> List[DownloadTask]:
        """Get all download tasks"""
        return list(self.tasks.values())
    
    def _update_history(self, task: DownloadTask, status: str, file_path: Optional[str] = None) -> None:
        """Write back download status to the DownloadHistory DB row.

        Uses a short-lived session that is always closed; never held across awaits.
        """
        from .database import SessionLocal
        from .models import DownloadHistory

        db = None
        try:
            db = SessionLocal()
            row = db.query(DownloadHistory).filter(DownloadHistory.id == task.id).first()
            if row is None:
                logger.warning("No DownloadHistory row for task %s; skipping writeback", task.id)
                return
            row.status = status
            if file_path is not None:
                row.file_path = file_path
            db.commit()
        except Exception:
            logger.exception("Failed to update DownloadHistory for task %s", task.id)
        finally:
            if db is not None:
                db.close()

    def get_queue_status(self) -> Dict[str, Any]:
        """Get queue status summary"""
        return {
            "total": len(self.tasks),
            "pending": sum(1 for t in self.tasks.values() if t.status == DownloadStatus.PENDING),
            "downloading": sum(1 for t in self.tasks.values() if t.status == DownloadStatus.DOWNLOADING),
            "converting": sum(1 for t in self.tasks.values() if t.status == DownloadStatus.CONVERTING),
            "completed": sum(1 for t in self.tasks.values() if t.status == DownloadStatus.COMPLETED),
            "failed": sum(1 for t in self.tasks.values() if t.status == DownloadStatus.FAILED),
            "active_tasks": self.active_tasks,
            "max_concurrent": self.max_concurrent
        }
    
    async def _worker(self):
        """Worker loop that processes download tasks from the queue"""
        while True:
            try:
                task_id = await self.queue.get()
                task = self.tasks.get(task_id)
                
                if not task or task.status == DownloadStatus.CANCELLED:
                    self.queue.task_done()
                    continue
                
                # Wait if we've reached max concurrent downloads
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
            except Exception:
                logger.exception("Worker error")
                await asyncio.sleep(1)
    
    async def _process_download(self, task: DownloadTask):
        """Process a single download task"""
        try:
            # Ensure save directory exists and is writable
            try:
                os.makedirs(task.save_path, exist_ok=True)
                # Test write permission
                test_file = os.path.join(task.save_path, '.write_test')
                with open(test_file, 'w') as f:
                    f.write('test')
                os.remove(test_file)
            except PermissionError as e:
                await self._fail_task(task, f"Permission denied: Cannot write to '{task.save_path}'")
                return
            except OSError as e:
                await self._fail_task(task, f"Cannot create directory: {str(e)}")
                return

            task.status = DownloadStatus.DOWNLOADING
            await self._broadcast_progress(task)
            self._update_history(task, DownloadStatus.DOWNLOADING.value)

            if task.download_type == "direct":
                # Direct file download via wget
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._download_with_wget, task)
            else:
                # YouTube download via yt-dlp
                format_ext = task.format.lower()
                outputtmpl = os.path.join(task.save_path, '%(title)s.%(ext)s')

                ydl_opts = {
                    'outtmpl': outputtmpl,
                    'format': 'bestaudio/best',
                    'nooverwrites': True,
                    'nopostoverwrites': True,
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': format_ext,
                        'preferredquality': '192',
                    }],
                    'progress_hooks': [lambda d: self._progress_hook(task, d)],
                    'quiet': True,
                    'no_warnings': True,
                }

                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._download_with_ytdlp, task, ydl_opts)

            if task.status != DownloadStatus.CANCELLED:
                # Check if splitting is needed (YouTube only)
                if task.split_mode and task.download_type == "youtube":
                    await self._split_audio(task)

                if task.status != DownloadStatus.FAILED:
                    task.status = DownloadStatus.COMPLETED
                    task.progress = 100.0
                    task.completed_at = datetime.now()
                    self._terminal_at[task.id] = task.completed_at
                    await self._broadcast_progress(task)
                    self._update_history(task, DownloadStatus.COMPLETED.value, file_path=task.file_path)

        except Exception as e:
            if task.status != DownloadStatus.CANCELLED:
                await self._fail_task(task, str(e))
    
    def _download_with_ytdlp(self, task: DownloadTask, ydl_opts: dict):
        """Download using yt-dlp (runs in thread pool)"""
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([task.url])
            task.file_path = self._find_downloaded_file(task)
        except Exception as e:
            if task.status == DownloadStatus.CANCELLED:
                # Cooperative abort raised from _progress_hook; not a failure.
                return
            raise Exception(f"Download failed: {str(e)}") from e

    def _download_with_wget(self, task: DownloadTask):
        """Download a direct file using wget with resume support (runs in thread pool).

        Parses wget's stderr progress output to update task progress, speed, and ETA.
        """
        # Ensure we have a filename for resume support
        filename = task.current_file
        if not filename:
            from urllib.parse import urlparse, unquote
            parsed = urlparse(task.url)
            filename = unquote(os.path.basename(parsed.path)) or "download"
            task.current_file = filename

        output_path = os.path.join(task.save_path, filename)

        cmd = [
            "wget",
            "-c",                # continue partial downloads
            "--show-progress",   # show progress in stderr
            "-q",               # quiet (no non-progress output)
            "--timeout=30",
            "--tries=3",
            "-O", output_path,
            task.url,
        ]

        try:
            proc = subprocess.Popen(
                cmd,
                stderr=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                text=True,
                bufsize=1,  # line-buffered
            )
            task.process = proc

            if task.status == DownloadStatus.CANCELLED:
                proc.terminate()

            # Pattern:  "  55%  12.3MB/s  eta 10s"
            # or:       " 100%  2.1MiB/s  in 5s"
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
                    elif "in" in line:
                        # " 100%  2.1MiB/s  in 5s"
                        in_match = re.search(r"in\s+(\S+)\s*$", line)
                        if in_match:
                            task.eta = ""

                    if self._loop and self._loop.is_running():
                        broadcast_sync(
                            self.websockets, self._loop,
                            build_message("download_progress", **self._progress_fields(task)),
                        )

            proc.wait()

            if proc.returncode != 0 and task.status != DownloadStatus.CANCELLED:
                raise Exception(f"wget exited with code {proc.returncode}")

        except Exception as e:
            if task.status != DownloadStatus.CANCELLED:
                raise Exception(f"Download failed: {str(e)}") from e

    def _progress_hook(self, task: DownloadTask, d: dict):
        """Progress hook for yt-dlp"""
        if task.status == DownloadStatus.CANCELLED:
            # Cooperative abort: raising here aborts ydl.download().
            raise Exception("Download cancelled")
        if d['status'] == 'downloading':
            # Extract progress info
            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            downloaded_bytes = d.get('downloaded_bytes', 0)
            
            if total_bytes > 0:
                task.progress = (downloaded_bytes / total_bytes) * 100
            
            # Speed and ETA
            speed = d.get('speed')
            if speed:
                if speed > 1024 * 1024:
                    task.speed = f"{speed / (1024 * 1024):.1f} MB/s"
                elif speed > 1024:
                    task.speed = f"{speed / 1024:.1f} KB/s"
                else:
                    task.speed = f"{speed:.0f} B/s"
            
            eta = d.get('eta')
            if eta:
                if eta > 3600:
                    task.eta = f"{eta // 3600}h {((eta % 3600) // 60)}m"
                elif eta > 60:
                    task.eta = f"{eta // 60}m {eta % 60}s"
                else:
                    task.eta = f"{eta}s"
            
            # Current filename
            filename = d.get('filename', '')
            if filename:
                task.current_file = os.path.basename(filename)
            
            # Broadcast progress (thread-safe)
            if self._loop and self._loop.is_running():
                broadcast_sync(
                    self.websockets, self._loop,
                    build_message("download_progress", **self._progress_fields(task)),
                )

        elif d['status'] == 'finished':
            task.status = DownloadStatus.CONVERTING
            task.progress = 100.0
            task.speed = ""
            task.eta = ""
            if self._loop and self._loop.is_running():
                broadcast_sync(
                    self.websockets, self._loop,
                    build_message("download_progress", **self._progress_fields(task)),
                )
    
    async def _split_audio(self, task: DownloadTask):
        """Split audio file if split_mode is set; failure marks the task FAILED"""
        try:
            # Update status to splitting
            task.status = DownloadStatus.SPLITTING
            await self._broadcast_progress(task)

            # Find the downloaded audio file
            audio_file = self._find_downloaded_file(task)
            if not audio_file:
                await self._fail_task(task, "Could not find downloaded file to split")
                return

            # Perform the split
            result = await audio_splitter.split_audio(
                audio_file_path=audio_file,
                split_mode=task.split_mode or "chapter_info",
                keep_original=task.keep_original,
                output_dir=task.save_path
            )

            if result.success:
                logger.info("Successfully split %d tracks from %s", len(result.files), audio_file)
            else:
                await self._fail_task(task, f"Split failed: {result.error}")

        except Exception as e:
            await self._fail_task(task, f"Error during audio splitting: {e}")

    async def _fail_task(self, task: DownloadTask, error: str) -> None:
        """Mark a task FAILED, broadcast it, and write back to the DB."""
        task.status = DownloadStatus.FAILED
        task.error = error
        task.completed_at = datetime.now()
        self._terminal_at[task.id] = task.completed_at
        await self._broadcast_progress(task)
        self._update_history(task, DownloadStatus.FAILED.value)
    
    def _find_downloaded_file(self, task: DownloadTask) -> Optional[str]:
        """Find the downloaded audio file based on task info"""
        # Look for files in the save path that match the title
        if os.path.exists(task.save_path):
            for file in os.listdir(task.save_path):
                if file.startswith(task.title) and file.endswith(f".{task.format}"):
                    return os.path.join(task.save_path, file)
        
        # Try to find any audio file in the save path
        audio_extensions = [f".{task.format}", ".mp3", ".m4a", ".flac"]
        if os.path.exists(task.save_path):
            for file in os.listdir(task.save_path):
                if any(file.endswith(ext) for ext in audio_extensions):
                    return os.path.join(task.save_path, file)
        
        return None
    
    def _progress_fields(self, task: DownloadTask) -> Dict[str, Any]:
        """Build the progress message fields (shape shared with the frontend)."""
        return {
            "task_id": task.id,
            "status": task.status.value,
            "progress": task.progress,
            "speed": task.speed,
            "eta": task.eta,
            "current_file": task.current_file,
            "title": task.title,
            "error": task.error,
            "queue_status": self.get_queue_status()
        }

    async def _broadcast_progress(self, task: DownloadTask):
        """Broadcast progress update to all connected WebSockets"""
        await broadcast_serialized(self.websockets, "download_progress", **self._progress_fields(task))


# Global download manager instance
download_manager = DownloadManager(max_concurrent=3)
