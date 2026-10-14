"""Migration 010 (account isolation): upgrade / downgrade 009 on a clean and a filled DB.

- clean: a throwaway database in the test cluster, 003 → seed 'work' (004
  needs it) → head → 009 → head.
- filled: the test DB itself; legacy tables are fingerprinted before and after
  head → 009 → head and must not change; every account gets its partitions
  back; audit functions are SECURITY DEFINER at head and INVOKER at 009.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg2
import pytest

from tests.conftest import TEST_DB_URL
from tests.isolation_env import DATA_TABLES, sql

REPO_ROOT = Path(__file__).resolve().parent.parent
HEAD = "011"

NEW_TABLES = ("api_keys", "people", "identities", "people_merge_log", *DATA_TABLES)
LEGACY_FINGERPRINTS = {
    t: f"SELECT count(*), md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) FROM {t} x"
    for t in (
        "tg_users", "tg_chats", "tg_messages", "tg_media", "tg_sync_state",
        "max_users", "max_chats", "max_messages", "max_sync_state",
        "chat_access", "users_registry", "account_tool_policy",
    )
}
LEGACY_FINGERPRINTS["accounts"] = (
    "SELECT count(*), md5(coalesce(string_agg(id || ':' || alias || ':' || mode || ':' || platform,"
    " '|' ORDER BY id), '')) FROM accounts"
)


def _alembic(url: str, *args: str, destructive: bool = True, expect_ok: bool = True) -> str:
    env = {**os.environ, "DATABASE_URL": url}
    env.pop("MIGRATIONS_DATABASE_URL", None)
    env.pop("ALLOW_DESTRUCTIVE_DOWNGRADE", None)
    if destructive:
        env["ALLOW_DESTRUCTIVE_DOWNGRADE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=300,
    )
    if expect_ok:
        assert proc.returncode == 0, f"alembic {' '.join(args)}:\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout + proc.stderr


def _q(dsn: str, statement: str, params=None):
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(statement, params)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


def _state(dsn: str) -> dict:
    tables = {r[0] for r in _q(dsn, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    tables |= {r[0] for r in _q(dsn, "SELECT relname FROM pg_class WHERE relkind = 'p'")}
    secdef = dict(_q(dsn, "SELECT proname, prosecdef FROM pg_proc WHERE proname IN"
                          " ('ensure_audit_partition','create_audit_partition',"
                          "  'drop_old_audit_partitions','drain_audit_default_for_month')"))
    has_col = bool(_q(dsn, "SELECT 1 FROM information_schema.columns"
                           " WHERE table_name = 'accounts' AND column_name = 'external_user_id'"))
    return {"tables": tables, "secdef": secdef, "external_user_id": has_col}


def _missing_partitions(dsn: str) -> list:
    return _q(dsn, "SELECT a.id, p.parent FROM accounts a, account_partitions(a.id) p WHERE p.rows IS NULL")


def _assert_head(dsn: str) -> None:
    st = _state(dsn)
    assert set(NEW_TABLES) <= st["tables"]
    assert st["external_user_id"] and all(st["secdef"].values()) and len(st["secdef"]) == 4
    assert _missing_partitions(dsn) == []


def _assert_009(dsn: str) -> None:
    st = _state(dsn)
    assert not set(NEW_TABLES) & st["tables"]
    assert not st["external_user_id"] and not any(st["secdef"].values())


@pytest.fixture
def clean_db():
    name = f"tg_iso_mig_{uuid.uuid4().hex[:8]}"
    sql(f'CREATE DATABASE "{name}"')
    u = urlparse(TEST_DB_URL)
    url = urlunparse(u._replace(path=f"/{name}"))
    try:
        yield url
    finally:
        sql(f'DROP DATABASE "{name}" WITH (FORCE)')


def test_upgrade_downgrade_clean_db(clean_db):
    _alembic(clean_db, "upgrade", "003")
    _q(_dsn(clean_db), "INSERT INTO accounts (alias, phone, mode) VALUES ('work', '+7', 'rw')")
    _alembic(clean_db, "upgrade", "head")
    _assert_head(_dsn(clean_db))
    _alembic(clean_db, "downgrade", "009")
    _assert_009(_dsn(clean_db))
    _alembic(clean_db, "upgrade", "head")
    _assert_head(_dsn(clean_db))


def test_upgrade_downgrade_filled_db():
    dsn = _dsn(TEST_DB_URL)
    [(rev,)] = _q(dsn, "SELECT version_num FROM alembic_version")
    assert rev == HEAD, "test DB must be at head before the suite"
    before = {t: _q(dsn, q)[0] for t, q in LEGACY_FINGERPRINTS.items()}
    try:
        _alembic(TEST_DB_URL, "downgrade", "009")
        _assert_009(dsn)
        assert {t: _q(dsn, q)[0] for t, q in LEGACY_FINGERPRINTS.items()} == before
    finally:
        _alembic(TEST_DB_URL, "upgrade", "head")
    _assert_head(dsn)
    assert {t: _q(dsn, q)[0] for t, q in LEGACY_FINGERPRINTS.items()} == before


def test_downgrade_refuses_when_new_tables_have_rows(clean_db):
    dsn = _dsn(clean_db)
    _alembic(clean_db, "upgrade", "003")
    _q(dsn, "INSERT INTO accounts (alias, phone, mode) VALUES ('work', '+7', 'rw')")
    _alembic(clean_db, "upgrade", "head")
    _q(dsn, "INSERT INTO people (display_name) VALUES ('keep me')")

    out = _alembic(clean_db, "downgrade", "009", destructive=False, expect_ok=False)
    assert "downgrade 010 refused: people" in out and "STORAGE_MODE=legacy" in out
    _assert_head(dsn)  # nothing was dropped

    _alembic(clean_db, "downgrade", "009", destructive=True)
    _assert_009(dsn)


def test_downgrade_of_empty_new_tables_needs_no_flag(clean_db):
    _alembic(clean_db, "upgrade", "003")
    _q(_dsn(clean_db), "INSERT INTO accounts (alias, phone, mode) VALUES ('work', '+7', 'rw')")
    _alembic(clean_db, "upgrade", "head")
    _alembic(clean_db, "downgrade", "009", destructive=False)
    _assert_009(_dsn(clean_db))
