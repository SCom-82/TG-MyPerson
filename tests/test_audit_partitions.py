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


# ---------------------------------------------------------------------------
# Migration 008 — DEFAULT partition (Layer 0) + drain (Layer 3)
# ---------------------------------------------------------------------------

def _far_future_month_start():
    """First day of a month ~7 months ahead — guaranteed to have no monthly partition.

    ensure/create only materialises current + 2 months, so this month always
    falls through to the DEFAULT partition.
    """
    from datetime import date

    month = date.today().month + 7
    year = date.today().year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    return date(year, month, 1)


@pytest.mark.asyncio
async def test_default_partition_exists(pg: AsyncSession):
    """Migration 008 must create the audit_logs_default DEFAULT partition."""
    result = await pg.execute(
        text("SELECT COUNT(*) FROM pg_class WHERE relname = 'audit_logs_default'")
    )
    assert result.scalar_one() == 1, "audit_logs_default partition must exist"


@pytest.mark.asyncio
async def test_insert_unpartitioned_month_lands_in_default(pg: AsyncSession):
    """INSERT for a month with no dedicated partition must succeed via DEFAULT."""
    far = _far_future_month_start()
    far_ts = datetime(far.year, far.month, 15, tzinfo=timezone.utc)
    marker = f"default_insert_{far.strftime('%Y_%m')}"

    # Sanity: no monthly partition for that month.
    part_name = f"audit_logs_{far.strftime('%Y_%m')}"
    existing = await pg.execute(
        text("SELECT COUNT(*) FROM pg_class WHERE relname = :n"), {"n": part_name}
    )
    assert existing.scalar_one() == 0, f"precondition: {part_name} must not exist yet"

    try:
        await pg.execute(
            text("""
                INSERT INTO audit_logs (ts, tool, is_write, status)
                VALUES (:ts, :tool, false, 'ok')
            """),
            {"ts": far_ts, "tool": marker},
        )
        await pg.commit()
    except Exception as e:
        await pg.rollback()
        pytest.fail(f"INSERT into unpartitioned month must not fail (DEFAULT): {e}")

    # Row physically resides in the DEFAULT partition.
    in_default = await pg.execute(
        text("SELECT COUNT(*) FROM audit_logs_default WHERE tool = :t"), {"t": marker}
    )
    assert in_default.scalar_one() == 1, "row must land in audit_logs_default"

    # Cleanup
    await pg.execute(text("DELETE FROM audit_logs WHERE tool = :t"), {"t": marker})
    await pg.commit()


@pytest.mark.asyncio
async def test_drain_moves_rows_from_default_to_monthly(pg: AsyncSession):
    """drain_audit_default_for_month must move DEFAULT rows into the monthly partition."""
    far = _far_future_month_start()
    far_ts = datetime(far.year, far.month, 10, tzinfo=timezone.utc)
    marker = f"drain_insert_{far.strftime('%Y_%m')}"
    part_name = f"audit_logs_{far.strftime('%Y_%m')}"

    # Seed a row that lands in DEFAULT.
    await pg.execute(
        text("""
            INSERT INTO audit_logs (ts, tool, is_write, status)
            VALUES (:ts, :tool, false, 'ok')
        """),
        {"ts": far_ts, "tool": marker},
    )
    await pg.commit()

    # Drain that month out of DEFAULT.
    drained = await pg.execute(
        text("SELECT drain_audit_default_for_month(:d)"),
        {"d": far},
    )
    assert drained.scalar_one() == part_name
    await pg.commit()

    # Monthly partition now exists and DEFAULT no longer holds the row.
    exists = await pg.execute(
        text("SELECT COUNT(*) FROM pg_class WHERE relname = :n"), {"n": part_name}
    )
    assert exists.scalar_one() == 1, f"{part_name} must exist after drain"

    in_default = await pg.execute(
        text("SELECT COUNT(*) FROM audit_logs_default WHERE tool = :t"), {"t": marker}
    )
    assert in_default.scalar_one() == 0, "DEFAULT must be empty for that month after drain"

    in_monthly = await pg.execute(
        text(f"SELECT COUNT(*) FROM {part_name} WHERE tool = :t"), {"t": marker}
    )
    assert in_monthly.scalar_one() == 1, "row must now live in the monthly partition"

    # Cleanup: drop the freshly-created partition (also removes the row).
    await pg.execute(text(f"DROP TABLE IF EXISTS {part_name}"))
    await pg.commit()


@pytest.mark.asyncio
async def test_drop_old_does_not_drop_default(pg: AsyncSession):
    """drop_old_audit_partitions(0) must never drop the DEFAULT partition.

    The DEFAULT partition has no upper bound, so the regex yields NULL and it
    must be skipped by the retention logic.
    """
    result = await pg.execute(text("SELECT drop_old_audit_partitions(0)"))
    dropped = [r[0] for r in result.fetchall()]
    await pg.commit()

    assert "audit_logs_default" not in dropped, "DEFAULT partition must not be dropped"

    still_there = await pg.execute(
        text("SELECT COUNT(*) FROM pg_class WHERE relname = 'audit_logs_default'")
    )
    assert still_there.scalar_one() == 1, "audit_logs_default must survive retention sweep"
