"""test_registry_service.py — UsersRegistry upsert logic tests.

Tests (require real Postgres on :5433):
  1. New user with tg_user_id → creates row in users_registry
  2. Same tg_user_id, different username → primary_username NOT overwritten (fill-nulls)
  3. Without tg_user_id, only username → row created with tg_user_id=NULL
  4. Each upsert appends to users_registry_sources
  5. last_seen_at updated on second upsert; first_seen_at NOT updated
  6. Null/empty fields filled from incoming data on second upsert
"""

import pytest
import asyncio
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _TestSessionFactory
from app.services.registry_service import upsert_user_from_member, append_source
from app.models import UsersRegistry, UsersRegistrySource

# Use high IDs to avoid collision with real data
_BASE_TG_ID = 9_000_000


async def _new_session():
    """Return a fresh AsyncSession (caller must close/commit it)."""
    return _TestSessionFactory()


async def _cleanup(tg_id: int | None = None, username_prefix: str | None = None):
    """Clean up test rows by tg_user_id or username prefix."""
    async with _TestSessionFactory() as s:
        if tg_id is not None:
            await s.execute(
                text("DELETE FROM users_registry_sources WHERE registry_id IN "
                     "(SELECT id FROM users_registry WHERE tg_user_id = :tid)"),
                {"tid": tg_id},
            )
            await s.execute(
                text("DELETE FROM users_registry WHERE tg_user_id = :tid"),
                {"tid": tg_id},
            )
        if username_prefix is not None:
            await s.execute(
                text("DELETE FROM users_registry_sources WHERE registry_id IN "
                     "(SELECT id FROM users_registry WHERE primary_username LIKE :p)"),
                {"p": username_prefix + "%"},
            )
            await s.execute(
                text("DELETE FROM users_registry WHERE primary_username LIKE :p"),
                {"p": username_prefix + "%"},
            )
        await s.commit()


# ---------------------------------------------------------------------------
# Test 1: New user with tg_user_id → row created
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_new_user_with_tg_user_id_creates_row():
    """upsert_user_from_member with tg_user_id must create a UsersRegistry row."""
    tg_id = _BASE_TG_ID + 1
    await _cleanup(tg_id=tg_id)

    member_data = {
        "tg_user_id": tg_id,
        "username": "test_alice",
        "first_name": "Alice",
        "last_name": "Smith",
        "phone": None,
    }

    async with _TestSessionFactory() as session:
        result = await upsert_user_from_member(
            session=session,
            member_data=member_data,
            account_id=None,
            snapshot_id=None,
        )
        await session.commit()

    assert result is not None
    assert result.tg_user_id == tg_id
    assert result.primary_username == "test_alice"
    assert result.primary_name == "Alice Smith"
    assert result.id is not None

    await _cleanup(tg_id=tg_id)


# ---------------------------------------------------------------------------
# Test 2: Same tg_user_id, different username → primary_username NOT overwritten
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_tg_user_id_username_not_overwritten():
    """Fill-nulls policy: existing non-null primary_username must not be replaced."""
    tg_id = _BASE_TG_ID + 2
    await _cleanup(tg_id=tg_id)

    # First insert
    async with _TestSessionFactory() as session:
        first_data = {"tg_user_id": tg_id, "username": "test_bob_original", "first_name": "Bob"}
        entry = await upsert_user_from_member(session, first_data, None, None)
        await session.commit()
    original_first_seen = entry.first_seen_at

    await asyncio.sleep(0.02)

    # Second upsert — different username
    async with _TestSessionFactory() as session:
        second_data = {"tg_user_id": tg_id, "username": "test_bob_new", "first_name": "Bob"}
        updated = await upsert_user_from_member(session, second_data, None, None)
        await session.commit()

    assert updated.primary_username == "test_bob_original", (
        "primary_username must not be overwritten when already set (fill-nulls policy)"
    )
    assert updated.last_seen_at >= original_first_seen, (
        "last_seen_at must be >= first_seen_at after second upsert"
    )

    await _cleanup(tg_id=tg_id)


