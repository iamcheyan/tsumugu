"""Configuration, NAS mount, cache, and dependency management service.

Extracted from ``app/routers/config.py`` and the startup logic in
``app/main.py`` — no FastAPI/HTTP dependencies.
"""
from __future__ import annotations

import glob
import os
import shutil
import socket
import json as _json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ..db import Config, SyncFolder
from .nas_mount import mount_nas, unmount_nas, get_mount_status, _get_mount_point

# ── Defaults seeded on first run ───────────────────────────────────────────

_DEFAULTS: dict[str, tuple[str, str]] = {
    "nas_root": ("/nas", "Root directory for NAS files"),
    "deletion_strategy": ("recycle_bin", "File deletion strategy: recycle_bin or direct_delete"),
    "default_model": ("", "Default AI model for downloads/processing"),
    "nas_address": ("", "NAS IP address or hostname (e.g. 192.168.1.100)"),
    "nas_protocol": ("smb", "Connection protocol: smb or nfs"),
    "nas_share": ("", "Shared folder name (e.g. volume1/music)"),
    "nas_username": ("", "NAS login username"),
    "nas_password": ("", "NAS login password"),
    "nas_port": ("445", "SMB port (default 445) or NFS port (default 2049)"),
}


def get_config_value(db: Session, key: str, default: str = "") -> str:
    row = db.query(Config).filter(Config.key == key).first()
    return str(row.value) if row and row.value is not None else default


def set_config_value(db: Session, key: str, value: str, description: str | None = None) -> None:
    row = db.query(Config).filter(Config.key == key).first()
    if row:
        setattr(row, "value", value)
    else:
        db.add(Config(key=key, value=value, description=description or ""))
    db.commit()


def get_all_configs(db: Session) -> list[dict[str, Any]]:
    return [
        {"key": c.key, "value": c.value, "description": c.description}
        for c in db.query(Config).all()
    ]


def get_nas_root(db: Session) -> str:
    return get_config_value(db, "nas_root", "/nas")


def seed_defaults(db: Session) -> None:
    """Insert default config rows if missing (mirrors the old startup_event)."""
    for key, (default_val, desc) in _DEFAULTS.items():
        existing = db.query(Config).filter(Config.key == key).first()
        if not existing:
            db.add(Config(key=key, value=default_val, description=desc))
    db.commit()


def refresh_dependency_status(db: Session) -> dict[str, str]:
    """Detect yt-dlp / ffmpeg and persist status. Returns the status dict."""
    ytdlp = "installed" if shutil.which("yt-dlp") else "missing"
    ffmpeg = "installed" if shutil.which("ffmpeg") else "missing"
    for key, val in (("ytdlp_status", ytdlp), ("ffmpeg_status", ffmpeg)):
        row = db.query(Config).filter(Config.key == key).first()
        if row:
            setattr(row, "value", val)
        else:
            db.add(Config(key=key, value=val, description=f"{key} status"))
    db.commit()
    return {"yt-dlp": ytdlp, "ffmpeg": ffmpeg}


def auto_mount_nas(db: Session) -> dict[str, Any]:
    """Mount NAS on startup if configured. Updates nas_root on success."""
    address = get_config_value(db, "nas_address")
    share = get_config_value(db, "nas_share")
    if not address or not share:
        return {"success": False, "message": "NAS not configured"}
    result = mount_nas(
        address,
        get_config_value(db, "nas_protocol", "smb"),
        share,
        get_config_value(db, "nas_username"),
        get_config_value(db, "nas_password"),
        get_config_value(db, "nas_port", "445"),
    )
    if result["success"]:
        set_config_value(db, "nas_root", result["mount_point"])
    return result


def ensure_default_sync_folder(db: Session) -> None:
    if db.query(SyncFolder).count() > 0:
        return
    nas_root = get_nas_root(db)
    music_path = os.path.join(nas_root, "Music")
    if os.path.isdir(music_path):
        db.add(SyncFolder(path="/Music", name="Music", enabled=True))
        db.commit()


# ── NAS connection / mount operations ──────────────────────────────────────


