"""
Shared path helpers — single source of truth for NAS root lookup and
NAS-relative path resolution with strict boundary enforcement.

Every endpoint that touches the filesystem must resolve the user-supplied
path through :func:`resolve_within_nas` so that symlinks and `..` segments
cannot escape the configured NAS root.
"""
import os
import re
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .models import Config


def get_nas_root(db: Session) -> str:
    """Return the configured NAS root directory (default "/nas")."""
    config = db.query(Config).filter(Config.key == "nas_root").first()
    return str(config.value) if config else "/nas"


def resolve_within_nas(db: Session, path: str, *, default_root: bool = True) -> str:
    """Resolve a NAS-relative path to an absolute path inside the NAS root.

    The path is interpreted relative to the configured NAS root (a leading
    "/" is treated as the root itself). Both the input and the fully
    resolved path (after following symlinks) must stay inside the NAS root;
    otherwise a 403 HTTPException is raised.

    Args:
        db: SQLAlchemy session (used to read the nas_root config).
        path: NAS-relative path, e.g. "/Media/music/album".
        default_root: if True (default), path "/" resolves to the NAS root.

    Returns:
        The real, absolute filesystem path inside the NAS root.
    """
    nas_root = get_nas_root(db)
    if path is None:
        path = "/"
    path = str(path)

    if default_root and (not path or path == "/"):
        full_path = nas_root
    else:
        full_path = os.path.join(nas_root, path.lstrip("/"))

    real_nas = os.path.realpath(nas_root)
    real_full = os.path.realpath(full_path)
    if real_full != real_nas and not real_full.startswith(real_nas + os.sep):
        raise HTTPException(status_code=403, detail="Access denied: path is outside the NAS root")
    return real_full


def clean_filename(name: str) -> str:
    """Sanitize a user/AI-supplied filename: no path separators, no traversal.

    Returns a bare filename (no directories) with control characters and
    filesystem-forbidden characters removed. Returns "" for empty/dot-only
    results so callers can decide to skip.
    """
    if not name:
        return ""
    # Keep only the final path component (also strips any ../ traversal).
    name = os.path.basename(str(name).strip())
    # Control characters and common filesystem-forbidden characters.
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = re.sub(r'[\\/:*?"<>|]', "", name)
    # Strip leading/trailing dots and spaces (dotfiles / ".." remnants).
    name = name.strip(" .")
    return name


def ensure_save_dir(path: str) -> None:
    """Create the directory if missing (mirrors old inline behavior)."""
    os.makedirs(path, exist_ok=True)
