"""test_max_migration.py — migration 009 (MAX platform), PR-1.

Covers alembic/versions/009_max_platform.py against the real test Postgres:
  1. upgrade → downgrade → upgrade round-trip leaves tg_* and accounts untouched
     (row counts and md5 of the rows are equal before and after)
  2. existing accounts get platform='telegram'
  3. CHECK ck_accounts_platform rejects anything but telegram|max
  4. downgrade really removes max_* and the four accounts columns
  5. ORM models match the migrated schema (column names of max_* == tg_*)

Requires real Postgres on :5433 with migrations applied through head (009).
Leaves the DB at head even if an assertion fails.
"""

import os
import subprocess
import sys
from pathlib import Path

import psycopg2
import pytest

from tests.conftest import TEST_DB_URL

REPO_ROOT = Path(__file__).resolve().parent.parent

# Seed ids far away from anything other tests insert.
_CHAT_ID = -990_000_000_009
_USER_ID = 990_000_000_009

_MAX_TABLES = (
    "max_users",
    "max_chats",
    "max_messages",
    "max_media",
    "max_sync_state",
    "max_raw_events",
)
_NEW_ACCOUNT_COLUMNS = ("platform", "platform_user_id", "write_chat_ids", "write_rate_per_hour")

# Fingerprint queries: row count + md5 over the full row text, deterministic order.
_FINGERPRINTS = {
    "tg_users": "SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY id), '')) FROM tg_users t",
    "tg_chats": "SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY id), '')) FROM tg_chats t",
    "tg_messages": "SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY id), '')) FROM tg_messages t",
    "tg_media": "SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY id), '')) FROM tg_media t",
    "tg_sync_state": "SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY id), '')) FROM tg_sync_state t",
    "accounts": (
        "SELECT count(*), md5(coalesce(string_agg(id || ':' || alias || ':' || mode, '|' ORDER BY id), '')) "
        "FROM accounts"
    ),
}


def _dsn() -> str:
    return TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://")


def _connect():
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    return conn


def _alembic(*args: str) -> None:
    env = {**os.environ, "DATABASE_URL": TEST_DB_URL}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"alembic {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}"


def _current_revision(cur) -> str:
    cur.execute("SELECT version_num FROM alembic_version")
    return cur.fetchone()[0]


def _fingerprint(cur) -> dict:
    out = {}
    for name, sql in _FINGERPRINTS.items():
        cur.execute(sql)
        out[name] = cur.fetchone()
    return out


def _tables(cur) -> set[str]:
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {r[0] for r in cur.fetchall()}


