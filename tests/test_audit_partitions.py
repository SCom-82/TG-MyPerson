"""test_audit_partitions.py — C2: DB partition helper functions.

Tests require real Postgres on :5433 with migrations 001→005 applied.

Tests:
  1. create_audit_partition(1) creates next month's partition (idempotent)
  2. create_audit_partition(2) creates partition 2 months ahead
  3. Idempotency: calling create_audit_partition(1) twice → no error, same name
  4. Insert audit_logs row with ts=now+35d → lands in future partition (no constraint error)
  5. drop_old_audit_partitions(0) → drops partitions with upper_bound <= today
"""

import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _TestSessionFactory


@pytest_asyncio.fixture
async def pg():
    """Raw async session; no auto-rollback (DDL can't be rolled back in Postgres)."""
    async with _TestSessionFactory() as session:
        yield session


# ---------------------------------------------------------------------------
# Test 1: create_audit_partition(1) → creates next-month partition, returns name
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_audit_partition_next_month(pg: AsyncSession):
    """create_audit_partition(1) must create the partition for next month."""
    from datetime import date
    import calendar

    result = await pg.execute(text("SELECT create_audit_partition(1)"))
    part_name = result.scalar_one()

    # Compute expected name
    today = date.today()
    target = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    expected_name = f"audit_logs_{target.strftime('%Y_%m')}"

    assert part_name == expected_name, (
        f"Expected partition name '{expected_name}', got '{part_name}'"
    )

    # Verify partition actually exists in pg_class
    check = await pg.execute(
        text("SELECT COUNT(*) FROM pg_class WHERE relname = :name"),
        {"name": expected_name},
    )
    count = check.scalar_one()
    assert count >= 1, f"Partition '{expected_name}' must exist in pg_class after creation"

    await pg.commit()


# ---------------------------------------------------------------------------
# Test 2: create_audit_partition(2) → partition 2 months ahead
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_audit_partition_two_months_ahead(pg: AsyncSession):
    """create_audit_partition(2) must create the partition 2 months ahead."""
    from datetime import date

    result = await pg.execute(text("SELECT create_audit_partition(2)"))
    part_name = result.scalar_one()

    today = date.today()
    # 2 months ahead
    month = today.month + 2
    year = today.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    expected_name = f"audit_logs_{year:04d}_{month:02d}"

    assert part_name == expected_name, (
        f"Expected 2-months-ahead partition '{expected_name}', got '{part_name}'"
    )

    await pg.commit()


# ---------------------------------------------------------------------------
# Test 3: Idempotency — calling create_audit_partition(1) twice → no error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_audit_partition_idempotent(pg: AsyncSession):
    """Calling create_audit_partition(1) twice must not raise an error."""
    try:
        r1 = await pg.execute(text("SELECT create_audit_partition(1)"))
        name1 = r1.scalar_one()
        r2 = await pg.execute(text("SELECT create_audit_partition(1)"))
        name2 = r2.scalar_one()
    except Exception as e:
        pytest.fail(f"create_audit_partition(1) raised on second call: {e}")

    assert name1 == name2, "Idempotent call must return same partition name"
    await pg.commit()


# ---------------------------------------------------------------------------
# Test 4: Insert audit_log row with ts in future partition → succeeds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_future_audit_log_lands_in_partition(pg: AsyncSession):
    """Insert with ts=now+35d must succeed (landing in next month's partition)."""
    # Ensure next month's partition exists
    await pg.execute(text("SELECT create_audit_partition(1)"))
    await pg.execute(text("SELECT create_audit_partition(2)"))
    await pg.commit()

    # ts 35 days from now
    future_ts = datetime.now(timezone.utc) + timedelta(days=35)

    try:
        await pg.execute(
            text("""
                INSERT INTO audit_logs (ts, tool, is_write, status)
                VALUES (:ts, 'test_partition_insert', false, 'ok')
            """),
            {"ts": future_ts},
        )
        await pg.commit()
    except Exception as e:
        pytest.fail(
            f"Insert with future ts failed — partition may be missing: {e}"
        )

    # Verify the row is actually there
    result = await pg.execute(
        text("SELECT COUNT(*) FROM audit_logs WHERE tool = 'test_partition_insert'")
    )
    count = result.scalar_one()
    assert count >= 1, "Future audit_log row must be queryable"

    # Cleanup
    await pg.execute(
        text("DELETE FROM audit_logs WHERE tool = 'test_partition_insert'")
    )
    await pg.commit()


# ---------------------------------------------------------------------------
# Test 5: drop_old_audit_partitions(0) → drops partitions older than today
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drop_old_audit_partitions_drops_expired(pg: AsyncSession):
    """drop_old_audit_partitions(0) must drop partitions whose upper_bound <= today.

    Fixed by migration 006: regex updated from ([0-9-]+) to ([^']+)::timestamptz::date
    to correctly handle timezone-aware partition bounds like '2026-02-01 00:00:00+00'.
    """
    from datetime import date

    today = date.today()
    m = today.month - 3
    y = today.year
    if m <= 0:
        m += 12
        y -= 1
    old_start = date(y, m, 1)
    old_end = date(y, m + 1, 1) if m < 12 else date(y + 1, 1, 1)
    old_name = f"audit_logs_{old_start.strftime('%Y_%m')}"

    try:
        await pg.execute(text(
            f"CREATE TABLE IF NOT EXISTS {old_name} "
            f"PARTITION OF audit_logs FOR VALUES FROM ('{old_start}') TO ('{old_end}')"
        ))
        await pg.commit()
    except Exception:
        await pg.rollback()

    result = await pg.execute(text("SELECT drop_old_audit_partitions(0)"))
    dropped = [r[0] for r in result.fetchall()]
    await pg.commit()

    check = await pg.execute(
        text("SELECT COUNT(*) FROM pg_class WHERE relname = :name"),
        {"name": old_name},
    )
    remaining = check.scalar_one()
    assert remaining == 0, (
        f"Partition '{old_name}' must be dropped, but still exists. dropped={dropped}"
    )
