import asyncio
import logging

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from app.services import tg_store
from app.services.stream_service import stream_manager

log = logging.getLogger(__name__)
router = APIRouter(prefix="/stream", tags=["stream"])


@router.get("/messages")
async def stream_messages(request: Request):
    """SSE endpoint for real-time message streaming.

    STORAGE_MODE=new: only events of the request's account.
    """
    account_id = getattr(request.state, "account_id", None) if tg_store.new_mode() else None

    async def event_generator():
        queue = stream_manager.subscribe(account_id)
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
