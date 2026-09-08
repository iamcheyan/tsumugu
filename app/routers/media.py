"""Stable machine-facing media job API for Hermes and other clients."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..download_manager import DownloadStatus, DownloadTask, download_manager
from ..models import DownloadHistory
from ..paths import resolve_within_nas
from ..settings import get_settings
from .youtube import (
    _is_direct_url,
    _validate_direct_url,
    fetch_video_info_async,
)

router = APIRouter(prefix="/api/media", tags=["media"])


class MediaJobRequest(BaseModel):
    url: str
    format: str = Field(default="mp3", pattern="^(mp3|m4a|flac)$")
    save_path: str = Field(default_factory=lambda: get_settings().media.default_path)
    split_policy: str = Field(default="auto", pattern="^(auto|none|chapter_info|silence_detection)$")
    keep_original: bool = False
    title: str = ""


def _history_payload(row: DownloadHistory) -> dict:
    return {
        "job_id": row.id,
        "url": row.url,
        "title": row.title,
        "format": row.format,
        "status": row.status,
        "file_path": row.file_path,
        "split_policy": row.split_mode,
        "created_at": row.created_at,
        "completed_at": row.completed_at,
    }


@router.post("/jobs")
async def submit_media_job(request: MediaJobRequest, db: Session = Depends(get_db)):
    """Submit a media job without relying on the browser UI."""
    url = request.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    download_type = "direct" if _is_direct_url(url) else "youtube"
    if download_type == "direct":
        _validate_direct_url(url)

    save_path = resolve_within_nas(db, request.save_path)
    title = request.title.strip()
    split_mode: Optional[str] = None
    auto_split_fallback = False
    if download_type == "youtube":
        if request.split_policy == "none":
            split_mode = None
        elif request.split_policy == "auto":
            info = await fetch_video_info_async(url)
            title = title or str(info.get("title", "Unknown Title"))
            duration = float(info.get("duration") or 0)
            collection_words = ("mix", "合集", "playlist", "continuous", "full album", "歌单")
            lowered = title.casefold()
            if duration >= 20 * 60 or any(word in lowered for word in collection_words):
                split_mode = "chapter_info"
                auto_split_fallback = True
        else:
            split_mode = request.split_policy
    elif not title:
        title = "download"

    row = DownloadHistory(
        url=url,
        title=title,
        format=request.format,
        split_mode=split_mode,
        file_path=save_path,
        status=DownloadStatus.PENDING.value,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    row_id = int(getattr(row, "id"))
    task = DownloadTask(
        id=row_id,
        url=url,
        title=title,
        format=request.format,
        split_mode=split_mode,
        keep_original=request.keep_original,
        save_path=save_path,
        download_type=download_type,
        auto_split_fallback=auto_split_fallback,
    )
    await download_manager.add_download(task)
    return {
        "job_id": row.id,
        "status": DownloadStatus.PENDING.value,
        "split_mode": split_mode,
        "save_path": save_path,
    }


@router.get("/jobs/{job_id}")
async def get_media_job(job_id: int, db: Session = Depends(get_db)):
    task = download_manager.get_task_detail(job_id)
    if task is not None:
        return task
    row = db.query(DownloadHistory).filter(DownloadHistory.id == job_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _history_payload(row)


@router.post("/jobs/{job_id}/cancel")
async def cancel_media_job(job_id: int):
    if not await download_manager.cancel_download(job_id):
        raise HTTPException(status_code=404, detail="Job not found or cannot be cancelled")
    return {"job_id": job_id, "status": DownloadStatus.CANCELLED.value}
