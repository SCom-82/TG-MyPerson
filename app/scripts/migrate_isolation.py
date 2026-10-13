"""Account isolation data migration tool (ТЗ 2026-10-13 §8).

Run by infra-ops inside the container, as the table owner:

    python -m app.scripts.migrate_isolation probe [--out-dir /tmp]

Commands are idempotent; each prints a JSON report to stdout and writes it to
<out-dir>/migrate_isolation-<cmd>-<ts>.json.

probe (§8.1) — which chats each enabled Telegram account is a member of.
  For every enabled TG account with an active session: get_me() and
  iter_dialogs(), nothing else (the client is wrapped so any other Telegram
  call raises). Result goes to mig_membership — created by this command, not
  by a migration, dropped in PR-6. Per account the rows are replaced in one
  transaction after the dialog walk succeeded, so a rerun reflects current
  membership and a failed walk leaves the previous rows intact. No other
  table is written (accounts.tg_user_id included: get_me goes to the report,
  external_user_id is filled by `copy`, step A).

  The probe opens a second connection on the account's auth key while the
  service is running (same IP, separate MTProto session). It does not
  subscribe to updates (receive_updates=False) and disconnects at the end.
"""

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy import select, text

from app.config import settings
from app.database import async_session
from app.models import Account, AccountSession
from app.services.chat_service import _get_chat_id, _get_chat_type, _get_title

log = logging.getLogger(__name__)

MEMBERSHIP_DDL = """
CREATE TABLE IF NOT EXISTS mig_membership (
    account_id       int    NOT NULL REFERENCES accounts(id),
    external_chat_id bigint NOT NULL,
    chat_type        text   NOT NULL,
    title            text,
    role             text,
    access_hash      bigint,
    PRIMARY KEY (account_id, external_chat_id)
)
"""

# Report lists are capped; counts are always exact.
REPORT_LIST_LIMIT = 200


# ---------------------------------------------------------------------------
# Read-only Telegram client
# ---------------------------------------------------------------------------

