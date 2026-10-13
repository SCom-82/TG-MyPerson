"""raw_events — platform frames before typed processing (ТЗ 2026-10-13 §2.4)."""

from datetime import datetime

from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RawEvent
from app.repo import ensure_owner, ensure_scope


async def add_raw_event(
    db: AsyncSession,
    account_id: int,
    kind: str,
    payload: dict | None,
    *,
    normalized: bool = False,
    error: str | None = None,
) -> int:
    ensure_scope(db, account_id)
    return (await db.execute(
        pg_insert(RawEvent)
        .values(account_id=account_id, kind=kind, payload=payload, normalized=normalized, error=error)
        .returning(RawEvent.id)
    )).scalar_one()


async def mark_normalized(db: AsyncSession, account_id: int, event_id: int, error: str | None = None) -> None:
    ensure_scope(db, account_id)
    await db.execute(
        update(RawEvent)
        .where(RawEvent.account_id == account_id, RawEvent.id == event_id)
        .values(normalized=error is None, error=error)
    )


async def purge_older_than(db: AsyncSession, before: datetime) -> int:
    """Retention across all accounts — owner session only."""
    ensure_owner(db)
    result = await db.execute(delete(RawEvent).where(RawEvent.received_at < before))
    return result.rowcount or 0
