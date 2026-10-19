"""MAX chats (API spec §3): same paths, names and schemas as app/api/chats.py."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.write_limits import ACTION, write_slot
from app.database import get_db
from app.api import data_reads
from app.max import groups, normalize, repo
from app.max.api import groups as mx_groups
from app.max.api.common import authorized_or_error, call_max, call_max_read
from app.services.tg_store import new_mode, shadow
from app.schemas import (
    ChatResponse,
    ChatUpdateRequest,
    LeaveChatRequest,
    MemberResponse,
    MyRightsResponse,
    PaginatedResponse,
)

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
    # The slot first (QA PR-7 W-D3): a refused write does not touch MAX at all. A
    # failed history read or an empty chat gives the slot back.
    async with write_slot(request, ACTION, "mark_read", chat_id):
        history = await call_max_read(session.client.fetch_history(chat_id, backward=1))
        if not history:
            raise HTTPException(status_code=404, detail="Chat has no messages")
        last = max(history, key=lambda m: (m.time or 0, m.id))
        await call_max(session.client.read_message(int(last.id), chat_id))
    return {"status": "read", "chat_id": chat_id, "message_id": last.id}


# ---------------------------------------------------------------------------
# Members, leave, my rights (plan 2026-10-19 Р3 Р-29): same paths, names and
# answers as the TG routes; groups and channels only.
# ---------------------------------------------------------------------------

def _role(chat, user_id: int) -> str:
    if chat.owner == user_id:
        return "creator"
    return "admin" if user_id in (chat.admins or []) else "member"


@router.get("/{chat_id}/members", name="list_members")
async def list_members(
    chat_id: int,
    request: Request,
    search: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
):
    """Live from MAX, paging by marker up to limit; search by name/username on our side."""
    await mx_groups.known_chat(request, chat_id, kinds=("group", "channel"))
    session = await mx_groups.connected(request)
    chat = await call_max_read(groups.fresh_chat(session.client, chat_id))
    found = await call_max_read(groups.members(session.client, chat_id, limit))
    users = [m.contact for m in found if getattr(m, "contact", None) is not None]
    await mx_groups.store_members(session, users)
    items = []
    for u in users:
        row = normalize.normalize_user(u)
        item = MemberResponse(user_id=row["id"], username=row["username"], first_name=row["first_name"],
                              last_name=row["last_name"], role=_role(chat, row["id"]))
        text = " ".join(filter(None, (item.first_name, item.last_name, item.username))).lower()
        if search is None or search.lower() in text:
            items.append(item)
    return {"items": items, "total": len(items)}


@router.post("/leave", name="leave_chat_endpoint")
async def leave_chat_endpoint(req: LeaveChatRequest, request: Request):
    chat = await mx_groups.known_chat(request, req.chat_id, kinds=("group", "channel"))
    session = await mx_groups.connected(request)
    async with write_slot(request, ACTION, "leave_chat_endpoint", req.chat_id):
        await call_max(groups.leave(session.client, req.chat_id, chat.chat_type), mx_groups.UNCERTAIN)
    await mx_groups.unmonitor(session, req.chat_id)
    return {"status": "left", "chat_id": req.chat_id}


@router.get("/{chat_id}/my_rights", response_model=MyRightsResponse, name="get_my_rights")
async def get_my_rights(chat_id: int, request: Request):
    """creator / admin from owner / admins; an admin's rights from admin_participants[me] by the bit mask."""
    await mx_groups.known_chat(request, chat_id, kinds=("group", "channel"))
    session = await mx_groups.connected(request)
    chat = await call_max_read(groups.fresh_chat(session.client, chat_id))
    await mx_groups.store_chat(session, chat)
    me = mx_groups.me_of(session)
    creator = me is not None and chat.owner == me
    admin = creator or me in (chat.admins or [])
    raw = (chat.admin_participants or {}).get(me) if me is not None else None
    mask = raw.get("permissions") if isinstance(raw, dict) else None
    names = set(groups.PERMISSIONS) if creator else set(groups.permission_names(mask))
    return MyRightsResponse(
        chat_id=chat_id,
        is_member=creator or me in {int(p) for p in (chat.participants or {})},
        is_admin=admin,
        is_creator=creator,
        can_post_messages=True,
        can_edit_messages=admin,
        can_delete_messages="delete_message" in names,
        can_pin_messages="pin_message" in names or bool((chat.options or {}).get("ALL_CAN_PIN_MESSAGE")),
        can_add_admins="add_admin" in names,
        can_invite_users="add_remove_member" in names or not (chat.options or {}).get("ONLY_ADMIN_CAN_ADD_MEMBER"),
        can_change_info="change_chat_info" in names,
        raw=raw if isinstance(raw, dict) else None,
    )
