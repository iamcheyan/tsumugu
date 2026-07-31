"""File browsing and operations service.

Extracted from ``app/routers/files.py`` — pure logic, no HTTP layer.
"""
from __future__ import annotations

import datetime
import errno
import hashlib
import os
import re
import shutil
import tempfile
import zipfile
from typing import Any

from sqlalchemy.orm import Session

from ..db import Config, FileMetadata
from ..utils.files import (
    AUDIO_EXTENSIONS,
    clean_filename,
    detect_extension,
    detect_file_type,
    format_modified,
    has_extension,
    has_subdirectories,
    is_within_nas,
    relative_path,
    resolve_full_path,
    sort_key,
    guess_tag_from_size,
)


def list_files(
    nas_root: str,
    path: str = "/",
    search: str = "",
    sort: str = "name",
    order: str = "asc",
    show_hidden: bool = False,
    db: Session | None = None,
) -> list[dict[str, Any]]:
    """Return a sorted, filtered list of entries in *path*."""
    full_path = resolve_full_path(nas_root, path)
    files: list[dict[str, Any]] = []
    if not (os.path.exists(full_path) and os.path.isdir(full_path)):
        return files
    try:
        for item in os.listdir(full_path):
            item_path = os.path.join(full_path, item)
            try:
                stat_info = os.stat(item_path)
            except OSError:
                continue
            file_type = detect_file_type(item, item_path)
            rel = relative_path(path, item)
            tag = None
            if db is not None:
                meta = db.query(FileMetadata).filter(
                    FileMetadata.file_path == rel
                ).first()
                if meta:
                    tag = meta.tag
            files.append({
                "name": item,
                "type": file_type,
                "size": stat_info.st_size if not os.path.isdir(item_path) else 0,
                "modified": format_modified(stat_info.st_mtime),
                "path": rel,
                "tag": tag,
            })
        if not show_hidden:
            files = [f for f in files if not f["name"].startswith(".")]
        if search:
            sl = search.lower()
            files = [f for f in files if sl in str(f["name"]).lower()]
        files.sort(key=lambda f: sort_key(sort, order, f), reverse=(order == "desc"))
    except PermissionError:
        pass
    return files


# ── File operations ────────────────────────────────────────────────────────


def create_folder(nas_root: str, path: str, folder_name: str) -> tuple[bool, str]:
    parent = resolve_full_path(nas_root, path)
    new_path = os.path.join(parent, folder_name)
    try:
        os.makedirs(new_path, exist_ok=True)
        return True, f"Folder '{folder_name}' created"
    except Exception as exc:
        return False, f"Failed to create folder: {exc}"


def rename(nas_root: str, path: str, new_name: str) -> tuple[bool, str]:
    full_path = resolve_full_path(nas_root, path)
    parent_dir = os.path.dirname(full_path)
    new_full = os.path.join(parent_dir, new_name)
    try:
        if os.path.exists(full_path):
            os.rename(full_path, new_full)
            return True, f"Renamed to '{new_name}'"
        return False, "File or folder not found"
    except Exception as exc:
        return False, f"Failed to rename: {exc}"


def delete(nas_root: str, path: str, strategy: str) -> tuple[bool, str]:
    full_path = resolve_full_path(nas_root, path)
    try:
        if not os.path.exists(full_path):
            return False, "File or folder not found"
        if strategy == "recycle_bin":
            recycle_bin = os.path.join(nas_root, ".recycle_bin")
            os.makedirs(recycle_bin, exist_ok=True)
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            original = os.path.basename(full_path)
            dest = os.path.join(recycle_bin, f"{timestamp}_{original}")
            shutil.move(full_path, dest)
            return True, f"Moved to recycle bin: '{original}'"
        else:
            if os.path.isdir(full_path):
                shutil.rmtree(full_path)
            else:
                os.remove(full_path)
            return True, f"Deleted '{os.path.basename(full_path)}' permanently"
    except Exception as exc:
        return False, f"Failed to delete: {exc}"


def move(nas_root: str, source_path: str, destination_path: str) -> tuple[bool, str]:
    source_full = resolve_full_path(nas_root, source_path)
    dest_full = resolve_full_path(nas_root, destination_path)
    if not os.path.exists(source_full):
        return False, f"Source not found: {source_path}"
    if not os.path.isdir(dest_full):
        return False, f"Destination is not a directory: {destination_path}"
    source_name = os.path.basename(source_full)
    final_dest = os.path.join(dest_full, source_name)
    if os.path.exists(final_dest):
        base, ext = os.path.splitext(source_name)
        counter = 1
        while os.path.exists(final_dest):
            final_dest = os.path.join(dest_full, f"{base} ({counter}){ext}")
            counter += 1
    try:
        os.rename(source_full, final_dest)
        return True, f"Moved '{source_name}' to '{destination_path}'"
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            return False, f"Permission denied moving '{source_name}' — {exc.strerror or str(exc)}"
        if exc.errno != errno.EXDEV:
            return False, f"Failed to move '{source_name}': {exc}"
    # Fallback: copy + delete (cross-filesystem)
    try:
        if os.path.isdir(source_full):
            shutil.copytree(source_full, final_dest)
        else:
            shutil.copy2(source_full, final_dest)
    except PermissionError:
        return False, f"Permission denied copying '{source_name}'"
    except Exception as exc:
        return False, f"Failed to copy '{source_name}': {exc}"
    try:
        if os.path.isdir(source_full):
            shutil.rmtree(source_full)
        else:
            os.remove(source_full)
    except PermissionError:
        return True, f"Copied '{source_name}' but could not delete original"
    except Exception as exc:
        return True, f"Copied '{source_name}' but could not delete original: {exc}"
    return True, f"Moved '{source_name}' to '{destination_path}'"


