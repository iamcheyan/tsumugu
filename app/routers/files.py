from fastapi import APIRouter, Depends, HTTPException, Query, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from starlette.background import BackgroundTask
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import FileMetadata, Config
from ..compressor import compressor, CompressStatus
from ..paths import get_nas_root, resolve_within_nas, clean_filename
from typing import List, Optional, Dict, Any
import os
import re
import shutil
import tempfile
import datetime
import errno
from pathlib import Path

router = APIRouter(prefix="/api/files", tags=["files"])
templates = Jinja2Templates(directory="app/templates")

# Register custom filters (same as in main.py)
def filesizeformat(value):
    if value == 0:
        return "0 B"
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    i = 0
    size = float(value)
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.1f} {units[i]}"

def path_to_id(path):
    from urllib.parse import quote
    return quote(path, safe='').replace('%2F', '--')

templates.env.filters['filesizeformat'] = filesizeformat
templates.env.filters['path_to_id'] = path_to_id

@router.get("/")
async def list_files(
    request: Request,
    path: str = Query("/", description="Directory path to list"),
    search: str = Query("", description="Search query to filter files by name"),
    sort: str = Query("name", description="Sort field: name, size, modified, type"),
    order: str = Query("asc", description="Sort order: asc or desc"),
    show_hidden: bool = Query(False, description="Show hidden files (names starting with .)"),
    db: Session = Depends(get_db)
):
    # Build full filesystem path (confined to NAS root)
    full_path = resolve_within_nas(db, path)
    
    # Get files and directories
    files = []
    try:
        if os.path.exists(full_path) and os.path.isdir(full_path):
            items = os.listdir(full_path)
            # Batch-load tags for this directory (avoids N+1 queries)
            rel_paths = [
                f"{path.rstrip('/')}/{item}" if path != "/" else f"/{item}"
                for item in items
            ]
            tag_map = {}
            if rel_paths:
                for meta in db.query(FileMetadata).filter(FileMetadata.file_path.in_(rel_paths)).all():
                    tag_map[meta.file_path] = meta.tag

            for item in items:
                item_path = os.path.join(full_path, item)
                stat_info = os.stat(item_path)
                
                # Determine file type
                if os.path.isdir(item_path):
                    file_type = "folder"
                elif item.lower().endswith(tuple(AUDIO_EXTENSIONS)):
                    file_type = "audio"
                else:
                    file_type = "generic"
                
                # Format modified time
                modified_time = datetime.datetime.fromtimestamp(stat_info.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                
                # Look up tag from the preloaded map
                rel_path = f"{path.rstrip('/')}/{item}" if path != "/" else f"/{item}"
                tag = tag_map.get(rel_path)

                files.append({
                    "name": item,
                    "type": file_type,
                    "size": stat_info.st_size if not os.path.isdir(item_path) else 0,
                    "modified": modified_time,
                    "path": rel_path,
                    "tag": tag,
                })
            
            # Filter hidden files
            if not show_hidden:
                files = [f for f in files if not f["name"].startswith(".")]

            # Apply search filter
            if search:
                search_lower = search.lower()
                files = [f for f in files if search_lower in str(f["name"]).lower()]
            
            # Apply sorting
            def get_sort_key(file: Dict[str, Any]) -> Any:
                file_type = str(file["type"])
                file_name = str(file["name"])
                file_size = int(file["size"])
                file_modified = str(file["modified"])
                
                if sort == "name":
                    return (file_type != "folder", file_name.lower())
                elif sort == "size":
                    return (file_type != "folder", file_size)
                elif sort == "modified":
                    return (file_type != "folder", file_modified)
                elif sort == "type":
                    return (file_type, file_name.lower())
                else:
                    return (file_type != "folder", file_name.lower())
            
            files.sort(key=get_sort_key, reverse=(order == "desc"))
    except PermissionError:
        # Handle permission error
        pass
    
    return templates.TemplateResponse(
        name="components/file_list.html",
        request=request,
        context={
            "request": request,
            "files": files,
            "current_path": path,
            "search": search,
            "sort": sort,
            "order": order
        }
    )


@router.get("/download")
async def download_file(
    request: Request,
    path: str = Query(..., description="File or folder path to download"),
    db: Session = Depends(get_db)
):
    """Download a file directly, or zip a folder on the fly before downloading."""
    from urllib.parse import quote as url_quote

    # Build full filesystem path (confined to NAS root)
    full_path = resolve_within_nas(db, path)

    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="File or folder not found")

    # Determine download filename (pure ASCII fallback for header)
    item_name = os.path.basename(full_path.rstrip('/'))

    if os.path.isfile(full_path):
        # Direct file download
        encoded_name = f"filename*=UTF-8''{url_quote(item_name)}"
        return FileResponse(
            path=full_path,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; {encoded_name}",
            },
        )

    # Folder → zip on the fly
    tmp_dir = tempfile.mkdtemp()
    try:
        zip_base = os.path.join(tmp_dir, item_name)
        zip_path = shutil.make_archive(zip_base, 'zip', full_path)

        def cleanup():
            try:
                shutil.rmtree(tmp_dir)
            except OSError:
                pass

        encoded_name = f"filename*=UTF-8''{url_quote(item_name + '.zip')}"
        return FileResponse(
            path=zip_path,
            media_type="application/zip",
            background=BackgroundTask(cleanup),
            headers={
                "Content-Disposition": f"attachment; {encoded_name}",
            },
        )
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Failed to create zip archive")


