"""Automation defaults used by non-interactive media clients."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AutomationPolicy:
    """Defaults for Telegram/Hermes jobs.

    Web callers can continue to pass explicit split modes. Automation uses
    ``auto`` so the service can choose chapter metadata first and silence
    detection as a fallback.
    """

    default_format: str = "mp3"
    default_path: str = "/Music"
    default_split: str = "auto"
    default_keep_original: bool = False
    long_audio_seconds: int = 20 * 60


def choose_split_policy(
    *,
    duration: float | int | None = None,
    title: str = "",
    requested: str | None = None,
    policy: AutomationPolicy | None = None,
) -> str | None:
    """Choose an explicit splitter mode for an automated request.

    ``auto`` remains a policy decision, not a splitter mode: chapter metadata
    is attempted first by the job service, then silence detection can be used
    when chapters are unavailable.
    """
    if requested in {"chapter_info", "silence_detection"}:
        return requested
    if requested is None:
        requested = "auto"
    if requested != "auto":
        raise ValueError(f"Unsupported split policy: {requested}")

    active = policy or AutomationPolicy()
    lowered = title.casefold()
    collection_words = ("mix", "合集", "playlist", "continuous", "full album", "歌单")
    is_collection = any(word in lowered for word in collection_words)
    is_long = duration is not None and float(duration) >= active.long_audio_seconds
    return "chapter_info" if is_collection or is_long else None
