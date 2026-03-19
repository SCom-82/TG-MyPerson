import logging

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.tl.types import User

from app.models import TgUser

log = logging.getLogger(__name__)


async def upsert_user(session: AsyncSession, user: User) -> TgUser | None:
    """Create or update a TgUser from a Telethon User object."""
    if user is None:
        return None

    user_id = user.id
    db_user = await session.get(TgUser, user_id)

    if db_user is None:
        db_user = TgUser(
            id=user_id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            phone=user.phone,
            is_bot=user.bot or False,
            is_self=user.is_self or False,
            raw_data=_user_to_dict(user),
        )
        session.add(db_user)
    else:
        db_user.username = user.username
        db_user.first_name = user.first_name
        db_user.last_name = user.last_name
        db_user.phone = user.phone or db_user.phone
        db_user.is_bot = user.bot or False
        db_user.raw_data = _user_to_dict(user)

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
