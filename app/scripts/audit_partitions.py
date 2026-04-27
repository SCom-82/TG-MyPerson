"""Audit log partition maintenance — run monthly via cron.

Creates audit_logs partitions for N+1 and N+2 months (double safety margin)
and drops partitions whose upper bound is older than 90 days.

Usage:
    python -m app.scripts.audit_partitions

The script calls SQL functions installed by migration 005:
    create_audit_partition(months_ahead int)
    drop_old_audit_partitions(retention_days int)
"""

import asyncio
import logging

from sqlalchemy import text

from app.database import async_session

log = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    async with async_session() as session:
        # Create partitions for next two months (idempotent)
        for offset in (1, 2):
            result = await session.execute(
                text(f"SELECT create_audit_partition({offset})")
            )
            part_name = result.scalar()
            log.info("Ensured partition: %s (months_ahead=%d)", part_name, offset)

        # Drop partitions older than 90 days
        result = await session.execute(
            text("SELECT drop_old_audit_partitions(90)")
        )
        dropped = result.scalars().all()
        if dropped:
            log.info("Dropped old partitions: %s", dropped)
        else:
            log.info("No old partitions to drop (retention_days=90)")

        await session.commit()


if __name__ == "__main__":
    asyncio.run(main())
