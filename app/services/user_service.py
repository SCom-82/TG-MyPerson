import logging

from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.tl.types import User

from app.models import TgUser

log = logging.getLogger(__name__)


async def upsert_user(session: AsyncSession, user: User) -> TgUser | None:
    """Create or update a TgUser from a Telethon User object.

    Single INSERT … ON CONFLICT (id) DO UPDATE: a select-then-insert lost
    messages when two sessions (accounts, catch-up after reconnect) saw the
    same new user at once — the loser hit tg_users_pkey and rolled back the
    whole message.
    """
    if user is None:
        return None

    stmt = pg_insert(TgUser).values(
        id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        phone=user.phone,
        is_bot=user.bot or False,
        is_self=user.is_self or False,
        raw_data=_user_to_dict(user),
    )
    ex = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[TgUser.id],
        set_={
            "username": ex.username,
            "first_name": ex.first_name,
            "last_name": ex.last_name,
            "phone": func.coalesce(ex.phone, TgUser.phone),
            "is_bot": ex.is_bot,
            "raw_data": ex.raw_data,
            "updated_at": func.now(),
        },
    ).returning(TgUser)
    db_user = (
        await session.scalars(stmt, execution_options={"populate_existing": True})
    ).one()

    await session.commit()
    return db_user


async def get_users(
    session: AsyncSession,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[TgUser], int]:
    """Get users with optional search by name/username."""
    q = select(TgUser)
    count_q = select(func.count()).select_from(TgUser)

    if search:
        pattern = f"%{search}%"
        flt = (
            TgUser.username.ilike(pattern)
            | TgUser.first_name.ilike(pattern)
            | TgUser.last_name.ilike(pattern)
        )
        q = q.where(flt)
        count_q = count_q.where(flt)

    total = (await session.execute(count_q)).scalar() or 0
    q = q.order_by(TgUser.first_seen_at.desc()).limit(limit).offset(offset)
    result = await session.execute(q)
    return list(result.scalars().all()), total


def _user_to_dict(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "phone": user.phone,
        "bot": user.bot,
        "verified": user.verified,
        "premium": getattr(user, "premium", None),
    }
