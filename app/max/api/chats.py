"""MAX chats (API spec §3): same paths, names and schemas as app/api/chats.py."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.max import repo
from app.schemas import ChatResponse, ChatUpdateRequest, PaginatedResponse

router = APIRouter(prefix="/chats", tags=["max-chats"])


@router.get("", response_model=PaginatedResponse, name="list_chats")
async def list_chats(
    chat_type: str | None = Query(None, description="Filter by type: private/group/channel"),
    search: str | None = Query(None, description="Search by title or username"),
    is_monitored: bool | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    chats, total = await repo.get_chats(
        db, chat_type=chat_type, search=search, is_monitored=is_monitored, limit=limit, offset=offset
    )
    return PaginatedResponse(
        items=[ChatResponse.model_validate(c) for c in chats], total=total, limit=limit, offset=offset
    )


@router.get("/{chat_id}", response_model=ChatResponse, name="get_chat_detail")
async def get_chat_detail(chat_id: int, db: AsyncSession = Depends(get_db)):
    chat = await repo.get_chat(db, chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return ChatResponse.model_validate(chat)


@router.patch("/{chat_id}", response_model=ChatResponse, name="update_chat_settings")
async def update_chat_settings(chat_id: int, req: ChatUpdateRequest, db: AsyncSession = Depends(get_db)):
    chat = await repo.set_chat_monitored(db, chat_id, req.is_monitored)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return ChatResponse.model_validate(chat)
