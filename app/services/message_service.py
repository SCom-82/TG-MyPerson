import logging
from datetime import datetime

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.tl.types import (
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaWebPage,
    MessageMediaGeo,
    MessageMediaContact,
    MessageMediaPoll,
)

from app.models import TgMessage, TgMedia, TgChat
from app.services.chat_service import upsert_chat
from app.services.user_service import upsert_user

log = logging.getLogger(__name__)


async def upsert_message_from_event(session: AsyncSession, event, is_edit: bool = False) -> dict | None:
    """Process a Telethon message event and upsert into DB. Returns dict for SSE."""
    message = event.message
    if message is None:
        return None

    chat = await event.get_chat()
    if chat is None:
        return None

    # Upsert chat
    db_chat = await upsert_chat(session, chat)
    if not db_chat.is_monitored:
        return None

    # Upsert sender
    sender = await event.get_sender()
    if sender and hasattr(sender, "id"):
        await upsert_user(session, sender)

    return await upsert_message(session, message, db_chat)


async def upsert_message(session: AsyncSession, message, db_chat: TgChat) -> dict | None:
    """Upsert a single Telethon Message into DB. Returns dict for SSE."""
    from telethon.utils import get_peer_id

    chat_id = db_chat.id
    msg_id = message.id
    sender_id = message.sender_id

    # Determine message type
    msg_type = _detect_message_type(message)

    # Check existing
    stmt = select(TgMessage).where(
        and_(TgMessage.message_id == msg_id, TgMessage.chat_id == chat_id)
    )
    result = await session.execute(stmt)
    db_msg = result.scalar_one_or_none()

    text_content = message.text or message.message or None
    text_html = None
    if message.entities and text_content:
        try:
            from telethon.extensions import html
            text_html = html.unparse(text_content, message.entities)
        except Exception:
            pass

    is_outgoing = message.out or False
    edit_date = message.edit_date

    if db_msg is None:
        db_msg = TgMessage(
            message_id=msg_id,
            chat_id=chat_id,
            from_user_id=sender_id,
            reply_to_message_id=_get_reply_to(message),
            forward_from_chat_id=_get_forward_chat_id(message),
            forward_from_message_id=_get_forward_msg_id(message),
            message_type=msg_type,
            text=text_content,
            text_html=text_html,
            tg_date=message.date,
            is_outgoing=is_outgoing,
            is_edited=edit_date is not None,
            edit_date=edit_date,
            views=message.views,
            raw_data=_message_to_raw(message),
        )
        session.add(db_msg)
        await session.flush()
    else:
        db_msg.text = text_content
        db_msg.text_html = text_html
        db_msg.is_edited = True
        db_msg.edit_date = edit_date
        db_msg.views = message.views or db_msg.views
        db_msg.raw_data = _message_to_raw(message)

    # Update chat last_message
    if db_chat.last_message_id is None or msg_id >= db_chat.last_message_id:
        db_chat.last_message_id = msg_id
        db_chat.last_message_at = message.date

    # Handle media metadata
    if message.media:
        await _upsert_media(session, db_msg, message.media)

    await session.commit()

    return {
        "event": "message",
        "message_id": msg_id,
        "chat_id": chat_id,
        "from_user_id": sender_id,
        "text": text_content,
        "message_type": msg_type,
        "tg_date": message.date.isoformat() if message.date else None,
        "is_outgoing": is_outgoing,
        "is_edited": edit_date is not None,
    }


async def get_messages(
    session: AsyncSession,
    chat_id: int | None = None,
    from_user_id: int | None = None,
    search: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    message_type: str | None = None,
    is_outgoing: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[TgMessage], int]:
    """Query messages with filters."""
    q = select(TgMessage)
    count_q = select(func.count()).select_from(TgMessage)

    filters = []
    if chat_id is not None:
        filters.append(TgMessage.chat_id == chat_id)
    if from_user_id is not None:
        filters.append(TgMessage.from_user_id == from_user_id)
    if search:
        filters.append(TgMessage.text.ilike(f"%{search}%"))
    if date_from:
        filters.append(TgMessage.tg_date >= date_from)
    if date_to:
        filters.append(TgMessage.tg_date <= date_to)
    if message_type:
        filters.append(TgMessage.message_type == message_type)
    if is_outgoing is not None:
        filters.append(TgMessage.is_outgoing == is_outgoing)

    if filters:
        combined = and_(*filters)
        q = q.where(combined)
        count_q = count_q.where(combined)

    total = (await session.execute(count_q)).scalar() or 0
    q = q.order_by(TgMessage.tg_date.desc()).limit(limit).offset(offset)
    result = await session.execute(q)
    return list(result.scalars().all()), total


