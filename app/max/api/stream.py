"""MAX stream_messages (API spec §3): SSE of MAX events only (separate manager)."""

import asyncio

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from app.max.stream import max_stream_manager

router = APIRouter(prefix="/stream", tags=["max-stream"])


@router.get("/messages", name="stream_messages")
async def stream_messages():
    async def event_generator():
        queue = max_stream_manager.subscribe()
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
            max_stream_manager.unsubscribe(queue)

    return EventSourceResponse(event_generator())
