"""Platform-neutral rows ("core") the TG and MAX normalisers produce (ТЗ §4.2).

Everything platform specific stays in raw. External ids only; the repo maps
them to internal ids of the account.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ChatRow:
    external_id: int
    chat_type: str  # private | group | supergroup | channel
    title: str | None = None
    username: str | None = None
    description: str | None = None
    members_count: int | None = None
    # TG channels/supergroups (and the peer of a private chat). None for a min
    # entity: a None never overwrites a stored hash.
    access_hash: int | None = None
    my_role: str | None = None  # member | admin | creator
    last_message_external_id: int | None = None
    last_message_at: datetime | None = None
    raw: dict | None = None


@dataclass
class UserRow:
    external_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None  # any format, normalised to E.164 by the repo
    is_bot: bool = False
    # TG only, from a non-min User of this account; None never overwrites.
    access_hash: int | None = None
    # None = unknown (keep stored), True/False = from the account's address book.
    is_contact: bool | None = None
    raw: dict | None = None


@dataclass
class MediaRow:
    file_type: str
    attach_index: int = 0
    file_external_id: str | None = None
    file_unique_id: str | None = None
    file_name: str | None = None
    file_size: int | None = None
    mime_type: str | None = None


@dataclass
class MessageRow:
    external_id: int
    sent_at: datetime
    # User sender (external id); its users row must be upserted first,
    # otherwise sender_user_id stays NULL instead of failing the FK.
    sender_external_id: int | None = None
    sender_chat_external_id: int | None = None
    reply_to_external_id: int | None = None
    forward_from_chat_external_id: int | None = None
    forward_from_message_external_id: int | None = None
    message_type: str = "text"
    text: str | None = None
    text_html: str | None = None
    is_edited: bool = False
    edited_at: datetime | None = None
    deleted_at: datetime | None = None
    views: int | None = None
    raw: dict | None = None
    # Platform hint (TG message.out), used only while the account's
    # external_user_id is unknown.
    is_outgoing_hint: bool | None = None
    media: list[MediaRow] = field(default_factory=list)


@dataclass
class SnapshotMemberRow:
    external_user_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    role: str | None = None
    raw: dict | None = None