def _columns(cur, table: str) -> list[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


@pytest.fixture
def pg():
    conn = _connect()
    cur = conn.cursor()
    assert _current_revision(cur) == "009", "test DB must be migrated to head (009) before the suite"
    # Seed TG data so the round-trip has something to (not) change.
    cur.execute(
        "INSERT INTO tg_users (id, username, first_name) VALUES (%s, 'mig009', 'Mig') "
        "ON CONFLICT (id) DO NOTHING",
        (_USER_ID,),
    )
    cur.execute(
        "INSERT INTO tg_chats (id, chat_type, title) VALUES (%s, 'group', 'mig009') "
        "ON CONFLICT (id) DO NOTHING",
        (_CHAT_ID,),
    )
    cur.execute(
        "INSERT INTO tg_messages (message_id, chat_id, from_user_id, text, tg_date) "
        "VALUES (1, %s, %s, 'hello', now()), (2, %s, %s, 'world', now()) "
        "ON CONFLICT (message_id, chat_id) DO NOTHING",
        (_CHAT_ID, _USER_ID, _CHAT_ID, _USER_ID),
    )
    try:
        yield cur
    finally:
        try:
            if _current_revision(cur) != "009":
                _alembic("upgrade", "head")
        finally:
            cur.execute("DELETE FROM tg_messages WHERE chat_id = %s", (_CHAT_ID,))
            cur.execute("DELETE FROM tg_chats WHERE id = %s", (_CHAT_ID,))
            cur.execute("DELETE FROM tg_users WHERE id = %s", (_USER_ID,))
            cur.execute("DELETE FROM accounts WHERE alias LIKE 'mig009-%%'")
            conn.close()


def test_roundtrip_keeps_tg_data_and_accounts(pg):
    """upgrade → downgrade → upgrade: tg_* and accounts(id,alias,mode) byte-equal."""
    before = _fingerprint(pg)
    assert before["tg_messages"][0] >= 2

    _alembic("downgrade", "008")
    assert _current_revision(pg) == "008"
    assert _fingerprint(pg) == before, "downgrade 009 must not touch tg_* / accounts rows"

    _alembic("upgrade", "head")
    assert _current_revision(pg) == "009"
    assert _fingerprint(pg) == before, "upgrade 009 must not touch tg_* / accounts rows"


def test_downgrade_removes_max_schema(pg):
    """downgrade drops all max_* tables and the four accounts columns; upgrade restores them."""
    assert set(_MAX_TABLES) <= _tables(pg)

    _alembic("downgrade", "008")
    assert not (set(_MAX_TABLES) & _tables(pg))
    cols = set(_columns(pg, "accounts"))
    assert not (set(_NEW_ACCOUNT_COLUMNS) & cols)

    _alembic("upgrade", "head")
    assert set(_MAX_TABLES) <= _tables(pg)
    assert set(_NEW_ACCOUNT_COLUMNS) <= set(_columns(pg, "accounts"))


def test_existing_accounts_get_telegram_platform(pg):
    """Rows that existed before 009 get platform='telegram' and NULL write guards."""
    _alembic("downgrade", "008")
    pg.execute(
        "INSERT INTO accounts (alias, phone, mode) VALUES ('mig009-old', '+79000000009', 'ro')"
    )
    _alembic("upgrade", "head")

    pg.execute(
        "SELECT platform, platform_user_id, write_chat_ids, write_rate_per_hour "
        "FROM accounts WHERE alias = 'mig009-old'"
    )
    assert pg.fetchone() == ("telegram", None, None, None)

    pg.execute("SELECT count(*) FROM accounts WHERE platform IS DISTINCT FROM 'telegram'")
    assert pg.fetchone()[0] == 0


def test_platform_check_constraint(pg):
    """CHECK accepts telegram|max, rejects anything else."""
    pg.execute(
        "INSERT INTO accounts (alias, phone, mode, platform) "
        "VALUES ('mig009-max', '+79000000010', 'ro', 'max')"
    )
    with pytest.raises(psycopg2.errors.CheckViolation):
        pg.execute(
            "INSERT INTO accounts (alias, phone, mode, platform) "
            "VALUES ('mig009-x', '+79000000011', 'ro', 'x')"
        )


@pytest.mark.parametrize(
    "tg_table,max_table,extra",
    [
        ("tg_users", "max_users", set()),
        ("tg_chats", "max_chats", set()),
        ("tg_messages", "max_messages", {"deleted_at"}),
        ("tg_media", "max_media", {"attach_index"}),
        ("tg_sync_state", "max_sync_state", {"oldest_time_ms", "newest_time_ms", "last_catchup_at"}),
    ],
)
def test_max_tables_mirror_tg_columns(pg, tg_table, max_table, extra):
    """max_* = tg_* column-for-column plus the documented MAX-only extras (ADR §2.A)."""
    assert set(_columns(pg, max_table)) == set(_columns(pg, tg_table)) | extra


def test_orm_models_match_migrated_schema(pg):
    """ORM classes declare exactly the columns the migration creates."""
    from app.models import (
        Account,
        MaxChat,
        MaxMedia,
        MaxMessage,
        MaxRawEvent,
        MaxSyncState,
        MaxUser,
    )

    for model in (MaxUser, MaxChat, MaxMessage, MaxMedia, MaxSyncState, MaxRawEvent, Account):
        table = model.__table__.name
        assert {c.name for c in model.__table__.columns} == set(_columns(pg, table)), table