# ── Folder Compression (background with progress) ─────────────────

@router.post("/compress")
async def compress_folder(
    request: Request,
    path: str = Form(..., description="Folder path to compress"),
    db: Session = Depends(get_db)
):
    """Start background compression of a folder. Returns a task_id for tracking."""
    # Resolve folder path (confined to NAS root)
    full_path = resolve_within_nas(db, path)

    if not os.path.exists(full_path) or not os.path.isdir(full_path):
        raise HTTPException(status_code=404, detail="Folder not found")

    folder_name = os.path.basename(full_path.rstrip('/'))
    task_id = await compressor.start_compress(full_path, folder_name)

    return {"task_id": task_id, "folder_name": folder_name}


@router.get("/compress/{task_id}")
async def get_compress_status(task_id: int):
    """Get compression task status."""
    task = compressor.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return {
        "task_id": task.id,
        "status": task.status.value,
        "progress": task.progress,
        "folder_name": task.folder_name,
        "total_files": task.total_files,
        "processed_files": task.processed_files,
        "zip_size": task.zip_size,
        "error": task.error,
    }


@router.get("/compress/{task_id}/download")
async def download_compressed(task_id: int):
    """Download the completed zip file and clean up."""
    task = compressor.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != CompressStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Compression not complete")
    if not task.zip_path or not os.path.exists(task.zip_path):
        raise HTTPException(status_code=404, detail="Zip file not found")

    from urllib.parse import quote as url_quote
    encoded_name = f"filename*=UTF-8''{url_quote(task.folder_name + '.zip')}"

    # Schedule cleanup after response is sent
    return FileResponse(
        path=task.zip_path,
        media_type="application/zip",
        background=BackgroundTask(compressor.cleanup_task, task_id),
        headers={
            "Content-Disposition": f"attachment; {encoded_name}",
        },
    )


@router.delete("/compress/{task_id}")
async def cancel_compress(task_id: int):
    """Cancel a running compression task."""
    task = compressor.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if compressor.cancel(task_id):
        return {"success": True, "message": "Compression cancelled"}
    else:
        return {"success": False, "message": "Task cannot be cancelled"}


@router.post("/create-folder")
async def create_folder(
    request: Request,
    path: str = Form(..., description="Parent directory path"),
    folder_name: str = Form(..., description="Name of new folder"),
    db: Session = Depends(get_db)
):
    """Create a new folder in the specified directory."""
    # Build full filesystem path (confined to NAS root)
    full_path = resolve_within_nas(db, path)
    
    # Create the folder (clean the name: no separators or traversal)
    folder_name_clean = clean_filename(folder_name)
    if not folder_name_clean:
        raise HTTPException(status_code=400, detail="Invalid folder name")
    new_folder_path = os.path.join(full_path, folder_name_clean)
    try:
        os.makedirs(new_folder_path, exist_ok=True)
        return {"success": True, "message": f"Folder '{folder_name_clean}' created successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create folder: {str(e)}")

@router.put("/rename")
async def rename_file(
    request: Request,
    path: str = Form(..., description="Current file/folder path"),
    new_name: str = Form(..., description="New name for file/folder"),
    db: Session = Depends(get_db)
):
    """Rename a file or folder."""
    # Build full filesystem path (confined to NAS root)
    full_path = resolve_within_nas(db, path)
    parent_dir = os.path.dirname(full_path)
    new_name_clean = clean_filename(new_name)
    if not new_name_clean:
        raise HTTPException(status_code=400, detail="Invalid new name")
    new_full_path = os.path.join(parent_dir, new_name_clean)
    
    try:
        if os.path.exists(full_path):
            os.rename(full_path, new_full_path)
            return {"success": True, "message": f"Renamed to '{new_name_clean}' successfully"}
        else:
            raise HTTPException(status_code=404, detail="File or folder not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to rename: {str(e)}")

