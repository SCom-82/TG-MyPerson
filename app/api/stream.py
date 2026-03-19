import asyncio
import logging

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from app.services.stream_service import stream_manager

log = logging.getLogger(__name__)
router = APIRouter(prefix="/stream", tags=["stream"])


@router.get("/messages")
async def stream_messages():
    """SSE endpoint for real-time message streaming."""

    async def event_generator():
        queue = stream_manager.subscribe()
        try:
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield {"event": "message", "data": data}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}
        except asyncio.CancelledError:
            pass
        finally:
            stream_manager.unsubscribe(queue)

    return EventSourceResponse(event_generator())