# ---------------------------------------------------------------------------
# Test 3: No tg_user_id, only username → created with tg_user_id=NULL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_tg_user_id_creates_row_with_null():
    """Without tg_user_id, upsert must create row with tg_user_id=NULL."""
    await _cleanup(username_prefix="test_charlie_noid")

    member_data = {
        "tg_user_id": None,
        "username": "test_charlie_noid",
        "first_name": "Charlie",
    }

    async with _TestSessionFactory() as session:
        result = await upsert_user_from_member(session, member_data, None, None)
        await session.commit()

    assert result is not None
    assert result.tg_user_id is None, "tg_user_id must be NULL when not provided"
    assert result.primary_username == "test_charlie_noid"

    await _cleanup(username_prefix="test_charlie_noid")


# ---------------------------------------------------------------------------
# Test 4: append_source creates UsersRegistrySource row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_append_source_creates_registry_source():
    """Each append_source call must insert a row in users_registry_sources."""
    tg_id = _BASE_TG_ID + 4
    await _cleanup(tg_id=tg_id)

    async with _TestSessionFactory() as session:
        member_data = {"tg_user_id": tg_id, "username": "test_dave"}
        entry = await upsert_user_from_member(session, member_data, None, None)
        await session.commit()
        entry_id = entry.id

    async with _TestSessionFactory() as session:
        await append_source(
            session=session,
            registry_id=entry_id,
            source_type="snapshot",
            source_ref="snap-42",
            account_id=None,
            observed_fields={"username": "test_dave"},
        )
        await session.commit()

    async with _TestSessionFactory() as session:
        result = await session.execute(
            select(UsersRegistrySource).where(UsersRegistrySource.registry_id == entry_id)
        )
        sources = result.scalars().all()

    assert len(sources) >= 1, f"Expected at least 1 source, got {len(sources)}"
    assert sources[0].source_type == "snapshot"
    assert sources[0].source_ref == "snap-42"

    await _cleanup(tg_id=tg_id)


# ---------------------------------------------------------------------------
# Test 5: first_seen_at never updated; last_seen_at updated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_seen_at_never_updated():
    """first_seen_at must never be updated; last_seen_at must be updated."""
    tg_id = _BASE_TG_ID + 5
    await _cleanup(tg_id=tg_id)

    async with _TestSessionFactory() as session:
        first_data = {"tg_user_id": tg_id, "username": "test_eve"}
        entry = await upsert_user_from_member(session, first_data, None, None)
        await session.commit()
    original_first_seen = entry.first_seen_at

    await asyncio.sleep(0.02)

    async with _TestSessionFactory() as session:
        second_data = {"tg_user_id": tg_id, "username": "test_eve2"}
        updated = await upsert_user_from_member(session, second_data, None, None)
        await session.commit()

    assert updated.first_seen_at == original_first_seen, (
        "first_seen_at must never be updated on subsequent upserts"
    )
    assert updated.last_seen_at > original_first_seen, (
        "last_seen_at must be updated to current time"
    )

    await _cleanup(tg_id=tg_id)


# ---------------------------------------------------------------------------
# Test 6: Null fields filled from incoming data
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_null_fields_filled_from_incoming():
    """If existing field is null, incoming non-null value must fill it."""
    tg_id = _BASE_TG_ID + 6
    await _cleanup(tg_id=tg_id)

    async with _TestSessionFactory() as session:
        first_data = {"tg_user_id": tg_id, "username": "test_frank", "phone": None}
        entry = await upsert_user_from_member(session, first_data, None, None)
        await session.commit()
    assert entry.primary_phone is None

    async with _TestSessionFactory() as session:
        second_data = {"tg_user_id": tg_id, "username": "test_frank", "phone": "+79001112233"}
        updated = await upsert_user_from_member(session, second_data, None, None)
        await session.commit()

    assert updated.primary_phone == "+79001112233", (
        "Null phone must be filled from incoming data"
    )

    await _cleanup(tg_id=tg_id)
