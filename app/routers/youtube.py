from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import Config, DownloadHistory
from ..download_manager import download_manager, DownloadTask, DownloadStatus
from ..compressor import compressor
from ..paths import get_nas_root, resolve_within_nas
from pydantic import BaseModel
from typing import Optional
import yt_dlp
import asyncio
import urllib.request
from urllib.parse import urlparse, parse_qs, unquote
import re
import os
import ipaddress
import socket

router = APIRouter(prefix="/api/youtube", tags=["youtube"])

# Networks that must never be reachable through SSRF-prone endpoints
# (loopback, RFC1918, link-local, CGNAT, multicast, reserved, IPv6 ULA/ll).
_BLOCKED_NETWORKS: tuple = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)


def _is_public_ip(ip_str: str) -> bool:
    """True when the address is a routable, non-internal IP."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not any(ip in net for net in _BLOCKED_NETWORKS)


def _validate_direct_url(url: str) -> None:
    """SSRF guard: reject http(s) URLs that point into non-public space.

    Checks IP literals directly and resolves hostnames, verifying every
    returned address is public. Raises HTTPException(400) when unsafe.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Only http/https URLs are supported")
    host = parsed.hostname or ""
    if not host:
        raise HTTPException(status_code=400, detail="Invalid URL host")

    try:
        ip = ipaddress.ip_address(host)
        if not _is_public_ip(str(ip)):
            raise HTTPException(status_code=400, detail="URL points to a non-public address")
        return
    except ValueError:
        pass  # hostname, resolve below

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="Could not resolve host")
    addresses = {info[4][0] for info in infos}
    if not addresses or not all(_is_public_ip(a) for a in addresses):
        raise HTTPException(status_code=400, detail="URL resolves to a non-public address")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect hop so SSRF cannot be reached via redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_direct_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

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
    Confinement follows symlinks (realpath) so a symlink inside the NAS root
    cannot redirect the download outside it.
    """
    requested = (save_path or "/").strip()
    nas_root = get_nas_root(db)
    abs_root = os.path.abspath(nas_root)
    # Treat the input as pre-resolved only when it is an absolute path that
    # already points inside the NAS root; everything else is NAS-relative
    # (frontend paths like "/Music" are POSIX-absolute but app-relative).
    if os.path.isabs(requested) and (
        requested == abs_root or requested.startswith(abs_root + os.sep)
    ):
        # Pre-resolved absolute path: re-verify after following symlinks so a
        # symlink inside the root cannot redirect the download outside it.
        real_root = os.path.realpath(abs_root)
        real_req = os.path.realpath(requested)
        if real_req != real_root and not real_req.startswith(real_root + os.sep):
            raise HTTPException(status_code=403, detail="Save path must be inside the configured NAS root")
        return real_req
    # NAS-relative (frontend paths like "/Music" are POSIX-absolute but
    # app-relative); confined by resolve_within_nas.
    return resolve_within_nas(db, requested)

def is_valid_youtube_url(url: str) -> bool:
    """Validate YouTube URL format"""
    youtube_patterns = [
        r'^(https?://)?(www\.)?youtube\.com/watch\?v=[\w-]+',
        r'^(https?://)?(www\.)?youtube\.com/embed/[\w-]+',
        r'^(https?://)?(www\.)?youtube\.com/v/[\w-]+',
        r'^(https?://)?youtu\.be/[\w-]+',
        r'^(https?://)?(www\.)?youtube\.com/shorts/[\w-]+',
        r'^(https?://)?(www\.)?youtube\.com/@[\w.-]+',
        r'^(https?://)?(www\.)?youtube\.com/channel/[\w-]+',
        r'^(https?://)?(www\.)?youtube\.com/c/[\w.-]+',
        r'^(https?://)?(www\.)?youtube\.com/user/[\w.-]+',
    ]
    return any(re.match(pattern, url) for pattern in youtube_patterns)


def is_channel_url(url: str) -> bool:
    """Check if URL is a YouTube channel URL"""
    channel_patterns = [
        r'^(https?://)?(www\.)?youtube\.com/@[\w.-]+',
        r'^(https?://)?(www\.)?youtube\.com/channel/[\w-]+',
        r'^(https?://)?(www\.)?youtube\.com/c/[\w.-]+',
        r'^(https?://)?(www\.)?youtube\.com/user/[\w.-]+',
    ]
    return any(re.match(pattern, url) for pattern in channel_patterns)

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
        opener = urllib.request.build_opener(_SafeRedirectHandler())
        with opener.open(req, timeout=15) as resp:
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

    loop = asyncio.get_running_loop()
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

    loop = asyncio.get_running_loop()
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

async def fetch_channel_info_async(url: str) -> dict:
    """Fetch channel info using yt-dlp subprocess (flat-playlist JSON)"""
    def _fetch_channel_info():
        import subprocess
        import json as _json

        # Use subprocess for faster flat-playlist enumeration
        cmd = [
            "yt-dlp",
            "--flat-playlist",
            "--print", "%(id)s\t%(title)s\t%(duration)s\t%(thumbnail)s",
            "--no-warnings",
            url,
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                raise Exception(f"yt-dlp error: {result.stderr.strip() or 'unknown error'}")

            entries = []
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("\t", 3)
                if len(parts) >= 3:
                    vid_id = parts[0]
                    title = parts[1]
                    duration = float(parts[2]) if parts[2] and parts[2] != "NA" else 0
                    thumbnail = parts[3] if len(parts) > 3 and parts[3] != "NA" else ""
                    entries.append({
                        "id": vid_id,
                        "title": title,
                        "url": f"https://www.youtube.com/watch?v={vid_id}",
                        "duration": duration,
                        "thumbnail": thumbnail,
                    })

            # Get channel metadata separately
            meta_cmd = [
                "yt-dlp",
                "--print", "%(channel)s|||%(uploader)s|||%(thumbnail)s",
                "--no-warnings",
                "--playlist-items", "1",
                url,
            ]
            meta_result = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=30)
            channel_name = "Unknown Channel"
            uploader = "Unknown"
            channel_thumb = ""
            if meta_result.returncode == 0 and meta_result.stdout.strip():
                meta_parts = meta_result.stdout.strip().split("|||")
                if len(meta_parts) >= 1 and meta_parts[0] and meta_parts[0] != "NA":
                    channel_name = meta_parts[0]
                if len(meta_parts) >= 2 and meta_parts[1] and meta_parts[1] != "NA":
                    uploader = meta_parts[1]
                if len(meta_parts) >= 3 and meta_parts[2] and meta_parts[2] != "NA":
                    channel_thumb = meta_parts[2]

            if entries:
                return {
                    "title": channel_name,
                    "uploader": uploader,
                    "thumbnail": channel_thumb or (entries[0].get("thumbnail", "") if entries else ""),
                    "entry_count": len(entries),
                    "entries": entries,
                }

        except subprocess.TimeoutExpired:
            raise Exception("Channel info fetch timed out")
        except Exception as e:
            raise Exception(f"Error fetching channel info: {str(e)}")

        return None

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch_channel_info)


@router.post("/channel-info")
async def get_channel_info(video_info: VideoInfo):
    """Fetch channel metadata and video list"""
    url = video_info.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    if not is_channel_url(url):
        raise HTTPException(status_code=400, detail="Not a valid YouTube channel URL")

    try:
        info = await fetch_channel_info_async(url)
        if not info:
            raise HTTPException(status_code=404, detail="Channel not found or unavailable")

        return {
            "url": url,
            "title": info['title'],
            "uploader": info['uploader'],
            "thumbnail": info['thumbnail'],
            "entry_count": info['entry_count'],
            "entries": info['entries']
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch channel info: {str(e)}")


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
            except Exception:
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

    # SSRF guard: reject private / loopback / link-local targets
    _validate_direct_url(url)

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

    # SSRF guard: direct downloads must target public addresses
    if download_type == "direct":
        _validate_direct_url(download_request.url)

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
