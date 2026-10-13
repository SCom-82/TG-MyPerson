"""One account's data as a whole: list, grants, dump, restore, drop (ТЗ 2026-10-13 §11.4).

Run by infra-ops inside the container as the table owner
(MIGRATIONS_DATABASE_URL, else DATABASE_URL):

    python -m app.scripts.account_partitions list    --account N
    python -m app.scripts.account_partitions grants
    python -m app.scripts.account_partitions dump    --account N [--out-dir DIR]
    python -m app.scripts.account_partitions restore --account N --file DIR/accN_<ts>.dump
    python -m app.scripts.account_partitions drop    --account N --confirm <alias> [--out-dir DIR]

dump: pg_dump -Fc --data-only of the 8 partitions <t>_aN + a manifest with
  row counts (<file>.json). people/identities are owner level and not part of
  the dump: users_aN references identities.id, so restore into the same DB
  (or after people/identities are restored).
restore: create_account_partitions(N) → partitions must be empty → pg_restore
  table by table in parent-before-child order → sequences moved past the
  restored ids → row counts compared with the manifest.
drop: only a disabled account with a dump younger than 1 hour in --out-dir.
  Detaches and drops the partitions child-first, deletes the account's
  sessions, API keys and tool policies; the accounts row stays
  (is_enabled=false, audit_logs refer to it). People/identities stay.

Every action belongs in _system/infra/changelog.md.
pg_dump/pg_restore are taken from PATH (override: PG_DUMP / PG_RESTORE); their
major version must equal the server's, else dump/restore are refused.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import psycopg2

# Parent before child (create order); drop goes in reverse.
DATA_TABLES = (
    "users",
    "chats",
    "messages",
    "media",
    "sync_state",
    "member_snapshots",
    "snapshot_members",
    "raw_events",
)
SEQUENCE_TABLES = tuple(t for t in DATA_TABLES if t != "sync_state")
DEFAULT_OUT_DIR = Path("/root/backups/tg-myperson")
DUMP_MAX_AGE_SECONDS = 3600

APP_ROLE = "tg_myperson_app"
# Same block as migration 010: grants on every non-partition table.
GRANTS_SQL = f"""
DO $do$
DECLARE r record;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
    RAISE EXCEPTION 'role {APP_ROLE} does not exist';
  END IF;
  FOR r IN
    SELECT c.relname FROM pg_class c
    WHERE c.relnamespace = 'public'::regnamespace
      AND c.relkind IN ('r', 'p') AND NOT c.relispartition
      AND c.relname <> 'alembic_version'
  LOOP
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO {APP_ROLE}', r.relname);
  END LOOP;
  EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}';
  EXECUTE 'GRANT EXECUTE ON FUNCTION create_account_partitions(int) TO {APP_ROLE}';
  EXECUTE 'GRANT EXECUTE ON FUNCTION create_audit_partition(int), drop_old_audit_partitions(int),
           drain_audit_default_for_month(date), ensure_audit_partition(int) TO {APP_ROLE}';