def test_connection(address: str, port: str = "445", protocol: str = "smb") -> dict[str, Any]:
    if not address:
        return {"success": False, "message": "Address is required"}
    try:
        p = int(port) if port else (445 if protocol == "smb" else 2049)
        sock = socket.create_connection((address, p), timeout=5)
        sock.close()
        return {"success": True, "message": f"Connection to {address}:{p} successful"}
    except socket.timeout:
        return {"success": False, "message": f"Connection timed out ({address}:{p})"}
    except socket.gaierror:
        return {"success": False, "message": f"Cannot resolve hostname: {address}"}
    except ConnectionRefusedError:
        return {"success": False, "message": f"Connection refused ({address}:{p})"}
    except Exception as exc:
        return {"success": False, "message": f"Connection failed: {str(exc)}"}


def mount_share(db: Session) -> dict[str, Any]:
    address = get_config_value(db, "nas_address")
    if not address:
        return {"success": False, "message": "NAS address required"}
    result = mount_nas(
        address,
        get_config_value(db, "nas_protocol", "smb"),
        get_config_value(db, "nas_share"),
        get_config_value(db, "nas_username"),
        get_config_value(db, "nas_password"),
        get_config_value(db, "nas_port", "445"),
    )
    if result["success"]:
        set_config_value(db, "nas_root", result["mount_point"])
    return result


def unmount_share(db: Session) -> dict[str, Any]:
    return unmount_nas(get_config_value(db, "nas_share"))


def mount_status(db: Session) -> dict[str, Any]:
    return get_mount_status(get_config_value(db, "nas_share"))


# ── AI model selection (reads opencode.json) ───────────────────────────────


def get_available_models() -> dict[str, Any]:
    """Read opencode.json and return available AI models grouped by provider."""
    config_path = os.path.expanduser("~/.config/opencode/opencode.json")
    if not os.path.exists(config_path):
        return {"models": [], "current": ""}
    try:
        with open(config_path, "r") as f:
            config = _json.load(f)
    except Exception:
        return {"models": [], "current": ""}
    current_model = config.get("model", "")
    providers = config.get("provider", {})
    models: list[dict[str, Any]] = []
    for provider_key, provider_data in providers.items():
        provider_name = provider_data.get("name", provider_key)
        has_apikey = bool(provider_data.get("options", {}).get("apiKey", ""))
        for model_id, model_data in provider_data.get("models", {}).items():
            full_id = f"{provider_key}/{model_id}"
            models.append({
                "id": full_id,
                "name": model_data.get("name", model_id),
                "provider": provider_name,
                "provider_key": provider_key,
                "has_apikey": has_apikey,
            })
    return {"models": models, "current": current_model}


# ── Cache management ───────────────────────────────────────────────────────


def _get_dir_size(path: str) -> int:
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


