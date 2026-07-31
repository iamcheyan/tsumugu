"""Initialize download + compressor singletons in the running event loop."""
from __future__ import annotations

import asyncio

from ..services.download_service import download_manager
from ..services.compress_service import compressor


def init_services(app) -> None:
    """Start the background download worker and bind the compressor's loop."""
    loop = asyncio.get_event_loop()
    compressor.set_loop(loop)
    # download_manager.start() is async — schedule it
    loop.create_task(download_manager.start())