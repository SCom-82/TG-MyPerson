"""MAX chats (API spec §3): same paths, names and schemas as app/api/chats.py.

Release 4 (plan 2026-10-19 Р4 Р-42, Р-48): join_chat / resolve_chat by a MAX
link (join/<code> or a public https://max.ru/<name>), MAX-only delete_dialog.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.write_limits import ACTION, CHAT_JOIN, bundle_slot, write_slot
from app.database import get_db
from app.api import data_reads
from app.max import frames, groups, normalize, repo
from app.max.api import groups as mx_groups
from app.max.api.common import authorized_or_error, call_max, call_max_read
from app.max.schemas import MaxChatResponse, MaxMemberResponse, MaxMyRightsResponse
from app.repo import chats as chats_repo
from app.services.tg_store import new_mode, shadow
from app.storage import account_db
from app.schemas import (
    ChatResponse,
    ChatUpdateRequest,
    JoinChatRequest,
    LeaveChatRequest,
    PaginatedResponse,
    ResolveRequest,
    ResolveResponse,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/chats", tags=["max-chats"])


# ---------------------------------------------------------------------------
# Join / resolve by a link (Р-48) — declared before /{chat_id}
# ---------------------------------------------------------------------------

_SITE = re.compile(r"^(?:https?://)?(?:www\.)?max\.ru/(?P<path>[^?#\s]+?)/?(?:[?#].*)?$", re.IGNORECASE)
_CODE = re.compile(r"^[A-Za-z0-9_-]+$")
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
LINK_FORMS = "https://max.ru/join/<code>, join/<code>, https://max.ru/<name> or @name"
ALREADY_MEMBER_NOTE = "already_member comes from the stored chat; if the chat is not visible, tell the operator"


def parse_target(target: str) -> tuple[str, str]:
    """('join', 'join/<code>') or ('public', 'https://max.ru/<name>'); anything else → 422."""
    t = target.strip()
    if t.startswith("@") and _NAME.fullmatch(t[1:]):
        return "public", f"https://max.ru/{t[1:]}"
    m = _SITE.match(t)
    path = m["path"] if m else (t if t.lower().startswith("join/") else None)
    if path is not None:
        if path.lower().startswith("join/") and _CODE.fullmatch(path[5:]):
            return "join", f"join/{path[5:]}"
        if m and _NAME.fullmatch(path):
            return "public", f"https://max.ru/{path}"
    raise HTTPException(status_code=422, detail=f"target: {LINK_FORMS}")


async def _resolve(client: Any, kind: str, link: str) -> Any:
    if kind == "join":
        return await client.resolve_group_by_link(link)
    return await frames.link_info(client, link)


async def _is_joined(request: Request, session, chat: Any) -> bool:
    """MAX's participants decide when the answer has them (QA-R4 D3: a stored row outlives
    a leave); without them — the account holds the chat with a status not LEFT/REMOVED/…"""
    me = mx_groups.me_of(session)
    participants = getattr(chat, "participants", None) or {}
    if participants and me is not None:
        return me in {int(p) for p in participants}
    if not new_mode():
        return False
    account_id = getattr(request.state, "account_id", None)
    async with account_db(account_id) as db:
        row = await chats_repo.get_chat(db, account_id, chat.id)
    if row is None:
        return False
    return (row.raw or {}).get("status") not in chats_repo.INACTIVE_STATUSES


async def _chat_answer(request: Request, session, chat: Any) -> dict:
    """ChatResponse as the TG join answers: the stored row, else from the chat itself."""
    if new_mode():
        try:
            return (await data_reads.get_chat(request, chat.id)).model_dump(mode="json")
        except HTTPException:
            log.warning("max[%s]: joined chat %s is not stored", session.alias, chat.id)
    row = normalize.normalize_chat(chat)
    now = datetime.now(timezone.utc).isoformat()
    return {"id": chat.id, "chat_type": row["chat_type"], "title": row["title"], "username": row["username"],
            "description": getattr(chat, "description", None), "members_count": getattr(chat, "participants_count", None),
            "is_monitored": True, "last_message_id": None, "last_message_at": None, "created_at": now, "updated_at": now}


@router.post("/join", name="join_chat_endpoint")
async def join_chat_endpoint(req: JoinChatRequest, request: Request):
    """Join by a link (chats_joined_per_day, 5 a day). Already a member → 200
    already_member=true: no join frame, no slot. already_member may come from the stored
    chat (rows without raw.status count as active, review 17 R17-6): the answer says so."""
    kind, link = parse_target(req.target)
    session = await mx_groups.connected(request)
    try:
        seen = await call_max_read(_resolve(session.client, kind, link))
    except HTTPException as exc:  # the join itself says what is wrong with the link
        log.info("max[%s]: link not resolved before join: %s", session.alias, exc.detail)
        seen = None
    if seen is not None and await _is_joined(request, session, seen):
        await mx_groups.store_chat(session, seen)
        return {**await _chat_answer(request, session, seen), "already_member": True, "note": ALREADY_MEMBER_NOTE}
    async with bundle_slot(request, "join_chat_endpoint", {CHAT_JOIN: 1}):
        join = session.client.join_group(link) if kind == "join" else session.client.join_channel(link)
        chat = await call_max(join, "the join may have been applied, check list_chats")
    await mx_groups.store_chat(session, chat)
    return {**await _chat_answer(request, session, chat), "already_member": False}


@router.post("/resolve", response_model=ResolveResponse, name="resolve_chat_endpoint")
async def resolve_chat_endpoint(req: ResolveRequest, request: Request):
    """What a link points to, without joining (a read: ro accounts too)."""
    kind, link = parse_target(req.target)
    session = await mx_groups.connected(request)
    chat = await call_max_read(_resolve(session.client, kind, link))
    if chat is None:
        raise HTTPException(status_code=404, detail="MAX does not know this link")
    return ResolveResponse(
        id=chat.id,
        type=normalize.normalize_chat(chat)["chat_type"],
        title=chat.title,
        username=link.rsplit("/", 1)[1] if kind == "public" else None,
        members_count=chat.participants_count or len(chat.participants or {}) or None,
        description=chat.description,
        is_joined=await _is_joined(request, session, chat),
    )


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


@router.get("/{chat_id}", response_model=MaxChatResponse, name="get_chat_detail")
async def get_chat_detail(chat_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """+ pinned_message (Р4.1), access and options (Р5 Р-56): from the stored chat, kept fresh by
    pin / unpin, group changes and chat frames."""
    if new_mode():
        chat = await data_reads.get_chat(request, chat_id)
        account_id = getattr(request.state, "account_id", None)
        async with account_db(account_id) as adb:
            row = await chats_repo.get_chat(adb, account_id, chat_id)
        return MaxChatResponse(**chat.model_dump(), **MaxChatResponse.extras(row.raw if row else None))
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
    search: str | None = Query(None, min_length=1),
    role: Literal["all", "admins"] = Query("all"),
    limit: int = Query(200, ge=1, le=1000),
):
    """Live from MAX, paging by marker up to limit. role=admins — MAX's admin list (59 type ADMIN),
    search — MAX's own search (59 query, Р-56). An admin's item carries its rights from the chat."""
    chat_row = await mx_groups.known_chat(request, chat_id, kinds=("group", "channel"))
    session = await mx_groups.connected(request)
    chat = await call_max_read(groups.fresh_chat(session.client, chat_id))
    if role == "admins" or search:
        kind = "ADMIN" if role == "admins" else "MEMBER"
        found = await call_max_read(groups.find_members(session.client, chat_id, limit, kind=kind, query=search))
    else:
        found = await call_max_read(groups.members(session.client, chat_id, limit))
    users = [m.contact for m in found if getattr(m, "contact", None) is not None]
    await mx_groups.store_members(session, users)
    channel = chat_row.chat_type == "channel"
    items = []
    for u in users:
        row = normalize.normalize_user(u)
        item = MaxMemberResponse(user_id=row["id"], username=row["username"], first_name=row["first_name"],
                                 last_name=row["last_name"], role=_role(chat, row["id"]), is_bot=row["is_bot"])
        if item.role != "member":
            mask = groups.admin_mask(chat, item.user_id)
            item.mask = mask  # None: MAX did not tell this admin's rights (review 18 R18-3)
            item.permissions = groups.permission_names(mask, channel=channel) if mask is not None else None
            item.inviter_id = groups.admin_inviter(chat, item.user_id)
        items.append(item)
    return {"items": items, "total": len(items)}


@router.post("/leave", name="leave_chat_endpoint")
async def leave_chat_endpoint(req: LeaveChatRequest, request: Request):
    chat = await mx_groups.known_chat(request, req.chat_id, kinds=("group", "channel"))
    session = await mx_groups.connected(request)
    async with write_slot(request, ACTION, "leave_chat_endpoint", req.chat_id):
        await call_max(groups.leave(session.client, req.chat_id, chat.chat_type), mx_groups.UNCERTAIN)
    await mx_groups.mark_left(session, req.chat_id)
    return {"status": "left", "chat_id": req.chat_id}


@router.get("/{chat_id}/my_rights", response_model=MaxMyRightsResponse, name="get_my_rights")
async def get_my_rights(chat_id: int, request: Request):
    """creator / admin from owner / admins; an admin's rights from admin_participants[me] by the
    client's 12 bits (Р5 Р-56): delete in a group is bit 1, in a channel 1024 (or 1). The owner — 4095."""
    await mx_groups.known_chat(request, chat_id, kinds=("group", "channel"))
    session = await mx_groups.connected(request)
    chat = await call_max_read(groups.fresh_chat(session.client, chat_id))
    await mx_groups.store_chat(session, chat)
    me = mx_groups.me_of(session)
    creator = me is not None and chat.owner == me
    admin = creator or me in (chat.admins or [])
    raw = (chat.admin_participants or {}).get(me) if me is not None else None
    options = chat.options if isinstance(chat.options, dict) else {}  # PyMax annotates dict | int
    channel = groups.is_channel(chat)
    mask = groups.admin_mask(chat, me) if admin and me is not None else None
    bits = mask or 0
    return MaxMyRightsResponse(
        chat_id=chat_id,
        is_member=creator or me in {int(p) for p in (chat.participants or {})},
        is_admin=admin,
        is_creator=creator,
        can_post_messages=_channel_bit(creator, admin, mask, 256) if channel else True,  # review 15 R15-2
        can_edit_messages=_channel_bit(creator, admin, mask, 512) if channel else admin,
        can_delete_messages=bool(bits & ((1024 | 1) if channel else 1)),
        can_pin_messages=bool(bits & 16) or bool(options.get("ALL_CAN_PIN_MESSAGE")),
        can_add_admins=bool(bits & 4),
        can_invite_users=bool(bits & 2) or not options.get("ONLY_ADMIN_CAN_ADD_MEMBER"),
        can_change_info=bool(bits & 8),
        raw=raw if isinstance(raw, dict) else None,
        mask=mask,
        permissions=groups.permission_names(mask, channel=channel) if mask is not None else None,
        can_read_all_messages=bool(bits & 32),
        can_change_invite_link=bool(bits & 128),
        can_call=bool(bits & 64) or (not channel and not options.get("ONLY_ADMIN_CAN_CALL")),
        can_view_stats=bool(bits & 2048),
    )


