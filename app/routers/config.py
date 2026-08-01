from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import Config
from ..nas_mount import mount_nas, unmount_nas, get_mount_status, _get_mount_point
from ..paths import get_nas_root
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import shutil
import socket
import json
import os

router = APIRouter(prefix="/api/config", tags=["config"])

class ConfigUpdate(BaseModel):
    value: str
    description: Optional[str] = None

@router.get("/")
async def get_all_configs(db: Session = Depends(get_db)):
    configs = db.query(Config).all()
    return [{"key": c.key, "value": c.value, "description": c.description} for c in configs]


@router.get("/models")
async def get_available_models():
    """Read opencode.json and return available AI models grouped by provider."""
    config_path = os.path.expanduser("~/.config/opencode/opencode.json")
    if not os.path.exists(config_path):
        return {"models": [], "current": ""}

    try:
        with open(config_path, "r") as f:
            config = json.load(f)
    except Exception:
        return {"models": [], "current": ""}

    current_model = config.get("model", "")
    providers = config.get("provider", {})
    models = []

    for provider_key, provider_data in providers.items():
        provider_name = provider_data.get("name", provider_key)
        api_type = provider_data.get("api", "")
        has_apikey = bool(provider_data.get("options", {}).get("apiKey", ""))
        provider_models = provider_data.get("models", {})

        for model_id, model_data in provider_models.items():
            full_id = f"{provider_key}/{model_id}"
            models.append({
                "id": full_id,
                "name": model_data.get("name", model_id),
                "provider": provider_name,
                "provider_key": provider_key,
                "family": model_data.get("family", ""),
                "context_limit": model_data.get("limit", {}).get("context", 0),
                "output_limit": model_data.get("limit", {}).get("output", 0),
                "has_apikey": has_apikey,
            })

    return {"models": models, "current": current_model}


@router.get("/refresh-status")
async def refresh_dependency_status(db: Session = Depends(get_db)):
    # Refresh dependency status
    ytdlp_status = "installed" if shutil.which("yt-dlp") else "missing"
    ffmpeg_status = "installed" if shutil.which("ffmpeg") else "missing"
    
    # Update configs
    ytdlp_config = db.query(Config).filter(Config.key == "ytdlp_status").first()
    if ytdlp_config:
        setattr(ytdlp_config, 'value', ytdlp_status)
    else:
        db.add(Config(key="ytdlp_status", value=ytdlp_status, description="yt-dlp installation status"))
    
    ffmpeg_config = db.query(Config).filter(Config.key == "ffmpeg_status").first()
    if ffmpeg_config:
        setattr(ffmpeg_config, 'value', ffmpeg_status)
    else:
        db.add(Config(key="ffmpeg_status", value=ffmpeg_status, description="ffmpeg installation status"))
    
    db.commit()
    
    # Return HTML fragment for HTMX
    html = f"""
    <div class="dependency-item">
        <span class="dependency-name">yt-dlp:</span>
        <span class="dependency-status {'status-installed' if ytdlp_status == 'installed' else 'status-missing'}">
            {'✅ Installed' if ytdlp_status == 'installed' else '❌ Missing'}
        </span>
    </div>
    
    <div class="dependency-item">
        <span class="dependency-name">ffmpeg:</span>
        <span class="dependency-status {'status-installed' if ffmpeg_status == 'installed' else 'status-missing'}">
            {'✅ Installed' if ffmpeg_status == 'installed' else '❌ Missing'}
        </span>
    </div>
    """
    return HTMLResponse(content=html)

class ConnectionTestRequest(BaseModel):
    address: str
    protocol: str = "smb"
    share: str = ""
    username: str = ""
    password: str = ""
    port: str = "445"


@router.post("/test-connection")
async def test_connection(req: ConnectionTestRequest):
    """Test NAS connectivity by attempting a TCP connection to the address/port."""
    if not req.address:
        return {"success": False, "message": "Address is required"}

    try:
        port = int(req.port) if req.port else (445 if req.protocol == "smb" else 2049)
        sock = socket.create_connection((req.address, port), timeout=5)
        sock.close()
        return {"success": True, "message": f"Connection to {req.address}:{port} successful"}
    except socket.timeout:
        return {"success": False, "message": f"Connection timed out ({req.address}:{port})"}
    except socket.gaierror:
        return {"success": False, "message": f"Cannot resolve hostname: {req.address}"}
    except ConnectionRefusedError:
        return {"success": False, "message": f"Connection refused ({req.address}:{port})"}
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {str(e)}"}


@router.post("/mount")
async def mount_share(db: Session = Depends(get_db)):
    """Mount the configured NAS share to a local directory."""
    def get_val(key, default=""):
        c = db.query(Config).filter(Config.key == key).first()
        return c.value if c else default

    address = get_val("nas_address")
    protocol = get_val("nas_protocol", "smb")
    share = get_val("nas_share")
    username = get_val("nas_username")
    password = get_val("nas_password")
    port = get_val("nas_port")

    if not address or not share:
        return {"success": False, "message": "NAS address and share are required. Configure them in Settings first."}

    result = mount_nas(address, protocol, share, username, password, port)

    if result["success"]:
        # Update nas_root to the mount point
        nas_root_config = db.query(Config).filter(Config.key == "nas_root").first()
        if nas_root_config:
            setattr(nas_root_config, 'value', result["mount_point"])
        else:
            db.add(Config(key="nas_root", value=result["mount_point"], description="Root directory for NAS files"))
        db.commit()

    return result


