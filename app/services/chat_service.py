import logging
import re

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.tl.types import Chat, Channel, User
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest, DeleteChatUserRequest

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


def _parse_target(target: str) -> tuple[str, str]:
    """Parse target string into (type, value).
    Returns ('invite', hash) or ('username', username).
    """
    # Invite links: https://t.me/+HASH, https://t.me/joinchat/HASH, t.me/+HASH
    invite_match = re.search(r'(?:t\.me/\+|t\.me/joinchat/)([a-zA-Z0-9_-]+)', target)
    if invite_match:
        return 'invite', invite_match.group(1)

    # Username: @username or https://t.me/username
    username_match = re.search(r't\.me/([a-zA-Z0-9_]+)', target)
    if username_match:
        return 'username', username_match.group(1)

    # Plain @username or username
    clean = target.lstrip('@').strip()
    return 'username', clean


async def join_chat(session: AsyncSession, client, target: str) -> TgChat:
    """Join a channel/group by username or invite link. Returns upserted TgChat."""
    target_type, value = _parse_target(target)

    if target_type == 'invite':
        updates = await client(ImportChatInviteRequest(value))
        # ImportChatInviteRequest returns Updates with chats
        entity = None
        if hasattr(updates, 'chats') and updates.chats:
            chat_obj = updates.chats[0]
            entity = await client.get_entity(chat_obj.id)
        if entity is None:
            raise ValueError("Failed to get entity after joining via invite link")
    else:
        entity = await client.get_entity(value)
        if isinstance(entity, (Channel, Chat)):
            await client(JoinChannelRequest(entity))
        # If it's a User (private chat), no join needed

    return await upsert_chat(session, entity)


async def leave_chat(client, chat_id: int) -> None:
    """Leave a channel or group."""
    entity = await client.get_entity(chat_id)
    if isinstance(entity, Channel):
        await client(LeaveChannelRequest(entity))
    elif isinstance(entity, Chat):
        me = await client.get_me()
        await client(DeleteChatUserRequest(entity.id, me.id))
    else:
        raise ValueError(f"Cannot leave entity of type {type(entity).__name__}")


async def resolve_target(client, target: str) -> dict:
    """Resolve a target (username or invite link) without joining. Returns info dict."""
    target_type, value = _parse_target(target)

    if target_type == 'invite':
        result = await client(CheckChatInviteRequest(value))
        # ChatInvite or ChatInviteAlready
        type_name = type(result).__name__
        if type_name == 'ChatInviteAlready':
            chat = result.chat
            return {
                'id': _get_chat_id(chat),
                'type': _get_chat_type(chat),
                'title': _get_title(chat),
                'username': getattr(chat, 'username', None),
                'members_count': getattr(chat, 'participants_count', None),
                'description': getattr(chat, 'about', None),
                'is_joined': True,
            }
        else:
            # ChatInvite — not yet joined
            return {
                'id': None,
                'type': 'chat_invite',
                'title': getattr(result, 'title', None),
                'username': None,
                'members_count': getattr(result, 'participants_count', None),
                'description': getattr(result, 'about', None),
                'is_joined': False,
            }
    else:
        entity = await client.get_entity(value)
        return {
            'id': _get_chat_id(entity) if not isinstance(entity, User) else entity.id,
            'type': _get_chat_type(entity) if not isinstance(entity, User) else 'user',
            'title': _get_title(entity),
            'username': getattr(entity, 'username', None),
            'members_count': getattr(entity, 'participants_count', None),
            'description': getattr(entity, 'about', None),
            'is_joined': None,
        }


async def get_members(client, chat_id: int, search: str | None = None, limit: int = 200) -> list[dict]:
    """Get participants of a chat/channel."""
    entity = await client.get_entity(chat_id)
    participants = await client.get_participants(entity, search=search or '', limit=limit)

    members = []
    for p in participants:
        role = 'member'
        if hasattr(p, 'participant'):
            part = p.participant
            part_type = type(part).__name__
            if 'Creator' in part_type:
                role = 'creator'
            elif 'Admin' in part_type:
                role = 'admin'
        members.append({
            'user_id': p.id,
            'username': p.username,
            'first_name': p.first_name,
            'last_name': p.last_name,
            'role': role,
        })
    return members


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