def get_cache_stats(db: Session) -> dict[str, Any]:
    categories: list[dict[str, Any]] = []

    # 1. Temp zip archives
    tmp_items: list[dict[str, Any]] = []
    try:
        for entry in os.scandir("/tmp"):
            if entry.is_dir(follow_symlinks=False) and entry.name.startswith("tmp."):
                size = _get_dir_size(entry.path)
                if size > 0:
                    tmp_items.append({"name": entry.name, "size": size})
    except (PermissionError, OSError):
        pass
    categories.append({
        "name": "临时文件 (Temp Archives)",
        "description": "文件夹下载压缩包等临时文件",
        "size": sum(i["size"] for i in tmp_items),
        "count": len(tmp_items),
        "key": "temp_archives",
    })

    # 2. yt-dlp cache
    ytdlp_cache = os.path.expanduser("~/.cache/yt-dlp")
    ytdlp_size = _get_dir_size(ytdlp_cache) if os.path.isdir(ytdlp_cache) else 0
    categories.append({
        "name": "yt-dlp 缓存", "description": "视频缩略图和元数据缓存",
        "size": ytdlp_size, "count": 1 if ytdlp_size > 0 else 0, "key": "ytdlp_cache",
    })

    # 3. Recycle bin
    nas_root = get_nas_root(db)
    recycle_bin = os.path.join(nas_root, ".recycle_bin")
    recycle_size = _get_dir_size(recycle_bin) if os.path.isdir(recycle_bin) else 0
    try:
        recycle_count = sum(1 for _ in os.scandir(recycle_bin))
    except (PermissionError, OSError):
        recycle_count = 0
    categories.append({
        "name": "回收站 (Recycle Bin)", "description": "已删除但未永久清除的文件",
        "size": recycle_size, "count": recycle_count, "key": "recycle_bin",
    })

    # 4. NAS mount point
    nas_mnt_base = "/tmp/nas_mnt"
    nas_mnt_size = _get_dir_size(nas_mnt_base) if os.path.isdir(nas_mnt_base) else 0
    categories.append({
        "name": "NAS 挂载点", "description": "NAS 挂载缓存目录",
        "size": nas_mnt_size, "count": 1 if nas_mnt_size > 0 else 0, "key": "nas_mount",
    })

    # 5. NAS credentials
    creds_dir = "/tmp/nas_creds"
    creds_size = _get_dir_size(creds_dir) if os.path.isdir(creds_dir) else 0
    categories.append({
        "name": "NAS 凭据缓存", "description": "临时存储的 NAS 登录凭据",
        "size": creds_size, "count": 1 if creds_size > 0 else 0, "key": "nas_creds",
    })

    return {"total_size": sum(c["size"] for c in categories), "categories": categories}


def clear_cache(db: Session) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    total_freed = 0

    # 1. Temp zip archives
    freed = count = 0
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
    freed = _get_dir_size(ytdlp_cache) if os.path.isdir(ytdlp_cache) else 0
    shutil.rmtree(ytdlp_cache, ignore_errors=True)
    results.append({"name": "yt-dlp 缓存", "freed": freed, "count": 1 if freed > 0 else 0})
    total_freed += freed

    # 3. Recycle bin
    nas_root = get_nas_root(db)
    recycle_bin = os.path.join(nas_root, ".recycle_bin")
    freed = count = 0
    if os.path.isdir(recycle_bin):
        try:
            for entry in os.scandir(recycle_bin):
                ep = os.path.join(recycle_bin, entry.name)
                try:
                    size = _get_dir_size(ep) if entry.is_dir() else entry.stat().st_size
                    if entry.is_dir():
                        shutil.rmtree(ep, ignore_errors=True)
                    else:
                        os.remove(ep)
                    freed += size
                    count += 1
                except (PermissionError, OSError):
                    pass
        except (PermissionError, OSError):
            pass
    results.append({"name": "回收站", "freed": freed, "count": count})
    total_freed += freed

    # 4. NAS mount point (clean non-mounted dirs)
    nas_mnt_base = "/tmp/nas_mnt"
    freed = 0
    if os.path.isdir(nas_mnt_base):
        for entry in os.scandir(nas_mnt_base):
            if entry.is_dir(follow_symlinks=False) and not os.path.ismount(entry.path):
                size = _get_dir_size(entry.path)
                shutil.rmtree(entry.path, ignore_errors=True)
                freed += size
    results.append({"name": "NAS 挂载点缓存", "freed": freed, "count": 1 if freed > 0 else 0})
    total_freed += freed

    # 5. NAS credentials
    creds_dir = "/tmp/nas_creds"
    freed = _get_dir_size(creds_dir) if os.path.isdir(creds_dir) else 0
    shutil.rmtree(creds_dir, ignore_errors=True)
    results.append({"name": "NAS 凭据缓存", "freed": freed, "count": 1 if freed > 0 else 0})
    total_freed += freed

    return {
        "success": True,
        "total_freed": total_freed,
        "results": results,
        "message": f"已清理 {_format_size(total_freed)} 缓存空间",
    }


def _format_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    s = float(size_bytes)
    while s >= 1024 and i < len(units) - 1:
        s /= 1024
        i += 1
    return f"{s:.1f} {units[i]}"