from .config import router as config_router
from .files import router as files_router
from .youtube import router as youtube_router
from .audio import router as audio_router
from .sync import router as sync_router

__all__ = ["config_router", "files_router", "youtube_router", "audio_router", "sync_router"]