class ReadOnlyClient:
    """Exposes only the Telegram calls probe is allowed to make."""

    _ALLOWED = frozenset(
        {"connect", "disconnect", "is_user_authorized", "get_me", "iter_dialogs"}
    )

    def __init__(self, client: Any) -> None:
        object.__setattr__(self, "_client", client)

    def __getattr__(self, name: str) -> Any:
        if name not in self._ALLOWED:
            raise PermissionError(f"migrate_isolation probe: Telegram call '{name}' is not allowed")
        return getattr(self._client, name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise PermissionError("migrate_isolation probe: client is read-only")

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise PermissionError("migrate_isolation probe: raw Telegram requests are not allowed")


ClientFactory = Callable[[Account, str], Any]


def telethon_client(account: Account, session_string: str) -> Any:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    return TelegramClient(
        StringSession(session_string),
        settings.tg_api_id,
        settings.tg_api_hash,
        system_version="4.16.30-vxCUSTOM",
        receive_updates=False,
    )


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------

def _role(entity: Any) -> str | None:
    """creator | admin | member for groups and channels, None for private chats."""
    from telethon.tl.types import User

    if isinstance(entity, User):
        return None
    if getattr(entity, "creator", False):
        return "creator"
    if getattr(entity, "admin_rights", None) is not None:
        return "admin"
    return "member"


def _access_hash(entity: Any) -> int | None:
    # A min entity carries a hash that is not valid for this account.
    if getattr(entity, "min", False):
        return None
    return getattr(entity, "access_hash", None)


def membership_row(account_id: int, entity: Any) -> dict:
    return {
        "account_id": account_id,
        "external_chat_id": _get_chat_id(entity),
        "chat_type": _get_chat_type(entity),
        "title": _get_title(entity),
        "role": _role(entity),
        "access_hash": _access_hash(entity),
    }


async def _probe_account(account: Account, session_string: str, factory: ClientFactory) -> dict:
    client = ReadOnlyClient(factory(account, session_string))
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return {"status": "not_authorized"}
        me = await client.get_me()
        rows: dict[int, dict] = {}
        async for dialog in client.iter_dialogs():
            row = membership_row(account.id, dialog.entity)
            rows[row["external_chat_id"]] = row
    finally:
        await client.disconnect()

    async with async_session() as db:
        await db.execute(text(MEMBERSHIP_DDL))
        await db.execute(
            text("DELETE FROM mig_membership WHERE account_id = :a"), {"a": account.id}
        )
        if rows:
            await db.execute(
                text(
                    "INSERT INTO mig_membership"
                    " (account_id, external_chat_id, chat_type, title, role, access_hash)"
                    " VALUES (:account_id, :external_chat_id, :chat_type, :title, :role, :access_hash)"
                ),
                list(rows.values()),
            )
        await db.commit()

    return {
        "status": "ok",
        "external_user_id": me.id,
        "tg_user_id_in_accounts": account.tg_user_id,
        "tg_user_id_matches": account.tg_user_id in (None, me.id),
        "dialogs": len(rows),
        "by_type": dict(Counter(r["chat_type"] for r in rows.values())),
        "with_access_hash": sum(1 for r in rows.values() if r["access_hash"] is not None),
    }


async def _membership_summary() -> dict:
    async with async_session() as db:
        await db.execute(text(MEMBERSHIP_DDL))
        await db.commit()
        shared = (await db.execute(text(
            "SELECT chat_type, count(*) FROM ("
            "  SELECT external_chat_id, min(chat_type) AS chat_type FROM mig_membership"
            "  GROUP BY external_chat_id HAVING count(DISTINCT account_id) > 1"
            ") s GROUP BY chat_type"
        ))).all()
        orphan_where = (
            " FROM tg_chats c WHERE NOT EXISTS"
            " (SELECT 1 FROM mig_membership m WHERE m.external_chat_id = c.id)"
        )
        orphan_total = (await db.execute(text("SELECT count(*)" + orphan_where))).scalar()
        orphan_by_type = (await db.execute(text(
            "SELECT c.chat_type, count(*)" + orphan_where + " GROUP BY c.chat_type"
        ))).all()
        orphans = (await db.execute(text(
            "SELECT c.id, c.chat_type, c.title,"
            " (SELECT count(*) FROM tg_messages t WHERE t.chat_id = c.id) AS messages"
            + orphan_where + " ORDER BY messages DESC, c.id LIMIT :lim"
        ), {"lim": REPORT_LIST_LIMIT})).all()
    return {
        "shared_chats": {t: n for t, n in shared},
        "shared_chats_total": sum(n for _, n in shared),
        "tg_chats_without_member": {
            "total": orphan_total,
            "by_type": {t: n for t, n in orphan_by_type},
            "top_by_messages": [
                {"chat_id": cid, "chat_type": ct, "title": title, "messages": msgs}
                for cid, ct, title, msgs in orphans
            ],
        },
    }


async def probe(factory: ClientFactory = telethon_client) -> dict:
    async with async_session() as db:
        accounts = (await db.execute(
            select(Account, AccountSession.session_plaintext)
            .outerjoin(
                AccountSession,
                (AccountSession.account_id == Account.id) & AccountSession.is_active,
            )
            .where(Account.platform == "telegram", Account.is_enabled)
            .order_by(Account.id)
        )).all()

    report: dict[str, Any] = {"command": "probe", "accounts": {}}
    for account, session_string in accounts:
        if not session_string:
            result: dict = {"status": "no_session"}
        else:
            try:
                result = await _probe_account(account, session_string, factory)
            except Exception as exc:  # one account must not stop the others
                log.exception("probe: account %s failed", account.alias)
                result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        report["accounts"][account.alias] = {"account_id": account.id, **result}

    report.update(await _membership_summary())
    report["ok"] = all(a["status"] == "ok" for a in report["accounts"].values())
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

COMMANDS: dict[str, Callable[[], Awaitable[dict]]] = {"probe": probe}


def write_report(report: dict, cmd: str, out_dir: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"migrate_isolation-{cmd}-{ts}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.scripts.migrate_isolation")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--out-dir", type=Path, default=Path("/tmp"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    report = asyncio.run(COMMANDS[args.command]())
    path = write_report(report, args.command, args.out_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    log.info("report: %s", path)
    return 0 if report.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
