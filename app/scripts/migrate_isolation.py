"""Account isolation data migration tool (ТЗ 2026-10-13 §8).

Run by infra-ops inside the container, as the table owner:

    python -m app.scripts.migrate_isolation probe [--out-dir /tmp] [--url http://127.0.0.1:8000]
    python -m app.scripts.migrate_isolation probe --service-stopped   # only with the service down

Commands are idempotent; each prints a JSON report to stdout and writes it to
<out-dir>/migrate_isolation-<cmd>-<ts>.json.

probe (§8.1) — which chats each enabled Telegram account is a member of.
  Runs INSIDE the service process on the pool's own clients
  (POST /api/v1/admin/isolation/probe, X-Admin-Key): a second MTProto
  connection on the same auth key while the service holds it risks
  AUTH_KEY_DUPLICATED and the loss of the session (review 13.10, P0-1). The
  CLI only calls that endpoint. --service-stopped opens its own connections
  and is refused while a uvicorn process of the service runs.

  Per account: get_me() and iter_dialogs(), nothing else — the client is
  wrapped, any other Telegram call raises, and the shared client cannot be
  connected or disconnected through the wrapper. Result goes to
  mig_membership — created by this command, not by a migration, dropped in
  PR-6. Per account the rows are replaced in one transaction after the dialog
  walk succeeded, so a rerun reflects current membership and a failed walk
  leaves the previous rows intact. No other table is written
  (accounts.external_user_id is filled by `copy`, step A).
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
DEFAULT_URL = "http://127.0.0.1:8000"
PROBE_PATH = "/api/v1/admin/isolation/probe"


# ---------------------------------------------------------------------------
# Read-only Telegram client
# ---------------------------------------------------------------------------

class ReadOnlyClient:
    """Exposes only the Telegram calls probe is allowed to make.

    No connect/disconnect: the wrapped client is the service's shared one,
    and disconnecting it would take the account offline.
    """

    _ALLOWED = frozenset({"is_user_authorized", "get_me", "iter_dialogs"})

    def __init__(self, client: Any) -> None:
        object.__setattr__(self, "_client", client)

    def __getattr__(self, name: str) -> Any:
        if name not in self._ALLOWED:
            raise PermissionError(f"migrate_isolation: Telegram call '{name}' is not allowed")
        return getattr(self._client, name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise PermissionError("migrate_isolation: client is read-only")

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise PermissionError("migrate_isolation: raw Telegram requests are not allowed")


class OwnedReadOnlyClient(ReadOnlyClient):
    """--service-stopped only: our own connection, so we connect and disconnect it."""

    _ALLOWED = ReadOnlyClient._ALLOWED | {"connect", "disconnect"}


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


async def _probe_account(account: Account, client: ReadOnlyClient) -> dict:
    """Walk the dialogs of one account on an already connected client."""
    if not await client.is_user_authorized():
        return {"status": "not_authorized"}
    me = await client.get_me()
    rows: dict[int, dict] = {}
    async for dialog in client.iter_dialogs():
        row = membership_row(account.id, dialog.entity)
        rows[row["external_chat_id"]] = row

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


# (account, session string) → a read-only client to probe with, or None if the
# account has no usable session. Owns the connection lifecycle of what it returns.
ClientSource = Callable[[Account, str | None], Awaitable[Any]]


async def probe(source: ClientSource) -> dict:
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
        try:
            client = await source(account, session_string)
            if client is None:
                result: dict = {"status": "no_session"}
            else:
                result = await _probe_account(account, client)
        except Exception as exc:  # one account must not stop the others
            log.exception("probe: account %s failed", account.alias)
            result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        report["accounts"][account.alias] = {"account_id": account.id, **result}

    report.update(await _membership_summary())
    report["ok"] = all(a["status"] == "ok" for a in report["accounts"].values())
    return report


async def probe_in_service() -> dict:
    """On the running service's pool (admin endpoint): no new connections."""
    from app.telegram.pool import pool

    async def source(account: Account, _session_string: str | None):
        session = await pool.get(account.alias)
        if session.client is None:
            return None
        return ReadOnlyClient(session.client)

    return await probe(source)


async def probe_service_stopped() -> dict:
    """Own connections — only while the service is down."""
    opened: list[OwnedReadOnlyClient] = []

    async def source(account: Account, session_string: str | None):
        if not session_string:
            return None
        client = OwnedReadOnlyClient(telethon_client(account, session_string))
        await client.connect()
        opened.append(client)
        return client

    try:
        return await probe(source)
    finally:
        for client in opened:
            await client.disconnect()


def service_running(proc: Path = Path("/proc")) -> bool:
    """A uvicorn process of the service in this container."""
    for cmdline in proc.glob("[0-9]*/cmdline"):
        try:
            argv = cmdline.read_bytes().split(b"\0")
        except OSError:
            continue
        if any(a.endswith(b"uvicorn") for a in argv) and b"app.main:app" in argv:
            return True
    return False


def call_service(url: str, path: str, payload: dict | None = None, timeout: float = 900) -> dict:
    """POST to an admin endpoint of the running service (X-Admin-Key from the env)."""
    key = settings.admin_api_key
    if not key:
        raise SystemExit("TG_ADMIN_API_KEY is not set")
    req = urllib.request.Request(
        url.rstrip("/") + path,
        data=json.dumps(payload or {}).encode(),
        headers={"X-Admin-Key": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{path}: HTTP {exc.code}: {exc.read().decode(errors='replace')}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_probe(args: argparse.Namespace) -> dict:
    if args.service_stopped:
        if service_running():
            raise SystemExit("--service-stopped: the service (uvicorn app.main:app) is running; refusing")
        return asyncio.run(probe_service_stopped())
    return call_service(args.url, PROBE_PATH)


COMMANDS: dict[str, Callable[[argparse.Namespace], dict]] = {"probe": run_probe}


def write_report(report: dict, cmd: str, out_dir: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"migrate_isolation-{cmd}-{ts}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.scripts.migrate_isolation")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--out-dir", type=Path, default=Path("/tmp"))
    parser.add_argument("--url", default=os.environ.get("TG_MYPERSON_URL_LOCAL", DEFAULT_URL))
    parser.add_argument("--service-stopped", action="store_true",
                        help="own Telegram connections; only while the service is down")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    report = COMMANDS[args.command](args)
    path = write_report(report, args.command, args.out_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    log.info("report: %s", path)
    return 0 if report.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
