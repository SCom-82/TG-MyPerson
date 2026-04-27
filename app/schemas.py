from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


ParseMode = Literal["markdown", "html"]


# --- Auth ---

class AuthStatusResponse(BaseModel):
    connected: bool
    phone_number: str | None = None
    user_id: int | None = None
    username: str | None = None

class AuthMeResponse(BaseModel):
    user_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    is_premium: bool = False
    is_verified: bool = False
    is_bot: bool = False
    dc_id: int | None = None
    lang_code: str | None = None

class LoginRequest(BaseModel):
    phone_number: str

class LoginCodeRequest(BaseModel):
    code: str
    password: str | None = None

class SessionImportRequest(BaseModel):
    session_string: str


# --- Users ---

class UserResponse(BaseModel):
    id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    is_bot: bool
    is_self: bool
    first_seen_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Chats ---

class ChatResponse(BaseModel):
    id: int
    chat_type: str
    title: str | None = None
    username: str | None = None
    description: str | None = None
    members_count: int | None = None
    is_monitored: bool
    last_message_id: int | None = None
    last_message_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

class ChatUpdateRequest(BaseModel):
    is_monitored: bool

class MyRightsResponse(BaseModel):
    chat_id: int
    is_member: bool = False
    is_admin: bool = False
    is_creator: bool = False
    can_post_messages: bool = False
    can_edit_messages: bool = False
    can_delete_messages: bool = False
    can_pin_messages: bool = False
    can_add_admins: bool = False
    can_invite_users: bool = False
    can_change_info: bool = False
    raw: dict | None = None


# --- Media ---

class MediaResponse(BaseModel):
    id: int
    file_id: str | None = None
    file_unique_id: str | None = None
    file_type: str
    file_name: str | None = None
    file_size: int | None = None
    mime_type: str | None = None

    model_config = {"from_attributes": True}


# --- Messages ---

class MessageResponse(BaseModel):
    id: int
    message_id: int
    chat_id: int
    from_user_id: int | None = None
    from_user: UserResponse | None = None
    reply_to_message_id: int | None = None
    forward_from_chat_id: int | None = None
    forward_from_message_id: int | None = None
    message_type: str
    text: str | None = None
    text_html: str | None = None
    tg_date: datetime
    is_outgoing: bool
    is_edited: bool
    edit_date: datetime | None = None
    views: int | None = None
    media: list[MediaResponse] = Field(default_factory=list)
    created_at: datetime

    model_config = {"from_attributes": True}

class SendMessageRequest(BaseModel):
    chat_id: int
    text: str
    reply_to_message_id: int | None = None
    parse_mode: ParseMode | None = None
    schedule_date: datetime | None = Field(
        default=None,
        description="If set, message is scheduled for this UTC datetime instead of sent immediately",
    )

class ForwardMessageRequest(BaseModel):
    from_chat_id: int
    to_chat_id: int
    message_id: int | None = None
    message_ids: list[int] | None = Field(
        default=None,
        description="Batch forward. If provided, overrides message_id",
    )

class SendPollRequest(BaseModel):
    chat_id: int
    question: str
    options: list[str] = Field(min_length=2, max_length=10)
    is_anonymous: bool = True
    allows_multiple: bool = False
    quiz_correct_option: int | None = Field(
        default=None,
        description="If set, poll is a quiz; index of correct answer",
    )
    schedule_date: datetime | None = None
    reply_to_message_id: int | None = None


# --- Scheduled messages ---

class ScheduledMessageItem(BaseModel):
    message_id: int
    chat_id: int
    text: str | None = None
    date: datetime
    has_media: bool = False
    media_type: str | None = None


# --- Sync ---

class BackfillRequest(BaseModel):
    chat_id: int
    limit: int = Field(default=1000, ge=1, le=10000)

class SyncStateResponse(BaseModel):
    chat_id: int
    chat_title: str | None = None
    oldest_message_id: int | None = None
    newest_message_id: int | None = None
    is_fully_synced: bool
    total_messages_synced: int
    last_backfill_at: datetime | None = None

    model_config = {"from_attributes": True}


# --- Pagination ---

class PaginatedResponse(BaseModel):
    items: list
    total: int
    limit: int
    offset: int


# --- Join / Leave / Resolve ---

class JoinChatRequest(BaseModel):
    target: str = Field(description="@username, https://t.me/channel, or https://t.me/+inviteHash")

class LeaveChatRequest(BaseModel):
    chat_id: int

class ResolveRequest(BaseModel):
    target: str = Field(description="@username, https://t.me/channel, or invite link")

class ResolveResponse(BaseModel):
    id: int | None = None
    type: str  # user, channel, supergroup, group, chat_invite
    title: str | None = None
    username: str | None = None
    members_count: int | None = None
    description: str | None = None
    is_joined: bool | None = None


# --- Edit / Pin / React ---

class EditMessageRequest(BaseModel):
    chat_id: int
    message_id: int
    text: str
    parse_mode: ParseMode | None = None
    scheduled: bool = Field(
        default=False,
        description="True if editing a scheduled (not yet sent) message",
    )

class ReactRequest(BaseModel):
    emoticon: str | None = Field(None, description="Emoji to react with, or null to remove")

class ArchiveRequest(BaseModel):
    archived: bool


# --- Members ---

class MemberResponse(BaseModel):
    user_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    role: str  # creator, admin, member


# --- Resolve user ---

class ResolveUserRequest(BaseModel):
    username: str


# --- Bulk resolve by tg_user_id ---

class BulkResolveByIdRequest(BaseModel):
    user_ids: list[int] = Field(
        description="List of Telegram user IDs to resolve. Max 500 per request.",
    )
    persist: bool = Field(
        default=True,
        description="If true, resolved users are upserted into users_registry.",
    )


class ResolvedUserItem(BaseModel):
    user_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    is_premium: bool = False


class UnresolvedUserItem(BaseModel):
    user_id: int
    error: str


class BulkResolveStats(BaseModel):
    requested: int
    resolved: int
    unresolved: int
    took_ms: int


class BulkResolveResponse(BaseModel):
    session: str
    resolved: list[ResolvedUserItem]
    unresolved: list[UnresolvedUserItem]
    stats: BulkResolveStats
