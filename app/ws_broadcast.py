"""
Shared WebSocket broadcast helpers for background task managers
(download_manager, compressor).

Each manager keeps its own list of connected websockets; these helpers
serialize a message dict to JSON and push it to every socket, pruning
disconnected ones. The *_sync variant is safe to call from worker threads.
"""
import asyncio
import json
from typing import Any, Dict, List

from fastapi import WebSocket


def build_message(message_type: str, **fields: Any) -> str:
    """Serialize a progress message dict to a JSON string."""
    data: Dict[str, Any] = {"type": message_type}
    data.update(fields)
    return json.dumps(data)


async def broadcast(websockets: List[WebSocket], message: str) -> None:
    """Send a pre-serialized message to all websockets; drop dead ones."""
    if not websockets:
        return
    disconnected = []
    for ws in websockets:
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in websockets:
            websockets.remove(ws)


async def broadcast_serialized(
    websockets: List[WebSocket], message_type: str, **fields: Any
) -> None:
    """Serialize and broadcast (async context)."""
    await broadcast(websockets, build_message(message_type, **fields))


def broadcast_sync(
    websockets: List[WebSocket],
    loop: "asyncio.AbstractEventLoop",
    message: str,
) -> None:
    """Queue a broadcast from a worker thread onto the event loop."""
    if loop and loop.is_running() and websockets:
        try:
            asyncio.run_coroutine_threadsafe(broadcast(websockets, message), loop)
        except RuntimeError:
            pass