@router.delete("/delete")
async def delete_file(
    request: Request,
    path: str = Query(..., description="File/folder path to delete"),
    db: Session = Depends(get_db)
):
    """Delete a file or folder based on configured deletion strategy."""
    # Resolve confined path
    full_path = resolve_within_nas(db, path)
    nas_root = get_nas_root(db)
    
    # Get deletion strategy
    deletion_strategy_config = db.query(Config).filter(Config.key == "deletion_strategy").first()
    deletion_strategy: str = str(deletion_strategy_config.value) if deletion_strategy_config else "recycle_bin"
    
    try:
        if os.path.exists(full_path):
            if deletion_strategy == "recycle_bin":
                # Move to recycle bin
                recycle_bin_path = os.path.join(nas_root, ".recycle_bin")
                os.makedirs(recycle_bin_path, exist_ok=True)
                
                # Create unique name in recycle bin
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                original_name = os.path.basename(full_path)
                recycle_item_path = os.path.join(recycle_bin_path, f"{timestamp}_{original_name}")
                
                shutil.move(full_path, recycle_item_path)
                return {"success": True, "message": f"Moved to recycle bin: '{original_name}'"}
            else:
                # Direct delete
                if os.path.isdir(full_path):
                    shutil.rmtree(full_path)
                else:
                    os.remove(full_path)
                return {"success": True, "message": f"Deleted '{os.path.basename(full_path)}' permanently"}
        else:
            raise HTTPException(status_code=404, detail="File or folder not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete: {str(e)}")

@router.post("/move")
async def move_file(
    request: Request,
    source_path: str = Form(..., description="Source file/folder path"),
    destination_path: str = Form(..., description="Destination directory path"),
    db: Session = Depends(get_db)
):
    """Move a file or folder to a new location."""
    # Build full filesystem paths (both confined to NAS root)
    source_full_path = resolve_within_nas(db, source_path)
    destination_full_path = resolve_within_nas(db, destination_path)

    print(f"[MOVE] source_path={source_path} -> {source_full_path}")
    print(f"[MOVE] dest_path={destination_path} -> {destination_full_path}")

    # Validate source exists
    if not os.path.exists(source_full_path):
        print(f"[MOVE] FAIL: source not found: {source_full_path}")
        return {"success": False, "message": f"Source not found: {source_path}"}

    # Ensure destination is a directory
    if not os.path.isdir(destination_full_path):
        print(f"[MOVE] FAIL: destination not a directory: {destination_full_path}")
        return {"success": False, "message": f"Destination is not a directory: {destination_path}"}

    # Build final destination path
    source_name = os.path.basename(source_full_path)
    final_destination = os.path.join(destination_full_path, source_name)

    # If destination already exists, append a number
    if os.path.exists(final_destination):
        base, ext = os.path.splitext(source_name)
        counter = 1
        while os.path.exists(final_destination):
            final_destination = os.path.join(destination_full_path, f"{base} ({counter}){ext}")
            counter += 1

    # Try rename first (fast, preserves metadata, works on the same SMB mount).
    try:
        os.rename(source_full_path, final_destination)
        return {"success": True, "message": f"Moved '{source_name}' to '{destination_path}'"}
    except OSError as e:
        print(f"[MOVE] rename failed: {type(e).__name__}: {e}")
        if e.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            return {
                "success": False,
                "message": f"Permission denied moving '{source_name}' — {e.strerror or str(e)}",
            }
        if e.errno != errno.EXDEV:
            return {"success": False, "message": f"Failed to move '{source_name}': {str(e)}"}

    # Fallback: copy + delete (cross-filesystem or rename not supported)
    import shutil as _shutil
    try:
        if os.path.isdir(source_full_path):
            _shutil.copytree(source_full_path, final_destination)
        else:
            _shutil.copy2(source_full_path, final_destination)
    except PermissionError:
        return {"success": False, "message": f"Permission denied copying '{source_name}' — check NAS share write permissions"}
    except Exception as e:
        return {"success": False, "message": f"Failed to copy '{source_name}': {str(e)}"}

    # Copy succeeded, now delete source
    try:
        if os.path.isdir(source_full_path):
            _shutil.rmtree(source_full_path)
        else:
            os.remove(source_full_path)
    except PermissionError:
        # Copy succeeded but can't delete source — file is at destination, warn user
        return {"success": True, "message": f"Copied '{source_name}' to '{destination_path}' but could not delete original (check NAS delete permissions)"}
    except Exception as e:
        return {"success": True, "message": f"Copied '{source_name}' to '{destination_path}' but could not delete original: {str(e)}"}

    return {"success": True, "message": f"Moved '{source_name}' to '{destination_path}'"}

