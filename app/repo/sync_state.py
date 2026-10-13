"""sync_state — backfill/catch-up cursors per account chat (ТЗ 2026-10-13 §2.4)."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SyncState
from app.repo import ensure_scope

FIELDS = frozenset({
    "oldest_external_id", "newest_external_id", "oldest_ts", "newest_ts",
    "is_fully_synced", "total_synced", "last_backfill_at", "last_catchup_at",
})


async def get_state(db: AsyncSession, account_id: int, chat_id: int) -> SyncState | None:
    ensure_scope(db, account_id)
    return (await db.execute(
        select(SyncState).where(SyncState.account_id == account_id, SyncState.chat_id == chat_id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def upsert_state(db: AsyncSession, account_id: int, chat_id: int, **fields) -> None:
    """ON CONFLICT (account_id, chat_id): only the given fields change."""
    ensure_scope(db, account_id)
    unknown = set(fields) - FIELDS
    if unknown:
        raise ValueError(f"unknown sync_state fields: {sorted(unknown)}")
    stmt = pg_insert(SyncState).values(account_id=account_id, chat_id=chat_id, **fields)
    if fields:
        stmt = stmt.on_conflict_do_update(
            index_elements=[SyncState.account_id, SyncState.chat_id],
            set_={k: stmt.excluded[k] for k in fields},
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=[SyncState.account_id, SyncState.chat_id])
    await db.execute(stmt)


async def list_states(db: AsyncSession, account_id: int):
    """(SyncState, chat external id, chat title) of the account's chats."""
    from app.models import Chat

    ensure_scope(db, account_id)
    return (await db.execute(
        select(SyncState, Chat.external_id, Chat.title)
        .join(Chat, (Chat.account_id == SyncState.account_id) & (Chat.id == SyncState.chat_id))
        .where(SyncState.account_id == account_id)
        .order_by(Chat.external_id)
    )).all()