END
$do$;
"""


class Refused(Exception):
    """A safety check failed; nothing was changed."""


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _db_url() -> str:
    url = os.environ.get("MIGRATIONS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("MIGRATIONS_DATABASE_URL or DATABASE_URL is required")
    return url


def libpq_env(url: str | None = None) -> dict[str, str]:
    """PG* variables for pg_dump/pg_restore (the password stays out of argv)."""
    u = urlparse(re.sub(r"^postgresql\+\w+://", "postgresql://", url or _db_url()))
    env = {
        "PGHOST": u.hostname or "localhost",
        "PGPORT": str(u.port or 5432),
        "PGUSER": unquote(u.username or ""),
        "PGDATABASE": u.path.lstrip("/"),
    }
    if u.password:
        env["PGPASSWORD"] = unquote(u.password)
    return env


def connect():
    env = libpq_env()
    conn = psycopg2.connect(
        host=env["PGHOST"], port=env["PGPORT"], user=env["PGUSER"],
        password=env.get("PGPASSWORD"), dbname=env["PGDATABASE"],
    )
    conn.autocommit = False
    return conn


def _partition(table: str, account_id: int) -> str:
    return f"{table}_a{int(account_id)}"


def _tool(name: str) -> str:
    """pg_dump / pg_restore from PATH, or PG_DUMP / PG_RESTORE."""
    return os.environ.get(name.upper(), name)


def tool_major(tool: str) -> int:
    """Major version of pg_dump / pg_restore ('pg_restore (PostgreSQL) 16.15 …' → 16)."""
    out = subprocess.run([_tool(tool), "--version"], capture_output=True, text=True, check=True).stdout
    match = re.search(r"\(PostgreSQL\)\s+(\d+)", out)
    if not match:
        raise Refused(f"cannot parse {tool} --version: {out.strip()!r}")
    return int(match.group(1))


def server_major(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SHOW server_version_num")
        return int(cur.fetchone()[0]) // 10000


def check_client_version(conn, tool: str) -> None:
    """The client major must equal the server's: a newer pg_restore writes
    settings an older server rejects (17 → 16: transaction_timeout)."""
    client, server = tool_major(tool), server_major(conn)
    if client != server:
        raise Refused(
            f"{tool} is PostgreSQL {client}, the server is {server}: "
            f"use postgresql-client-{server} (Dockerfile)"
        )


def _run(cmd: list[str]) -> None:
    print("+ " + " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, env={**os.environ, **libpq_env()}, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({proc.returncode}): {proc.stderr.strip()}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_list(conn, account_id: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT parent, partition, rows FROM account_partitions(%s)", (account_id,))
        return [{"parent": p, "partition": part, "rows": rows} for p, part, rows in cur.fetchall()]


def cmd_grants(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(GRANTS_SQL)
    conn.commit()


def _counts(conn, account_id: int) -> dict[str, int | None]:
    return {r["parent"]: r["rows"] for r in cmd_list(conn, account_id)}


def cmd_dump(conn, account_id: int, out_dir: Path) -> Path:
    counts = _counts(conn, account_id)
    missing = [t for t, n in counts.items() if n is None]
    if missing:
        raise Refused(f"account {account_id} has no partitions for {missing}")
    check_client_version(conn, "pg_dump")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"acc{account_id}_{ts}.dump"
    cmd = [_tool("pg_dump"), "-Fc", "--data-only", "-f", str(path)]
    for t in DATA_TABLES:
        cmd += ["-t", f"public.{_partition(t, account_id)}"]
    _run(cmd)
    manifest = {"account_id": account_id, "created_at": ts, "rows": counts}
    Path(f"{path}.json").write_text(json.dumps(manifest, indent=2))
    return path


def cmd_restore(conn, account_id: int, dump: Path) -> dict:
    manifest = json.loads(Path(f"{dump}.json").read_text())
    if manifest["account_id"] != account_id:
        raise Refused(f"dump is of account {manifest['account_id']}, not {account_id}")
    check_client_version(conn, "pg_restore")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM create_account_partitions(%s)", (account_id,))
    conn.commit()
    non_empty = {t: n for t, n in _counts(conn, account_id).items() if n}
    if non_empty:
        raise Refused(f"partitions of account {account_id} are not empty: {non_empty}")

    for t in DATA_TABLES:  # parent before child: FKs are checked row by row
        _run([
            _tool("pg_restore"), "--data-only", "--exit-on-error",
            "-d", libpq_env()["PGDATABASE"], "-t", _partition(t, account_id), str(dump),
        ])

    with conn.cursor() as cur:
        for t in SEQUENCE_TABLES:
            # A restore into another database must not hand out restored ids again.
            cur.execute(
                f"SELECT setval('{t}_id_seq', greatest("
                f"(SELECT coalesce(max(id), 0) FROM {t}), "
                f"(SELECT last_value FROM {t}_id_seq)))"
            )
    conn.commit()

    restored = _counts(conn, account_id)
    expected = {t: n for t, n in manifest["rows"].items()}
    if restored != expected:
        raise RuntimeError(f"row counts differ after restore: expected {expected}, got {restored}")
    return restored


def _fresh_dump(out_dir: Path, account_id: int) -> Path | None:
    now = time.time()
    dumps = [
        p for p in out_dir.glob(f"acc{account_id}_*.dump")
        if now - p.stat().st_mtime < DUMP_MAX_AGE_SECONDS and Path(f"{p}.json").exists()
    ]
    return max(dumps, key=lambda p: p.stat().st_mtime) if dumps else None


def cmd_drop(conn, account_id: int, confirm: str, out_dir: Path) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT alias, is_enabled FROM accounts WHERE id = %s", (account_id,))
        row = cur.fetchone()
    if row is None:
        raise Refused(f"account {account_id} not found")
    alias, is_enabled = row
    if confirm != alias:
        raise Refused(f"--confirm must be the account alias ({alias!r})")
    if is_enabled:
        raise Refused(f"account {alias!r} is enabled; disable it first")
    dump = _fresh_dump(out_dir, account_id)
    if dump is None:
        raise Refused(f"no dump of account {account_id} younger than 1 hour in {out_dir}; run dump first")
    print(f"dump: {dump}", flush=True)

    dropped = []
    with conn.cursor() as cur:
        for t in reversed(DATA_TABLES):
            part = _partition(t, account_id)
            cur.execute("SELECT to_regclass(%s)", (part,))
            if cur.fetchone()[0] is None:
                continue
            cur.execute(f'ALTER TABLE "{t}" DETACH PARTITION "{part}"')
            cur.execute(f'DROP TABLE "{part}"')
            dropped.append(part)
        for table in ("account_sessions", "api_keys", "account_tool_policy"):
            cur.execute(f"DELETE FROM {table} WHERE account_id = %s", (account_id,))
    conn.commit()
    return dropped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.scripts.account_partitions")
    parser.add_argument("command", choices=("list", "grants", "dump", "restore", "drop"))
    parser.add_argument("--account", type=int)
    parser.add_argument("--file", type=Path)
    parser.add_argument("--confirm")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args(argv)
    if args.command != "grants" and args.account is None:
        parser.error("--account is required")

    conn = connect()
    try:
        if args.command == "list":
            result = cmd_list(conn, args.account)
        elif args.command == "grants":
            cmd_grants(conn)
            result = {"granted_to": APP_ROLE}
        elif args.command == "dump":
            result = {"file": str(cmd_dump(conn, args.account, args.out_dir))}
        elif args.command == "restore":
            if args.file is None:
                parser.error("restore needs --file")
            result = {"restored": cmd_restore(conn, args.account, args.file)}
        else:
            if not args.confirm:
                parser.error("drop needs --confirm <alias>")
            result = {"dropped": cmd_drop(conn, args.account, args.confirm, args.out_dir)}
    except Refused as exc:
        conn.rollback()
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
