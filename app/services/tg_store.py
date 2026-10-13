"""Telegram writes into the per-account tables (STORAGE_MODE=new, ТЗ 2026-10-13 §5.1).

Every function takes the account id first and works in that account's
session (app.storage.account_db). The legacy writers stay untouched; while
SHADOW_LEGACY_WRITE is on, callers also run them through shadow() in a
separate transaction, whose failure is only logged.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, true, update
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.tl.types import Channel, User

from app.config import settings
from app.models import Chat as ChatModel
from app.models import User as UserModel
from app.repo import chats, messages, snapshots, sync_state, users
from app.repo.rows import ChatRow, SnapshotMemberRow, UserRow
from app.storage import account_db
from app.telegram.normalize import chat_row, message_row, user_row
from app.utils.sender import is_user_entity

log = logging.getLogger(__name__)

PLATFORM = "telegram"


def new_mode() -> bool:
    return settings.storage_mode == "new"


async def shadow(what: str, write: Callable[[], Awaitable[Any]]) -> None:
    """Legacy write next to the primary one; never fails the caller."""
    if not settings.shadow_legacy_write:
        return
    try:
        await write()
    except Exception as exc:  # noqa: BLE001
        log.warning("shadow legacy write failed (%s): %s: %s", what, type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# Building blocks (caller's session, no commit)
# ---------------------------------------------------------------------------

async def put_user(db: AsyncSession, account_id: int, user: Any, via: str, *, is_contact: bool | None = None) -> int:
    row = user_row(user)
    if is_contact is not None:
        row.is_contact = is_contact
    return await users.upsert_user(db, account_id, PLATFORM, row, via)


async def put_sender(db: AsyncSession, account_id: int, message: Any, sender: Any) -> None:
    """The sender's users row: full from the entity, a stub from the id otherwise
    (so the message keeps its sender instead of NULL)."""
    if sender is not None and is_user_entity(sender):
        await put_user(db, account_id, sender, "message")
        return
    sender_id = getattr(message, "sender_id", None)
    if sender_id is not None and sender_id > 0:
        await users.upsert_user(db, account_id, PLATFORM, UserRow(external_id=sender_id), "message")


async def put_chat(db: AsyncSession, account_id: int, entity: Any, *, last_message: Any = None) -> int:
    return await chats.upsert_chat(db, account_id, chat_row(entity, last_message=last_message))


async def _is_monitored(db: AsyncSession, account_id: int, chat_pk: int) -> bool:
    return bool((await db.execute(
        select(ChatModel.is_monitored).where(ChatModel.account_id == account_id, ChatModel.id == chat_pk)
    )).scalar_one())


def sse_payload(message: Any, chat_external_id: int) -> dict:
    """Same keys as the legacy TG stream payload."""
    row = message_row(message)
    return {
        "event": "message",
        "message_id": row.external_id,
        "chat_id": chat_external_id,
        "from_user_id": row.sender_external_id,
        "sender_chat_id": row.sender_chat_external_id,
        "text": row.text,
        "message_type": row.message_type,
        "tg_date": row.sent_at.isoformat() if row.sent_at else None,
        "is_outgoing": row.is_outgoing_hint,
        "is_edited": row.is_edited,
    }


# ---------------------------------------------------------------------------
# Live events
# ---------------------------------------------------------------------------

async def store_event(account_id: int, event: Any) -> dict | None:
    """NewMessage / MessageEdited of this account. None if not stored (chat not monitored)."""
    message = event.message
    if message is None:
        return None
    chat = await event.get_chat()
    if chat is None:
        return None
    sender = await event.get_sender()
    async with account_db(account_id) as db:
        chat_pk = await put_chat(db, account_id, chat)
        if not await _is_monitored(db, account_id, chat_pk):
            await db.commit()
            return None
        await put_sender(db, account_id, message, sender)
        await messages.upsert_message(db, account_id, chat_pk, message_row(message))
        await db.commit()
        external_id = chat_row(chat).external_id
    return sse_payload(message, external_id)


async def store_deleted(account_id: int, chat_external_id: int | None, ids: list[int]) -> int:
    """MessageDeleted (§5.4): with a chat id → that chat; without → the account's
    private chats and basic groups (ids are unique in the account's counter)."""
    at = datetime.now(timezone.utc)
    async with account_db(account_id) as db:
        chat_pk = None
        if chat_external_id is not None:
            chat_pk = await chats.chat_pk(db, account_id, chat_external_id)
            if chat_pk is None:
                return 0
        marked = await messages.mark_deleted(db, account_id, chat_pk, list(ids), at)
        await db.commit()
    return marked


# ---------------------------------------------------------------------------
# History (backfill)
# ---------------------------------------------------------------------------

async def ensure_chat(account_id: int, client: Any, chat_external_id: int) -> int | None:
    """Internal id of the chat; fetched from Telegram if the account does not know it."""
    async with account_db(account_id) as db:
        pk = await chats.chat_pk(db, account_id, chat_external_id)
        if pk is not None:
            return pk
    try:
        entity = await client.get_entity(chat_external_id)
    except Exception:  # noqa: BLE001
        log.error("tg_store: cannot find chat %s for account %s", chat_external_id, account_id)
        return None
    async with account_db(account_id) as db:
        pk = await put_chat(db, account_id, entity)
        await db.commit()
    return pk


async def store_history(account_id: int, chat_pk: int, batch: list[Any]) -> tuple[int, list[int]]:
    """One page of history; per-message errors are isolated. Returns (stored, stored ids)."""
    stored: list[int] = []
    async with account_db(account_id) as db:
        for msg in batch:
            if msg is None or msg.id is None:
                continue
            try:
                async with db.begin_nested():
                    await put_sender(db, account_id, msg, getattr(msg, "sender", None))
                    await messages.upsert_message(db, account_id, chat_pk, message_row(msg))
                stored.append(msg.id)
            except Exception:  # noqa: BLE001
                log.warning("tg_store: skipped message %s in chat pk %s", getattr(msg, "id", "?"),
                            chat_pk, exc_info=True)
        await db.commit()
    return len(stored), stored


async def get_cursor(account_id: int, chat_pk: int):
    async with account_db(account_id) as db:
        return await sync_state.get_state(db, account_id, chat_pk)


async def save_cursor(account_id: int, chat_pk: int, **fields) -> None:
    async with account_db(account_id) as db:
        await sync_state.upsert_state(db, account_id, chat_pk, **fields)
        await db.commit()


# ---------------------------------------------------------------------------
# Dialogs, resolve, contacts, snapshots
# ---------------------------------------------------------------------------

async def store_dialogs(account_id: int, client: Any) -> int:
    """iter_dialogs → chats (access_hash, my_role, last message) + private peers via='dialog'."""
    count = 0
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        async with account_db(account_id) as db:
            await put_chat(db, account_id, entity, last_message=dialog.message)
            if isinstance(entity, User):
                await put_user(db, account_id, entity, "dialog")
            await db.commit()
        count += 1
    return count


async def store_resolved(account_id: int, user: Any) -> int:
    async with account_db(account_id) as db:
        pk = await put_user(db, account_id, user, "resolve")
        await db.commit()
    return pk


async def store_contacts(account_id: int, contacts: list[Any], *, full: bool) -> int:
    """Address book of the account: is_contact=true; with full=True everyone else
    of the account loses is_contact (daily contacts_sync)."""
    seen: list[int] = []
    async with account_db(account_id) as db:
        for u in contacts:
            if isinstance(u, User):
                await put_user(db, account_id, u, "contact", is_contact=True)
                seen.append(u.id)
        if full:
            await db.execute(
                update(UserModel)
                .where(
                    UserModel.account_id == account_id,
                    UserModel.is_contact.is_(True),
                    UserModel.external_id.not_in(seen) if seen else true(),
                )
                .values(is_contact=False)
            )
        await db.commit()
    return len(seen)


async def store_snapshot(
    account_id: int,
    client: Any,
    chat_external_id: int,
    participants: list[Any],
    members: list[dict],
    *,
    note: str | None,
) -> int:
    """member_snapshots + snapshot_members, participants → users(via='member')."""
    chat_pk = await ensure_chat(account_id, client, chat_external_id)
    async with account_db(account_id) as db:
        if chat_pk is None:
            chat_pk = await chats.upsert_chat(db, account_id, ChatRow(chat_external_id, "unknown"))
        for p in participants:
            user = getattr(p, "user", p)
            if isinstance(user, User):
                await put_user(db, account_id, user, "member")
        snapshot_id = await snapshots.create_snapshot(
            db, account_id, chat_pk,
            [
                SnapshotMemberRow(
                    external_user_id=m.get("tg_user_id"),
                    username=m.get("username"),
                    first_name=m.get("first_name"),
                    last_name=m.get("last_name"),
                    phone=m.get("phone"),
                    role=m.get("role"),
                    raw=m,
                )
                for m in members
            ],
            members_count=len(members), source="api", note=note,
        )
        await db.commit()
    return snapshot_id


# ---------------------------------------------------------------------------
# Entity cache preload (§5.2)
# ---------------------------------------------------------------------------

async def entity_rows(account_id: int) -> list[tuple]:
    """(marked_id, access_hash, username, phone, name) of the account's known peers."""
    async with account_db(account_id) as db:
        user_rows = (await db.execute(
            select(UserModel.external_id, UserModel.access_hash, UserModel.username,
                   UserModel.phone, UserModel.first_name, UserModel.last_name)
            .where(UserModel.account_id == account_id, UserModel.access_hash.is_not(None))
        )).all()
        chat_rows = (await db.execute(
            select(ChatModel.external_id, ChatModel.access_hash, ChatModel.username, ChatModel.title)
            .where(
                ChatModel.account_id == account_id,
                ChatModel.access_hash.is_not(None),
                ChatModel.chat_type.in_(("channel", "supergroup")),
            )
        )).all()
    rows = [
        (
            u.external_id, u.access_hash, (u.username or "").lower() or None,
            (u.phone or "").lstrip("+") or None,
            " ".join(p for p in (u.first_name, u.last_name) if p) or None,
        )
        for u in user_rows
    ]
    rows += [
        (c.external_id, c.access_hash, (c.username or "").lower() or None, None, c.title)
        for c in chat_rows
    ]
    return rows


def preload_entities(client: Any, rows: list[tuple]) -> int:
    """Put rows into Telethon's in-memory entity cache (MemorySession._entities).

    Private Telethon API (1.45): guarded by tests/test_tg_write_new.py; on a
    layout change this raises instead of silently doing nothing.
    """
    session = client.session
    entities = getattr(session, "_entities", None)
    to_row = getattr(session, "_entity_values_to_row", None)
    if not isinstance(entities, set) or not callable(to_row):
        raise RuntimeError("Telethon session has no MemorySession entity cache — preload unsupported")
    session._entities = entities | {to_row(*r) for r in rows}
    return len(rows)


async def preload(account_id: int, client: Any) -> int:
    rows = await entity_rows(account_id)
    return preload_entities(client, rows)
