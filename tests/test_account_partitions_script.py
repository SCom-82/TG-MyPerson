"""T28: account_partitions dump → drop → restore (ТЗ 2026-10-13 §11.4)."""

import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.repo import chats, messages, raw_events, users
from app.repo.rows import ChatRow, MediaRow, MessageRow, UserRow
from app.scripts import account_partitions as ap
from app.storage import account_db
from tests.conftest import TEST_DB_URL
from tests.isolation_env import DATA_TABLES, create_account, drop_account, drop_test_contacts, sql

T0 = datetime(2026, 10, 13, tzinfo=timezone.utc)
ALIAS = "iso-dump"

pytestmark = pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump not installed")


@pytest.fixture
def account(app_role, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    monkeypatch.delenv("MIGRATIONS_DATABASE_URL", raising=False)
    for (acc,) in sql("SELECT id FROM accounts WHERE alias = %s", (ALIAS,), fetch=True):
        drop_account(acc)
    acc = create_account(ALIAS, external_user_id=9_400_000_050)
    yield acc
    drop_account(acc)
    drop_test_contacts()


async def _fill(acc: int) -> None:
    async with account_db(acc) as db:
        chat = await chats.upsert_chat(db, acc, ChatRow(-1_009_400_000_900, "supergroup", title="dump"))
        await users.upsert_user(db, acc, "telegram", UserRow(9_400_000_901, username="u"), "message")
        for i in range(1, 6):
            await messages.upsert_message(db, acc, chat, MessageRow(
                external_id=i, sent_at=T0, sender_external_id=9_400_000_901, text=f"m{i}",
                media=[MediaRow("photo", file_external_id=f"f{i}")] if i % 2 else [],
            ))
        await raw_events.add_raw_event(db, acc, "tg:test", {"x": 1})
        await db.commit()


def _counts(conn, acc):
    return {r["parent"]: r["rows"] for r in ap.cmd_list(conn, acc)}


async def test_dump_drop_restore_roundtrip(account, tmp_path):
    await _fill(account)
    conn = ap.connect()
    try:
        before = _counts(conn, account)
        assert before["messages"] == 5 and before["media"] == 3

        with pytest.raises(ap.Refused, match="enabled"):
            ap.cmd_drop(conn, account, ALIAS, tmp_path)
        conn.rollback()
        sql("UPDATE accounts SET is_enabled = false WHERE id = %s", (account,))
        with pytest.raises(ap.Refused, match="no dump"):
            ap.cmd_drop(conn, account, ALIAS, tmp_path)
        conn.rollback()
        with pytest.raises(ap.Refused, match="alias"):
            ap.cmd_drop(conn, account, "wrong", tmp_path)
        conn.rollback()

        dump = ap.cmd_dump(conn, account, tmp_path)
        assert dump.exists() and Path(f"{dump}.json").exists()

        dropped = ap.cmd_drop(conn, account, ALIAS, tmp_path)
        assert dropped == [f"{t}_a{account}" for t in reversed(DATA_TABLES)]
        assert all(n is None for n in _counts(conn, account).values())
        assert sql("SELECT is_enabled FROM accounts WHERE id = %s", (account,), fetch=True) == [(False,)]

        restored = ap.cmd_restore(conn, account, dump)
        assert restored == before
        # FKs valid after restore: every media row has its message, every message its chat.
        assert sql(
            f"SELECT count(*) FROM media_a{account} md LEFT JOIN messages_a{account} m"
            " ON m.id = md.message_id WHERE m.id IS NULL", fetch=True,
        ) == [(0,)]
        with pytest.raises(ap.Refused, match="not empty"):
            ap.cmd_restore(conn, account, dump)
    finally:
        conn.close()


def test_stale_dump_is_not_enough(account, tmp_path):
    sql("UPDATE accounts SET is_enabled = false WHERE id = %s", (account,))
    old = tmp_path / f"acc{account}_20200101T000000Z.dump"
    old.write_bytes(b"x")
    Path(f"{old}.json").write_text("{}")
    past = time.time() - 2 * 3600
    os.utime(old, (past, past))

    conn = ap.connect()
    try:
        with pytest.raises(ap.Refused, match="no dump"):
            ap.cmd_drop(conn, account, ALIAS, tmp_path)
    finally:
        conn.close()


def test_grants_command(app_role):
    conn = ap.connect()
    try:
        ap.cmd_grants(conn)
    finally:
        conn.close()
    [(ok,)] = sql("SELECT has_table_privilege('tg_myperson_app', 'messages', 'INSERT')", fetch=True)
    assert ok


def _fake_tool(tmp_path, name: str, version: str) -> str:
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\necho '{name} (PostgreSQL) {version}'\n")
    path.chmod(0o755)
    return str(path)


async def test_client_major_must_match_server(account, tmp_path, monkeypatch):
    await _fill(account)
    conn = ap.connect()
    try:
        server = ap.server_major(conn)
        dump = ap.cmd_dump(conn, account, tmp_path)  # matching client from PATH

        monkeypatch.setenv("PG_RESTORE", _fake_tool(tmp_path, "pg_restore", f"{server + 1}.0"))
        with pytest.raises(ap.Refused, match=f"pg_restore is PostgreSQL {server + 1}, the server is {server}"):
            ap.cmd_restore(conn, account, dump)
        conn.rollback()

        monkeypatch.setenv("PG_DUMP", _fake_tool(tmp_path, "pg_dump", f"{server + 1}.2 (Debian)"))
        with pytest.raises(ap.Refused, match="postgresql-client-"):
            ap.cmd_dump(conn, account, tmp_path)
    finally:
        conn.close()