@router.get("/find-duplicates")
async def find_duplicates(
    request: Request,
    path: str = Query("/", description="Directory path to scan for duplicates"),
    db: Session = Depends(get_db)
):
    """Find duplicate files in the specified directory based on MD5 hash."""
    import hashlib

    # Build full filesystem path (confined to NAS root)
    full_path = resolve_within_nas(db, path)

    if not os.path.exists(full_path) or not os.path.isdir(full_path):
        return {"success": False, "message": "Directory not found"}

    # Scan files and compute MD5 hashes
    md5_map = {}  # md5 -> list of files
    try:
        for item in os.listdir(full_path):
            item_path = os.path.join(full_path, item)
            if os.path.isfile(item_path):
                # Skip hidden files
                if item.startswith('.'):
                    continue
                try:
                    # Compute MD5 hash
                    md5_hash = hashlib.md5()
                    with open(item_path, 'rb') as f:
                        for chunk in iter(lambda: f.read(8192), b''):
                            md5_hash.update(chunk)
                    md5 = md5_hash.hexdigest()

                    file_info = {
                        "name": item,
                        "path": f"{path.rstrip('/')}/{item}" if path != "/" else f"/{item}",
                        "size": os.path.getsize(item_path)
                    }

                    if md5 not in md5_map:
                        md5_map[md5] = []
                    md5_map[md5].append(file_info)
                except (PermissionError, OSError):
                    continue
    except PermissionError:
        return {"success": False, "message": "Permission denied"}

    # Filter to only groups with duplicates
    duplicates = []
    for md5, files in md5_map.items():
        if len(files) > 1:
            # Sort by size (largest first), then by name
            files.sort(key=lambda f: (-f["size"], f["name"]))
            duplicates.append({
                "md5": md5,
                "files": files
            })

    return {"success": True, "duplicates": duplicates}

class AIRenameRequest(BaseModel):
    files: List[Dict[str, str]]

@router.post("/ai-rename")
async def ai_rename_files(
    request: AIRenameRequest,
    db: Session = Depends(get_db)
):
    """Use AI to analyze filenames and suggest clean 'Song-Artist' names."""
    from ..ai_rename import analyze_filenames
    import asyncio

    if not request.files:
        return {"success": False, "message": "No files provided"}

    try:
        # analyze_filenames performs blocking LLM HTTP calls (up to 30s timeout).
        # Run it in a thread pool so the event loop is not blocked.
        suggestions = await asyncio.get_running_loop().run_in_executor(
            None, analyze_filenames, request.files
        )
        return {
            "success": True,
            "suggestions": [
                {
                    "original_path": s.original_path,
                    "original_name": s.original_name,
                    "suggested_name": s.suggested_name,
                    "confidence": s.confidence,
                    "reason": s.reason
                }
                for s in suggestions
            ]
        }
    except Exception as e:
        return {"success": False, "message": str(e)}

@router.post("/apply-rename")
async def apply_rename(
    request: Request,
    db: Session = Depends(get_db)
):
    """Apply rename suggestions to files."""
    body = await request.json()
    renames = body.get("renames", [])

    if not renames:
        return {"success": False, "message": "No renames provided"}

    # Get NAS root from config
    nas_root = get_nas_root(db)

    success_count = 0
    error_count = 0
    errors = []

    for rename in renames:
        old_path = rename.get("old_path", "")
        new_name = rename.get("new_name", "")

        if not old_path or not new_name:
            continue

        # Confine old path to NAS root
        try:
            old_full_path = resolve_within_nas(db, old_path)
        except HTTPException:
            error_count += 1
            errors.append(f"Access denied: {old_path}")
            continue
        parent_dir = os.path.dirname(old_full_path)

        # Sanitize the new name: bare filename only, preserve extension
        new_name_clean = clean_filename(new_name)
        if not new_name_clean:
            error_count += 1
            errors.append(f"Invalid new name for {old_path}")
            continue
        old_ext = os.path.splitext(os.path.basename(old_full_path))[1]
        if not os.path.splitext(new_name_clean)[1]:
            new_name_clean = new_name_clean + old_ext
        new_full_path = os.path.join(parent_dir, new_name_clean)

        try:
            if os.path.exists(old_full_path):
                os.rename(old_full_path, new_full_path)
                success_count += 1
            else:
                error_count += 1
                errors.append(f"File not found: {old_path}")
        except Exception as e:
            error_count += 1
            errors.append(f"Failed to rename {old_path}: {str(e)}")

    return {
        "success": error_count == 0,
        "message": f"Renamed {success_count} file(s)" + (f", {error_count} failed" if error_count > 0 else ""),
        "errors": errors
    }