# ── Download (single file or zip-a-folder) ─────────────────────────────────


def download_to_temp(nas_root: str, path: str) -> tuple[str | None, str | None]:
    """Return (local_path, display_name) for a file or a temp zip of a folder.

    Caller is responsible for cleaning up the temp zip.
    """
    full_path = resolve_full_path(nas_root, path)
    if not is_within_nas(nas_root, full_path):
        return None, None
    if not os.path.exists(full_path):
        return None, None
    item_name = os.path.basename(full_path.rstrip("/"))
    if os.path.isfile(full_path):
        return full_path, item_name
    tmp_dir = tempfile.mkdtemp()
    zip_base = os.path.join(tmp_dir, item_name)
    zip_path = shutil.make_archive(zip_base, "zip", full_path)
    return zip_path, item_name + ".zip"


# ── Duplicate finder ───────────────────────────────────────────────────────


def find_duplicates(nas_root: str, path: str = "/") -> dict[str, Any]:
    full_path = resolve_full_path(nas_root, path)
    if not os.path.exists(full_path) or not os.path.isdir(full_path):
        return {"success": False, "message": "Directory not found"}
    md5_map: dict[str, list[dict[str, Any]]] = {}
    try:
        for item in os.listdir(full_path):
            item_path = os.path.join(full_path, item)
            if not os.path.isfile(item_path) or item.startswith("."):
                continue
            try:
                h = hashlib.md5()
                with open(item_path, "rb") as f:
                    for chunk in iter(lambda: f.read(8192), b""):
                        h.update(chunk)
                md5 = h.hexdigest()
                info = {
                    "name": item,
                    "path": relative_path(path, item),
                    "size": os.path.getsize(item_path),
                }
                md5_map.setdefault(md5, []).append(info)
            except (PermissionError, OSError):
                continue
    except PermissionError:
        return {"success": False, "message": "Permission denied"}
    duplicates = []
    for md5, files in md5_map.items():
        if len(files) > 1:
            files.sort(key=lambda f: (-f["size"], f["name"]))
            duplicates.append({"md5": md5, "files": files})
    return {"success": True, "duplicates": duplicates}


# ── Filename cleaning ──────────────────────────────────────────────────────


def clean_names(nas_root: str, path: str = "/") -> dict[str, Any]:
    """Clean filenames and fix missing extensions in a directory."""
    full_path = resolve_full_path(nas_root, path)
    if not os.path.exists(full_path) or not os.path.isdir(full_path):
        return {"success": False, "message": "Directory not found", "renamed": []}
    renamed: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    try:
        for item in os.listdir(full_path):
            src = os.path.join(full_path, item)
            if os.path.isdir(src):
                continue
            cleaned = clean_filename(item)
            if cleaned and not has_extension(cleaned):
                ext = detect_extension(src)
                if ext:
                    cleaned = cleaned + ext
            if cleaned and cleaned != item:
                dst = os.path.join(full_path, cleaned)
                if os.path.exists(dst) and os.path.normpath(src) != os.path.normpath(dst):
                    base, ext = os.path.splitext(cleaned)
                    counter = 1
                    while os.path.exists(dst):
                        dst = os.path.join(full_path, f"{base} ({counter}){ext}")
                        counter += 1
                try:
                    os.rename(src, dst)
                    renamed.append({"old": item, "new": os.path.basename(dst)})
                except Exception as exc:
                    errors.append({"name": item, "error": str(exc)})
    except PermissionError:
        return {"success": False, "message": "Permission denied", "renamed": []}
    return {
        "success": True, "renamed": renamed, "errors": errors,
        "message": f"Cleaned {len(renamed)} filename(s)",
    }


# ── Directory tree ─────────────────────────────────────────────────────────


def build_tree(
    nas_root: str, db: Session | None = None, show_hidden: bool = False
) -> dict[str, Any]:
    """Build the top-level directory tree structure."""
    if not os.path.exists(nas_root):
        try:
            os.makedirs(nas_root, exist_ok=True)
        except PermissionError:
            return {
                "name": "NAS Root", "path": "/", "has_children": False,
                "expanded": True, "children": [],
            }
    root_name = "NAS Root"
    if db:
        from .config_service import get_config_value
        addr = get_config_value(db, "nas_address")
        share = get_config_value(db, "nas_share")
        if addr and share:
            root_name = f"{addr}/{share}"
        elif addr:
            root_name = addr
        else:
            root_name = os.path.basename(nas_root.rstrip("/")) or nas_root
    else:
        root_name = os.path.basename(nas_root.rstrip("/")) or nas_root

    tree: dict[str, Any] = {
        "name": root_name, "path": "/", "has_children": True,
        "expanded": True, "children": [],
    }
    try:
        for item in sorted(os.listdir(nas_root)):
            if not show_hidden and item.startswith("."):
                continue
            item_path = os.path.join(nas_root, item)
            if os.path.isdir(item_path):
                tree["children"].append({
                    "name": item, "path": f"/{item}",
                    "has_children": has_subdirectories(item_path),
                    "expanded": False, "children": [],
                })
    except PermissionError:
        pass
    return tree


