"""AI service — rename suggestions and music/podcast tagging.

Wraps the existing :mod:`ai_rename` module (pure HTTP LLM calls) and adds
the tagging logic previously inlined in the files router.
"""
from __future__ import annotations

import os
from typing import Any

from sqlalchemy.orm import Session

from ..db import FileMetadata
from ..utils.files import AUDIO_EXTENSIONS, guess_tag_from_size, resolve_full_path
from .ai_rename import analyze_filenames, call_llm, RenameSuggestion
from .file_service import _update_file_index_tag


def get_rename_suggestions(files: list[dict[str, str]]) -> dict[str, Any]:
    """Ask the LLM for clean 'Song-Artist' names for *files*."""
    if not files:
        return {"success": False, "message": "No files provided"}
    try:
        suggestions = analyze_filenames(files)
        return {
            "success": True,
            "suggestions": [
                {
                    "original_path": s.original_path,
                    "original_name": s.original_name,
                    "suggested_name": s.suggested_name,
                    "confidence": s.confidence,
                    "reason": s.reason,
                }
                for s in suggestions
            ],
        }
    except Exception as exc:
        return {"success": False, "message": str(exc)}


def ai_tag_files(
    nas_root: str,
    files_to_tag: list[dict[str, str]],
    db: Session,
) -> dict[str, Any]:
    """AI-tag selected audio files as 'music' or 'podcast'."""
    if not files_to_tag:
        return {"success": False, "message": "No files provided"}

    tagged: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for info in files_to_tag:
        file_path = info.get("path", "")
        file_name = info.get("name", "")
        full_path = resolve_full_path(nas_root, file_path)
        ext = os.path.splitext(file_name)[1].lower()
        if ext not in AUDIO_EXTENSIONS:
            continue
        try:
            file_size = os.path.getsize(full_path)
            tag = None
            try:
                resp = call_llm(
                    f"Is this file more likely music or a podcast?\n"
                    f"Filename: {file_name}\n"
                    f"Size: {file_size} bytes ({file_size / 1024 / 1024:.1f} MB)\n"
                    f"Reply with ONLY one word: music or podcast",
                    "Reply with exactly one word: music or podcast. Nothing else.",
                )
                tag_raw = resp.strip().lower()
                if "podcast" in tag_raw:
                    tag = "podcast"
                elif "music" in tag_raw:
                    tag = "music"
            except Exception:
                pass
            if not tag:
                tag = guess_tag_from_size(file_size)

            meta = db.query(FileMetadata).filter(
                FileMetadata.file_path == file_path
            ).first()
            if meta:
                meta.tag = tag
            else:
                db.add(FileMetadata(
                    file_path=file_path, file_name=file_name,
                    file_size=file_size, file_type="audio", tag=tag,
                ))
            db.commit()
            _update_file_index_tag(nas_root, file_path, tag)
            tagged.append({"path": file_path, "name": file_name, "tag": tag})
        except Exception as exc:
            errors.append({"path": file_path, "error": str(exc)})

    return {
        "success": True, "tagged": tagged, "errors": errors,
        "message": f"Tagged {len(tagged)} file(s)",
    }