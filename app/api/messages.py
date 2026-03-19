import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import MessageResponse, SendMessageRequest, ForwardMessageRequest, PaginatedResponse
from app.services.message_service import get_messages, get_message
from app.telegram.client import tg_bridge

log = logging.getLogger(__name__)
router = APIRouter(prefix="/messages", tags=["messages"])


@router.get("", response_model=PaginatedResponse)
async def list_messages(
    chat_id: int | None = Query(None),
    from_user_id: int | None = Query(None),
    search: str | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    message_type: str | None = Query(None),
    is_outgoing: bool | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    messages, total = await get_messages(
        db,
        chat_id=chat_id,
        from_user_id=from_user_id,
        search=search,
        date_from=date_from,
        date_to=date_to,
        message_type=message_type,
        is_outgoing=is_outgoing,
        limit=limit,
        offset=offset,
    )
    return PaginatedResponse(
        items=[MessageResponse.model_validate(m) for m in messages],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{chat_id}/{message_id}", response_model=MessageResponse)
async def get_single_message(chat_id: int, message_id: int, db: AsyncSession = Depends(get_db)):
    msg = await get_message(db, chat_id, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return MessageResponse.model_validate(msg)


@router.post("")
async def send_message(req: SendMessageRequest):
    client = tg_bridge.client
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=503, detail="Telegram client not authorized")

    try:
        result = await client.send_message(
            entity=req.chat_id,
            message=req.text,
            reply_to=req.reply_to_message_id,
        )
        return {
            "status": "sent",
            "message_id": result.id,
            "chat_id": req.chat_id,
        }
    except Exception as e:
        log.exception("Failed to send message to chat %d", req.chat_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/forward")
async def forward_message(req: ForwardMessageRequest):
    client = tg_bridge.client
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=503, detail="Telegram client not authorized")

    try:
        result = await client.forward_messages(
            entity=req.to_chat_id,
            messages=req.message_id,
            from_peer=req.from_chat_id,
        )
        fwd = result[0] if isinstance(result, list) else result
        return {
            "status": "forwarded",
            "message_id": fwd.id,
            "to_chat_id": req.to_chat_id,
        }
    except Exception as e:
        log.exception("Failed to forward message")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{chat_id}/{message_id}")
async def delete_message(chat_id: int, message_id: int):
    client = tg_bridge.client
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=503, detail="Telegram client not authorized")

    try:
        await client.delete_messages(entity=chat_id, message_ids=[message_id])
        return {"status": "deleted", "chat_id": chat_id, "message_id": message_id}
    except Exception as e:
        log.exception("Failed to delete message %d in chat %d", message_id, chat_id)
        raise HTTPException(status_code=500, detail=str(e))