def get_children(
    nas_root: str, parent_path: str, show_hidden: bool = False
) -> list[dict[str, Any]]:
    """Get child directories of *parent_path* (one level)."""
    full_path = resolve_full_path(nas_root, parent_path)
    if not os.path.exists(full_path) or not os.path.isdir(full_path):
        return []
    children: list[dict[str, Any]] = []
    try:
        for item in sorted(os.listdir(full_path)):
            if not show_hidden and item.startswith("."):
                continue
            item_path = os.path.join(full_path, item)
            if os.path.isdir(item_path):
                child_path = (
                    f"/{item}" if parent_path == "/"
                    else f"{parent_path.rstrip('/')}/{item}"
                )
                children.append({
                    "name": item, "path": child_path,
                    "has_children": has_subdirectories(item_path),
                    "expanded": False, "children": [],
                })
    except PermissionError:
        pass
    return children


# ── Tagging ────────────────────────────────────────────────────────────────


def get_file_tags(nas_root: str, path: str, db: Session) -> dict[str, str]:
    """Get tags for all files in a directory."""
    full_path = resolve_full_path(nas_root, path)
    tags: dict[str, str] = {}
    try:
        if os.path.exists(full_path) and os.path.isdir(full_path):
            for item in os.listdir(full_path):
                rp = relative_path(path, item)
                meta = db.query(FileMetadata).filter(
                    FileMetadata.file_path == rp
                ).first()
                if meta and meta.tag:
                    tags[rp] = meta.tag
    except PermissionError:
        pass
    return tags


def set_file_tag(
    db: Session, nas_root: str, file_path: str, tag: str | None
) -> tuple[bool, str]:
    """Set or clear the tag for a single file (updates DB + file_index.json)."""
    if tag and tag not in ("music", "podcast"):
        return False, "Invalid tag. Use 'music', 'podcast', or ''."
    tag = tag or None
    meta = db.query(FileMetadata).filter(
        FileMetadata.file_path == file_path
    ).first()
    if meta:
        meta.tag = tag
    else:
        full_path = resolve_full_path(nas_root, file_path)
        file_size = 0
        try:
            file_size = os.path.getsize(full_path)
        except OSError:
            pass
        db.add(FileMetadata(
            file_path=file_path, file_name=os.path.basename(full_path),
            file_size=file_size, file_type="audio", tag=tag,
        ))
    db.commit()
    _update_file_index_tag(nas_root, file_path, tag)
    return True, f"Tag updated to '{tag or 'none'}'"


def _update_file_index_tag(nas_root: str, file_path: str, tag: str | None) -> None:
    """Update a file's tag in file_index.json."""
    index_path = os.path.join(nas_root, "file_index.json")
    index: dict[str, Any] = {"generated_at": 0, "file_count": 0, "files": []}
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                index = _json_load(f)
        except Exception:
            pass
    found = False
    for entry in index.get("files", []):
        if entry.get("path") == file_path:
            entry["tag"] = tag
            found = True
            break
    if not found:
        full_path = resolve_full_path(nas_root, file_path)
        file_size = 0
        try:
            file_size = os.path.getsize(full_path)
        except OSError:
            pass
        index.setdefault("files", []).append({
            "path": file_path, "size": file_size, "tag": tag, "md5": "",
        })
    index["file_count"] = len(index.get("files", []))
    try:
        with open(index_path, "w", encoding="utf-8") as f:
            import json
            json.dump(index, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        print(f"[file_index] Failed to write: {exc}")


def _json_load(f):
    import json
    return json.load(f)


# ── Apply AI rename suggestions ────────────────────────────────────────────


def apply_renames(nas_root: str, renames: list[dict[str, str]]) -> dict[str, Any]:
    """Apply a batch of rename operations."""
    success_count = error_count = 0
    errors: list[str] = []
    for r in renames:
        old_path = r.get("old_path", "")
        new_name = r.get("new_name", "")
        if not old_path or not new_name:
            continue
        old_full = resolve_full_path(nas_root, old_path)
        new_full = os.path.join(os.path.dirname(old_full), new_name)
        try:
            if os.path.exists(old_full):
                os.rename(old_full, new_full)
                success_count += 1
            else:
                error_count += 1
                errors.append(f"File not found: {old_path}")
        except Exception as exc:
            error_count += 1
            errors.append(f"Failed to rename {old_path}: {exc}")
    return {
        "success": error_count == 0,
        "message": f"Renamed {success_count} file(s)"
        + (f", {error_count} failed" if error_count else ""),
        "errors": errors,
    }