"""Audio service — splitting and file discovery.

Wraps :mod:`audio_splitter` plus the split-file lookup that used to live in
the audio router.  No playback (deferred per user decision).
"""
from __future__ import annotations

import os
from typing import Any

from sqlalchemy.orm import Session

from ..db import DownloadHistory
from .audio_splitter import audio_splitter, SplitResult


async def split_audio(
    db: Session,
    download_id: int,
    split_mode: str,
    silence_threshold: float = -30.0,
    min_silence_length: float = 0.5,
) -> dict[str, Any]:
    """Split a downloaded audio file. Updates DownloadHistory status."""
    download = db.query(DownloadHistory).filter(
        DownloadHistory.id == download_id
    ).first()
    if not download:
        return {"success": False, "error": "Download not found"}

    file_path = str(download.file_path) if download.file_path else None
    if not file_path or not os.path.exists(file_path):
        # Try to locate by title + format
        title = str(download.title) if download.title else None
        fmt = str(download.format) if download.format else None
        if title and fmt and file_path:
            search_dir = os.path.dirname(file_path)
            for f in os.listdir(search_dir):
                if f.startswith(title) and f.endswith(f".{fmt}"):
                    file_path = os.path.join(search_dir, f)
                    break
    if not file_path or not os.path.exists(file_path):
        return {"success": False, "error": "Audio file not found"}

    setattr(download, "status", "splitting")
    db.commit()

    try:
        result: SplitResult = await audio_splitter.split_audio(
            audio_file_path=file_path,
            split_mode=split_mode,
            keep_original=True,
            output_dir=os.path.dirname(file_path),
        )
        if result.success:
            setattr(download, "status", "split_completed")
            db.commit()
            return {
                "success": True,
                "status": "split_completed",
                "split_mode": split_mode,
                "files": result.files,
                "message": f"Successfully split into {len(result.files)} tracks",
            }
        else:
            setattr(download, "status", "split_failed")
            db.commit()
            return {"success": False, "status": "split_failed", "error": result.error}
    except Exception as exc:
        setattr(download, "status", "split_failed")
        db.commit()
        return {"success": False, "status": "split_failed", "error": str(exc)}


def get_split_files(download_id: int, db: Session) -> dict[str, Any]:
    """Find split files produced for *download_id*."""
    download = db.query(DownloadHistory).filter(
        DownloadHistory.id == download_id
    ).first()
    if not download or not download.file_path:
        return {"download_id": download_id, "files": []}
    base_dir = os.path.dirname(str(download.file_path))
    base_name = os.path.splitext(os.path.basename(str(download.file_path)))[0]
    split_files: list[dict[str, Any]] = []
    if os.path.exists(base_dir):
        for f in os.listdir(base_dir):
            if f.startswith(base_name) and " - " in f and f.endswith(".mp3"):
                fp = os.path.join(base_dir, f)
                title = f.replace(base_name + " - ", "").replace(".mp3", "")
                split_files.append({
                    "file_path": fp, "title": title,
                    "duration": 0, "file_name": f,
                })
    return {
        "download_id": download_id,
        "files": sorted(split_files, key=lambda x: x["file_name"]),
    }