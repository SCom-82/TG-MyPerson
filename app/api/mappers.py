"""New models → the existing response schemas (ТЗ 2026-10-13 §6.3).

The REST contract does not change: chat_id / message_id / from_user_id /
tg_date carry external values. `id` is the internal id where the response has
a separate external field (messages, media); for chats and users `id` IS the
external id clients address them by, so it stays external.
"""

from app.models import Chat, Media, Message, User
from app.schemas import ChatResponse, MediaResponse, MessageResponse, UserResponse


def chat_response(c: Chat) -> ChatResponse:
    return ChatResponse(
        id=c.external_id,
        chat_type=c.chat_type,
        title=c.title,
        username=c.username,
        description=c.description,
        members_count=c.members_count,
        is_monitored=c.is_monitored,
        last_message_id=c.last_message_external_id,
        last_message_at=c.last_message_at,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


def user_response(u: User) -> UserResponse:
    return UserResponse(
        id=u.external_id,
        username=u.username,
        first_name=u.first_name,
        last_name=u.last_name,
        phone=u.phone,
        is_bot=u.is_bot,
        is_self=u.is_self,
        first_seen_at=u.first_seen_at,
        updated_at=u.updated_at,
    )


def media_response(m: Media) -> MediaResponse:
    return MediaResponse(
        id=m.id,
        file_id=m.file_external_id,
        file_unique_id=m.file_unique_id,
        file_type=m.file_type,
        file_name=m.file_name,
        file_size=m.file_size,
        mime_type=m.mime_type,
    )


def message_response(m: Message, chat_external_id: int, cls: type[MessageResponse] = MessageResponse):
    return cls(
        id=m.id,
        message_id=m.external_id,
        chat_id=chat_external_id,
        from_user_id=m.sender.external_id if m.sender is not None else None,
        from_user=user_response(m.sender) if cls.fill_from_user and m.sender is not None else None,
        sender_chat_id=m.sender_chat_external_id,
        reply_to_message_id=m.reply_to_external_id,
        forward_from_chat_id=m.forward_from_chat_external_id,
        forward_from_message_id=m.forward_from_message_external_id,
        message_type=m.message_type,
        text=m.text,
        text_html=m.text_html,
        tg_date=m.sent_at,
        is_outgoing=m.is_outgoing,
        is_edited=m.is_edited,
        edit_date=m.edited_at,
        views=m.views,
        media=[media_response(x) for x in m.media],
        created_at=m.created_at,
        deleted_at=m.deleted_at,
    )
