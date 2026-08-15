"""Shared file-system utilities — path safety, type detection, name cleaning.

Extracted from the original ``app/routers/files.py`` with no Web dependencies.
"""
from __future__ import annotations

import datetime
import os
import re
from typing import Any

AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac", ".wma", ".opus",
}

_AUDIO_EXT = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac", ".wma"}
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp", ".tiff"}
_VIDEO_EXT = {".mp4", ".avi", ".mkv", ".mov", ".webm"}
_ALL_KNOWN = _AUDIO_EXT | _IMAGE_EXT | _VIDEO_EXT | {".opus"}


def resolve_full_path(nas_root: str, path: str) -> str:
    """Translate a NAS-relative *path* (``/`` = root) to a full filesystem path."""
    if path == "/" or not path:
        return nas_root
    return os.path.join(nas_root, path.lstrip("/"))


def is_within_nas(nas_root: str, full_path: str) -> bool:
    """Security: ensure *full_path* resolves under *nas_root*."""
    real_nas = os.path.realpath(nas_root)
    real_full = os.path.realpath(full_path)
    return real_full == real_nas or real_full.startswith(real_nas + os.sep)


def filesizeformat(value: int | float) -> str:
    """Format file size in human readable form."""
    if value == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(value)
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.1f} {units[i]}"


def detect_file_type(name: str, full_path: str) -> str:
    """Classify an entry as ``folder``/``audio``/``video``/``generic``."""
    if os.path.isdir(full_path):
        return "folder"
    lower = name.lower()
    if lower.endswith(tuple(AUDIO_EXTENSIONS)):
        return "audio"
    if lower.endswith(tuple(_VIDEO_EXT)):
        return "video"
    return "generic"


def format_modified(mtime: float) -> str:
    return datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")


def relative_path(path: str, item: str) -> str:
    """Build the NAS-relative path for *item* inside directory *path*."""
    if path == "/":
        return f"/{item}"
    return f"{path.rstrip('/')}/{item}"


# ── Filename cleaning & extension detection ────────────────────────────────


def clean_filename(name: str) -> str:
    """Remove problematic characters from a filename (keeps CJK, letters, etc.)."""
    result = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", name)
    result = re.sub(r'[\\/:*?"<>|]', "", result)
    result = re.sub(
        r"[^\w\s.\-()（）　-〿぀-ゟ゠-ヿ一-鿿"
        r"가-힯㐀-䶿豈-﫿,]+",
        "",
        result,
        flags=re.UNICODE,
    )
    result = re.sub(r"\s+", " ", result)
    return result.strip(" .")


def has_extension(filename: str) -> bool:
    _, ext = os.path.splitext(filename)
    return ext.lower() in _ALL_KNOWN


def detect_extension(filepath: str) -> str | None:
    """Detect file type by reading magic bytes. Returns the extension or ``None``."""
    try:
        with open(filepath, "rb") as f:
            header = f.read(16)
        if len(header) < 4:
            return None
        if len(header) >= 8 and header[4:8] == b"ftyp":
            brand = header[8:12] if len(header) >= 12 else b""
            if brand in (b"M4A ", b"mp4a"):
                return ".m4a"
            if brand in (b"qt  ",):
                return ".mov"
            return ".mp4"
        if header[:4] == b"RIFF" and len(header) >= 12:
            sub = header[8:12]
            if sub == b"WAVE":
                return ".wav"
            if sub == b"WEBP":
                return ".webp"
            return ".wav"
        if header[:4] == b"OggS":
            return ".ogg"
        if header[:4] == b"fLaC":
            return ".flac"
        if header[:3] == b"ID3":
            return ".mp3"
        if header[0] == 0xFF and (header[1] & 0xE0) == 0xE0:
            return ".mp3"
        if header[:4] == b"MThd":
            return ".mid"
        if header[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if header[:3] == b"\xff\xd8\xff":
            return ".jpg"
        if header[:6] in (b"GIF87a", b"GIF89a"):
            return ".gif"
        if header[:2] == b"PK":
            return ".zip"
        if header[:2] == b"\x1f\x8b":
            return ".gz"
    except (OSError, IOError):
        pass
    return None


def has_subdirectories(path: str) -> bool:
    try:
        for item in os.listdir(path):
            if os.path.isdir(os.path.join(path, item)):
                return True
    except PermissionError:
        pass
    return False


def guess_tag_from_size(file_size: int) -> str:
    """Heuristic: small audio = music, large audio = podcast."""
    if file_size < 15 * 1024 * 1024:  # < 15 MB
        return "music"
    return "podcast"


def sort_key(field: str, order: str, f: dict[str, Any]) -> Any:
    ft = str(f["type"])
    fn = str(f["name"])
    fs = int(f["size"])
    fm = str(f["modified"])
    if field == "name":
        key = (ft != "folder", fn.lower())
    elif field == "size":
        key = (ft != "folder", fs)
    elif field == "modified":
        key = (ft != "folder", fm)
    elif field == "type":
        key = (ft, fn.lower())
    else:
        key = (ft != "folder", fn.lower())
    return key