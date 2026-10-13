"""In-process retention of max_raw_events (ADR §2.E): daily tick, like partition_loop."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app import database
from app.max import repo
from app.max.config import MaxSettings
from app.repo import raw_events
from app.services.tg_store import new_mode
from app.storage import owner_db

log = logging.getLogger(__name__)

_LOOP_INTERVAL_SECONDS = 86_400


async def purge_raw_events_once(settings: MaxSettings) -> int:
    """Delete raw frames older than MAX_RAW_EVENTS_RETENTION_DAYS. Never raises."""
    try:
        async with database.async_session() as db:
            deleted = await repo.purge_raw_events(db, settings.raw_events_retention_days)
            await db.commit()
        if new_mode():
            # raw_events of every account: a service operation under the owner scope.
            cutoff = datetime.now(timezone.utc) - timedelta(days=settings.raw_events_retention_days)
            async with owner_db() as db:
                deleted += await raw_events.purge_older_than(db, cutoff)
                await db.commit()
        if deleted:
            log.info("max raw events: purged %d row(s) older than %d days", deleted, settings.raw_events_retention_days)
        return deleted
    except Exception as exc:  # noqa: BLE001 — maintenance must never crash the app
        log.warning("max raw events: purge failed: %s", exc)
        return 0


async def raw_events_loop(settings: MaxSettings) -> None:
    while True:
        await purge_raw_events_once(settings)
        await asyncio.sleep(_LOOP_INTERVAL_SECONDS)
