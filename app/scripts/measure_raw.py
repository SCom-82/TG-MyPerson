"""Size of the full TG raw (ТЗ 2026-10-13 §5.1, risk §13) on live messages — read only.

    python -m app.scripts.measure_raw --alias personal-ro --chat -1002175364727 [--limit 1000]

Reads up to --limit messages of one chat with a second, read-only connection
(same wrapper as migrate_isolation probe; iter_messages only) and compares the
legacy raw_data (7 fields) with tl_to_json(message.to_dict()). Writes nothing.
Escalate before merging PR-2 if the full raw adds more than +100 % to a
messages row (avg tg_messages row on the prod copy: 632 B, raw_data 155 B).
"""

import argparse
import asyncio
import json
import statistics
import sys

from sqlalchemy import select

from app.database import async_session
from app.models import Account, AccountSession
from app.scripts.migrate_isolation import ReadOnlyClient, telethon_client
from app.services.message_service import _message_to_raw
from app.telegram.normalize import raw_of

ROW_BYTES = 632  # avg tg_messages row on the prod copy, 13.10


class _Reader(ReadOnlyClient):
    _ALLOWED = ReadOnlyClient._ALLOWED | {"iter_messages"}


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


async def measure(alias: str, chat: int, limit: int) -> dict:
    async with async_session() as db:
        row = (await db.execute(
            select(Account, AccountSession.session_plaintext)
            .join(AccountSession, (AccountSession.account_id == Account.id) & AccountSession.is_active)
            .where(Account.alias == alias)
        )).one()
    client = _Reader(telethon_client(row[0], row[1]))
    await client.connect()
    try:
        messages = [m async for m in client.iter_messages(chat, limit=limit)]
    finally:
        await client.disconnect()
    return {"alias": alias, "chat": chat, **sizes(messages)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.scripts.measure_raw")
    p.add_argument("--alias", required=True)
    p.add_argument("--chat", type=int, required=True)
    p.add_argument("--limit", type=int, default=1000)
    a = p.parse_args(argv)
    print(json.dumps(asyncio.run(measure(a.alias, a.chat, a.limit)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
