import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import BackfillRequest
from app.services.backfill_service import start_backfill, get_sync_states
from app.services.chat_service import sync_chat_list
from app.telegram.client import tg_bridge

log = logging.getLogger(__name__)
router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("/backfill")
async def trigger_backfill(req: BackfillRequest):
    result = await start_backfill(req.chat_id, req.limit)
    if result.get("status") == "error":
        raise HTTPException(status_code=503, detail=result.get("detail"))
    return result


@router.get("/status")
async def sync_status(db: AsyncSession = Depends(get_db)):
    states = await get_sync_states(db)
    return {"states": states}


@router.post("/chats")
async def sync_chats(db: AsyncSession = Depends(get_db)):
    client = tg_bridge.client
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=503, detail="Telegram client not authorized")

    count = await sync_chat_list(db, client)
    return {"status": "ok", "chats_synced": count}
