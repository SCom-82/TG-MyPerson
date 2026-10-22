"""Pure normalization: PyMax models / raw frames → rows of max_* (ADR §2.E). No IO.

Column names follow tg_* (tg_date included), so the TG response schemas read
the rows as they are. Message types reuse the TG vocabulary where the meaning
matches; anything PyMax does not recognize becomes 'unknown' with the whole
payload kept in raw_data.
"""

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import pymax
from pymax.protocol.enums import Command, Opcode
from pymax.types import Chat, MessageDeleteEvent, User
from pymax.types.domain import Message
from pymax.types.domain.attachments.enums import AttachmentType
from pymax.types.domain.enums import ChatType, LinkType, MessageStatus

from app.utils.phone import normalize_phone
from app.utils.raw import model_raw, strip_bytes_markers

PYMAX_VERSION = pymax.__version__

# Frames journaled into max_raw_events before typed processing (ADR §2.E).
OP_MESSAGE = int(Opcode.NOTIF_MESSAGE)
OP_EDIT = int(Opcode.MSG_EDIT)
OP_DELETE = int(Opcode.NOTIF_MSG_DELETE)
OP_CHAT = int(Opcode.NOTIF_CHAT)
OP_ATTACH = int(Opcode.NOTIF_ATTACH)  # upload ready signals: only logged (Р4.1)
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


def forward_link(msg: Message) -> Any:
    link = msg.link
    if link is not None and _value(link.type) == LinkType.FORWARD.value and link.message is not None:
        return link
    return None


def all_attaches(msg: Message) -> list[tuple[int, Any, bool]]:
    """(attach_index, attachment, forwarded) of a message (plan 2026-10-19 Р-6).

    Own attachments 0..n-1, then those of a forwarded message n..n+m-1: one
    numbering for media rows, the message type and GET …/media?index=.
    """
    own = list(msg.attaches or [])
    out = [(i, att, False) for i, att in enumerate(own)]
    link = forward_link(msg)
    if link is not None:
        out += [(len(own) + j, att, True) for j, att in enumerate(link.message.attaches or [])]
    return out


def message_type(msg: Message) -> str:
    """By the first attachment that defines a type (own first, then forwarded); none → text."""
    for _, att, _ in all_attaches(msg):
        kind = _attachment_kind(att)
        if kind is not None:
            return kind
    return "text"


def media_rows(msg: Message) -> list[dict]:
    rows = []
    for index, att, _ in all_attaches(msg):
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


def contact_cards(msg: Message, phone_of: Callable[[int], str | None] | None = None) -> list[dict]:
    """Contact cards of a message, own and forwarded (Р-8).

    PyMax's ContactAttachment has no phone field, but its model keeps extra
    keys (extra="allow"): MAX sends phone as '79…' on some cards. Without it,
    phone_of(contactId) may give the phone the account knows (users.phone).
    """
    cards = []
    for _, att, forwarded in all_attaches(msg):
        if _value(getattr(att, "type", None)) != AttachmentType.CONTACT.value:
            continue
        first, last = getattr(att, "first_name", None), getattr(att, "last_name", None)
        name = getattr(att, "name", None) or " ".join(p for p in (first, last) if p) or None
        user_id = getattr(att, "contact_id", None)
        phone = normalize_phone(getattr(att, "phone", None))
        if phone is None and phone_of is not None and user_id is not None:
            phone = phone_of(user_id)
        cards.append({"user_id": user_id, "name": name, "first_name": first, "last_name": last,
                      "phone": phone, "forwarded": forwarded})
    return cards