def _channel_bit(creator: bool, admin: bool, mask: int | None, bit: int) -> bool:
    """A channel right by the client's bits (review 18 R18-4): bit or the spare bit 1; an admin whose
    mask MAX did not send — as before, by being an admin."""
    if creator:
        return True
    if mask is None:
        return admin
    return bool(mask & (bit | 1))


# ---------------------------------------------------------------------------
# Delete a private dialog (Р-42), MAX-only
# ---------------------------------------------------------------------------

class DeleteDialogRequest(BaseModel):
    confirm_user_id: int
    for_all: bool = False


@router.post("/{chat_id}/delete", name="delete_dialog")
async def delete_dialog(chat_id: int, req: DeleteDialogRequest, request: Request):
    """A private dialog only, confirmed by the peer's id (the dialog id is me XOR peer);
    for_all=false by default: deleted for this account only. Local rows stay as history."""
    if chat_id == 0:
        raise HTTPException(status_code=422, detail="chat 0 is Saved messages, it is not deleted")
    await mx_groups.known_chat(request, chat_id, kinds=("private",))
    session = await mx_groups.connected(request)
    me = mx_groups.me_of(session)
    peer = chat_id ^ me if me is not None else None
    if peer is None or req.confirm_user_id != peer:
        raise HTTPException(status_code=409, detail="confirm_user_id is not the peer of this dialog")
    request.state.audit_target_user_id = peer
    async with write_slot(request, ACTION, "delete_dialog", chat_id):
        await call_max(groups.delete(session.client, chat_id, req.for_all), mx_groups.UNCERTAIN)
    await mx_groups.unmonitor(session, chat_id)
    return {"chat_id": chat_id, "deleted": True, "for_all": req.for_all}
