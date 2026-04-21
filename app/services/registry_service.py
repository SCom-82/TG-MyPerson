"""UsersRegistry service — canonical user aggregation from all sources.

Merge strategy: fill nulls / empty fields if incoming data has a value.
first_seen_at is NEVER updated on conflict; last_seen_at = now().
tags are not managed automatically (Phase 3 scope).
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UsersRegistry, UsersRegistrySource

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _better(existing: str | None, incoming: str | None) -> str | None:
    """Return the 'better' value: prefer incoming if existing is null/empty."""
    if not existing:
        return incoming or existing
    return existing


async def upsert_user_from_member(
    session: AsyncSession,
    member_data: dict,
    account_id: int | None,
    snapshot_id: int | None,
) -> UsersRegistry:
    """Upsert a user into UsersRegistry.

    Conflict resolution:
    - ON CONFLICT tg_user_id: update fields only if current value is null/empty.
    - If no tg_user_id: lookup by primary_username; create if not found.
    - first_seen_at is never updated; last_seen_at = now().
    """
    tg_user_id: int | None = member_data.get("tg_user_id")
    username: str | None = member_data.get("username") or None
    first_name: str | None = member_data.get("first_name") or None
    last_name: str | None = member_data.get("last_name") or None
    phone: str | None = member_data.get("phone") or None

    # Build primary_name
    parts = [p for p in [first_name, last_name] if p]
    primary_name = " ".join(parts) if parts else None

    now = _now()
    existing: UsersRegistry | None = None

    if tg_user_id is not None:
        result = await session.execute(
            select(UsersRegistry).where(UsersRegistry.tg_user_id == tg_user_id)
        )
        existing = result.scalar_one_or_none()
    elif username:
        result = await session.execute(
            select(UsersRegistry).where(UsersRegistry.primary_username == username)
        )
        existing = result.scalar_one_or_none()

    if existing is None:
        # Create new entry
        entry = UsersRegistry(
            tg_user_id=tg_user_id,
            primary_username=username,
            primary_name=primary_name,
            primary_phone=phone,
            first_seen_at=now,
            last_seen_at=now,
            tags=[],
        )
        session.add(entry)
        await session.flush()  # get id without committing
        return entry

    # Update: fill nulls / empty with incoming values
    if tg_user_id is not None and existing.tg_user_id is None:
        existing.tg_user_id = tg_user_id
    if _better(existing.primary_username, username) != existing.primary_username:
        existing.primary_username = username
    if _better(existing.primary_name, primary_name) != existing.primary_name:
        existing.primary_name = primary_name
    if _better(existing.primary_phone, phone) != existing.primary_phone:
        existing.primary_phone = phone
    # Never update first_seen_at
    existing.last_seen_at = now

    await session.flush()
    return existing


async def append_source(
    session: AsyncSession,
    registry_id: int,
    source_type: str,
    source_ref: str | None,
    account_id: int | None,
    observed_fields: dict | None = None,
) -> None:
    """Insert a new source record for a registry entry (no deduplication)."""
    source = UsersRegistrySource(
        registry_id=registry_id,
        source_type=source_type,
        source_ref=source_ref,
        account_id=account_id,
        observed_at=_now(),
        observed_fields=observed_fields,
    )
    session.add(source)
    await session.flush()


async def bulk_upsert_from_snapshot(
    session: AsyncSession,
    snapshot_id: int,
    members: list[dict],
    account_id: int | None,
) -> int:
    """Upsert all members from a snapshot into users_registry + registry_sources.

    Runs within the caller's transaction (no commit here).
    Returns count of processed members.
    """
    count = 0
    for member_data in members:
        try:
            registry_entry = await upsert_user_from_member(
                session=session,
                member_data=member_data,
                account_id=account_id,
                snapshot_id=snapshot_id,
            )
            await append_source(
                session=session,
                registry_id=registry_entry.id,
                source_type="snapshot",
                source_ref=str(snapshot_id),
                account_id=account_id,
                observed_fields={
                    k: member_data.get(k)
                    for k in ("username", "first_name", "last_name", "phone", "tg_user_id")
                    if member_data.get(k) is not None
                },
            )
            count += 1
        except Exception as exc:
            log.warning(
                "registry_service: failed to upsert member tg_user_id=%s: %s",
                member_data.get("tg_user_id"),
                exc,
            )

    return count
