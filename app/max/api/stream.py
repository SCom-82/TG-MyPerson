"""MAX stream_messages (API spec §3): SSE of MAX events only (separate manager)."""

import asyncio

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from app.max.stream import max_stream_manager
from app.services.stream_service import stream_manager
from app.services.tg_store import new_mode

router = APIRouter(prefix="/stream", tags=["max-stream"])


@router.get("/messages", name="stream_messages")
async def stream_messages(request: Request):
    # STORAGE_MODE=new: one manager scoped by account (ТЗ §5.5) instead of a MAX-only one.
    manager = stream_manager if new_mode() else max_stream_manager
    account_id = getattr(request.state, "account_id", None) if new_mode() else None

    async def event_generator():
        queue = manager.subscribe(account_id)
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
            manager.unsubscribe(queue)

    return EventSourceResponse(event_generator())
