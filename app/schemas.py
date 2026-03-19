from datetime import datetime

from pydantic import BaseModel, Field


# --- Auth ---

class AuthStatusResponse(BaseModel):
    connected: bool
    phone_number: str | None = None
    user_id: int | None = None
    username: str | None = None

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

class ForwardMessageRequest(BaseModel):
    from_chat_id: int
    message_id: int
    to_chat_id: int


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
