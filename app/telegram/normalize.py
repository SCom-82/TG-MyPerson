"""Telethon objects → platform-neutral rows of app/repo (ТЗ 2026-10-13 §4.2, §5.1). No IO.

The full TL object goes to raw (tl_to_json of to_dict()): everything platform
specific lives there, the columns carry the core only.
"""

import base64
from datetime import datetime
from typing import Any

from telethon.tl.types import Channel, Chat, User

from app.repo.rows import ChatRow, MediaRow, MessageRow, UserRow
from app.services.chat_service import _get_chat_id, _get_chat_type, _get_title
from app.services.message_service import (
    _detect_message_type,
    _detect_message_type_from_media,
    _get_forward_chat_id,
    _get_forward_msg_id,
    _get_reply_to,
)
from app.utils.sender import classify_sender


def tl_to_json(value: Any) -> Any:
    """to_dict() output → JSON-safe: bytes → base64, datetime → ISO, the rest as is."""
    if isinstance(value, dict):
        return {str(k): tl_to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [tl_to_json(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return tl_to_json(to_dict())
    return str(value)


def raw_of(obj: Any) -> dict | None:
    to_dict = getattr(obj, "to_dict", None)
    if not callable(to_dict):
        return None
    try:
        return tl_to_json(to_dict())
    except Exception:  # noqa: BLE001 — raw is best effort, never blocks the row
        return None


def _is_min(entity: Any) -> bool:
    return bool(getattr(entity, "min", False))


def role_of(entity: Any) -> str | None:
    """creator | admin | member for groups and channels, None for private chats."""
    if isinstance(entity, User):
        return None
    if getattr(entity, "creator", False):
        return "creator"
    if getattr(entity, "admin_rights", None) is not None:
        return "admin"
    return "member"


def access_hash_of(entity: Any) -> int | None:
    """Hash valid for this account; a min entity's hash is not."""
    if _is_min(entity):
        return None
    return getattr(entity, "access_hash", None)


def chat_row(entity: Any, *, last_message: Any = None) -> ChatRow:
    return ChatRow(
        external_id=_get_chat_id(entity),
        chat_type=_get_chat_type(entity),
        title=_get_title(entity),
        username=getattr(entity, "username", None),
        description=getattr(entity, "about", None),
        members_count=getattr(entity, "participants_count", None),
        # Private chat: the peer's hash addresses the chat.
        access_hash=access_hash_of(entity) if isinstance(entity, (User, Channel)) else None,
        my_role=role_of(entity) if isinstance(entity, (Channel, Chat)) and not _is_min(entity) else None,
        last_message_external_id=getattr(last_message, "id", None),
        last_message_at=getattr(last_message, "date", None),
        raw=raw_of(entity),
    )


def user_row(user: Any) -> UserRow:
    min_user = _is_min(user)
    return UserRow(
        external_id=user.id,
        username=getattr(user, "username", None),
        first_name=getattr(user, "first_name", None),
        last_name=getattr(user, "last_name", None),
        phone=getattr(user, "phone", None),
        is_bot=bool(getattr(user, "bot", False)),
        access_hash=None if min_user else getattr(user, "access_hash", None),
        # User.contact is relative to this account; unreliable on a min user.
        is_contact=None if min_user else bool(getattr(user, "contact", False)),
        raw=raw_of(user),
    )


def media_rows(media: Any) -> list[MediaRow]:
    """Same metadata as the legacy tg_media row, attach_index 0."""
    from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

    if isinstance(media, MessageMediaPhoto) and media.photo:
        return [MediaRow(
            file_type=_detect_message_type_from_media(media),
            file_external_id=str(media.photo.id),
        )]
    if isinstance(media, MessageMediaDocument) and media.document:
        doc = media.document
        file_name = next(
            (a.file_name for a in doc.attributes if hasattr(a, "file_name")), None
        )
        return [MediaRow(
            file_type=_detect_message_type_from_media(media),
            file_external_id=str(doc.id),
            file_unique_id=str(doc.access_hash),
            file_name=file_name,
            file_size=doc.size,
            mime_type=doc.mime_type,
        )]
    return []


def message_row(message: Any) -> MessageRow:
    from_user_id, sender_chat_id = classify_sender(message.sender_id)
    text = message.text or message.message or None
    text_html = None
    if message.entities and text:
        try:
            from telethon.extensions import html

            text_html = html.unparse(text, message.entities)
        except Exception:  # noqa: BLE001
            pass
    edit_date = message.edit_date
    return MessageRow(
        external_id=message.id,
        sent_at=message.date,
        sender_external_id=from_user_id,
        sender_chat_external_id=sender_chat_id,
        reply_to_external_id=_get_reply_to(message),
        forward_from_chat_external_id=_get_forward_chat_id(message),
        forward_from_message_external_id=_get_forward_msg_id(message),
        message_type=_detect_message_type(message),
        text=text,
        text_html=text_html,
        is_edited=edit_date is not None,
        edited_at=edit_date,
        views=message.views,
        raw=raw_of(message),
        is_outgoing_hint=bool(message.out),
        media=media_rows(message.media) if message.media else [],
    )
