"""Repair of stored MAX rows from their own raw (plan 2026-10-19 Р-10) — no MAX, no network.

Run by infra-ops inside the container, as the app role (DATABASE_URL, RLS on):

    python -m app.scripts.max_repair --account max-personal-ro                    # dry-run
    python -m app.scripts.max_repair --account max-personal-ro --apply > undo.json
    python -m app.scripts.max_repair --undo undo.json                             # dry-run
    python -m app.scripts.max_repair --undo undo.json --apply
    docker exec -i <app> python -m app.scripts.max_repair --undo - --apply < undo.json   # journal on the host

1. titles: private chats get the peer's name (repo.chats.fill_dialog_titles).
2. messages that are forwards or carry contact cards: message_type and an
   empty text from normalize_message() over the stored raw; media rows of
   forwarded attachments are added (ON CONFLICT DO NOTHING: own ones exist).
   deleted_at, is_edited, raw and the sender are not touched; a non-empty text
   stays as it is.

Dry-run (the default) does everything in one transaction and rolls it back:
the counts are exact. --apply commits and writes the undo journal (JSON) to
--undo-file (default "-" = stdout; logs go to stderr). Only accounts with
platform 'max'. A second run gives zeros. Every run belongs in
_system/infra/changelog.md.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone

from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.max import normalize
from app.models import Account, Chat, Media
from app.models import Message as MessageRow
from app.repo.chats import fill_dialog_titles
from app.storage import account_db

_CANDIDATES = text("""
SELECT id, message_type, text, raw FROM messages
WHERE account_id = :a AND raw IS NOT NULL
  AND (raw -> 'link' ->> 'type' = 'FORWARD' OR raw -> 'attaches' @> '[{"_type": "CONTACT"}]'::jsonb)