@router.get("/tree")
async def get_directory_tree(
    request: Request,
    path: str = Query("/", description="Root path for directory tree"),
    selected: str = Query("/", description="Currently selected directory"),
    show_hidden: bool = Query(False, description="Show hidden files"),
    db: Session = Depends(get_db)
):
    # Get NAS root from config
    nas_root = get_nas_root(db)

    # Build directory tree
    tree_data = await _build_directory_tree(nas_root, path, selected, db, show_hidden)

    return templates.TemplateResponse(
        name="components/tree.html",
        request=request,
        context={"request": request, "tree": tree_data, "selected": selected, "show_hidden": show_hidden}
    )

@router.get("/tree/children")
async def get_tree_children(
    request: Request,
    path: str = Query("/", description="Parent directory path"),
    selected: str = Query("/", description="Currently selected directory"),
    show_hidden: bool = Query(False, description="Show hidden files"),
    db: Session = Depends(get_db)
):
    # Get NAS root from config
    nas_root = get_nas_root(db)

    # Get children of the specified path
    children = await _get_directory_children(db, path, selected, show_hidden)

    return templates.TemplateResponse(
        name="components/tree_children.html",
        request=request,
        context={"request": request, "children": children, "selected": selected, "parent_path": path, "show_hidden": show_hidden}
    )

@router.get("/metadata/{file_path:path}")
async def get_file_metadata(file_path: str, db: Session = Depends(get_db)):
    metadata = db.query(FileMetadata).filter(FileMetadata.file_path == file_path).first()
    if not metadata:
        raise HTTPException(status_code=404, detail="File metadata not found")
    return {
        "file_path": metadata.file_path,
        "file_name": metadata.file_name,
        "file_size": metadata.file_size,
        "file_type": metadata.file_type,
        "modified_at": metadata.modified_at,
        "created_at": metadata.created_at
    }

async def _build_directory_tree(nas_root: str, current_path: str, selected: str, db: Session = None, show_hidden: bool = False) -> Dict[str, Any]:
    """Build the full directory tree structure."""
    # Ensure nas_root exists
    if not os.path.exists(nas_root):
        try:
            os.makedirs(nas_root, exist_ok=True)
        except PermissionError:
            return {
                "name": "NAS Root",
                "path": "/",
                "is_selected": selected == "/",
                "has_children": False,
                "expanded": True,
                "children": []
            }

    # Build display name: "address/share" if configured, else directory basename
    root_name = "NAS Root"
    if db:
        def get_val(key, default=""):
            c = db.query(Config).filter(Config.key == key).first()
            return c.value if c else default
        addr = get_val("nas_address")
        share = get_val("nas_share")
        if addr and share:
            root_name = f"{addr}/{share}"
        elif addr:
            root_name = addr
        else:
            root_name = os.path.basename(nas_root.rstrip('/')) or nas_root
    else:
        root_name = os.path.basename(nas_root.rstrip('/')) or nas_root

    tree: Dict[str, Any] = {
        "name": root_name,
        "path": "/",
        "is_selected": selected == "/",
        "has_children": True,
        "expanded": True,
        "children": []
    }
    
    # Get immediate children of root
    try:
        items = sorted(os.listdir(nas_root))
        for item in items:
            if not show_hidden and item.startswith("."):
                continue
            item_path = os.path.join(nas_root, item)
            if os.path.isdir(item_path):
                child_path = f"/{item}"
                child = {
                    "name": item,
                    "path": child_path,
                    "is_selected": selected == child_path,
                    "has_children": _has_subdirectories(item_path),
                    "expanded": False,
                    "children": []
                }
                tree["children"].append(child)
    except PermissionError:
        pass
    
    return tree

async def _get_directory_children(db: Session, parent_path: str, selected: str, show_hidden: bool = False) -> List[Dict[str, Any]]:
    """Get children of a specific directory."""
    full_path = resolve_within_nas(db, parent_path)
    
    if not os.path.exists(full_path) or not os.path.isdir(full_path):
        return []
    
    children = []
    try:
        items = sorted(os.listdir(full_path))
        for item in items:
            if not show_hidden and item.startswith("."):
                continue
            item_path = os.path.join(full_path, item)
            if os.path.isdir(item_path):
                child_path = f"{parent_path.rstrip('/')}/{item}"
                if parent_path == "/":
                    child_path = f"/{item}"
                
                child = {
                    "name": item,
                    "path": child_path,
                    "is_selected": selected == child_path,
                    "has_children": _has_subdirectories(item_path),
                    "expanded": False,
                    "children": []
                }
                children.append(child)
    except PermissionError:
        pass
    
    return children

