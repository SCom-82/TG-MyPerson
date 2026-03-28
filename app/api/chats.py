import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import (
    ChatResponse, ChatUpdateRequest, PaginatedResponse,
    JoinChatRequest, LeaveChatRequest, ResolveRequest, ResolveResponse,
    MemberResponse, ArchiveRequest,
)
from app.services.chat_service import get_chats, get_chat, update_chat, join_chat, leave_chat, resolve_target, get_members
from app.telegram.client import tg_bridge

log = logging.getLogger(__name__)

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


# --- Конкретные пути ПЕРЕД /{chat_id} чтобы FastAPI не матчил "join" как int ---

@router.post("/join", response_model=ChatResponse)
async def join_chat_endpoint(req: JoinChatRequest, db: AsyncSession = Depends(get_db)):
    client = tg_bridge.client
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=503, detail="Telegram client not authorized")
    try:
        db_chat = await join_chat(db, client, req.target)
        return ChatResponse.model_validate(db_chat)
    except Exception as e:
        log.exception("Failed to join chat: %s", req.target)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/leave")
async def leave_chat_endpoint(req: LeaveChatRequest):
    client = tg_bridge.client
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=503, detail="Telegram client not authorized")
    try:
        await leave_chat(client, req.chat_id)
        return {"status": "left", "chat_id": req.chat_id}
    except Exception as e:
        log.exception("Failed to leave chat %d", req.chat_id)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/resolve", response_model=ResolveResponse)
async def resolve_chat_endpoint(req: ResolveRequest):
    client = tg_bridge.client
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=503, detail="Telegram client not authorized")
    try:
        info = await resolve_target(client, req.target)
        return ResolveResponse(**info)
    except Exception as e:
        log.exception("Failed to resolve: %s", req.target)
        raise HTTPException(status_code=400, detail=str(e))


# --- Параметризованные пути ---

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


@router.get("/{chat_id}/members")
async def list_members(
    chat_id: int,
    search: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
):
    client = tg_bridge.client
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=503, detail="Telegram client not authorized")
    try:
        members = await get_members(client, chat_id, search=search, limit=limit)
        return {"items": [MemberResponse(**m) for m in members], "total": len(members)}
    except Exception as e:
        log.exception("Failed to get members for chat %d", chat_id)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{chat_id}/read")
async def mark_read(chat_id: int):
    client = tg_bridge.client
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=503, detail="Telegram client not authorized")
    try:
        await client.send_read_acknowledge(entity=chat_id)
        return {"status": "read", "chat_id": chat_id}
    except Exception as e:
        log.exception("Failed to mark chat %d as read", chat_id)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{chat_id}/archive")
async def archive_chat(chat_id: int, req: ArchiveRequest):
    client = tg_bridge.client
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=503, detail="Telegram client not authorized")
    try:
        await client.edit_folder(entity=chat_id, folder=1 if req.archived else 0)
        return {"status": "archived" if req.archived else "unarchived", "chat_id": chat_id}
    except Exception as e:
        log.exception("Failed to archive/unarchive chat %d", chat_id)
        raise HTTPException(status_code=400, detail=str(e))
