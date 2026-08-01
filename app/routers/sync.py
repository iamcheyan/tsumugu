from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional
import os
import time

from ..database import get_db
from ..models import SyncFolder
from .. import sync_service
from ..paths import get_nas_root, resolve_within_nas

router = APIRouter(prefix="/api/sync", tags=["sync"])


class AddFolderRequest(BaseModel):
    path: str
    name: Optional[str] = None


# ── File Index (for device download) ────────────────────────────────

@router.get("/file-index.json")
async def get_file_index(force: bool = False, db: Session = Depends(get_db)):
    """
    Download the file index JSON.
    Other devices call this endpoint to sync.
    """
    nas_root = get_nas_root(db)
    folders = db.query(SyncFolder).filter(SyncFolder.enabled == True).all()

    index = sync_service.get_index(nas_root, folders, db, force=force)
    return index


@router.get("/file-index/download")
async def download_file_index(db: Session = Depends(get_db)):
    """Download file_index.json as a file."""
    nas_root = get_nas_root(db)
    folders = db.query(SyncFolder).filter(SyncFolder.enabled == True).all()

    index = sync_service.get_index(nas_root, folders, db)
    index_dir = nas_root
    filepath = sync_service.save_index(index, index_dir)

    return FileResponse(
        path=filepath,
        media_type="application/json",
        filename="file_index.json",
    )


# ── Sync Folders Management ─────────────────────────────────────────

@router.get("/folders")
async def list_sync_folders(db: Session = Depends(get_db)):
    """List all configured sync folders."""
    folders = db.query(SyncFolder).all()
    return {
        "folders": [
            {
                "id": f.id,
                "path": f.path,
                "name": f.name or os.path.basename(f.path),
                "enabled": f.enabled,
                "created_at": f.created_at.isoformat() if f.created_at else None,
            }
            for f in folders
        ]
    }


@router.post("/folders")
async def add_sync_folder(req: AddFolderRequest, db: Session = Depends(get_db)):
    """Add a folder to the sync list."""
    nas_root = get_nas_root(db)
    path = req.path.strip().rstrip("/")

    # Confine: reject paths that resolve outside the NAS root (403).
    full_path = resolve_within_nas(db, path)
    if not os.path.exists(full_path):
        raise HTTPException(status_code=400, detail=f"Folder not found: {path}")
    if not os.path.isdir(full_path):
        raise HTTPException(status_code=400, detail=f"Not a directory: {path}")

    # Check for duplicates
    existing = db.query(SyncFolder).filter(SyncFolder.path == path).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Folder already in sync list: {path}")

    folder = SyncFolder(
        path=path,
        name=req.name or os.path.basename(path),
        enabled=True,
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)

    return {
        "id": folder.id,
        "path": folder.path,
        "name": folder.name,
        "enabled": folder.enabled,
        "message": f"Added '{path}' to index",
    }


@router.delete("/folders/{folder_id}")
async def remove_sync_folder(folder_id: int, db: Session = Depends(get_db)):
    """Remove a folder from the sync list."""
    folder = db.query(SyncFolder).filter(SyncFolder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    path = folder.path
    db.delete(folder)
    db.commit()

    return {"message": f"Removed '{path}' from index"}


@router.put("/folders/{folder_id}/toggle")
async def toggle_sync_folder(folder_id: int, db: Session = Depends(get_db)):
    """Enable or disable a sync folder."""
    folder = db.query(SyncFolder).filter(SyncFolder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    folder.enabled = not folder.enabled
    db.commit()

    return {
        "id": folder.id,
        "path": folder.path,
        "enabled": folder.enabled,
        "message": f"{'Enabled' if folder.enabled else 'Disabled'} '{folder.path}'",
    }


# ── Rescan & Status ─────────────────────────────────────────────────

@router.post("/rescan")
async def rescan(db: Session = Depends(get_db)):
    """Force re-scan all sync folders and regenerate the index."""
    nas_root = get_nas_root(db)
    folders = db.query(SyncFolder).all()

    start = time.time()
    index = sync_service.get_index(nas_root, folders, db, force=True)
    elapsed = time.time() - start

    return {
        "success": True,
        "file_count": index["file_count"],
        "elapsed_seconds": round(elapsed, 2),
        "message": f"Indexed {index['file_count']} files in {elapsed:.1f}s",
    }


@router.get("/status")
async def sync_status(db: Session = Depends(get_db)):
    """Get current sync status."""
    nas_root = get_nas_root(db)
    folders = db.query(SyncFolder).all()
    status = sync_service.get_sync_status(nas_root, folders, db)
    return status