async def get_message(session: AsyncSession, chat_id: int, message_id: int) -> TgMessage | None:
    stmt = select(TgMessage).where(
        and_(TgMessage.chat_id == chat_id, TgMessage.message_id == message_id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _detect_message_type(message) -> str:
    media = message.media
    if media is None:
        return "text"
    if isinstance(media, MessageMediaPhoto):
        return "photo"
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if doc:
            for attr in doc.attributes:
                name = type(attr).__name__
                if name == "DocumentAttributeSticker":
                    return "sticker"
                if name == "DocumentAttributeVideo":
                    if getattr(attr, "round_message", False):
                        return "video_note"
                    return "video"
                if name == "DocumentAttributeAudio":
                    if getattr(attr, "voice", False):
                        return "voice"
                    return "audio"
                if name == "DocumentAttributeAnimated":
                    return "animation"
        return "document"
    if isinstance(media, MessageMediaWebPage):
        return "web_page"
    if isinstance(media, MessageMediaGeo):
        return "location"
    if isinstance(media, MessageMediaContact):
        return "contact"
    if isinstance(media, MessageMediaPoll):
        return "poll"
    return "other"


def _get_reply_to(message) -> int | None:
    if message.reply_to:
        return getattr(message.reply_to, "reply_to_msg_id", None)
    return None


def _get_forward_chat_id(message) -> int | None:
    fwd = message.fwd_from
    if fwd and fwd.from_id:
        peer = fwd.from_id
        if hasattr(peer, "channel_id"):
            return -1000000000000 - peer.channel_id
        if hasattr(peer, "chat_id"):
            return -peer.chat_id
        if hasattr(peer, "user_id"):
            return peer.user_id
    return None


def _get_forward_msg_id(message) -> int | None:
    fwd = message.fwd_from
    if fwd:
        return getattr(fwd, "channel_post", None)
    return None


def _message_to_raw(message) -> dict:
    return {
        "id": message.id,
        "date": message.date.isoformat() if message.date else None,
        "edit_date": message.edit_date.isoformat() if message.edit_date else None,
        "out": message.out,
        "views": message.views,
        "forwards": message.forwards,
        "grouped_id": message.grouped_id,
    }


async def _upsert_media(session: AsyncSession, db_msg: TgMessage, media) -> None:
    """Store media metadata (no file download)."""
    file_type = _detect_message_type_from_media(media)
    file_id = None
    file_unique_id = None
    file_name = None
    file_size = None
    mime_type = None

    if isinstance(media, MessageMediaPhoto) and media.photo:
        file_id = str(media.photo.id)
        file_size = None  # Photo sizes are in photo.sizes
    elif isinstance(media, MessageMediaDocument) and media.document:
        doc = media.document
        file_id = str(doc.id)
        file_unique_id = str(doc.access_hash)
        file_size = doc.size
        mime_type = doc.mime_type
        for attr in doc.attributes:
            if hasattr(attr, "file_name"):
                file_name = attr.file_name
                break
    else:
        return

    # Check if already exists
    existing = await session.execute(
        select(TgMedia).where(TgMedia.message_pk == db_msg.id)
    )
    if existing.scalar_one_or_none():
        return

    db_media = TgMedia(
        message_pk=db_msg.id,
        file_id=file_id,
        file_unique_id=file_unique_id,
        file_type=file_type,
        file_name=file_name,
        file_size=file_size,
        mime_type=mime_type,
    )
    session.add(db_media)


def _detect_message_type_from_media(media) -> str:
    if isinstance(media, MessageMediaPhoto):
        return "photo"
    if isinstance(media, MessageMediaDocument):
        return "document"
    return "other"