@router.post("/unmount")
async def unmount_share(db: Session = Depends(get_db)):
    """Unmount the current NAS share."""
    def get_val(key, default=""):
        c = db.query(Config).filter(Config.key == key).first()
        return c.value if c else default

    share = get_val("nas_share")
    result = unmount_nas(share)
    return result


@router.get("/mount-status")
async def mount_status(db: Session = Depends(get_db)):
    """Check if the NAS share is currently mounted."""
    def get_val(key, default=""):
        c = db.query(Config).filter(Config.key == key).first()
        return c.value if c else default

    share = get_val("nas_share")
    status = get_mount_status(share)
    return status


# ── Cache Management ────────────────────────────────────────────────

import glob
import shutil
import time
from pathlib import Path


def _get_dir_size(path: str) -> int:
    """Recursively calculate total size of a directory."""
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                total += entry.stat().st_size
            elif entry.is_dir(follow_symlinks=False):
                total += _get_dir_size(entry.path)
    except (PermissionError, OSError):
        pass
    return total


def _scan_cache_items(base: str, prefix: str = "") -> list[dict]:
    """Scan a directory for cache items, returning list of {path, name, size}."""
    items = []
    try:
        for entry in os.scandir(base):
            if not entry.name.startswith(".") and entry.name.startswith(prefix) if prefix else True:
                if entry.is_dir(follow_symlinks=False):
                    size = _get_dir_size(entry.path)
                    items.append({"path": entry.path, "name": entry.name, "size": size, "type": "dir"})
                elif entry.is_file(follow_symlinks=False):
                    size = entry.stat().st_size
                    items.append({"path": entry.path, "name": entry.name, "size": size, "type": "file"})
    except (PermissionError, OSError):
        pass
    return items


def _get_dir_size_approx(path: str, max_depth: int = 1) -> int:
    """Quick size estimate — only scans top level for speed."""
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                total += entry.stat().st_size
            elif entry.is_dir(follow_symlinks=False) and max_depth > 1:
                total += _get_dir_size(entry.path)
    except (PermissionError, OSError):
        pass
    return total


@router.get("/cache-stats")
async def get_cache_stats(db: Session = Depends(get_db)):
    """Return cache sizes for each category so the UI can display them."""
    categories = []

    # 1. Temp zip archives (tmp.* dirs created by tempfile.mkdtemp)
    tmp_base = "/tmp"
    tmp_items = []
    try:
        for entry in os.scandir(tmp_base):
            if entry.is_dir(follow_symlinks=False) and entry.name.startswith("tmp."):
                size = _get_dir_size(entry.path)
                if size > 0:
                    tmp_items.append({"name": entry.name, "size": size})
    except (PermissionError, OSError):
        pass
    tmp_total = sum(i["size"] for i in tmp_items)
    categories.append({
        "name": "临时文件 (Temp Archives)",
        "description": "文件夹下载压缩包等临时文件",
        "size": tmp_total,
        "count": len(tmp_items),
        "key": "temp_archives",
    })

    # 2. yt-dlp cache
    ytdlp_cache = os.path.expanduser("~/.cache/yt-dlp")
    ytdlp_size = _get_dir_size(ytdlp_cache) if os.path.isdir(ytdlp_cache) else 0
    categories.append({
        "name": "yt-dlp 缓存",
        "description": "视频缩略图和元数据缓存",
        "size": ytdlp_size,
        "count": 1 if ytdlp_size > 0 else 0,
        "key": "ytdlp_cache",
    })

    # 3. Recycle bin
    nas_root = get_nas_root(db)
    recycle_bin = os.path.join(nas_root, ".recycle_bin")
    recycle_size = _get_dir_size(recycle_bin) if os.path.isdir(recycle_bin) else 0
    recycle_count = 0
    try:
        recycle_count = sum(1 for _ in os.scandir(recycle_bin))
    except (PermissionError, OSError):
        pass
    categories.append({
        "name": "回收站 (Recycle Bin)",
        "description": "已删除但未永久清除的文件",
        "size": recycle_size,
        "count": recycle_count,
        "key": "recycle_bin",
    })

    # 4. NAS mount point cache (tmp dirs in /tmp/nas_mnt)
    #    Shallow scan only: the mounts are live CIFS shares, and a full
    #    recursive walk of a network filesystem can block for minutes.
    nas_mnt_base = "/tmp/nas_mnt"
    nas_mnt_size = _get_dir_size_approx(nas_mnt_base) if os.path.isdir(nas_mnt_base) else 0
    categories.append({
        "name": "NAS 挂载点",
        "description": "NAS 挂载缓存目录",
        "size": nas_mnt_size,
        "count": 1 if nas_mnt_size > 0 else 0,
        "key": "nas_mount",
    })

    # 5. NAS credentials cache
    creds_dir = "/tmp/nas_creds"
    creds_size = _get_dir_size(creds_dir) if os.path.isdir(creds_dir) else 0
    categories.append({
        "name": "NAS 凭据缓存",
        "description": "临时存储的 NAS 登录凭据",
        "size": creds_size,
        "count": 1 if creds_size > 0 else 0,
        "key": "nas_creds",
    })

    total_size = sum(c["size"] for c in categories)
    return {"total_size": total_size, "categories": categories}


