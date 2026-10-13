"""chats of an account (ТЗ 2026-10-13 §2.4, §4.2)."""

from datetime import datetime

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chat
from app.repo import ensure_scope
from app.repo.rows import ChatRow


def _monotonic(new_col, old_col, new_id, old_id):
    """Value of the newer last message: last_message_* never moves back."""
    return case(
        (and_(new_id.is_not(None), or_(old_id.is_(None), new_id > old_id)), new_col),
        else_=old_col,
    )


async def upsert_chat(db: AsyncSession, account_id: int, row: ChatRow) -> int:
    """ON CONFLICT (account_id, external_id). Returns the internal chat id.

    is_monitored is set on insert only; None fields keep stored values.
    """
    ensure_scope(db, account_id)
    stmt = pg_insert(Chat).values(
        account_id=account_id,
        external_id=row.external_id,
        chat_type=row.chat_type,
        title=row.title,
        username=row.username,
        description=row.description,
        members_count=row.members_count,
        access_hash=row.access_hash,
        my_role=row.my_role,
        last_message_external_id=row.last_message_external_id,
        last_message_at=row.last_message_at,
        raw=row.raw,
    )
    ex = stmt.excluded
    t = Chat.__table__.c
    stmt = stmt.on_conflict_do_update(
        index_elements=[Chat.account_id, Chat.external_id],
        set_={
            "chat_type": ex.chat_type,
            "title": func.coalesce(ex.title, t.title),
            "username": func.coalesce(ex.username, t.username),
            "description": func.coalesce(ex.description, t.description),
            "members_count": func.coalesce(ex.members_count, t.members_count),
            "access_hash": func.coalesce(ex.access_hash, t.access_hash),
            "my_role": func.coalesce(ex.my_role, t.my_role),
            "last_message_external_id": _monotonic(
                ex.last_message_external_id, t.last_message_external_id,
                ex.last_message_external_id, t.last_message_external_id,
            ),
            "last_message_at": _monotonic(
                ex.last_message_at, t.last_message_at,
                ex.last_message_external_id, t.last_message_external_id,
            ),
            "raw": func.coalesce(ex.raw, t.raw),
        },
    ).returning(Chat.id)
    return (await db.execute(stmt)).scalar_one()


async def chat_pk(db: AsyncSession, account_id: int, external_id: int) -> int | None:
    ensure_scope(db, account_id)
    return (await db.execute(
        select(Chat.id).where(Chat.account_id == account_id, Chat.external_id == external_id)
    )).scalar_one_or_none()


async def get_chat(db: AsyncSession, account_id: int, external_id: int) -> Chat | None:
    ensure_scope(db, account_id)
    return (await db.execute(
        select(Chat).where(Chat.account_id == account_id, Chat.external_id == external_id)
    )).scalar_one_or_none()


async def list_chats(
    db: AsyncSession,
    account_id: int,
    *,
    chat_type: str | None = None,
    search: str | None = None,
    is_monitored: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Chat], int]:
    ensure_scope(db, account_id)
    flt = [Chat.account_id == account_id]
    if chat_type:
        flt.append(Chat.chat_type == chat_type)
    if is_monitored is not None:
        flt.append(Chat.is_monitored == is_monitored)
    if search:
        pattern = f"%{search}%"
        flt.append(or_(Chat.title.ilike(pattern), Chat.username.ilike(pattern)))
    total = (await db.execute(select(func.count()).select_from(Chat).where(*flt))).scalar_one()
    rows = (await db.execute(
        select(Chat).where(*flt)
        .order_by(Chat.last_message_at.desc().nullslast(), Chat.id)
        .limit(limit).offset(offset)
    )).scalars().all()
    return list(rows), total


async def set_monitored(db: AsyncSession, account_id: int, external_id: int, value: bool) -> Chat | None:
    ensure_scope(db, account_id)
    return (await db.execute(
        update(Chat)
        .where(Chat.account_id == account_id, Chat.external_id == external_id)
        .values(is_monitored=value)
        .returning(Chat)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def touch_last_message(
    db: AsyncSession, account_id: int, chat_id: int, external_id: int, at: datetime | None
) -> None:
    """Move last_message_* forward only."""
    ensure_scope(db, account_id)
    await db.execute(
        update(Chat)
        .where(
            Chat.account_id == account_id,
            Chat.id == chat_id,
            or_(Chat.last_message_external_id.is_(None), Chat.last_message_external_id <= external_id),
        )
        .values(last_message_external_id=external_id, last_message_at=at)
    )
