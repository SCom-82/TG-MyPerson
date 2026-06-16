"""Self-sufficient audit_logs partition maintenance (in-process).

Implements Layers 1-2 of the partition rotation design — see
_system/docs/architect/2026-06-16-tg-myperson-self-sufficient-partition-lifecycle.md.
The service maintains its own partitions: there is NO external cron, sidecar, or
scheduled task. Maintenance is only needed while the service is writing audit logs,
and "writing" implies "alive", so an in-process loop is self-sufficient by design.

  - ensure_partitions(): ensure current + next two months exist, drop partitions
    past the 90-day retention window. Guarded by a session-level advisory lock so
    multiple replicas don't race. Never raises — any failure is logged and
    swallowed, because partition maintenance must never take down the app.
  - partition_loop(): daily background tick (bare asyncio, no APScheduler), started
    from lifespan() and cancelled on shutdown.

Layer 0 (the DEFAULT partition, migration 008) guarantees INSERTs never fail
regardless of this module's health.
"""

import asyncio
import logging

from sqlalchemy import text

from app.database import async_session

log = logging.getLogger(__name__)

# Arbitrary constant advisory-lock key, unique to partition maintenance. Shared
# across replicas so only one performs the DDL at a time.
_ADVISORY_LOCK_KEY = 728_001

# 24h between background ticks.
_LOOP_INTERVAL_SECONDS = 86_400


async def ensure_partitions() -> None:
    """Ensure current + next two months exist and drop partitions past retention.

    Idempotent. Guarded by pg_try_advisory_lock so concurrent replicas don't
    duplicate work — if another process already holds the lock we skip this run.
    Never raises: any failure is logged and swallowed.
    """
    try:
        async with async_session() as session:
            got_lock = (
                await session.execute(
                    text("SELECT pg_try_advisory_lock(:k)"),
                    {"k": _ADVISORY_LOCK_KEY},
                )
            ).scalar()
            if not got_lock:
                log.info("ensure_partitions: lock held elsewhere, skipping")
                return
            try:
                ensured = []
                for offset in (0, 1, 2):
                    name = (
                        await session.execute(
                            text("SELECT ensure_audit_partition(:o)"),
                            {"o": offset},
                        )
                    ).scalar()
                    ensured.append(name)
                dropped = (
                    (await session.execute(text("SELECT drop_old_audit_partitions(90)")))
                    .scalars()
                    .all()
                )
                await session.commit()
                log.info(
                    "ensure_partitions: verified %s; dropped %s",
                    ensured,
                    dropped or "none",
                )
            finally:
                # Release on the same connection; held at session level, so it
                # survives the intermediate commit above.
                await session.execute(
                    text("SELECT pg_advisory_unlock(:k)"),
                    {"k": _ADVISORY_LOCK_KEY},
                )
                await session.commit()
    except Exception as e:  # noqa: BLE001 — maintenance must never crash the app
        log.warning("ensure_partitions: failed: %s", e)


async def partition_loop() -> None:
    """Daily background tick. Bare asyncio; resilient to DB blips, never dies silently."""
    while True:
        try:
            await ensure_partitions()
        except Exception as e:  # noqa: BLE001
            log.warning("partition loop tick failed: %s", e)
        await asyncio.sleep(_LOOP_INTERVAL_SECONDS)
