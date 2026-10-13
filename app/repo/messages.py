"""messages of an account (ТЗ 2026-10-13 §2.4, §4.2, §5.4)."""

from datetime import datetime

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, Chat, Message, User
from app.repo import ensure_scope
from app.repo.chats import touch_last_message
from app.repo.media import upsert_media
from app.repo.rows import MessageRow


async def upsert_message(db: AsyncSession, account_id: int, chat_id: int, row: MessageRow) -> int:
    """ON CONFLICT (account_id, chat_id, external_id). Returns the internal message id.

    Merge rules (TG + MAX): a non-edited copy never overwrites an edited one,
    an older edit never overwrites a newer one (edited_at);
    deleted_at once set stays; views = coalesce(new, old); is_outgoing is
    recomputed every time as sender == accounts.external_user_id (the
    platform hint only without a user sender or while the account's own id
    is unknown).
    sender_user_id is the account's users row of the sender, NULL if absent.
    Media rows (row.media) are upserted; the chat's last message moves forward.
    """
    ensure_scope(db, account_id)
    account_user = (
        select(Account.external_user_id).where(Account.id == account_id).scalar_subquery()
    )
    sender_user = (
        select(User.id)
        .where(User.account_id == account_id, User.external_id == row.sender_external_id)
        .scalar_subquery()
        if row.sender_external_id is not None
        else None
    )
    # sender == the account itself; the platform hint (TG message.out) only
    # where that cannot be decided: no user sender (channel post) or the
    # account's own id still unknown.
    hint = bool(row.is_outgoing_hint)
    if row.sender_external_id is None:
        is_outgoing = hint
    else:
        is_outgoing = func.coalesce(account_user == row.sender_external_id, hint)

    stmt = pg_insert(Message).values(
        account_id=account_id,
        chat_id=chat_id,
        external_id=row.external_id,
        sender_user_id=sender_user,
        sender_chat_external_id=row.sender_chat_external_id,
        reply_to_external_id=row.reply_to_external_id,
        forward_from_chat_external_id=row.forward_from_chat_external_id,
        forward_from_message_external_id=row.forward_from_message_external_id,
        message_type=row.message_type,
        text=row.text,
        text_html=row.text_html,
        sent_at=row.sent_at,
        is_outgoing=is_outgoing,
        is_edited=row.is_edited,
        edited_at=row.edited_at,
        deleted_at=row.deleted_at,
        views=row.views,
        raw=row.raw,
    )
    ex = stmt.excluded
    t = Message.__table__.c
    stmt = stmt.on_conflict_do_update(
        index_elements=[Message.account_id, Message.chat_id, Message.external_id],
        set_={
            "sender_user_id": func.coalesce(ex.sender_user_id, t.sender_user_id),
            "message_type": ex.message_type,
            "text": ex.text,
            "text_html": ex.text_html,
            "is_outgoing": ex.is_outgoing,
            "is_edited": ex.is_edited,
            "edited_at": ex.edited_at,
            "deleted_at": func.coalesce(t.deleted_at, ex.deleted_at),
            "views": func.coalesce(ex.views, t.views),
            "raw": ex.raw,
        },
        # An older edit arriving late must not overwrite a newer one (review 13.10).
        where=or_(
            t.is_edited.is_(False),
            and_(ex.is_edited, or_(t.edited_at.is_(None), ex.edited_at.is_(None), ex.edited_at >= t.edited_at)),
        ),
    ).returning(Message.id)
    message_id = (await db.execute(stmt)).scalar_one_or_none()
    if message_id is None:
        # The stored row is an edit and this copy is not: keep it as is.
        message_id = (await db.execute(
            select(Message.id).where(
                Message.account_id == account_id,
                Message.chat_id == chat_id,
                Message.external_id == row.external_id,
            )
        )).scalar_one()

    if row.media:
        await upsert_media(db, account_id, message_id, row.media)
    await touch_last_message(db, account_id, chat_id, row.external_id, row.sent_at)
    return message_id


async def mark_deleted(
    db: AsyncSession, account_id: int, chat_id: int | None, external_ids: list[int], at: datetime
) -> int:
    """Soft delete; returns the number of rows marked now.

    chat_id None = TG update without a chat (private chats and basic groups):
    those message ids are unique within the account's own counter.
    """
    ensure_scope(db, account_id)
    if not external_ids:
        return 0
    flt = [
        Message.account_id == account_id,
        Message.external_id.in_(external_ids),
        Message.deleted_at.is_(None),
    ]
    if chat_id is not None:
        flt.append(Message.chat_id == chat_id)
    else:
        flt.append(Message.chat_id.in_(
            select(Chat.id).where(Chat.account_id == account_id, Chat.chat_type.in_(("private", "group")))
        ))
    result = await db.execute(update(Message).where(*flt).values(deleted_at=at))
    return result.rowcount or 0


async def get_messages(
    db: AsyncSession,
    account_id: int,
    *,
    chat_external_id: int | None = None,
    from_user_external_id: int | None = None,
    search: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    message_type: str | None = None,
    is_outgoing: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Message], int]:
    """Messages of the account by external ids; media and sender preloaded."""
    ensure_scope(db, account_id)
    flt = [Message.account_id == account_id]
    if chat_external_id is not None:
        flt.append(Message.chat_id.in_(
            select(Chat.id).where(Chat.account_id == account_id, Chat.external_id == chat_external_id)
        ))
    if from_user_external_id is not None:
        flt.append(Message.sender_user_id.in_(
            select(User.id).where(User.account_id == account_id, User.external_id == from_user_external_id)
        ))
    if search:
        flt.append(Message.text.ilike(f"%{search}%"))
    if date_from:
        flt.append(Message.sent_at >= date_from)
    if date_to:
        flt.append(Message.sent_at <= date_to)
    if message_type:
        flt.append(Message.message_type == message_type)
    if is_outgoing is not None:
        flt.append(Message.is_outgoing == is_outgoing)

    total = (await db.execute(select(func.count()).select_from(Message).where(*flt))).scalar_one()
    rows = (await db.execute(
        select(Message).where(*flt).order_by(Message.sent_at.desc(), Message.id.desc())
        .limit(limit).offset(offset)
    )).scalars().all()
    return list(rows), total


async def get_message(
    db: AsyncSession, account_id: int, chat_external_id: int, external_id: int
) -> Message | None:
    ensure_scope(db, account_id)
    return (await db.execute(
        select(Message)
        .join(Chat, and_(Chat.account_id == Message.account_id, Chat.id == Message.chat_id))
        .where(
            Message.account_id == account_id,
            Chat.external_id == chat_external_id,
            Message.external_id == external_id,
        )
    )).scalar_one_or_none()


async def search(db: AsyncSession, account_id: int, q: str, limit: int) -> list[tuple[Message, Chat]]:
    """Text search over the account's messages (MAX search_global)."""
    ensure_scope(db, account_id)
    rows = await db.execute(
        select(Message, Chat)
        .join(Chat, and_(Chat.account_id == Message.account_id, Chat.id == Message.chat_id))
        .where(Message.account_id == account_id, Message.text.ilike(f"%{q}%"))
        .order_by(Message.sent_at.desc())
        .limit(limit)
    )
    return [(m, c) for m, c in rows.all()]
