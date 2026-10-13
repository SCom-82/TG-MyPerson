"""Read endpoints over the account's own tables (STORAGE_MODE=new, ТЗ 2026-10-13 §6.3).

One implementation for TG and MAX: the routers call these when new_mode() and
keep their legacy bodies otherwise. External ids in parameters are resolved
inside the request's account; an id the account does not know → 404, never
an empty answer from somebody else's data.
"""

from datetime import datetime

from fastapi import HTTPException, Request

from app.api.mappers import chat_response, message_response, user_response
from app.repo import chats, messages, snapshots, sync_state, users
from app.schemas import MessageResponse, PaginatedResponse
from app.storage import account_db

ACCOUNT_REQUIRED = "account required: X-Session-Alias or per-account key"


def account_of(request: Request) -> int:
    account_id = getattr(request.state, "account_id", None)
    if account_id is None:
        raise HTTPException(status_code=400, detail=ACCOUNT_REQUIRED)
    return account_id


# -- chats -------------------------------------------------------------------------

async def list_chats(request: Request, *, chat_type, search, is_monitored, limit, offset) -> PaginatedResponse:
    a = account_of(request)
    async with account_db(a) as db:
        rows, total = await chats.list_chats(
            db, a, chat_type=chat_type, search=search, is_monitored=is_monitored, limit=limit, offset=offset
        )
        items = [chat_response(c) for c in rows]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


async def get_chat(request: Request, chat_id: int):
    a = account_of(request)
    async with account_db(a) as db:
        chat = await chats.get_chat(db, a, chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        return chat_response(chat)


async def set_monitored(request: Request, chat_id: int, value: bool):
    a = account_of(request)
    async with account_db(a) as db:
        chat = await chats.set_monitored(db, a, chat_id, value)
        if chat is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        await db.commit()
        return chat_response(chat)


# -- messages ----------------------------------------------------------------------

async def list_messages(
    request: Request,
    *,
    chat_id: int | None,
    from_user_id: int | None,
    search: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    message_type: str | None,
    is_outgoing: bool | None,
    limit: int,
    offset: int,
    cls: type[MessageResponse] = MessageResponse,
) -> PaginatedResponse:
    a = account_of(request)
    async with account_db(a) as db:
        if chat_id is not None and await chats.chat_pk(db, a, chat_id) is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        if from_user_id is not None and await users.user_pk(db, a, from_user_id) is None:
            raise HTTPException(status_code=404, detail="User not found")
        rows, total = await messages.get_messages(
            db, a, chat_external_id=chat_id, from_user_external_id=from_user_id, search=search,
            date_from=date_from, date_to=date_to, message_type=message_type, is_outgoing=is_outgoing,
            limit=limit, offset=offset,
        )
        ext = await chats.external_ids(db, a, {m.chat_id for m in rows})
        items = [message_response(m, ext[m.chat_id], cls) for m in rows]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


async def get_message(request: Request, chat_id: int, message_id: int, cls: type[MessageResponse] = MessageResponse):
    a = account_of(request)
    async with account_db(a) as db:
        msg = await messages.get_message(db, a, chat_id, message_id)
        if msg is None:
            raise HTTPException(status_code=404, detail="Message not found")
        return message_response(msg, chat_id, cls)


async def stored_raw(request: Request, chat_id: int, message_id: int) -> dict | None:
    """raw of a stored message of the account (MAX media download)."""
    a = account_of(request)
    async with account_db(a) as db:
        msg = await messages.get_message(db, a, chat_id, message_id)
        return dict(msg.raw) if msg is not None and msg.raw else None


async def search_global(request: Request, q: str, limit: int) -> dict:
    a = account_of(request)
    async with account_db(a) as db:
        rows = await messages.search(db, a, q, limit)
        items = [
            {
                "message_id": m.external_id,
                "text": m.text,
                "date": m.sent_at.isoformat() if m.sent_at else None,
                "from_user_id": m.sender.external_id if m.sender is not None else None,
                "chat": {"id": c.external_id, "title": c.title, "username": c.username},
            }
            for m, c in rows
        ]
    return {"items": items, "total": len(items)}


# -- users -------------------------------------------------------------------------

async def list_users(request: Request, *, search, limit, offset) -> PaginatedResponse:
    a = account_of(request)
    async with account_db(a) as db:
        rows, total = await users.list_users(db, a, search=search, limit=limit, offset=offset)
        items = [user_response(u) for u in rows]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


# -- sync status -------------------------------------------------------------------

async def sync_status(request: Request, *, is_running, max_fields: bool = False) -> dict:
    from app.max.normalize import dt_to_ms

    a = account_of(request)
    async with account_db(a) as db:
        rows = await sync_state.list_states(db, a)
    states = []
    for state, chat_external_id, title in rows:
        item = {
            "chat_id": chat_external_id,
            "chat_title": title,
            "oldest_message_id": state.oldest_external_id,
            "newest_message_id": state.newest_external_id,
            "is_fully_synced": state.is_fully_synced,
            "total_messages_synced": state.total_synced,
            "last_backfill_at": state.last_backfill_at,
            "is_running": is_running(chat_external_id, a),
        }
        if max_fields:
            item.update({
                "oldest_time_ms": dt_to_ms(state.oldest_ts),
                "newest_time_ms": dt_to_ms(state.newest_ts),
                "last_catchup_at": state.last_catchup_at,
            })
        states.append(item)
    return {"states": states}


# -- snapshots ---------------------------------------------------------------------

async def list_snapshots(request: Request, chat_id: int, limit: int) -> list[dict]:
    a = account_of(request)
    alias = getattr(request.state, "session_alias", None)
    async with account_db(a) as db:
        pk = await chats.chat_pk(db, a, chat_id)
        rows = (await snapshots.list_snapshots(db, a, pk))[:limit] if pk is not None else []
    return [
        {
            "snapshot_id": s.id,
            "taken_at": s.taken_at.isoformat() if s.taken_at else None,
            "members_count": s.members_count,
            "source": s.source,
            "note": s.note,
            "account_alias": alias,
        }
        for s in rows
    ]


async def snapshot_members(request: Request, snapshot_id: int, offset: int, limit: int) -> dict:
    a = account_of(request)
    async with account_db(a) as db:
        if await snapshots.get_snapshot(db, a, snapshot_id) is None:
            raise HTTPException(status_code=404, detail=f"Snapshot {snapshot_id} not found")
        rows, total = await snapshots.list_members(db, a, snapshot_id, limit=limit, offset=offset)
    return {
        "snapshot_id": snapshot_id,
        "total": total,
        "offset": offset,
        "limit": limit,
        "members": [
            {
                "id": r.id,
                "tg_user_id": r.external_user_id,
                "username": r.username,
                "first_name": r.first_name,
                "last_name": r.last_name,
                "phone": r.phone,
                "role": r.role,
            }
            for r in rows
        ],
    }