def _clean_filename(name: str) -> str:
    """Clean a filename by removing problematic characters.

    Keeps: letters (Latin, CJK, Japanese, Korean), digits, spaces,
           dots, hyphens, underscores, parentheses, brackets.
    Removes: emojis, symbols, control chars, forbidden sync-tool chars.
    """
    # Remove control characters
    result = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", name)

    # Remove characters forbidden by common sync tools: \ / : * ? " < > |
    result = re.sub(r'[\\/:*?"<>|]', "", result)

    # Remove emojis and symbols: strip anything that is NOT a kept character class.
    # Kept: word chars (letters/digits/_), CJK, Japanese, spaces, dots,
    #        hyphens, parentheses, brackets, commas.
    result = re.sub(
        r"[^\w\s.\-()（）　-〿぀-ゟ゠-ヿ一-鿿"
        r"가-힯㐀-䶿豈-﫿,]+",
        "",
        result,
        flags=re.UNICODE,
    )

    # Collapse multiple spaces into one
    result = re.sub(r"\s+", " ", result)

    # Strip leading/trailing spaces and dots
    result = result.strip(" .")

    return result


@router.post("/clean-names")
async def clean_filenames(
    request: Request,
    path: str = Form("/", description="Directory path"),
    db: Session = Depends(get_db)
):
    """Clean filenames and fix missing extensions in a directory."""
    full_path = resolve_within_nas(db, path)

    if not os.path.exists(full_path) or not os.path.isdir(full_path):
        return {"success": False, "message": "Directory not found", "renamed": []}

    renamed = []
    errors = []
    try:
        for item in os.listdir(full_path):
            src = os.path.join(full_path, item)
            if os.path.isdir(src):
                continue

            # Step 1: Clean the filename (remove emojis, bad chars)
            cleaned = _clean_filename(item)

            # Step 2: If no known extension, detect from file content and add it
            if cleaned and not _has_extension(cleaned):
                detected_ext = _detect_extension(src)
                if detected_ext:
                    cleaned = cleaned + detected_ext

            if cleaned and cleaned != item:
                dst = os.path.join(full_path, cleaned)
                # Avoid overwriting existing files
                if os.path.exists(dst) and os.path.normpath(src) != os.path.normpath(dst):
                    base, ext = os.path.splitext(cleaned)
                    counter = 1
                    while os.path.exists(dst):
                        cleaned_name = f"{base} ({counter}){ext}"
                        dst = os.path.join(full_path, cleaned_name)
                        counter += 1
                try:
                    os.rename(src, dst)
                    renamed.append({"old": item, "new": os.path.basename(dst)})
                except Exception as e:
                    errors.append({"name": item, "error": str(e)})
    except PermissionError:
        return {"success": False, "message": "Permission denied", "renamed": []}

    return {
        "success": True,
        "renamed": renamed,
        "errors": errors,
        "message": f"Cleaned {len(renamed)} filename(s)"
    }


# ── AI Tag (music / podcast) ────────────────────────────────────────

AUDIO_EXTENSIONS = {'.mp3', '.m4a', '.flac', '.wav', '.ogg', '.aac', '.wma', '.opus'}

def _guess_tag_from_size(file_size: int) -> str:
    """Heuristic: small audio = music, large audio = podcast."""
    # ~1MB per minute at 128kbps.  8MB ≈ 8 minutes threshold.
    if file_size < 8 * 1024 * 1024:
        return "music"
    return "podcast"


@router.post("/ai-tag")
async def ai_tag_files(
    request: Request,
    db: Session = Depends(get_db)
):
    """AI-tag selected audio files as 'music' or 'podcast'."""
    body = await request.json()
    files_to_tag = body.get("files", [])

    if not files_to_tag:
        return {"success": False, "message": "No files provided"}

    nas_root = get_nas_root(db)

    tagged = []
    errors = []

    for file_info in files_to_tag:
        file_path = file_info.get("path", "")
        file_name = file_info.get("name", "")
        try:
            full_path = resolve_within_nas(db, file_path)
        except HTTPException:
            errors.append({"path": file_path, "error": "Access denied"})
            continue

        ext = os.path.splitext(file_name)[1].lower()
        if ext not in AUDIO_EXTENSIONS:
            continue

        try:
            file_size = os.path.getsize(full_path)

            # Try AI first, fall back to size heuristic
            tag = None
            try:
                from ..ai_rename import call_llm
                resp = call_llm(
                    f'Is this file more likely music or a podcast?\n'
                    f'Filename: {file_name}\n'
                    f'Size: {file_size} bytes ({file_size / 1024 / 1024:.1f} MB)\n'
                    f'Reply with ONLY one word: music or podcast',
                    'Reply with exactly one word: music or podcast. Nothing else.'
                )
                tag_raw = resp.strip().lower()
                if 'podcast' in tag_raw:
                    tag = 'podcast'
                elif 'music' in tag_raw:
                    tag = 'music'
            except Exception:
                pass

            # Fallback to size heuristic
            if not tag:
                tag = _guess_tag_from_size(file_size)

            # Save to database
            meta = db.query(FileMetadata).filter(FileMetadata.file_path == file_path).first()
            if meta:
                meta.tag = tag
            else:
                meta = FileMetadata(
                    file_path=file_path,
                    file_name=file_name,
                    file_size=file_size,
                    file_type="audio",
                    tag=tag,
                )
                db.add(meta)
            db.commit()

            # Update file_index.json immediately
            _update_file_index_tag(nas_root, file_path, tag)

            tagged.append({"path": file_path, "name": file_name, "tag": tag})

        except Exception as e:
            errors.append({"path": file_path, "error": str(e)})

    return {
        "success": True,
        "tagged": tagged,
        "errors": errors,
        "message": f"Tagged {len(tagged)} file(s)"
    }


