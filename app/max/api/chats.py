"""MAX chats (API spec §3): same paths, names and schemas as app/api/chats.py."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.write_limits import ACTION, write_slot
from app.database import get_db
from app.api import data_reads
from app.max import repo
from app.max.api.common import authorized_or_error, call_max
from app.services.tg_store import new_mode, shadow
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
    request: Request = None,
):
    if new_mode():
        return await data_reads.list_chats(
            request, chat_type=chat_type, search=search, is_monitored=is_monitored, limit=limit, offset=offset
        )
    chats, total = await repo.get_chats(
        db, chat_type=chat_type, search=search, is_monitored=is_monitored, limit=limit, offset=offset
    )
    return PaginatedResponse(
        items=[ChatResponse.model_validate(c) for c in chats], total=total, limit=limit, offset=offset
    )


@router.get("/{chat_id}", response_model=ChatResponse, name="get_chat_detail")
async def get_chat_detail(chat_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if new_mode():
        return await data_reads.get_chat(request, chat_id)
    chat = await repo.get_chat(db, chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return ChatResponse.model_validate(chat)


@router.patch("/{chat_id}", response_model=ChatResponse, name="update_chat_settings")
async def update_chat_settings(
    chat_id: int, req: ChatUpdateRequest, request: Request, db: AsyncSession = Depends(get_db)
):
    if new_mode():
        result = await data_reads.set_monitored(request, chat_id, req.is_monitored)
        await shadow("max update_chat", lambda: repo.set_chat_monitored(db, chat_id, req.is_monitored))
        return result
    chat = await repo.set_chat_monitored(db, chat_id, req.is_monitored)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return ChatResponse.model_validate(chat)


@router.post("/{chat_id}/read", name="mark_read")
async def mark_read(chat_id: int, request: Request):
    """Read up to the chat's last message (MAX marks a message, not a chat).

    The last message comes from MAX (history, 1 message). Its id goes as a
    number on both transports: MAX rejects a string id ("proto.payload Expected
    number") and drops the connection (ticket 4b4ffe3c; PyMax 2.4.1 documents
    str for web, upstream dev/2.5.0 sends int).
    """
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    history = await call_max(session.client.fetch_history(chat_id, backward=1))
    if not history:
        raise HTTPException(status_code=404, detail="Chat has no messages")
    last = max(history, key=lambda m: (m.time or 0, m.id))
    message_id = int(last.id)
    async with write_slot(request, ACTION, "mark_read", chat_id):
        await call_max(session.client.read_message(message_id, chat_id))
    return {"status": "read", "chat_id": chat_id, "message_id": last.id}