@router.post("/clear-cache")
async def clear_cache(request: Request, db: Session = Depends(get_db)):
    """Clear all caches and return results per category."""
    results = []
    total_freed = 0

    # 1. Temp zip archives
    freed = 0
    count = 0
    try:
        for entry in os.scandir("/tmp"):
            if entry.is_dir(follow_symlinks=False) and entry.name.startswith("tmp."):
                size = _get_dir_size(entry.path)
                shutil.rmtree(entry.path, ignore_errors=True)
                freed += size
                count += 1
    except (PermissionError, OSError):
        pass
    results.append({"name": "临时文件", "freed": freed, "count": count})
    total_freed += freed

    # 2. yt-dlp cache
    ytdlp_cache = os.path.expanduser("~/.cache/yt-dlp")
    freed = 0
    if os.path.isdir(ytdlp_cache):
        freed = _get_dir_size(ytdlp_cache)
        shutil.rmtree(ytdlp_cache, ignore_errors=True)
    results.append({"name": "yt-dlp 缓存", "freed": freed, "count": 1 if freed > 0 else 0})
    total_freed += freed

    # 3. Recycle bin (clear contents, keep the directory)
    nas_root = get_nas_root(db)
    recycle_bin = os.path.join(nas_root, ".recycle_bin")
    freed = 0
    count = 0
    if os.path.isdir(recycle_bin):
        try:
            for entry in os.scandir(recycle_bin):
                entry_path = os.path.join(recycle_bin, entry.name)
                try:
                    size = _get_dir_size(entry_path) if entry.is_dir() else entry.stat().st_size
                    if entry.is_dir():
                        shutil.rmtree(entry_path, ignore_errors=True)
                    else:
                        os.remove(entry_path)
                    freed += size
                    count += 1
                except (PermissionError, OSError):
                    pass
        except (PermissionError, OSError):
            pass
    results.append({"name": "回收站", "freed": freed, "count": count})
    total_freed += freed

    # 4. NAS mount point cache (clean but don't unmount active mounts)
    nas_mnt_base = "/tmp/nas_mnt"
    freed = 0
    if os.path.isdir(nas_mnt_base):
        for entry in os.scandir(nas_mnt_base):
            if entry.is_dir(follow_symlinks=False):
                if not os.path.ismount(entry.path):
                    size = _get_dir_size(entry.path)
                    shutil.rmtree(entry.path, ignore_errors=True)
                    freed += size
    results.append({"name": "NAS 挂载点缓存", "freed": freed, "count": 1 if freed > 0 else 0})
    total_freed += freed

    # 5. NAS credentials
    creds_dir = "/tmp/nas_creds"
    freed = 0
    if os.path.isdir(creds_dir):
        freed = _get_dir_size(creds_dir)
        shutil.rmtree(creds_dir, ignore_errors=True)
    results.append({"name": "NAS 凭据缓存", "freed": freed, "count": 1 if freed > 0 else 0})
    total_freed += freed

    def _format_size(size_bytes):
        if size_bytes == 0:
            return "0 B"
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        i = 0
        s = float(size_bytes)
        while s >= 1024 and i < len(units) - 1:
            s /= 1024
            i += 1
        return f"{s:.1f} {units[i]}"

    return {
        "success": True,
        "total_freed": total_freed,
        "total_freed_display": _format_size(total_freed),
        "results": [
            {
                "name": r["name"],
                "freed": r["freed"],
                "freed_display": _format_size(r["freed"]),
                "count": r["count"],
            }
            for r in results
        ],
        "message": f"已清理 {_format_size(total_freed)} 缓存空间",
    }


@router.get("/{key}")
async def get_config(key: str, db: Session = Depends(get_db)):
    config = db.query(Config).filter(Config.key == key).first()
    if not config:
        raise HTTPException(status_code=404, detail="Config not found")
    return {"key": config.key, "value": config.value, "description": config.description}


@router.put("/{key}")
async def update_config(key: str, config_update: ConfigUpdate, db: Session = Depends(get_db)):
    config = db.query(Config).filter(Config.key == key).first()
    if not config:
        config = Config(
            key=key,
            value=config_update.value,
            description=config_update.description or f"User setting: {key}",
        )
        db.add(config)
        db.commit()
        db.refresh(config)
        return {"key": config.key, "value": config.value, "description": config.description}

    # Update config values
    setattr(config, 'value', config_update.value)
    if config_update.description:
        setattr(config, 'description', config_update.description)

    db.commit()
    db.refresh(config)
    return {"key": config.key, "value": config.value, "description": config.description}
