"""Size of the full TG raw (ТЗ 2026-10-13 §5.1, risk §13) on live messages — read only.

    python -m app.scripts.measure_raw --alias personal-ro --chat -1002175364727 [--limit 1000]

Runs inside the service on the pool's own client of the alias
(POST /api/v1/admin/isolation/measure_raw, X-Admin-Key): no second connection
on the account's auth key (review 13.10, P0-1). Reads up to --limit messages
of one chat (iter_messages only) and compares the legacy raw_data (7 fields)
with tl_to_json(message.to_dict()). Writes nothing.
Escalate before merging PR-2 if the full raw adds more than +100 % to a
messages row (avg tg_messages row on the prod copy: 632 B, raw_data 155 B).
"""

import argparse
import json
import os
import statistics
import sys

from app.scripts.migrate_isolation import DEFAULT_URL, ReadOnlyClient, call_service
from app.services.message_service import _message_to_raw
from app.telegram.normalize import raw_of

ROW_BYTES = 632  # avg tg_messages row on the prod copy, 13.10
MEASURE_PATH = "/api/v1/admin/isolation/measure_raw"


class _Reader(ReadOnlyClient):
    _ALLOWED = frozenset({"iter_messages"})


def sizes(messages) -> dict:
    legacy = [len(json.dumps(_message_to_raw(m), ensure_ascii=False, default=str)) for m in messages]
    full = [len(json.dumps(raw_of(m), ensure_ascii=False)) for m in messages]
    if not full:
        return {"messages": 0}
    return {
        "messages": len(full),
        "legacy_raw_avg": round(statistics.mean(legacy)),
        "full_raw_avg": round(statistics.mean(full)),
        "full_raw_p95": sorted(full)[int(len(full) * 0.95) - 1],
        "full_raw_max": max(full),
        "growth_vs_row_pct": round((statistics.mean(full) - statistics.mean(legacy)) / ROW_BYTES * 100),
    }


async def measure_in_service(alias: str, chat: int, limit: int) -> dict:
    """On the running pool's client of the alias (admin endpoint)."""
    from app.telegram.pool import pool

    session = await pool.get(alias)
    if session.client is None:
        return {"alias": alias, "chat": chat, "error": "session not available"}
    client = _Reader(session.client)
    messages = [m async for m in client.iter_messages(chat, limit=limit)]
    return {"alias": alias, "chat": chat, **sizes(messages)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.scripts.measure_raw")
    p.add_argument("--alias", required=True)
    p.add_argument("--chat", type=int, required=True)
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--url", default=os.environ.get("TG_MYPERSON_URL_LOCAL", DEFAULT_URL))
    a = p.parse_args(argv)
    report = call_service(a.url, MEASURE_PATH, {"alias": a.alias, "chat": a.chat, "limit": a.limit})
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