def cards_text(cards: list[dict]) -> str | None:
    """«Контакт: Имя, +7…» per card: the card is found by name and phone and shows in text-only output."""
    parts = []
    for c in cards:
        label = c["name"] or (str(c["user_id"]) if c["user_id"] is not None else "?")
        parts.append(f"Контакт: {label}, {c['phone']}" if c["phone"] else f"Контакт: {label}")
    return "; ".join(parts) or None


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
    kind = message_type(msg)
    media = media_rows(msg)
    if not text:
        # Only the card's own phone here: ingest does no DB lookups (users.phone is added on read).
        text = cards_text(contact_cards(msg))
        if text and not media:
            kind = "contact"

    status = _value(msg.status)
    edited = is_edit or status == MessageStatus.EDITED.value
    stats = msg.stats or {}

    raw = model_raw(msg)
    raw["_pymax"] = PYMAX_VERSION

    row = {
        "message_id": msg.id,
        "chat_id": chat_id,
        "from_user_id": from_user_id,
        "sender_chat_id": sender_chat_id,
        "reply_to_message_id": reply_to,
        "forward_from_chat_id": forward_chat,
        "forward_from_message_id": forward_msg,
        "message_type": kind,
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
    return row, media


def my_role(chat: Any, me_id: int | None) -> str | None:
    """creator / admin / member of a group or channel as MAX describes it (plan Р3 Р-28); dialogs: None."""
    if me_id is None or _CHAT_TYPES.get(str(_value(chat.type))) not in ("group", "channel"):
        return None
    if getattr(chat, "owner", None) == me_id:
        return "creator"
    if me_id in (getattr(chat, "admins", None) or []):
        return "admin"
    return "member"


def normalize_chat(chat: Chat, me_id: int | None = None) -> dict:
    """max_chats row; with me_id (the new storage) also my_role — max_chats has no such column."""
    kind = _value(chat.type)
    last = chat.last_message
    raw = model_raw(chat)
    raw["_pymax"] = PYMAX_VERSION
    extra = {"my_role": my_role(chat, me_id)} if me_id is not None else {}
    return {
        **extra,
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


def dialog_peer(chat_id: int, raw: dict | None, me_id: int) -> int | None:
    """The other side of a private chat: the participant that is not us, else chat_id XOR me (Р-5).

    Same rule as repo.chats.fill_dialog_titles (SQL). Chat 0 (saved messages) has no peer.
    """
    if chat_id == 0:
        return None
    participants = (raw or {}).get("participants")
    if isinstance(participants, dict):
        for key in participants:
            try:
                peer = int(key)
            except (TypeError, ValueError):
                continue
            if peer != me_id:
                return peer
    return chat_id ^ me_id


def normalize_user(user: User, *, me_id: int | None = None) -> dict:
    names = user.names or []
    first = names[0] if names else None
    first_name = getattr(first, "first_name", None) or getattr(first, "name", None)
    raw = model_raw(user)
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


DELAYED_SLACK_MS = 5_000


def is_delayed(payload: dict, now_ms: int) -> bool:
    """A scheduled message not sent yet (Р5 Р-73): its delayedAttributes.timeToFire is ahead.
    The frame of its real sending comes later and is stored then."""
    msg = payload.get("message") if isinstance(payload.get("message"), dict) else payload
    attrs = msg.get("delayedAttributes") if isinstance(msg, dict) else None
    fire = attrs.get("timeToFire") if isinstance(attrs, dict) else None
    try:
        return fire is not None and int(fire) > now_ms + DELAYED_SLACK_MS
    except (TypeError, ValueError):
        return False


def _delayed_filled(payload: dict) -> dict:
    """PyMax's Message requires delayedAttributes.notifySender / notifyOpponents, the client's schema
    (qs) does not: a fired scheduled message without them is still a message (Р5 Р-73)."""
    msg = payload.get("message") if isinstance(payload.get("message"), dict) else payload
    attrs = msg.get("delayedAttributes") if isinstance(msg, dict) else None
    if not isinstance(attrs, dict) or {"notifySender", "notifyOpponents"} <= set(attrs):
        return payload
    filled = {**msg, "delayedAttributes": {"notifySender": False, "notifyOpponents": False, **attrs}}
    return {**payload, "message": filled} if msg is not payload else filled


def parse_message(payload: dict) -> Message:
    """NOTIF_MESSAGE / MSG_EDIT payload → Message (raises pydantic ValidationError)."""
    return Message.model_validate(_delayed_filled(payload))


def parse_stored(raw: dict) -> Message:
    """A stored raw message → Message: without _pymax, binary markers {"_bytes": n} removed (R7-1).

    The one place the API, media and scripts parse a stored raw (PyMax stays
    behind the modules of test_s16_pymax_import_boundary, review 13 R13-1).
    """
    return parse_message(strip_bytes_markers({k: v for k, v in raw.items() if k != "_pymax"}))


def parse_delete(payload: dict) -> tuple[int, list[int]]:
    event = MessageDeleteEvent.model_validate(payload)
    return event.chat_id, list(event.message_ids)


def parse_chat(payload: dict) -> Chat:
    return Chat.model_validate(payload["chat"] if "chat" in payload else payload)
