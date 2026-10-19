"""chats of an account (ТЗ 2026-10-13 §2.4, §4.2)."""

from datetime import datetime

from sqlalchemy import Text, and_, case, cast, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, Chat
from app.repo import ensure_scope
from app.repo.rows import ChatRow


def _monotonic(new_col, old_col, new_id, old_id):
    """Value of the newer last message: last_message_* never moves back."""
    return case(
        (and_(new_id.is_not(None), or_(old_id.is_(None), new_id > old_id)), new_col),
        else_=old_col,
    )


async def upsert_chat(db: AsyncSession, account_id: int, row: ChatRow) -> int:
    """ON CONFLICT (account_id, external_id). Returns the internal chat id.

    is_monitored is set on insert only; None fields keep stored values.
    """
    ensure_scope(db, account_id)
    stmt = pg_insert(Chat).values(
        account_id=account_id,
        external_id=row.external_id,
        chat_type=row.chat_type,
        title=row.title,
        username=row.username,
        description=row.description,
        members_count=row.members_count,
        access_hash=row.access_hash,
        my_role=row.my_role,
        last_message_external_id=row.last_message_external_id,
        last_message_at=row.last_message_at,
        raw=row.raw,
    )
    ex = stmt.excluded
    t = Chat.__table__.c
    stmt = stmt.on_conflict_do_update(
        index_elements=[Chat.account_id, Chat.external_id],
        set_={
            "chat_type": ex.chat_type,
            "title": func.coalesce(ex.title, t.title),
            "username": func.coalesce(ex.username, t.username),
            "description": func.coalesce(ex.description, t.description),
            "members_count": func.coalesce(ex.members_count, t.members_count),
            "access_hash": func.coalesce(ex.access_hash, t.access_hash),
            "my_role": func.coalesce(ex.my_role, t.my_role),
            "last_message_external_id": _monotonic(
                ex.last_message_external_id, t.last_message_external_id,
                ex.last_message_external_id, t.last_message_external_id,
            ),
            "last_message_at": _monotonic(
                ex.last_message_at, t.last_message_at,
                ex.last_message_external_id, t.last_message_external_id,
            ),
            "raw": func.coalesce(ex.raw, t.raw),
        },
    ).returning(Chat.id)
    return (await db.execute(stmt)).scalar_one()


async def chat_pk(db: AsyncSession, account_id: int, external_id: int) -> int | None:
    ensure_scope(db, account_id)
    return (await db.execute(
        select(Chat.id).where(Chat.account_id == account_id, Chat.external_id == external_id)
    )).scalar_one_or_none()


async def get_chat(db: AsyncSession, account_id: int, external_id: int) -> Chat | None:
    ensure_scope(db, account_id)
    return (await db.execute(
        select(Chat).where(Chat.account_id == account_id, Chat.external_id == external_id)
    )).scalar_one_or_none()


async def list_chats(
    db: AsyncSession,
    account_id: int,
    *,
    chat_type: str | None = None,
    search: str | None = None,
    is_monitored: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Chat], int]:
    ensure_scope(db, account_id)
    flt = [Chat.account_id == account_id]
    if chat_type:
        flt.append(Chat.chat_type == chat_type)
    if is_monitored is not None:
        flt.append(Chat.is_monitored == is_monitored)
    if search:
        pattern = f"%{search}%"
        flt.append(or_(Chat.title.ilike(pattern), Chat.username.ilike(pattern)))
    total = (await db.execute(select(func.count()).select_from(Chat).where(*flt))).scalar_one()
    rows = (await db.execute(
        select(Chat).where(*flt)
        .order_by(Chat.last_message_at.desc().nullslast(), Chat.id)
        .limit(limit).offset(offset)
    )).scalars().all()
    return list(rows), total


