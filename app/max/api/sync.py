"""MAX sync endpoints (API spec §3): sync_status, trigger_backfill, sync_chats."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.max import repo
from app.max.api.common import authorized_or_error
from app.max.schemas import MaxBackfillRequest
from app.max.sync import SessionUnavailable, is_backfill_running

router = APIRouter(prefix="/sync", tags=["max-sync"])


@router.post("/backfill", name="trigger_backfill")
async def trigger_backfill(req: MaxBackfillRequest, request: Request):
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    try:
        result = await session.sync.start_backfill(req.chat_id, req.limit, req.direction, req.days)
    except SessionUnavailable as exc:
        return JSONResponse(status_code=503, content={"detail": f"Session '{session.alias}' not available",
                                                      "state": exc.state})
    if result.get("status") == "error":
        return JSONResponse(status_code=409, content=result)
    return result


@router.post("/chats", name="sync_chats")
async def sync_chats(request: Request):
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    try:
        count = await session.sync.sync_chats()
    except SessionUnavailable as exc:
        return JSONResponse(status_code=503, content={"detail": f"Session '{session.alias}' not available",
                                                      "state": exc.state})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"detail": f"{type(exc).__name__}: {exc}"})
    return {"status": "ok", "chats_synced": count}


@router.get("/status", name="sync_status")
async def sync_status(db: AsyncSession = Depends(get_db)):
    states = []
    for state, title in await repo.get_sync_states(db):
        states.append({
            "chat_id": state.chat_id,
            "chat_title": title,
            "oldest_message_id": state.oldest_message_id,
            "newest_message_id": state.newest_message_id,
            "is_fully_synced": state.is_fully_synced,
            "total_messages_synced": state.total_messages_synced,
            "last_backfill_at": state.last_backfill_at,
            "is_running": is_backfill_running(state.chat_id),
            # MAX-only: time cursors (history is paged by time)
            "oldest_time_ms": state.oldest_time_ms,
            "newest_time_ms": state.newest_time_ms,
            "last_catchup_at": state.last_catchup_at,
        })
    return {"states": states}
