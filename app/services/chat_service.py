import logging

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.tl.types import Chat, Channel, User

from app.models import TgChat

log = logging.getLogger(__name__)


async def upsert_chat(session: AsyncSession, entity) -> TgChat:
    """Create or update a TgChat from a Telethon entity."""
    chat_id = _get_chat_id(entity)
    chat_type = _get_chat_type(entity)
    title = _get_title(entity)
    username = getattr(entity, "username", None)

    db_chat = await session.get(TgChat, chat_id)

    if db_chat is None:
        db_chat = TgChat(
            id=chat_id,
            chat_type=chat_type,
            title=title,
            username=username,
            description=getattr(entity, "about", None),
            members_count=getattr(entity, "participants_count", None),
            raw_data=_entity_to_dict(entity),
        )
        session.add(db_chat)
    else:
        db_chat.chat_type = chat_type
        db_chat.title = title
        db_chat.username = username or db_chat.username
        db_chat.members_count = getattr(entity, "participants_count", None) or db_chat.members_count
        db_chat.raw_data = _entity_to_dict(entity)

    await session.commit()
    return db_chat


async def get_chats(
    session: AsyncSession,
    chat_type: str | None = None,
    search: str | None = None,
    is_monitored: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[TgChat], int]:
    """Get chats with filters."""
    q = select(TgChat)
    count_q = select(func.count()).select_from(TgChat)

    if chat_type:
        q = q.where(TgChat.chat_type == chat_type)
        count_q = count_q.where(TgChat.chat_type == chat_type)
    if is_monitored is not None:
        q = q.where(TgChat.is_monitored == is_monitored)
        count_q = count_q.where(TgChat.is_monitored == is_monitored)
    if search:
        pattern = f"%{search}%"
        flt = TgChat.title.ilike(pattern) | TgChat.username.ilike(pattern)
        q = q.where(flt)
        count_q = count_q.where(flt)

    total = (await session.execute(count_q)).scalar() or 0
    q = q.order_by(TgChat.last_message_at.desc().nullslast()).limit(limit).offset(offset)
    result = await session.execute(q)
    return list(result.scalars().all()), total


async def get_chat(session: AsyncSession, chat_id: int) -> TgChat | None:
    return await session.get(TgChat, chat_id)


async def update_chat(session: AsyncSession, chat_id: int, is_monitored: bool) -> TgChat | None:
    db_chat = await session.get(TgChat, chat_id)
    if db_chat is None:
        return None
    db_chat.is_monitored = is_monitored
    await session.commit()
    return db_chat


async def sync_chat_list(session: AsyncSession, client) -> int:
    """Fetch all dialogs from Telegram and upsert into DB. Returns count."""
    count = 0
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        chat_id = _get_chat_id(entity)
        chat_type = _get_chat_type(entity)
        title = _get_title(entity)

        db_chat = await session.get(TgChat, chat_id)
        if db_chat is None:
            db_chat = TgChat(
                id=chat_id,
                chat_type=chat_type,
                title=title,
                username=getattr(entity, "username", None),
                members_count=getattr(entity, "participants_count", None),
                raw_data=_entity_to_dict(entity),
            )
            session.add(db_chat)
        else:
            db_chat.chat_type = chat_type
            db_chat.title = title
            db_chat.username = getattr(entity, "username", None) or db_chat.username
            db_chat.members_count = getattr(entity, "participants_count", None) or db_chat.members_count
            db_chat.raw_data = _entity_to_dict(entity)

        if dialog.message:
            db_chat.last_message_id = dialog.message.id
            db_chat.last_message_at = dialog.message.date

        count += 1

    await session.commit()
    log.info("Synced %d chats from Telegram", count)
    return count


def _get_chat_id(entity) -> int:
    if isinstance(entity, User):
        return entity.id
    if isinstance(entity, Channel):
        return -1000000000000 - entity.id
    if isinstance(entity, Chat):
        return -entity.id
    return getattr(entity, "id", 0)


def _get_chat_type(entity) -> str:
    if isinstance(entity, User):
        return "private"
    if isinstance(entity, Channel):
        return "channel" if entity.broadcast else "supergroup"
    if isinstance(entity, Chat):
        return "group"
    return "unknown"


def _get_title(entity) -> str | None:
    if isinstance(entity, User):
        parts = [entity.first_name or "", entity.last_name or ""]
        return " ".join(p for p in parts if p) or None
    return getattr(entity, "title", None)


def _entity_to_dict(entity) -> dict:
    return {
        "id": getattr(entity, "id", None),
        "type": type(entity).__name__,
        "username": getattr(entity, "username", None),
        "title": getattr(entity, "title", None),
    }