async def set_monitored(db: AsyncSession, account_id: int, external_id: int, value: bool) -> Chat | None:
    ensure_scope(db, account_id)
    return (await db.execute(
        update(Chat)
        .where(Chat.account_id == account_id, Chat.external_id == external_id)
        .values(is_monitored=value)
        .returning(Chat)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def touch_last_message(
    db: AsyncSession, account_id: int, chat_id: int, external_id: int, at: datetime | None
) -> None:
    """Move last_message_* forward only."""
    ensure_scope(db, account_id)
    await db.execute(
        update(Chat)
        .where(
            Chat.account_id == account_id,
            Chat.id == chat_id,
            or_(Chat.last_message_external_id.is_(None), Chat.last_message_external_id <= external_id),
        )
        .values(last_message_external_id=external_id, last_message_at=at)
    )


async def external_ids(db: AsyncSession, account_id: int, chat_ids: set[int]) -> dict[int, int]:
    """internal chat id → external id, for building responses."""
    ensure_scope(db, account_id)
    if not chat_ids:
        return {}
    rows = await db.execute(
        select(Chat.id, Chat.external_id).where(Chat.account_id == account_id, Chat.id.in_(chat_ids))
    )
    return dict(rows.all())


SAVED_MESSAGES_TITLE = "Избранное"

# MAX private chats have no title of their own (plan 2026-10-19 Р-4, Р-5): the
# title is the peer's name. Peer = the key of raw.participants that is not the
# account, else external_id XOR the account's user id (how MAX builds dialog
# ids, pymax UserService.get_chat_id). Chat 0 is the account's saved messages.
_FILL_DIALOG_TITLES = text("""
UPDATE chats c SET title = n.name
FROM (
    SELECT ch.id,
           CASE WHEN ch.external_id = 0 THEN :saved
                ELSE coalesce(nullif(trim(concat_ws(' ', u.first_name, u.last_name)), ''), u.username)
           END AS name
    FROM chats ch
    CROSS JOIN LATERAL (
        SELECT coalesce(
            (SELECT k::bigint
             FROM jsonb_object_keys(CASE WHEN jsonb_typeof(ch.raw -> 'participants') = 'object'
                                         THEN ch.raw -> 'participants' ELSE '{}'::jsonb END) AS k
             WHERE k ~ '^-?[0-9]+$' AND k::bigint <> :me
             LIMIT 1),
            ch.external_id # :me
        ) AS peer
    ) p
    LEFT JOIN users u ON u.account_id = ch.account_id AND u.external_id = p.peer
    WHERE ch.account_id = :a AND ch.chat_type = 'private'
      AND (CAST(:ids AS bigint[]) IS NULL OR ch.external_id = ANY(CAST(:ids AS bigint[])))
) n
WHERE c.account_id = :a AND c.id = n.id AND n.name IS NOT NULL AND c.title IS DISTINCT FROM n.name
""")


async def fill_dialog_titles(db: AsyncSession, account_id: int, chat_ids: list[int] | None = None) -> int:
    """MAX private chats: title = the peer's name (recomputed when it changed). Returns rows changed.

    Other platforms and accounts without a known own user id: nothing (0).
    """
    ensure_scope(db, account_id)
    platform, me = (await db.execute(
        select(Account.platform, Account.external_user_id).where(Account.id == account_id)
    )).one_or_none() or (None, None)
    if platform != "max" or me is None:
        return 0
    result = await db.execute(_FILL_DIALOG_TITLES, {
        "a": account_id, "me": me, "saved": SAVED_MESSAGES_TITLE,
        "ids": list(chat_ids) if chat_ids is not None else None,
    })
    return result.rowcount or 0


# MAX chat statuses after which the account sees nothing new (plan R3.1): the owner
# deleted the group (CLOSED), the account was removed or left (LEFT), MAX refuses
# the chat (DENIED — our mark for chat.denied).
INACTIVE_STATUSES = ("CLOSED", "LEFT", "REMOVED", "DENIED")


def monitoring_after(old_status: str | None, new_status: str | None) -> bool | None:
    """is_monitored a status change implies; None = keep it (it is also the owner's setting).

    Inactive → off; ACTIVE after an inactive status (the account was added again) → on.
    """
    if new_status in INACTIVE_STATUSES:
        return False
    if new_status == "ACTIVE" and old_status in INACTIVE_STATUSES:
        return True
    return None


async def statuses(db: AsyncSession, account_id: int, external_ids: list[int]) -> dict[int, str | None]:
    ensure_scope(db, account_id)
    if not external_ids:
        return {}
    rows = await db.execute(
        select(Chat.external_id, Chat.raw["status"].astext)
        .where(Chat.account_id == account_id, Chat.external_id.in_(external_ids))
    )
    return dict(rows.all())


async def set_status(db: AsyncSession, account_id: int, external_id: int, status: str,
                     old_status: str | None, *, store: bool = True) -> None:
    """raw.status (store=False: the upsert wrote it already) and monitoring by monitoring_after()."""
    ensure_scope(db, account_id)
    t = Chat.__table__.c
    values: dict = {}
    if store:
        base = case((func.jsonb_typeof(t.raw) == "object", t.raw), else_=text("'{}'::jsonb"))  # NULL or JSON null
        values["raw"] = func.jsonb_set(base, text("'{status}'::text[]"), func.to_jsonb(cast(status, Text)))
    monitored = monitoring_after(old_status, status)
    if monitored is not None:
        values["is_monitored"] = monitored
    if values:
        await db.execute(
            update(Chat).where(Chat.account_id == account_id, Chat.external_id == external_id).values(**values)
        )
