"""Central non-secret application configuration loaded from config.toml."""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import os
import tomllib


_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"


@dataclass(frozen=True)
class NasSettings:
    address: str
    protocol: str
    share: str
    port: str
    username: str
    root: str


@dataclass(frozen=True)
class MediaSettings:
    default_path: str
    default_format: str
    split_policy: str
    keep_original: bool
    long_audio_seconds: int


@dataclass(frozen=True)
class AppSettings:
    nas: NasSettings
    media: MediaSettings
    service_url: str


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """Load the shared config once per process.

    Credentials are intentionally not represented here. The NAS password stays
    in the local database/credential store and is never read from config.toml.
    Set TSUMUGU_CONFIG to use another non-secret config file in deployments.
    """
    path = Path(os.environ.get("TSUMUGU_CONFIG", str(_CONFIG_PATH))).expanduser()
    with path.open("rb") as config_file:
        data = tomllib.load(config_file)

    nas = data.get("nas", {})
    media = data.get("media", {})
    return AppSettings(
        nas=NasSettings(
            address=str(nas.get("address", "")),
            protocol=str(nas.get("protocol", "smb")),
            share=str(nas.get("share", "")),
            port=str(nas.get("port", "445")),
            username=str(nas.get("username", "")),
            root=str(nas.get("root", "/tmp/nas_mnt/NAS")),
        ),
        media=MediaSettings(
            default_path=str(media.get("default_path", "/Media/music")),
            default_format=str(media.get("default_format", "mp3")),
            split_policy=str(media.get("split_policy", "auto")),
            keep_original=bool(media.get("keep_original", False)),
            long_audio_seconds=int(media.get("long_audio_seconds", 1200)),
        ),
        service_url=str(data.get("service_url", "http://127.0.0.1:8005")),
    )
