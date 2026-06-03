from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import Config, DownloadHistory
from ..download_manager import download_manager, DownloadTask, DownloadStatus
from ..compressor import compressor
from pydantic import BaseModel
from typing import Optional
import yt_dlp
import asyncio
import urllib.request
import urllib.error
from urllib.parse import urlparse, parse_qs, unquote
import re
import os

router = APIRouter(prefix="/api/youtube", tags=["youtube"])

class DownloadRequest(BaseModel):
    url: str
    format: str = "mp3"  # mp3, m4a, flac
    split_mode: Optional[str] = None  # chapter_info, silence_detection
    keep_original: bool = False
    save_path: str = "/"
    title: str = ""
    download_type: str = "youtube"  # "youtube" or "direct"

class VideoInfo(BaseModel):
    url: str


def _resolve_save_path(save_path: str, db: Session) -> str:
    """Resolve UI save paths inside the configured NAS root.

    The browser works with NAS-relative paths like /Music. The downloader needs
    the mounted filesystem path, for example /tmp/nas_mnt/NAS/Music.
    """
    nas_root_config = db.query(Config).filter(Config.key == "nas_root").first()
    nas_root = str(nas_root_config.value) if nas_root_config else "/nas"
    nas_root = os.path.abspath(nas_root)

    requested = (save_path or "/").strip()
    if not requested or requested == "/":
        resolved = nas_root
    elif os.path.isabs(requested) and (
        requested == nas_root or requested.startswith(nas_root + os.sep)
    ):
        resolved = os.path.abspath(requested)
    else:
        resolved = os.path.abspath(os.path.join(nas_root, requested.lstrip("/")))

    if os.path.commonpath([nas_root, resolved]) != nas_root:
        raise HTTPException(status_code=400, detail="Save path must be inside the configured NAS root")

    return resolved

def is_valid_youtube_url(url: str) -> bool:
    """Validate YouTube URL format"""
    youtube_patterns = [
        r'^(https?://)?(www\.)?youtube\.com/watch\?v=[\w-]+',
        r'^(https?://)?(www\.)?youtube\.com/embed/[\w-]+',
        r'^(https?://)?(www\.)?youtube\.com/v/[\w-]+',
        r'^(https?://)?youtu\.be/[\w-]+',
        r'^(https?://)?(www\.)?youtube\.com/shorts/[\w-]+'
    ]
    return any(re.match(pattern, url) for pattern in youtube_patterns)

def extract_video_id(url: str) -> Optional[str]:
    """Extract video ID from YouTube URL"""
    # Handle youtu.be URLs
    if 'youtu.be' in url:
        path = urlparse(url).path
        return path[1:]  # Remove leading /
    
    # Handle youtube.com URLs
    if 'youtube.com' in url:
        parsed_url = urlparse(url)
        if parsed_url.path == '/watch':
            query_params = parse_qs(parsed_url.query)
            return query_params.get('v', [None])[0]
        elif '/embed/' in parsed_url.path:
            return parsed_url.path.split('/embed/')[1]
        elif '/v/' in parsed_url.path:
            return parsed_url.path.split('/v/')[1]
        elif '/shorts/' in parsed_url.path:
            return parsed_url.path.split('/shorts/')[1]
    
    return None


def _is_youtube_url(url: str) -> bool:
    """Check if a URL is a YouTube URL."""
    return is_valid_youtube_url(url)


def _is_direct_url(url: str) -> bool:
    """Check if a URL is a direct HTTP(S) file link (not YouTube)."""
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and not _is_youtube_url(url)


def _extract_filename_from_url(url: str) -> str:
    """Extract a filename from a URL path, falling back to 'download'."""
    parsed = urlparse(url)
    path = unquote(parsed.path)
    name = os.path.basename(path)
    if name and "." in name:
        return name
    return "download"


