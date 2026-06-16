"""test_partition_maintenance.py — Layers 1-2 of the self-sufficient rotation.

Covers app/services/partition_maintenance.py:
  - ensure_partitions() is idempotent and creates current + next two months
  - ensure_partitions() skips work when the advisory lock is held elsewhere
  - ensure_partitions() swallows DB errors (never crashes the app)
  - partition_loop() survives an exception in a single tick

Requires real Postgres on :5433 with migrations applied through head (008).
"""

import asyncio
import logging
from datetime import date

import pytest
from sqlalchemy import text

import app.services.partition_maintenance as pm
from tests.conftest import _TestSessionFactory, TEST_DB_URL


def _expected_month_names():
    names = []
    for offset in (0, 1, 2):
        month = date.today().month + offset
        year = date.today().year + (month - 1) // 12
        month = ((month - 1) % 12) + 1
        names.append(f"audit_logs_{year:04d}_{month:02d}")
    return names


@pytest.mark.asyncio
async def test_ensure_partitions_creates_current_plus_two(caplog):
    """ensure_partitions() must materialise current + next two months."""
    with caplog.at_level(logging.INFO):
        await pm.ensure_partitions()

    async with _TestSessionFactory() as s:
        for name in _expected_month_names():
            cnt = (
                await s.execute(
                    text("SELECT COUNT(*) FROM pg_class WHERE relname = :n"),
                    {"n": name},
                )
            ).scalar_one()
            assert cnt == 1, f"partition {name} must exist after ensure_partitions()"


@pytest.mark.asyncio
async def test_ensure_partitions_idempotent():
    """Calling ensure_partitions() twice must not raise."""
    await pm.ensure_partitions()
    await pm.ensure_partitions()  # must be a no-op, no error


@pytest.mark.asyncio
async def test_ensure_partitions_skips_when_locked(caplog):
    """When the advisory lock is held elsewhere, ensure_partitions() must skip.

    The lock is held on a separate raw asyncpg connection (session-level lock).
    The app engine is disposed first so ensure_partitions() opens a fresh
    connection bound to this test's event loop (avoids stale-pool reuse).
    """
    import asyncpg

    from app.database import engine as app_engine

    await app_engine.dispose()

    dsn = TEST_DB_URL.replace("+asyncpg", "")
    raw = await asyncpg.connect(dsn)
    try:
        await raw.execute("SELECT pg_advisory_lock($1)", pm._ADVISORY_LOCK_KEY)

        with caplog.at_level(logging.INFO):
            await pm.ensure_partitions()

        assert any(
            "lock held elsewhere" in r.message for r in caplog.records
        ), "ensure_partitions() must log a skip when the lock is held"
    finally:
        await raw.execute("SELECT pg_advisory_unlock($1)", pm._ADVISORY_LOCK_KEY)
        await raw.close()


@pytest.mark.asyncio
async def test_ensure_partitions_swallows_db_errors(monkeypatch, caplog):
    """A DB failure inside ensure_partitions() must be logged, not raised."""

    def boom(*args, **kwargs):
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(pm, "async_session", boom)

    with caplog.at_level(logging.WARNING):
        await pm.ensure_partitions()  # must NOT raise

    assert any("ensure_partitions: failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_partition_loop_survives_tick_exception(monkeypatch):
    """partition_loop() must swallow a per-tick exception and keep going."""
    calls = {"n": 0}

    async def failing_ensure():
        calls["n"] += 1
        raise RuntimeError("tick failed")

    async def cancel_sleep(_seconds):
        # End the otherwise-infinite loop after the first tick.
        raise asyncio.CancelledError

    monkeypatch.setattr(pm, "ensure_partitions", failing_ensure)
    monkeypatch.setattr(pm.asyncio, "sleep", cancel_sleep)

    with pytest.raises(asyncio.CancelledError):
        await pm.partition_loop()

    # The tick ran once; its RuntimeError was swallowed (we reached sleep()).
    assert calls["n"] == 1
