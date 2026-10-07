"""Pure normalization: PyMax models / raw frames → rows of max_* (ADR §2.E). No IO.

Column names follow tg_* (tg_date included), so the TG response schemas read
the rows as they are. Message types reuse the TG vocabulary where the meaning
matches; anything PyMax does not recognize becomes 'unknown' with the whole
payload kept in raw_data.
"""

from datetime import datetime, timezone
from typing import Any

import pymax
from pymax.protocol.enums import Command, Opcode
from pymax.types import Chat, MessageDeleteEvent, User
from pymax.types.domain import Message
from pymax.types.domain.attachments.enums import AttachmentType
from pymax.types.domain.enums import ChatType, LinkType, MessageStatus

PYMAX_VERSION = pymax.__version__

# Frames journaled into max_raw_events before typed processing (ADR §2.E).
OP_MESSAGE = int(Opcode.NOTIF_MESSAGE)
OP_EDIT = int(Opcode.MSG_EDIT)
OP_DELETE = int(Opcode.NOTIF_MSG_DELETE)
OP_CHAT = int(Opcode.NOTIF_CHAT)
JOURNALED_OPCODES = frozenset({OP_MESSAGE, OP_EDIT, OP_DELETE, OP_CHAT})
SERVER_PUSH = int(Command.REQUEST)  # server-initiated frames carry cmd=REQUEST

_TYPE_BY_ATTACHMENT = {
    AttachmentType.PHOTO.value: "photo",
    AttachmentType.VIDEO.value: "video",
    AttachmentType.FILE.value: "document",
    AttachmentType.AUDIO.value: "voice",
    AttachmentType.STICKER.value: "sticker",
    AttachmentType.CONTACT.value: "contact",
    AttachmentType.SHARE.value: "web_page",
    AttachmentType.CALL.value: "call",
    AttachmentType.POLL.value: "poll",
    AttachmentType.CONTROL.value: "service",
}
# Attachments that carry a downloadable file → rows in max_media.
_MEDIA_TYPES = {"photo", "video", "document", "voice", "sticker"}

_CHAT_TYPES = {
    ChatType.DIALOG.value: "private",
    ChatType.CHAT.value: "group",
    ChatType.CHANNEL.value: "channel",
}


def ms_to_dt(ms: int | None) -> datetime | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def dt_to_ms(dt: datetime | None) -> int | None:
    return int(dt.timestamp() * 1000) if dt else None


def _value(v: Any) -> Any:
    return getattr(v, "value", v)


def _attachment_kind(att: Any) -> str | None:
    """TG-style type of one attachment; None for ones that do not define it (keyboards)."""
    kind = _value(getattr(att, "type", None))
    if kind == AttachmentType.INLINE_KEYBOARD.value:
        return None
    return _TYPE_BY_ATTACHMENT.get(kind, "unknown")


def message_type(msg: Message) -> str:
    """By the first attachment that defines a type; no attachments → text."""
    for att in msg.attaches:
        kind = _attachment_kind(att)
        if kind is not None:
            return kind
    return "text"


def media_rows(msg: Message) -> list[dict]:
    rows = []
    for index, att in enumerate(msg.attaches):
        kind = _attachment_kind(att)
        if kind not in _MEDIA_TYPES:
            continue
        file_id = (
            getattr(att, "photo_id", None)
            or getattr(att, "video_id", None)
            or getattr(att, "file_id", None)
            or getattr(att, "audio_id", None)
            or getattr(att, "sticker_id", None)
        )
        rows.append({
            "attach_index": index,
            "file_type": kind,
            "file_id": str(file_id) if file_id is not None else None,
            "file_unique_id": None,
            "file_name": getattr(att, "name", None),
            "file_size": getattr(att, "size", None),
            "mime_type": None,  # MAX attachments carry no MIME type
        })
    return rows


def is_channel_post(msg: Message, chat_type: str | None = None) -> bool:
    return chat_type == "channel" or str(msg.type).upper() == ChatType.CHANNEL.value


def guess_chat_type(msg: Message, chat_id: int) -> str:
    """Chat type for a stub row when the chat itself is not known yet."""
    if is_channel_post(msg):
        return "channel"
    return "private" if chat_id > 0 else "group"