@router.get("/tags")
async def get_file_tags(
    path: str = Query("/", description="Directory path"),
    db: Session = Depends(get_db)
):
    """Get tags for all files in a directory."""
    full_path = resolve_within_nas(db, path)

    tags = {}
    try:
        if os.path.exists(full_path) and os.path.isdir(full_path):
            for item in os.listdir(full_path):
                item_path_str = f"{path.rstrip('/')}/{item}" if path != "/" else f"/{item}"
                meta = db.query(FileMetadata).filter(FileMetadata.file_path == item_path_str).first()
                if meta and meta.tag:
                    tags[item_path_str] = meta.tag
    except PermissionError:
        pass

    return {"success": True, "tags": tags}


def _update_file_index_tag(nas_root: str, file_path: str, tag: str | None):
    """Update a file's tag in file_index.json (create if not exists)."""
    import json as _json
    index_path = os.path.join(nas_root, "file_index.json")

    # Load existing index or create new
    index = {"generated_at": 0, "file_count": 0, "files": []}
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                index = _json.load(f)
        except (_json.JSONDecodeError, OSError):
            pass

    # Find and update the file entry
    found = False
    for entry in index.get("files", []):
        if entry.get("path") == file_path:
            entry["tag"] = tag
            found = True
            break

    if not found:
        # Add new entry
        full_path = os.path.join(nas_root, file_path.lstrip("/"))
        file_size = 0
        try:
            file_size = os.path.getsize(full_path)
        except OSError:
            pass
        index.setdefault("files", []).append({
            "path": file_path,
            "size": file_size,
            "tag": tag,
            "md5": "",
        })

    index["file_count"] = len(index["files"])

    # Save back
    try:
        with open(index_path, "w", encoding="utf-8") as f:
            _json.dump(index, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"[file_index] Failed to write: {e}")


@router.post("/set-tag")
async def set_file_tag(
    request: Request,
    db: Session = Depends(get_db)
):
    """Set or clear the tag for a single file."""
    body = await request.json()
    file_path = body.get("path", "")
    tag = body.get("tag", "")  # "music", "podcast", or "" to clear

    if not file_path:
        return {"success": False, "message": "No file path provided"}

    # Normalize tag
    if tag and tag not in ("music", "podcast"):
        return {"success": False, "message": "Invalid tag. Use 'music', 'podcast', or ''."}
    tag = tag or None

    # Get NAS root for file_index.json
    nas_root = get_nas_root(db)

    meta = db.query(FileMetadata).filter(FileMetadata.file_path == file_path).first()
    if meta:
        meta.tag = tag
    else:
        # Confine path to NAS root before touching the filesystem
        try:
            full_path = resolve_within_nas(db, file_path)
        except HTTPException:
            raise HTTPException(status_code=403, detail="Access denied: path is outside the NAS root")
        file_name = os.path.basename(full_path)
        file_size = 0
        try:
            file_size = os.path.getsize(full_path)
        except OSError:
            pass
        meta = FileMetadata(
            file_path=file_path,
            file_name=file_name,
            file_size=file_size,
            file_type="audio",
            tag=tag,
        )
        db.add(meta)

    db.commit()

    # Update file_index.json immediately
    _update_file_index_tag(nas_root, file_path, tag)

    return {"success": True, "message": f"Tag updated to '{tag or 'none'}'"}