def _fetch_url_headers(url: str) -> dict:
    """Fetch HTTP headers for a URL (HEAD request). Returns dict with size, filename, content_type."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, timeout=15) as resp:
            headers = resp.headers
            content_length = int(headers.get("Content-Length", 0))
            content_type = headers.get("Content-Type", "")
            content_disp = headers.get("Content-Disposition", "")

            # Try to get filename from Content-Disposition
            filename = ""
            if content_disp:
                match = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\s]+)', content_disp, re.IGNORECASE)
                if match:
                    filename = unquote(match.group(1))

            if not filename:
                filename = _extract_filename_from_url(url)

            return {
                "filename": filename,
                "size": content_length,
                "content_type": content_type,
            }
    except Exception as e:
        # Fallback: just extract from URL
        return {
            "filename": _extract_filename_from_url(url),
            "size": 0,
            "content_type": "",
        }


async def fetch_video_info_async(url: str) -> dict:
    """Fetch video info using yt-dlp in a thread pool"""
    def _fetch_video_info():
        # Extract video ID and build a clean URL
        # This prevents yt-dlp from trying to fetch playlist info when URL has &list= params
        video_id = extract_video_id(url)
        if video_id:
            clean_url = f"https://www.youtube.com/watch?v={video_id}"
        else:
            clean_url = url

        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'no_playlist': True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(clean_url, download=False)
                if info:
                    return {
                        'title': info.get('title', 'Unknown Title'),
                        'thumbnail': info.get('thumbnail', ''),
                        'duration': info.get('duration', 0),
                        'uploader': info.get('uploader', 'Unknown'),
                        'view_count': info.get('view_count', 0)
                    }
        except yt_dlp.utils.DownloadError as e:
            raise Exception(f"Video not available: {str(e)}")
        except Exception as e:
            raise Exception(f"Error fetching video info: {str(e)}")

        return None

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_video_info)

async def fetch_playlist_info_async(url: str) -> dict:
    """Fetch playlist info using yt-dlp in a thread pool"""
    def _fetch_playlist_info():
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info and 'entries' in info:
                    entries = []
                    for entry in info['entries']:
                        if entry:
                            entries.append({
                                'id': entry.get('id', ''),
                                'title': entry.get('title', 'Unknown'),
                                'url': entry.get('webpage_url', f"https://www.youtube.com/watch?v={entry.get('id', '')}"),
                                'duration': entry.get('duration', 0),
                                'thumbnail': entry.get('thumbnail', ''),
                            })
                    return {
                        'title': info.get('title', 'Unknown Playlist'),
                        'uploader': info.get('uploader', 'Unknown'),
                        'entry_count': len(entries),
                        'entries': entries
                    }
        except Exception as e:
            raise Exception(f"Error fetching playlist info: {str(e)}")

        return None

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_playlist_info)

def is_playlist_url(url: str) -> bool:
    """Check if URL is a playlist URL"""
    return 'list=' in url or '/playlist' in url

@router.post("/info")
async def get_video_info(video_info: VideoInfo):
    """Fetch video metadata for preview"""
    # Validate URL format
    if not is_valid_youtube_url(video_info.url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL format")
    
    # Extract video ID to ensure we have a valid ID
    video_id = extract_video_id(video_info.url)
    if not video_id:
        raise HTTPException(status_code=400, detail="Could not extract video ID from URL")
    
    try:
        info = await fetch_video_info_async(video_info.url)
        if not info:
            raise HTTPException(status_code=404, detail="Video not found or unavailable")
        
        return {
            "url": video_info.url,
            "title": info['title'],
            "thumbnail": info['thumbnail'],
            "duration": info['duration'],
            "uploader": info['uploader'],
            "view_count": info['view_count']
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch video info: {str(e)}")

@router.post("/playlist-info")
async def get_playlist_info(video_info: VideoInfo):
    """Fetch playlist metadata for preview"""
    # Validate URL format
    if not is_valid_youtube_url(video_info.url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL format")

    try:
        info = await fetch_playlist_info_async(video_info.url)
        if not info:
            raise HTTPException(status_code=404, detail="Playlist not found or unavailable")

        return {
            "url": video_info.url,
            "title": info['title'],
            "uploader": info['uploader'],
            "entry_count": info['entry_count'],
            "entries": info['entries']
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch playlist info: {str(e)}")

@router.post("/download-playlist")
async def download_playlist(download_request: DownloadRequest, db: Session = Depends(get_db)):
    """Download all videos in a playlist"""
    try:
        info = await fetch_playlist_info_async(download_request.url)
        if not info or 'entries' not in info:
            raise HTTPException(status_code=404, detail="Playlist not found")

        download_ids = []
        for entry in info['entries']:
            if not entry or not entry.get('id'):
                continue

            # Create download history record
            download = DownloadHistory(
                url=entry.get('url', f"https://www.youtube.com/watch?v={entry['id']}"),
                title=entry.get('title', 'Unknown'),
                format=download_request.format,
                split_mode=download_request.split_mode,
                status="pending"
            )
            db.add(download)
            db.commit()
            db.refresh(download)

            # Create download task
            task = DownloadTask(
                id=int(download.id),
                url=entry.get('url', f"https://www.youtube.com/watch?v={entry['id']}"),
                title=entry.get('title', 'Unknown'),
                format=download_request.format,
                split_mode=download_request.split_mode,
                keep_original=download_request.keep_original,
                save_path=download_request.save_path
            )

            # Add to download queue
            await download_manager.add_download(task)
            download_ids.append(download.id)

        return {
            "status": "pending",
            "message": f"Added {len(download_ids)} videos to download queue",
            "download_ids": download_ids
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to download playlist: {str(e)}")

@router.websocket("/ws/progress")
async def websocket_progress(websocket: WebSocket):
    """WebSocket endpoint for real-time download progress updates"""
    await websocket.accept()
    download_manager.add_websocket(websocket)
    compressor.add_websocket(websocket)
    
    try:
        # Send current queue status on connection
        await websocket.send_json({
            "type": "queue_status",
            "queue_status": download_manager.get_queue_status()
        })

        # Send all active task details so the UI can rebuild after page refresh
        for task in download_manager.get_all_tasks():
            if task.status not in (DownloadStatus.COMPLETED, DownloadStatus.FAILED, DownloadStatus.CANCELLED):
                await websocket.send_json({
                    "type": "download_progress",
                    "task_id": task.id,
                    "status": task.status.value,
                    "progress": task.progress,
                    "speed": task.speed,
                    "eta": task.eta,
                    "current_file": task.current_file,
                    "title": task.title,
                    "error": task.error,
                    "queue_status": download_manager.get_queue_status()
                })
        
        # Keep connection alive and listen for client messages
        while True:
            data = await websocket.receive_text()
            # Handle client messages if needed (e.g., cancel download)
            try:
                import json
                message = json.loads(data)
                if message.get("action") == "cancel":
                    task_id = message.get("task_id")
                    if task_id:
                        await download_manager.cancel_download(task_id)
            except:
                pass
                
    except WebSocketDisconnect:
        download_manager.remove_websocket(websocket)
        compressor.remove_websocket(websocket)
    except Exception as e:
        download_manager.remove_websocket(websocket)
        compressor.remove_websocket(websocket)


@router.post("/fetch-url")
async def fetch_url_info(req: VideoInfo):
    """Fetch file info for a direct URL (HEAD request)."""
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
    if _is_youtube_url(url):
        raise HTTPException(status_code=400, detail="Use /info endpoint for YouTube URLs")
    if not _is_direct_url(url):
        raise HTTPException(status_code=400, detail="Invalid URL")

    info = _fetch_url_headers(url)
    return {
        "url": url,
        "filename": info["filename"],
        "size": info["size"],
        "content_type": info["content_type"],
    }


@router.post("/download")
async def start_download(download_request: DownloadRequest, db: Session = Depends(get_db)):
    """Start a new download task (YouTube or direct file)"""
    save_path = _resolve_save_path(download_request.save_path, db)

    # Auto-detect download type
    download_type = download_request.download_type
    if download_type == "youtube" and _is_direct_url(download_request.url):
        download_type = "direct"
    elif download_type == "direct" and _is_youtube_url(download_request.url):
        download_type = "youtube"

    # For direct downloads, extract filename from URL
    title = download_request.title
    current_file = ""
    if download_type == "direct":
        if not title:
            title = _extract_filename_from_url(download_request.url)
        current_file = title  # wget will use this as the output filename

    # Create download history record
    download = DownloadHistory(
        url=download_request.url,
        title=title,
        format=download_request.format,
        split_mode=download_request.split_mode,
        file_path=save_path,
        status="pending"
    )
    db.add(download)
    db.commit()
    db.refresh(download)

    # Create download task
    task = DownloadTask(
        id=int(download.id),
        url=download_request.url,
        title=title,
        format=download_request.format,
        split_mode=download_request.split_mode,
        keep_original=download_request.keep_original,
        save_path=save_path,
        download_type=download_type,
        current_file=current_file,
    )

    # Add to download queue
    await download_manager.add_download(task)

    return {
        "download_id": download.id,
        "status": "pending",
        "message": "Download added to queue"
    }


@router.get("/queue")
async def get_download_queue():
    """Get current download queue status and tasks"""
    tasks = download_manager.get_all_tasks()
    queue_status = download_manager.get_queue_status()
    
    return {
        "queue_status": queue_status,
        "tasks": [
            {
                "id": task.id,
                "url": task.url,
                "title": task.title,
                "format": task.format,
                "status": task.status.value,
                "progress": task.progress,
                "speed": task.speed,
                "eta": task.eta,
                "current_file": task.current_file,
                "error": task.error
            }
            for task in tasks
        ]
    }


@router.post("/cancel/{task_id}")
async def cancel_download(task_id: int):
    """Cancel a download task"""
    success = await download_manager.cancel_download(task_id)
    if success:
        return {"message": "Download cancelled"}
    else:
        raise HTTPException(status_code=404, detail="Task not found or cannot be cancelled")

@router.get("/history")
async def get_download_history(db: Session = Depends(get_db)):
    downloads = db.query(DownloadHistory).order_by(DownloadHistory.created_at.desc()).limit(10).all()
    return [
        {
            "id": d.id,
            "url": d.url,
            "title": d.title,
            "format": d.format,
            "status": d.status,
            "created_at": d.created_at
        }
        for d in downloads
    ]
