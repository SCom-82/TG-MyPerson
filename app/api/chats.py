from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import ChatResponse, ChatUpdateRequest, PaginatedResponse
from app.services.chat_service import get_chats, get_chat, update_chat

router = APIRouter(prefix="/chats", tags=["chats"])


@router.get("", response_model=PaginatedResponse)
async def list_chats(
    chat_type: str | None = Query(None, description="Filter by type: private/group/supergroup/channel"),
    search: str | None = Query(None, description="Search by title or username"),
    is_monitored: bool | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    chats, total = await get_chats(db, chat_type=chat_type, search=search, is_monitored=is_monitored, limit=limit, offset=offset)
    return PaginatedResponse(
        items=[ChatResponse.model_validate(c) for c in chats],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{chat_id}", response_model=ChatResponse)
async def get_chat_detail(chat_id: int, db: AsyncSession = Depends(get_db)):
    chat = await get_chat(db, chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return ChatResponse.model_validate(chat)


@router.patch("/{chat_id}", response_model=ChatResponse)
async def update_chat_settings(chat_id: int, req: ChatUpdateRequest, db: AsyncSession = Depends(get_db)):
    chat = await update_chat(db, chat_id, req.is_monitored)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return ChatResponse.model_validate(chat)