@router.post("/set-folder-tag")
async def set_folder_tag(
    request: Request,
    db: Session = Depends(get_db)
):
    """Set a tag on all audio files inside a folder (recursively)."""
    body = await request.json()
    folder_path = body.get("path", "")
    tag = body.get("tag", "")  # "music", "podcast", or "" to clear

    if not folder_path:
        return {"success": False, "message": "No folder path provided"}

    # Normalize tag
    if tag and tag not in ("music", "podcast"):
        return {"success": False, "message": "Invalid tag. Use 'music', 'podcast', or ''."}
    tag = tag or None

    # Get NAS root
    nas_root = get_nas_root(db)

    try:
        full_path = resolve_within_nas(db, folder_path)
    except HTTPException:
        return {"success": False, "message": "Access denied"}
    if not os.path.exists(full_path) or not os.path.isdir(full_path):
        return {"success": False, "message": "Folder not found"}

    # Recursively find all audio files
    tagged_count = 0
    errors = []

    for root, dirs, files in os.walk(full_path):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in AUDIO_EXTENSIONS:
                continue

            # Build relative path from NAS root
            abs_path = os.path.join(root, fname)
            rel_path = "/" + os.path.relpath(abs_path, nas_root)

            try:
                file_size = os.path.getsize(abs_path)

                # Update database
                meta = db.query(FileMetadata).filter(FileMetadata.file_path == rel_path).first()
                if meta:
                    meta.tag = tag
                else:
                    meta = FileMetadata(
                        file_path=rel_path,
                        file_name=fname,
                        file_size=file_size,
                        file_type="audio",
                        tag=tag,
                    )
                    db.add(meta)

                # Update file_index.json
                _update_file_index_tag(nas_root, rel_path, tag)
                tagged_count += 1
            except Exception as e:
                errors.append({"path": rel_path, "error": str(e)})

    # Also save tag on the folder itself for display purposes
    folder_meta = db.query(FileMetadata).filter(FileMetadata.file_path == folder_path).first()
    if folder_meta:
        folder_meta.tag = tag
    else:
        folder_meta = FileMetadata(
            file_path=folder_path,
            file_name=os.path.basename(folder_path.rstrip('/')),
            file_size=0,
            file_type="folder",
            tag=tag,
        )
        db.add(folder_meta)

    db.commit()

    tag_label = tag or "none"
    return {
        "success": True,
        "tagged_count": tagged_count,
        "errors": errors,
        "message": f"Tagged {tagged_count} file(s) in folder as '{tag_label}'"
    }


# ── File-type detection for missing extensions ──────────────────────

_AUDIO_EXT = AUDIO_EXTENSIONS | {".opus"}
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp", ".tiff"}
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp", ".tiff"}
_VIDEO_EXT = {".mp4", ".avi", ".mkv", ".mov", ".webm"}
_ALL_KNOWN = _AUDIO_EXT | _IMAGE_EXT | _VIDEO_EXT | {
    ".zip", ".gz", ".rar", ".7z", ".mid", ".midi",
    ".txt", ".pdf", ".doc", ".docx",
}


def _has_extension(filename: str) -> bool:
    """Check if filename already has a known extension."""
    _, ext = os.path.splitext(filename)
    return ext.lower() in _ALL_KNOWN


def _detect_extension(filepath: str) -> str | None:
    """Detect file type by reading magic bytes. Returns ext or None."""
    try:
        with open(filepath, "rb") as f:
            header = f.read(16)
        if len(header) < 4:
            return None

        # ftyp box → mp4 / m4a / mov
        if len(header) >= 8 and header[4:8] == b"ftyp":
            brand = header[8:12] if len(header) >= 12 else b""
            if brand in (b"M4A ", b"mp4a"):
                return ".m4a"
            if brand in (b"qt  ",):
                return ".mov"
            return ".mp4"

        # RIFF container → wav / webp
        if header[:4] == b"RIFF" and len(header) >= 12:
            sub = header[8:12]
            if sub == b"WAVE":
                return ".wav"
            if sub == b"WEBP":
                return ".webp"
            return ".wav"

        # Ogg
        if header[:4] == b"OggS":
            return ".ogg"

        # FLAC
        if header[:4] == b"fLaC":
            return ".flac"

        # MP3 (ID3 tag or sync word)
        if header[:3] == b"ID3":
            return ".mp3"
        if header[0] == 0xFF and (header[1] & 0xE0) == 0xE0:
            return ".mp3"

        # MIDI
        if header[:4] == b"MThd":
            return ".mid"

        # PNG
        if header[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"

        # JPEG
        if header[:3] == b"\xff\xd8\xff":
            return ".jpg"

        # GIF
        if header[:6] in (b"GIF87a", b"GIF89a"):
            return ".gif"

        # ZIP / GZIP
        if header[:2] == b"PK":
            return ".zip"
        if header[:2] == b"\x1f\x8b":
            return ".gz"

    except (OSError, IOError):
        pass
    return None


def _has_subdirectories(path: str) -> bool:
    """Check if a directory has any subdirectories."""
    try:
        for item in os.listdir(path):
            if os.path.isdir(os.path.join(path, item)):
                return True
    except PermissionError:
        pass
    return False