def normalize_message(
    msg: Message,
    *,
    me_id: int | None,
    chat_id: int | None = None,
    chat_type: str | None = None,
    received_at: datetime | None = None,
    is_edit: bool = False,
) -> tuple[dict, list[dict]]:
    """(max_messages row, max_media rows) for one message."""
    chat_id = msg.chat_id if msg.chat_id is not None else chat_id
    if chat_id is None:
        raise ValueError(f"message {msg.id} has no chat_id")
    received_at = received_at or datetime.now(timezone.utc)

    # Channel posts are authored by the channel (TG broadcast semantics).
    channel = is_channel_post(msg, chat_type)
    from_user_id = None if channel else msg.sender
    sender_chat_id = chat_id if channel else None

    reply_to = forward_chat = forward_msg = None
    text = msg.text or None
    link = msg.link
    if link is not None:
        if _value(link.type) == LinkType.REPLY.value:
            reply_to = link.message.id
        elif _value(link.type) == LinkType.FORWARD.value:
            forward_chat = link.chat_id
            forward_msg = link.message.id
            if not text:
                text = link.message.text or None  # keep forwards searchable

    status = _value(msg.status)
    edited = is_edit or status == MessageStatus.EDITED.value
    stats = msg.stats or {}

    raw = msg.model_dump(mode="json", by_alias=True)
    raw["_pymax"] = PYMAX_VERSION

    row = {
        "message_id": msg.id,
        "chat_id": chat_id,
        "from_user_id": from_user_id,
        "sender_chat_id": sender_chat_id,
        "reply_to_message_id": reply_to,
        "forward_from_chat_id": forward_chat,
        "forward_from_message_id": forward_msg,
        "message_type": message_type(msg),
        "text": text,
        "text_html": None,  # phase 1: markup stays in raw_data.elements
        "tg_date": ms_to_dt(msg.time),
        "is_outgoing": me_id is not None and msg.sender == me_id,
        "is_edited": edited,
        "edit_date": received_at if edited else None,
        "views": stats.get("views") if isinstance(stats, dict) else None,
        "raw_data": raw,
        "deleted_at": received_at if status == MessageStatus.REMOVED.value else None,
    }
    return row, media_rows(msg)


def normalize_chat(chat: Chat) -> dict:
    kind = _value(chat.type)
    last = chat.last_message
    raw = chat.model_dump(mode="json", by_alias=True)
    raw["_pymax"] = PYMAX_VERSION
    return {
        "id": chat.id,
        "chat_type": _CHAT_TYPES.get(str(kind), str(kind).lower()),
        "title": chat.title,
        "username": chat.link,
        "description": chat.description,
        "members_count": chat.participants_count or None,
        "last_message_id": last.id if last is not None else None,
        "last_message_at": ms_to_dt(last.time) if last is not None else None,
        "raw_data": raw,
    }


def normalize_user(user: User, *, me_id: int | None = None) -> dict:
    names = user.names or []
    first = names[0] if names else None
    first_name = getattr(first, "first_name", None) or getattr(first, "name", None)
    raw = user.model_dump(mode="json", by_alias=True)
    raw["_pymax"] = PYMAX_VERSION
    return {
        "id": user.id,
        "username": str(user.link) if user.link is not None else None,
        "first_name": first_name,
        "last_name": getattr(first, "last_name", None),
        "phone": f"+{user.phone}" if user.phone else None,
        "is_bot": "BOT" in {str(o).upper() for o in (user.options or [])},
        "is_self": me_id is not None and user.id == me_id,
        "raw_data": raw,
    }


# -- raw frames --------------------------------------------------------------

def frame_chat_id(opcode: int, payload: dict) -> int | None:
    """Chat of a journaled frame without validating it (used to order frames per chat)."""
    try:
        if opcode == OP_CHAT:
            chat = payload.get("chat")
            return int(chat["id"]) if isinstance(chat, dict) and "id" in chat else None
        value = payload.get("chatId")
        if value is None and isinstance(payload.get("message"), dict):
            value = payload["message"].get("chatId")
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_message(payload: dict) -> Message:
    """NOTIF_MESSAGE / MSG_EDIT payload → Message (raises pydantic ValidationError)."""
    return Message.model_validate(payload)


def parse_delete(payload: dict) -> tuple[int, list[int]]:
    event = MessageDeleteEvent.model_validate(payload)
    return event.chat_id, list(event.message_ids)


def parse_chat(payload: dict) -> Chat:
    return Chat.model_validate(payload["chat"] if "chat" in payload else payload)
