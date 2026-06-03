from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import DownloadHistory, Config
from ..audio_splitter import audio_splitter
from pydantic import BaseModel
from typing import Optional, List
import os
import mimetypes

router = APIRouter(prefix="/api/audio", tags=["audio"])

class SplitRequest(BaseModel):
    download_id: int
    split_mode: str  # chapter_info, silence_detection
    silence_threshold: float = -30.0  # dB
    min_silence_length: float = 0.5  # seconds

class AudioFile(BaseModel):
    file_path: str
    title: Optional[str] = None
    artist: Optional[str] = None
    duration: Optional[int] = None

@router.post("/split")
async def split_audio(split_request: SplitRequest, db: Session = Depends(get_db)):
    download = db.query(DownloadHistory).filter(DownloadHistory.id == split_request.download_id).first()
    if not download:
        raise HTTPException(status_code=404, detail="Download not found")
    
    # Check if file exists
    file_path = str(download.file_path) if download.file_path else None
    if not file_path or not os.path.exists(file_path):
        # Try to find the file based on title and format
        download_title = str(download.title) if download.title else None
        download_format = str(download.format) if download.format else None
        if download_title and download_format:
            # Search in the download directory
            search_dir = os.path.dirname(file_path) if file_path else "/"
            for file in os.listdir(search_dir):
                if file.startswith(download_title) and file.endswith(f".{download_format}"):
                    file_path = os.path.join(search_dir, file)
                    break
    
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Audio file not found")
    
    # Update status
    setattr(download, 'status', "splitting")
    db.commit()
    
    # Perform the split
    try:
        # Get chapters if available
        chapters = None
        if split_request.split_mode == "chapter_info":
            # For chapter info, we need to get chapters from yt-dlp
            # This is a placeholder - in real implementation, we'd fetch chapters
            chapters = None
        
        result = await audio_splitter.split_audio(
            audio_file_path=file_path,
            split_mode=split_request.split_mode,
            keep_original=True,  # Default to keeping original
            output_dir=os.path.dirname(file_path),
            chapters=chapters
        )
        
        if result.success:
            setattr(download, 'status', "split_completed")
            db.commit()
            
            return {
                "download_id": download.id,
                "status": "split_completed",
                "split_mode": split_request.split_mode,
                "files": result.files,
                "message": f"Successfully split into {len(result.files)} tracks"
            }
        else:
            setattr(download, 'status', "split_failed")
            db.commit()
            
            return {
                "download_id": download.id,
                "status": "split_failed",
                "error": result.error,
                "message": "Failed to split audio file"
            }
            
    except Exception as e:
        setattr(download, 'status', "split_failed")
        db.commit()
        raise HTTPException(status_code=500, detail=f"Split failed: {str(e)}")

@router.get("/files/{download_id}")
async def get_split_files(download_id: int, db: Session = Depends(get_db)):
    download = db.query(DownloadHistory).filter(DownloadHistory.id == download_id).first()
    if not download:
        raise HTTPException(status_code=404, detail="Download not found")
    
    # Check if split files exist
    if not download.file_path:
        return {
            "download_id": download_id,
            "files": []
        }
    
    # Find split files in the same directory
    base_dir = os.path.dirname(download.file_path)
    base_name = os.path.splitext(os.path.basename(download.file_path))[0]
    
    split_files = []
    if os.path.exists(base_dir):
        for file in os.listdir(base_dir):
            # Look for files that match the pattern: base_name - Track XX.mp3
            if file.startswith(base_name) and " - " in file and file.endswith(".mp3"):
                file_path = os.path.join(base_dir, file)
                # Extract title from filename
                title = file.replace(base_name + " - ", "").replace(".mp3", "")
                split_files.append({
                    "file_path": file_path,
                    "title": title,
                    "duration": 0,  # We could get this from ffprobe if needed
                    "file_name": file
                })
    
    return {
        "download_id": download_id,
        "files": sorted(split_files, key=lambda x: x["file_name"])
    }

@router.get("/stream")
async def stream_audio(request: Request, path: str = Query(..., description="Path to audio file"), db: Session = Depends(get_db)):
    """Stream audio file for playback with Range request support"""
    # Resolve path: try as-is first, then prepend NAS root
    resolved_path = path
    if not os.path.exists(path):
        nas_root_config = db.query(Config).filter(Config.key == "nas_root").first()
        nas_root = str(nas_root_config.value) if nas_root_config else "/nas"
        resolved_path = os.path.join(nas_root, path.lstrip('/'))
        if not os.path.exists(resolved_path):
            raise HTTPException(status_code=404, detail=f"Audio file not found: {path}")
    path = resolved_path

    # Get file size
    file_size = os.path.getsize(path)

    # Get MIME type
    mime_type, _ = mimetypes.guess_type(path)
    if not mime_type:
        mime_type = "audio/mpeg"

    # Handle Range requests for seeking
    range_header = request.headers.get("range")

    if range_header:
        # Parse Range: bytes=start-end
        range_str = range_header.replace("bytes=", "")
        parts = range_str.split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if parts[1] else file_size - 1
        end = min(end, file_size - 1)
        content_length = end - start + 1

        def iter_range():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk_size = min(8192, remaining)
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            iter_range(),
            status_code=206,
            media_type=mime_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(content_length),
            }
        )
    else:
        # Full file response
        def iter_file():
            with open(path, "rb") as f:
                while chunk := f.read(8192):
                    yield chunk

        return StreamingResponse(
            iter_file(),
            media_type=mime_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
            }
        )

@router.get("/player")
async def get_audio_player(request: Request, path: str = Query(..., description="Path to audio file")):
    """Get audio player component for the given file"""
    # Check if file exists
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Audio file not found")
    
    # Get filename from path
    filename = os.path.basename(path)
    
    # Create templates instance
    templates = Jinja2Templates(directory="app/templates")
    
    # Return the audio player template
    return templates.TemplateResponse(
        name="components/audio_player.html",
        request=request,
        context={
            "request": request,
            "filepath": path,
            "filename": filename
        }
    )

@router.get("/player/{file_path:path}")
async def get_audio_info(file_path: str):
    # Placeholder implementation
    # In US-011 this will be fully implemented
    return {
        "file_path": file_path,
        "duration": 300,
        "format": "mp3",
        "bitrate": 320
    }