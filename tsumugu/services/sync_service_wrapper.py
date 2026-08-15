"""Sync service — MD5-based file index generation for device sync.

Thin wrapper over the existing pure-logic :mod:`sync_service` module,
plus the sync-folder CRUD that used to live in the sync router.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..db import SyncFolder
from .sync_service import (
    get_index as _get_index,
    get_sync_status as _get_sync_status,
    load_index,
)


def _get_nas_root(db: Session) -> str:
    from .config_service import get_nas_root
    return get_nas_root(db)


def get_file_index(db: Session, force: bool = False) -> dict[str, Any]:
    nas_root = _get_nas_root(db)
    folders = db.query(SyncFolder).all()
    return _get_index(nas_root, folders, db, force=force)


def download_file_index(db: Session) -> tuple[bytes, str]:
    """Return (json_bytes, filename) for downloading the index."""
    import json
    index = get_file_index(db, force=False)
    data = json.dumps(index, indent=2, ensure_ascii=False).encode("utf-8")
    return data, "file_index.json"


def list_sync_folders(db: Session) -> list[dict[str, Any]]:
    return [
        {
            "id": f.id, "path": f.path, "name": f.name, "enabled": f.enabled,
        }
        for f in db.query(SyncFolder).all()
    ]


def add_sync_folder(db: Session, path: str, name: str | None = None) -> dict[str, Any]:
    existing = db.query(SyncFolder).filter(SyncFolder.path == path).first()
    if existing:
        return {"success": False, "message": f"Folder '{path}' already in index"}
    nas_root = _get_nas_root(db)
    full = nas_root + path if path.startswith("/") else os.path.join(nas_root, path)
    if not os.path.isdir(full):
        return {"success": False, "message": f"Directory does not exist: {path}"}
    folder = SyncFolder(path=path, name=name or os.path.basename(path.rstrip("/")) or path)
    db.add(folder)
    db.commit()
    return {"success": True, "message": f"Added '{path}' to index", "id": folder.id}


def remove_sync_folder(db: Session, folder_id: int) -> dict[str, Any]:
    folder = db.query(SyncFolder).filter(SyncFolder.id == folder_id).first()
    if not folder:
        return {"success": False, "message": "Folder not found"}
    path = folder.path
    db.delete(folder)
    db.commit()
    return {"success": True, "message": f"Removed '{path}' from index"}


def toggle_sync_folder(db: Session, folder_id: int) -> dict[str, Any]:
    folder = db.query(SyncFolder).filter(SyncFolder.id == folder_id).first()
    if not folder:
        return {"success": False, "message": "Folder not found"}
    folder.enabled = not folder.enabled
    db.commit()
    return {"success": True, "enabled": folder.enabled}


def rescan(db: Session) -> dict[str, Any]:
    index = get_file_index(db, force=True)
    return {
        "success": True,
        "message": f"Re-scanned {index['file_count']} files",
        "file_count": index["file_count"],
    }


def sync_status(db: Session) -> dict[str, Any]:
    nas_root = _get_nas_root(db)
    folders = db.query(SyncFolder).all()
    return _get_sync_status(nas_root, folders, db)


import os  # noqa: E402  (kept at bottom for circular-import safety)