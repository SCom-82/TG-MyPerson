"""MAX messages (API spec §3): list_messages, get_single_message. download_media — PR-5."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.max import repo
from app.max.schemas import MaxMessageResponse
from app.schemas import PaginatedResponse

router = APIRouter(prefix="/messages", tags=["max-messages"])


@router.get("", response_model=PaginatedResponse, name="list_messages")
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
    # Deleted messages are listed too (with deleted_at), like TG keeps them (ADR §2.G).
    messages, total = await repo.get_messages(
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
        items=[MaxMessageResponse.model_validate(m) for m in messages], total=total, limit=limit, offset=offset
    )


@router.get("/{chat_id}/{message_id}", response_model=MaxMessageResponse, name="get_single_message")
async def get_single_message(chat_id: int, message_id: int, db: AsyncSession = Depends(get_db)):
    msg = await repo.get_message(db, chat_id, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return MaxMessageResponse.model_validate(msg)
