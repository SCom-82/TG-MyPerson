"""The start-up delta of the switch to STORAGE_MODE=new (ТЗ 2026-10-13 §11.2 step 5, review R4-1).

With RUN_DELTA_ON_START=true the container copies what the old one wrote to
the legacy tables since the last manual delta — BEFORE any Telegram/MAX
session connects — and marks the cutover; from then on copy/delta refuse
(they would overwrite the new rows with legacy versions). A failed delta
stops the start: the rollback is STORAGE_MODE=legacy. Once the cutover is
marked, a restart with the flag still on skips the delta.
Runs as the table owner (MIGRATIONS_DATABASE_URL), in a worker thread.
"""

import asyncio
import json
import logging

from app.config import settings

log = logging.getLogger(__name__)


class StartupDeltaFailed(RuntimeError):
    pass


def _run() -> dict:
    from app.scripts import isolation_copy as ic

    ctx = ic.open_ctx(ic.Options())
    try:
        if ic.cutover_at(ctx) is not None:
            return {"command": "delta", "ok": True, "skipped": "cutover already marked"}
    finally:
        ctx.conn.close()
    return ic.delta(ic.Options(), cutover=True)


async def run_startup_delta() -> dict | None:
    if settings.storage_mode != "new" or not settings.run_delta_on_start:
        return None
    log.warning("startup delta: copying the legacy tail before sessions connect")
    try:
        report = await asyncio.to_thread(_run)
    except BaseException as exc:  # noqa: BLE001 — do not start on half-copied data
        raise StartupDeltaFailed(f"startup delta failed: {type(exc).__name__}: {exc}") from exc
    if not report.get("ok"):
        raise StartupDeltaFailed(f"startup delta not ok: {json.dumps(report, default=str)[:2000]}")
    log.warning("startup delta done: %s", json.dumps(report.get("changes") or report.get("skipped"),
                                                     default=str))
    return report
