"""
Sync Service - MD5-based file index generation for device synchronization.

Generates file_index.json for configured sync folders.
Uses MD5 as primary key to detect renames/moves across devices.
"""

import hashlib
import json
import os
import time

# Cache: {full_path: {"md5": str, "ino": int, "mtime": float, "size": int}}
_md5_cache: dict[str, dict] = {}

INDEX_FILENAME = "file_index.json"


def compute_md5(filepath: str, chunk_size: int = 8192) -> str:
    """Compute MD5 hash of a file."""
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_folder(nas_root: str, folder_path: str) -> list[dict]:
    """
    Recursively scan a folder and return file entries with MD5 hashes.
    Uses mtime+size cache to skip unchanged files.
    """
    full_dir = os.path.join(nas_root, folder_path.lstrip("/"))
    # Confine: the folder must resolve inside the NAS root (sync folders are
    # user-supplied; "../" or symlink escapes must not index arbitrary files).
    real_nas = os.path.realpath(nas_root)
    real_dir = os.path.realpath(full_dir)
    if real_dir != real_nas and not real_dir.startswith(real_nas + os.sep):
        return []
    if not os.path.isdir(full_dir):
        return []

    files = []
    for root, _dirs, filenames in os.walk(full_dir):
        for name in filenames:
            # Skip hidden files
            if name.startswith("."):
                continue

            full_path = os.path.join(root, name)
            try:
                stat = os.stat(full_path)
                size = stat.st_size
                mtime = stat.st_mtime
                ino = stat.st_ino

                # Check cache: if inode, mtime and size are unchanged, reuse MD5.
                # Keying on inode too catches files replaced in-place that reuse
                # an old size+mtime (e.g. rsync --times copies).
                cached = _md5_cache.get(full_path)
                if (
                    cached
                    and cached["ino"] == ino
                    and cached["mtime"] == mtime
                    and cached["size"] == size
                ):
                    md5 = cached["md5"]
                else:
                    md5 = compute_md5(full_path)
                    _md5_cache[full_path] = {
                        "md5": md5,
                        "ino": ino,
                        "mtime": mtime,
                        "size": size,
                    }

                # Relative path from NAS root
                rel_path = "/" + os.path.relpath(full_path, nas_root)

                files.append({
                    "md5": md5,
                    "path": rel_path,
                    "size": size,
                    "tag": None,  # Filled later from FileMetadata
                })
            except (PermissionError, OSError):
                continue

    return files


def generate_index(nas_root: str, sync_folders: list, db_session=None) -> dict:
    """
    Generate file_index.json from all enabled sync folders.

    Args:
        nas_root: NAS root directory path
        sync_folders: List of SyncFolder objects (with .path and .enabled)
        db_session: SQLAlchemy session for looking up tags

    Returns:
        The generated index dict
    """
    from .models import FileMetadata

    all_files = []
    for folder in sync_folders:
        if not folder.enabled:
            continue
        files = scan_folder(nas_root, folder.path)

        # Enrich with tags from database — one batched query per folder
        # instead of N individual lookups.
        if db_session and files:
            paths = [f["path"] for f in files]
            metas = db_session.query(FileMetadata).filter(
                FileMetadata.file_path.in_(paths)
            ).all()
            tag_by_path = {m.file_path: m.tag for m in metas}
            for f in files:
                tag = tag_by_path.get(f["path"])
                if tag:
                    f["tag"] = tag

        all_files.extend(files)

    index = {
        "generated_at": time.time(),
        "file_count": len(all_files),
        "files": all_files,
    }

    return index


def save_index(index: dict, index_dir: str) -> str:
    """Save index to file_index.json in the given directory. Returns file path."""
    os.makedirs(index_dir, exist_ok=True)
    filepath = os.path.join(index_dir, INDEX_FILENAME)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)
    return filepath


def load_index(index_dir: str) -> dict | None:
    """Load existing index from disk. Returns None if not found."""
    filepath = os.path.join(index_dir, INDEX_FILENAME)
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def get_index(nas_root: str, sync_folders: list, db_session=None, force: bool = False) -> dict:
    """
    Get the file index, regenerating if needed.

    If force=True, always regenerates.
    Otherwise, loads from disk if available.
    """
    index_dir = nas_root  # Store index at NAS root

    if not force:
        existing = load_index(index_dir)
        if existing:
            return existing

    # Generate fresh index
    index = generate_index(nas_root, sync_folders, db_session)
    save_index(index, index_dir)
    return index


def get_sync_status(nas_root: str, sync_folders: list, db_session=None) -> dict:
    """Get current sync status summary."""
    index = load_index(nas_root)
    if index:
        return {
            "indexed": True,
            "file_count": index.get("file_count", 0),
            "generated_at": index.get("generated_at"),
            "folder_count": len([f for f in sync_folders if f.enabled]),
        }
    return {
        "indexed": False,
        "file_count": 0,
        "generated_at": None,
        "folder_count": len([f for f in sync_folders if f.enabled]),
    }
