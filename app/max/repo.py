"""max_* data access: idempotent upserts for ingest, reads mirroring the TG services.

Reads are global over max_* (like TG reads over tg_*); visibility between MAX
accounts is shared, ADR §2.A. Telegram tables are never touched here.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MaxChat, MaxMedia, MaxMessage, MaxRawEvent, MaxSyncState, MaxUser

# ---------------------------------------------------------------------------
# Ingest: upserts (callers commit)
# ---------------------------------------------------------------------------


async def upsert_chat(db: AsyncSession, row: dict) -> None:
    """Insert or refresh a chat; never moves last_message backwards, keeps is_monitored."""
    stmt = insert(MaxChat).values(**row)
    excluded = stmt.excluded
    newer = or_(MaxChat.last_message_at.is_(None), excluded.last_message_at > MaxChat.last_message_at)
    stmt = stmt.on_conflict_do_update(
        index_elements=[MaxChat.id],
        set_={
            "chat_type": excluded.chat_type,
            "title": func.coalesce(excluded.title, MaxChat.title),
            "username": func.coalesce(excluded.username, MaxChat.username),
            "description": func.coalesce(excluded.description, MaxChat.description),
            "members_count": func.coalesce(excluded.members_count, MaxChat.members_count),
            "last_message_id": case((newer, excluded.last_message_id), else_=MaxChat.last_message_id),
            "last_message_at": case((newer, excluded.last_message_at), else_=MaxChat.last_message_at),
            "raw_data": excluded.raw_data,
            "updated_at": func.now(),
        },
    )
    await db.execute(stmt)


async def ensure_chat(db: AsyncSession, chat_id: int, chat_type: str) -> None:
    """Stub row so the message FK holds; real data arrives with chat events / sync."""
    await db.execute(
        insert(MaxChat).values(id=chat_id, chat_type=chat_type).on_conflict_do_nothing(index_elements=[MaxChat.id])
    )


async def ensure_users(db: AsyncSession, user_ids: set[int]) -> set[int]:
    """Stub rows for unknown users; returns the ids that were missing."""
    ids = {i for i in user_ids if i is not None}
    if not ids:
        return set()
    known = set((await db.execute(select(MaxUser.id).where(MaxUser.id.in_(ids)))).scalars())
    missing = ids - known
    if missing:
        await db.execute(
            insert(MaxUser).values([{"id": i} for i in sorted(missing)]).on_conflict_do_nothing(
                index_elements=[MaxUser.id]
            )
        )
    return missing


async def upsert_users(db: AsyncSession, rows: list[dict]) -> None:
    for row in rows:
        stmt = insert(MaxUser).values(**row)
        excluded = stmt.excluded
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=[MaxUser.id],
                set_={
                    "username": func.coalesce(excluded.username, MaxUser.username),
                    "first_name": func.coalesce(excluded.first_name, MaxUser.first_name),
                    "last_name": func.coalesce(excluded.last_name, MaxUser.last_name),
                    "phone": func.coalesce(excluded.phone, MaxUser.phone),
                    "is_bot": excluded.is_bot,
                    "is_self": excluded.is_self,
                    "raw_data": excluded.raw_data,
                    "updated_at": func.now(),
                },
            )
        )


async def upsert_message(db: AsyncSession, row: dict, media: list[dict]) -> int:
    """Idempotent on (message_id, chat_id); returns the row pk.

    Frames are dispatched concurrently, so an edit may land before the original:
    a non-edited version never overwrites an edited one, and deleted_at once set
    is never cleared.
    """
    stmt = insert(MaxMessage).values(**row)
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[MaxMessage.message_id, MaxMessage.chat_id],
        set_={
            "from_user_id": excluded.from_user_id,
            "sender_chat_id": excluded.sender_chat_id,
            "reply_to_message_id": excluded.reply_to_message_id,
            "forward_from_chat_id": excluded.forward_from_chat_id,
            "forward_from_message_id": excluded.forward_from_message_id,
            "message_type": excluded.message_type,
            "text": excluded.text,
            "text_html": excluded.text_html,
            "tg_date": excluded.tg_date,
            "is_outgoing": excluded.is_outgoing,
            "is_edited": excluded.is_edited,
            "edit_date": excluded.edit_date,
            "views": func.coalesce(excluded.views, MaxMessage.views),
            "raw_data": excluded.raw_data,
            "deleted_at": func.coalesce(MaxMessage.deleted_at, excluded.deleted_at),
        },
        where=or_(MaxMessage.is_edited.is_(False), excluded.is_edited.is_(True)),
    ).returning(MaxMessage.id)
    pk = (await db.execute(stmt)).scalar_one_or_none()
    if pk is None:  # update skipped by the guard: keep the stored (edited) version
        pk = (
            await db.execute(
                select(MaxMessage.id).where(
                    MaxMessage.message_id == row["message_id"], MaxMessage.chat_id == row["chat_id"]
                )
            )
        ).scalar_one()
        return pk

    for item in media:
        m = insert(MaxMedia).values(message_pk=pk, **item)
        await db.execute(
            m.on_conflict_do_update(
                index_elements=[MaxMedia.message_pk, MaxMedia.attach_index],
                set_={k: m.excluded[k] for k in ("file_id", "file_type", "file_name", "file_size", "mime_type")},
            )
        )
    return pk


async def bump_chat_last_message(db: AsyncSession, chat_id: int, message_id: int, at: datetime) -> None:
    await db.execute(
        update(MaxChat)
        .where(MaxChat.id == chat_id, or_(MaxChat.last_message_at.is_(None), MaxChat.last_message_at <= at))
        .values(last_message_id=message_id, last_message_at=at, updated_at=func.now())
    )


async def advance_cursor(db: AsyncSession, chat_id: int, message_id: int, time_ms: int) -> None:
    """newest_time_ms: "everything up to here, without gaps" (ADR §2.J). Callers decide
    whether they may move it — see MaxIngest.store_message."""
    stmt = insert(MaxSyncState).values(
        chat_id=chat_id,
        newest_message_id=message_id,
        newest_time_ms=time_ms,
        oldest_message_id=message_id,
        oldest_time_ms=time_ms,
        total_messages_synced=0,
    )
    excluded = stmt.excluded
    newer = or_(MaxSyncState.newest_time_ms.is_(None), excluded.newest_time_ms > MaxSyncState.newest_time_ms)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[MaxSyncState.chat_id],
            set_={
                "newest_time_ms": case((newer, excluded.newest_time_ms), else_=MaxSyncState.newest_time_ms),
                "newest_message_id": case((newer, excluded.newest_message_id), else_=MaxSyncState.newest_message_id),
                "updated_at": func.now(),
            },
        )
    )


async def mark_deleted(db: AsyncSession, chat_id: int, message_ids: list[int], at: datetime) -> set[int]:
    """deleted_at on known messages (text kept, ADR §2.G); returns ids that were found."""
    if not message_ids:
        return set()
    found = await db.execute(
        update(MaxMessage)
        .where(MaxMessage.chat_id == chat_id, MaxMessage.message_id.in_(message_ids))
        .values(deleted_at=func.coalesce(MaxMessage.deleted_at, at))
        .returning(MaxMessage.message_id)
    )
    return set(found.scalars())


# ---------------------------------------------------------------------------
# Raw frame journal
# ---------------------------------------------------------------------------


async def insert_raw_event(db: AsyncSession, account_id: int | None, opcode: int, payload: dict) -> int:
    return (
        await db.execute(
            insert(MaxRawEvent)
            .values(account_id=account_id, opcode=opcode, payload=payload)
            .returning(MaxRawEvent.id)
        )
    ).scalar_one()


async def finish_raw_event(db: AsyncSession, raw_id: int, *, normalized: bool, error: str | None) -> None:
    await db.execute(
        update(MaxRawEvent).where(MaxRawEvent.id == raw_id).values(normalized=normalized, error=error)
    )


async def purge_raw_events(db: AsyncSession, older_than_days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    result = await db.execute(delete(MaxRawEvent).where(MaxRawEvent.received_at < cutoff))
    return result.rowcount or 0


# ---------------------------------------------------------------------------
# Reads (mirror of app/services/{chat,message,user,backfill}_service.py)
# ---------------------------------------------------------------------------


async def get_chats(
    db: AsyncSession,
    chat_type: str | None = None,
    search: str | None = None,
    is_monitored: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[MaxChat], int]:
    q = select(MaxChat)
    count_q = select(func.count()).select_from(MaxChat)
    if chat_type:
        q = q.where(MaxChat.chat_type == chat_type)
        count_q = count_q.where(MaxChat.chat_type == chat_type)
    if is_monitored is not None:
        q = q.where(MaxChat.is_monitored == is_monitored)
        count_q = count_q.where(MaxChat.is_monitored == is_monitored)
    if search:
        pattern = f"%{search}%"
        flt = MaxChat.title.ilike(pattern) | MaxChat.username.ilike(pattern)
        q = q.where(flt)
        count_q = count_q.where(flt)
    total = (await db.execute(count_q)).scalar() or 0
    q = q.order_by(MaxChat.last_message_at.desc().nullslast()).limit(limit).offset(offset)
    return list((await db.execute(q)).scalars().all()), total


async def get_chat(db: AsyncSession, chat_id: int) -> MaxChat | None:
    return await db.get(MaxChat, chat_id)


async def set_chat_monitored(db: AsyncSession, chat_id: int, is_monitored: bool) -> MaxChat | None:
    chat = await db.get(MaxChat, chat_id)
    if chat is None:
        return None
    chat.is_monitored = is_monitored
    await db.commit()
    await db.refresh(chat)  # updated_at is server-side (onupdate) and expires on flush
    return chat


async def get_messages(
    db: AsyncSession,
    chat_id: int | None = None,
    from_user_id: int | None = None,
    search: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    message_type: str | None = None,
    is_outgoing: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[MaxMessage], int]:
    q = select(MaxMessage)
    count_q = select(func.count()).select_from(MaxMessage)
    filters = []
    if chat_id is not None:
        filters.append(MaxMessage.chat_id == chat_id)
    if from_user_id is not None:
        filters.append(MaxMessage.from_user_id == from_user_id)
    if search:
        filters.append(MaxMessage.text.ilike(f"%{search}%"))
    if date_from:
        filters.append(MaxMessage.tg_date >= date_from)
    if date_to:
        filters.append(MaxMessage.tg_date <= date_to)
    if message_type:
        filters.append(MaxMessage.message_type == message_type)
    if is_outgoing is not None:
        filters.append(MaxMessage.is_outgoing == is_outgoing)
    if filters:
        combined = and_(*filters)
        q = q.where(combined)
        count_q = count_q.where(combined)
    total = (await db.execute(count_q)).scalar() or 0
    q = q.order_by(MaxMessage.tg_date.desc()).limit(limit).offset(offset)
    return list((await db.execute(q)).scalars().all()), total


async def get_message(db: AsyncSession, chat_id: int, message_id: int) -> MaxMessage | None:
    return (
        await db.execute(
            select(MaxMessage).where(MaxMessage.chat_id == chat_id, MaxMessage.message_id == message_id)
        )
    ).scalar_one_or_none()


async def get_users(
    db: AsyncSession, search: str | None = None, limit: int = 50, offset: int = 0
) -> tuple[list[MaxUser], int]:
    q = select(MaxUser)
    count_q = select(func.count()).select_from(MaxUser)
    if search:
        pattern = f"%{search}%"
        flt = MaxUser.username.ilike(pattern) | MaxUser.first_name.ilike(pattern) | MaxUser.last_name.ilike(pattern)
        q = q.where(flt)
        count_q = count_q.where(flt)
    total = (await db.execute(count_q)).scalar() or 0
    q = q.order_by(MaxUser.updated_at.desc()).limit(limit).offset(offset)
    return list((await db.execute(q)).scalars().all()), total


async def find_user(db: AsyncSession, *, user_id: int | None = None, username: str | None = None,
                    phone: str | None = None) -> MaxUser | None:
    q = select(MaxUser)
    if user_id is not None:
        q = q.where(MaxUser.id == user_id)
    elif username is not None:
        q = q.where(func.lower(MaxUser.username) == username.lower())
    elif phone is not None:
        q = q.where(MaxUser.phone == phone)
    else:
        return None
    return (await db.execute(q.limit(1))).scalar_one_or_none()


async def get_users_by_ids(db: AsyncSession, user_ids: list[int]) -> dict[int, MaxUser]:
    if not user_ids:
        return {}
    rows = (await db.execute(select(MaxUser).where(MaxUser.id.in_(user_ids)))).scalars().all()
    return {u.id: u for u in rows}


async def search_messages(db: AsyncSession, q: str, limit: int) -> list[tuple[MaxMessage, MaxChat | None]]:
    stmt = (
        select(MaxMessage, MaxChat)
        .join(MaxChat, MaxMessage.chat_id == MaxChat.id, isouter=True)
        .where(MaxMessage.text.ilike(f"%{q}%"))
        .order_by(MaxMessage.tg_date.desc())
        .limit(limit)
    )
    return [(m, c) for m, c in (await db.execute(stmt)).all()]


async def get_sync_states(db: AsyncSession) -> list[tuple[MaxSyncState, str | None]]:
    stmt = select(MaxSyncState, MaxChat.title).join(MaxChat, MaxSyncState.chat_id == MaxChat.id, isouter=True)
    return [(s, t) for s, t in (await db.execute(stmt)).all()]