ORDER BY id
""")


class Refused(Exception):
    """The run is not allowed; nothing was changed."""


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


async def _account(alias: str | None, account_id: int | None = None) -> tuple[int, str]:
    from app.database import async_session

    async with async_session() as db:
        flt = Account.alias == alias if alias is not None else Account.id == account_id
        row = (await db.execute(select(Account.id, Account.alias, Account.platform).where(flt))).one_or_none()
    if row is None:
        raise Refused(f"account {alias or account_id!r} not found")
    if row.platform != "max":
        raise Refused(f"account {row.alias!r} is {row.platform}, max_repair works on MAX accounts only")
    return row.id, row.alias


def _repaired(raw: dict) -> tuple[str, str | None, list[dict]] | None:
    """(message_type, text, media rows) the current normalization gives the stored raw."""
    try:
        msg = normalize.parse_stored(raw)
    except Exception as exc:  # noqa: BLE001
        log(f"  raw does not parse, skipped: {type(exc).__name__}: {str(exc)[:200]}")
        return None
    row, media = normalize.normalize_message(msg, me_id=None, chat_id=0)
    return row["message_type"], row["text"], media


async def _titles(db, a: int) -> list[dict]:
    before = dict((await db.execute(
        select(Chat.id, Chat.title).where(Chat.account_id == a, Chat.chat_type == "private")
    )).all())
    await fill_dialog_titles(db, a)
    after = dict((await db.execute(
        select(Chat.id, Chat.title).where(Chat.account_id == a, Chat.chat_type == "private")
    )).all())
    return [{"chat_pk": pk, "title": before[pk]} for pk in sorted(before) if after.get(pk) != before[pk]]


async def _messages(db, a: int) -> tuple[list[dict], list[int]]:
    changed, inserted = [], []
    for pk, old_type, old_text, raw in (await db.execute(_CANDIDATES, {"a": a})).all():
        repaired = _repaired(raw)
        if repaired is None:
            continue
        new_type, new_text, media = repaired
        values = {}
        if new_type != old_type:
            values["message_type"] = new_type
        if not old_text and new_text:
            values["text"] = new_text
        if values:
            await db.execute(update(MessageRow).where(MessageRow.account_id == a, MessageRow.id == pk).values(**values))
            changed.append({"id": pk, "message_type": old_type, "text": old_text})
        for m in media:
            stmt = pg_insert(Media).values(
                account_id=a, message_id=pk, attach_index=m["attach_index"], file_type=m["file_type"],
                file_external_id=m["file_id"], file_unique_id=m["file_unique_id"], file_name=m["file_name"],
                file_size=m["file_size"], mime_type=m["mime_type"],
            ).on_conflict_do_nothing(index_elements=[Media.account_id, Media.message_id, Media.attach_index])
            new_id = (await db.execute(stmt.returning(Media.id))).scalar_one_or_none()
            if new_id is not None:
                inserted.append(new_id)
    return changed, inserted


async def repair(alias: str, *, apply: bool) -> dict:
    a, alias = await _account(alias)
    async with account_db(a) as db:
        titles = await _titles(db, a)
        changed, inserted = await _messages(db, a)
        if apply:
            await db.commit()
        else:
            await db.rollback()
    return {
        "account": alias, "account_id": a, "at": datetime.now(timezone.utc).isoformat(), "applied": apply,
        "titles": titles, "messages": changed, "media": inserted,
    }


async def undo(journal: dict, *, apply: bool) -> dict:
    a, alias = await _account(None, journal["account_id"])
    if alias != journal["account"]:
        raise Refused(f"journal is for {journal['account']!r}, account {a} is {alias!r} now")
    async with account_db(a) as db:
        media = journal.get("media") or []
        removed = (await db.execute(
            delete(Media).where(Media.account_id == a, Media.id.in_(media)).returning(Media.id)
        )).scalars().all() if media else []
        for m in journal.get("messages") or []:
            await db.execute(update(MessageRow).where(MessageRow.account_id == a, MessageRow.id == m["id"])
                             .values(message_type=m["message_type"], text=m["text"]))
        for t in journal.get("titles") or []:
            await db.execute(update(Chat).where(Chat.account_id == a, Chat.id == t["chat_pk"])
                             .values(title=t["title"]))
        if apply:
            await db.commit()
        else:
            await db.rollback()
    return {"account": alias, "titles": len(journal.get("titles") or []),
            "messages": len(journal.get("messages") or []), "media_removed": len(removed), "applied": apply}


def _run(coro):
    async def run():
        from app.database import engine

        try:
            return await coro
        finally:
            await engine.dispose()

    return asyncio.run(run())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.scripts.max_repair", description=__doc__.split("\n\n")[0])
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--account", help="alias of a MAX account")
    target.add_argument("--undo", metavar="PATH", help='undo journal of an --apply run ("-" = stdin)')
    p.add_argument("--apply", action="store_true", help="commit (default: dry-run, rolled back)")
    p.add_argument("--undo-file", default="-", help='where --apply writes the undo journal ("-" = stdout)')
    args = p.parse_args(argv)
    mode = "APPLY" if args.apply else "dry-run (nothing is written; --apply to commit)"
    try:
        if args.undo:
            if args.undo == "-":  # docker exec -i … --undo - < journal.json (the journal is on the host)
                journal = json.load(sys.stdin)
            else:
                with open(args.undo, encoding="utf-8") as f:
                    journal = json.load(f)
            result = _run(undo(journal, apply=args.apply))
            log(f"undo {result['account']} [{mode}]: titles={result['titles']}, messages={result['messages']}, "
                f"media_removed={result['media_removed']}")
            return 0
        journal = _run(repair(args.account, apply=args.apply))
    except Refused as exc:
        log(f"refused: {exc}")
        return 2
    log(f"{journal['account']} [{mode}]: titles={len(journal['titles'])}, messages={len(journal['messages'])}, "
        f"media_inserted={len(journal['media'])}")
    if args.apply:
        out = json.dumps(journal, ensure_ascii=False, indent=1, default=str)
        if args.undo_file == "-":
            print(out)
        else:
            with open(args.undo_file, "w", encoding="utf-8") as f:
                f.write(out + "\n")
            log(f"undo journal: {args.undo_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
