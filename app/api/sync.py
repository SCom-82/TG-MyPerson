import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import BackfillRequest
from app.services.backfill_service import start_backfill, get_sync_states
from app.services.chat_service import sync_chat_list
from app.telegram.pool import require_authorized_client

log = logging.getLogger(__name__)
router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("/backfill")
async def trigger_backfill(req: BackfillRequest, request: Request):
    alias = getattr(request.state, "session_alias", "work")
    result = await start_backfill(
        req.chat_id, req.limit, alias=alias, direction=req.direction
    )
    if result.get("status") == "error":
        raise HTTPException(status_code=503, detail=result.get("detail"))
    return result


@router.get("/status")
async def sync_status(db: AsyncSession = Depends(get_db)):
    states = await get_sync_states(db)
    return {"states": states}


@router.post("/chats")
async def sync_chats(request: Request, db: AsyncSession = Depends(get_db)):
    client = await require_authorized_client(request)
    count = await sync_chat_list(db, client)
    return {"status": "ok", "chats_synced": count}
