"""Deterministic, filesystem-safe names for media jobs."""

import re


def sanitize_component(value: str, fallback: str = "untitled", limit: int = 180) -> str:
    """Return a safe single filename component.

    This deliberately does not create directories. It removes path separators,
    control characters, reserved punctuation, and traversal-like dot prefixes.
    """
    text = str(value or "").strip()
    text = re.sub(r"[\\/\x00-\x1f\x7f]", "", text)
    text = re.sub(r'[<>:"|?*]', "", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text or text in {".", ".."}:
        return fallback
    return text[:limit].rstrip(" .") or fallback
