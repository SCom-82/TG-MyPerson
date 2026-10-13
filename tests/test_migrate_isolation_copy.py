"""migrate_isolation copy / delta / verify (ТЗ 2026-10-13 §8.2–8.4, T19).

A throwaway database at head with a small legacy world covering every rule:
probe membership, a chat_access signal, an unassigned channel, private chat
and basic group split by rule D (with reference 'out' messages), MAX with a
TG↔MAX phone match, a snapshot, a registry-only user.
"""

import os
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg2
import pytest

from app.scripts import isolation_copy as ic
from tests.conftest import TEST_DB_URL
from tests.isolation_env import sql

REPO_ROOT = Path(__file__).resolve().parent.parent
WORK_SELF, RO_SELF, MAX_SELF = 1000, 2000, 3000
ALICE, BOB, REG_ONLY, MAX_PEER = 501, 502, 503, 601
SHARED, SIGNAL, ORPHAN, GROUP = -1000000000111, -1000000000222, -1000000000333, -444
MAX_CHAT = 777

SEED = f"""
INSERT INTO accounts (alias, phone, mode, platform, tg_user_id, platform_user_id) VALUES
  ('personal-ro', '+72', 'ro', 'telegram', {RO_SELF}, NULL),
  ('max-work', '+73', 'ro', 'max', NULL, {MAX_SELF});
SELECT create_account_partitions(id) FROM accounts;

INSERT INTO tg_users (id, username, first_name, phone) VALUES
  ({WORK_SELF}, 'me_work', 'Work', NULL), ({RO_SELF}, 'me_ro', 'Ro', NULL),
  ({ALICE}, 'alice', 'Alice', '79001112233'), ({BOB}, 'bob', 'Bob', NULL);

INSERT INTO tg_chats (id, chat_type, title) VALUES
  ({SHARED}, 'channel', 'Shared'), ({SIGNAL}, 'supergroup', 'Signal'), ({ORPHAN}, 'channel', 'Orphan'),
  ({ALICE}, 'private', 'Alice'), ({GROUP}, 'group', 'Basic');

CREATE TABLE IF NOT EXISTS mig_membership (
    account_id int NOT NULL REFERENCES accounts(id), external_chat_id bigint NOT NULL,
    chat_type text NOT NULL, title text, role text, access_hash bigint,
    PRIMARY KEY (account_id, external_chat_id));
INSERT INTO mig_membership SELECT id, {SHARED}, 'channel', 'Shared', 'admin', 4242 FROM accounts WHERE platform = 'telegram';

INSERT INTO chat_access (account_id, chat_id, role, first_seen_at, last_seen_at)
  SELECT id, {SIGNAL}, 'member', now(), now() FROM accounts WHERE alias = 'personal-ro';

INSERT INTO tg_messages (id, message_id, chat_id, from_user_id, text, tg_date, raw_data, message_type) VALUES
  (1, 1, {SHARED}, {ALICE}, 'c1', '2026-01-01', '{{}}', 'text'),
  (2, 2, {SHARED}, {ALICE}, 'c2 photo', '2026-01-02', '{{}}', 'photo'),
  (3, 3, {SHARED}, NULL, 'c3 post', '2026-01-03', '{{}}', 'text'),
  (4, 10, {SIGNAL}, {BOB}, 's1', '2026-01-01', '{{}}', 'text'),
  (5, 11, {ORPHAN}, {BOB}, 'orphan', '2026-01-01', '{{}}', 'text'),
  (6, 50, {ALICE}, {ALICE}, 'old, before split', '2025-10-01', '{{}}', 'text'),
  (7, 100, {ALICE}, {ALICE}, 'work era', '2026-01-01', '{{}}', 'text'),
  (8, 101, {ALICE}, {WORK_SELF}, 'work wrote', '2026-01-01', '{{"out": true}}', 'text'),
  (9, 40, {ALICE}, {RO_SELF}, 'ro wrote', '2025-09-01', '{{"out": true}}', 'text'),
  (10, 160000, {ALICE}, {ALICE}, 'ro era again', '2026-01-02', '{{}}', 'text'),
  (11, 120, {GROUP}, {BOB}, 'group', '2026-02-01', '{{}}', 'text');
SELECT setval(pg_get_serial_sequence('tg_messages', 'id'), 11);
INSERT INTO tg_media (id, message_pk, file_id, file_type, file_name, file_size) VALUES (1, 2, 'f1', 'photo', 'a.jpg', 10);
SELECT setval(pg_get_serial_sequence('tg_media', 'id'), 1);
INSERT INTO tg_sync_state (chat_id, oldest_message_id, newest_message_id, is_fully_synced, total_messages_synced)
  VALUES ({SHARED}, 1, 3, true, 3), ({ALICE}, 40, 160000, false, 5);

INSERT INTO users_registry (tg_user_id, primary_username, primary_name, first_seen_at, last_seen_at, tags)
  VALUES ({REG_ONLY}, 'reg_only', 'Reg Only', now(), now(), '{{lead}}');
INSERT INTO users_registry_sources (registry_id, source_type, account_id, observed_at)
  SELECT r.id, 'snapshot', a.id, now() FROM users_registry r, accounts a WHERE a.alias = 'personal-ro';
INSERT INTO chat_members_snapshots (id, chat_id, account_id, taken_at, members_count, source)
  SELECT 5, {SIGNAL}, id, now(), 1, 'api' FROM accounts WHERE alias = 'personal-ro';
INSERT INTO chat_member_records (id, snapshot_id, tg_user_id, username, role) VALUES (9, 5, {BOB}, 'bob', 'member');

INSERT INTO max_users (id, first_name, phone) VALUES ({MAX_SELF}, 'Me', NULL), ({MAX_PEER}, 'Алиса', '+7 900 111-22-33');
INSERT INTO max_chats (id, chat_type, title) VALUES ({MAX_CHAT}, 'private', 'Алиса');
INSERT INTO max_messages (id, message_id, chat_id, from_user_id, text, tg_date, message_type) VALUES
  (1, 9001, {MAX_CHAT}, {MAX_PEER}, 'привет из max', '2026-03-01', 'text');
INSERT INTO max_sync_state (chat_id, oldest_message_id, newest_message_id, oldest_time_ms, newest_time_ms)
  VALUES ({MAX_CHAT}, 9001, 9001, 1772323200000, 1772323200000);
INSERT INTO max_raw_events (id, account_id, opcode, payload, normalized) VALUES
  (1, NULL, 128, '{{}}', true);
"""


def _url(name: str) -> str:
    u = urlparse(TEST_DB_URL)
    return urlunparse(u._replace(path=f"/{name}"))


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url}
    env.pop("MIGRATIONS_DATABASE_URL", None)
    proc = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.fixture
def legacy_db(monkeypatch):
    name = f"tg_iso_copy_{uuid.uuid4().hex[:8]}"
    sql(f'CREATE DATABASE "{name}"')
    url = _url(name)
    dsn = url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        _alembic(url, "upgrade", "003")
        _exec(dsn, "INSERT INTO accounts (alias, phone, mode) VALUES ('work', '+71', 'rw')")
        _alembic(url, "upgrade", "head")
        _exec(dsn, SEED)
        monkeypatch.setenv("MIGRATIONS_DATABASE_URL", url)
        yield dsn
    finally:
        sql(f'DROP DATABASE "{name}" WITH (FORCE)')


def _exec(dsn: str, statement: str, params=None, fetch: bool = False):
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(statement, params)
            return cur.fetchall() if fetch else None
    finally:
        conn.close()


def _opts(**kw) -> ic.Options:
    return ic.Options(selves={"work": WORK_SELF}, probe_report=Path("/nonexistent"), **kw)


def _messages(dsn: str) -> set[tuple]:
    return set(_exec(dsn, """
        SELECT a.alias, ch.external_id, nm.external_id, nm.text, nm.is_outgoing FROM messages nm
        JOIN accounts a ON a.id = nm.account_id
        JOIN chats ch ON ch.account_id = nm.account_id AND ch.id = nm.chat_id""", fetch=True))


def test_copy_verify_and_rules(legacy_db):
    report = ic.copy(_opts())
    assert report["ok"]
    msgs = _messages(legacy_db)

    # channel from probe: a copy for each member
    for alias in ("work", "personal-ro"):
        assert {(alias, SHARED, i) for i in (1, 2, 3)} <= {m[:3] for m in msgs}
    # chat_access signal → personal-ro only; orphan channel → unassigned, not copied
    assert {m[:3] for m in msgs if m[1] == SIGNAL} == {("personal-ro", SIGNAL, 10)}
    assert not [m for m in msgs if m[1] == ORPHAN]
    assert _exec(legacy_db, "SELECT legacy_chat_id FROM mig_unassigned", fetch=True) == [(ORPHAN,)]
    # rule D in the private chat and the basic group
    private = {m[2]: m[0] for m in msgs if m[1] == ALICE}
    assert private == {50: "personal-ro", 100: "work", 101: "work", 40: "personal-ro", 160000: "personal-ro"}
    assert {m[0] for m in msgs if m[1] == GROUP} == {"work"}
    # is_outgoing recomputed from the account's own user
    assert ("work", ALICE, 101, "work wrote", True) in msgs
    assert ("personal-ro", ALICE, 40, "ro wrote", True) in msgs
    # MAX
    assert ("max-work", MAX_CHAT, 9001, "привет из max", False) in msgs
    assert _exec(legacy_db, "SELECT count(*) FROM media", fetch=True) == [(2,)]  # one photo, two copies

    # contacts: TG alice and MAX peer share the phone → one person; registry-only user kept with tags
    persons = dict(_exec(legacy_db, "SELECT external_user_id, person_id FROM identities"
                                    " WHERE external_user_id IN (%s, %s)", (ALICE, MAX_PEER), fetch=True))
    assert persons[ALICE] == persons[MAX_PEER]
    assert _exec(legacy_db, "SELECT p.tags FROM people p JOIN identities i ON i.person_id = p.id"
                            " WHERE i.external_user_id = %s", (REG_ONLY,), fetch=True) == [(["lead"],)]
    # snapshot → personal-ro, member linked to the account's users row
    assert _exec(legacy_db, "SELECT m.username, u.external_id FROM snapshot_members m"
                            " JOIN users u ON u.account_id = m.account_id AND u.id = m.user_id", fetch=True) == [
        ("bob", BOB)]
    # sync state: channel copied to both, private chat (two owners) recomputed per owner
    states = dict(_exec(legacy_db, """
        SELECT a.alias || ':' || ch.external_id, s.newest_external_id FROM sync_state s
        JOIN chats ch ON ch.account_id = s.account_id AND ch.id = s.chat_id
        JOIN accounts a ON a.id = s.account_id""", fetch=True))
    assert states[f"work:{SHARED}"] == 3 and states[f"personal-ro:{SHARED}"] == 3
    assert states[f"work:{ALICE}"] == 101 and states[f"personal-ro:{ALICE}"] == 160000
    assert states[f"max-work:{MAX_CHAT}"] == 9001

    check = ic.verify(_opts())
    assert check["ok"], check


def test_copy_is_idempotent(legacy_db):
    ic.copy(_opts())
    again = ic.copy(_opts())
    assert set(again["changes"].values()) == {0}, again["changes"]


def test_delta_picks_up_new_edited_deleted_and_renamed(legacy_db):
    ic.copy(_opts())
    _exec(legacy_db, f"""
        INSERT INTO tg_messages (message_id, chat_id, from_user_id, text, tg_date, message_type)
          VALUES (4, {SHARED}, {BOB}, 'new after copy', '2026-01-04', 'text');
        UPDATE tg_messages SET text = 'c1 edited', is_edited = true, edit_date = now() WHERE id = 1;
        UPDATE max_messages SET deleted_at = now() WHERE id = 1;
        UPDATE tg_chats SET title = 'Shared renamed', updated_at = now() WHERE id = {SHARED};
    """)
    report = ic.delta(_opts())
    assert report["ok"]
    msgs = _messages(legacy_db)
    for alias in ("work", "personal-ro"):
        assert (alias, SHARED, 4, "new after copy", False) in msgs
        assert (alias, SHARED, 1, "c1 edited", False) in msgs
    assert _exec(legacy_db, "SELECT deleted_at IS NOT NULL FROM messages WHERE external_id = 9001", fetch=True) == [
        (True,)]
    assert {t for (t,) in _exec(legacy_db, f"SELECT title FROM chats WHERE external_id = {SHARED}", fetch=True)} == {
        "Shared renamed"}
    assert ic.verify(_opts())["ok"]


def test_precondition_third_tg_account_stops(legacy_db):
    _exec(legacy_db, "INSERT INTO accounts (alias, phone, mode, platform, tg_user_id) VALUES ('third', '+74', 'ro', 'telegram', 4000);"
                     " SELECT create_account_partitions(id) FROM accounts WHERE alias = 'third'")
    with pytest.raises(ic.PreconditionFailed, match="two TG accounts"):
        ic.copy(_opts())


def test_precondition_reference_mismatch_stops(legacy_db):
    # work's own message dated before the split → rule D gives it to personal-ro
    _exec(legacy_db, f"INSERT INTO tg_messages (message_id, chat_id, from_user_id, text, tg_date, raw_data)"
                     f" VALUES (30, {ALICE}, {WORK_SELF}, 'x', '2025-09-01', '{{\"out\": true}}')")
    with pytest.raises(ic.PreconditionFailed, match="contradict rule D"):
        ic.copy(_opts())


def test_unknown_own_id_stops(legacy_db):
    with pytest.raises(ic.PreconditionFailed, match="external_user_id unknown"):
        ic.copy(ic.Options(probe_report=Path("/nonexistent")))


def test_cli_reports_failed_precondition(legacy_db, tmp_path):
    from app.scripts import migrate_isolation as mi

    assert mi.main(["copy", "--out-dir", str(tmp_path), "--probe-report", "/nonexistent"]) == 1
    [report] = tmp_path.glob("migrate_isolation-copy-*.json")
    assert "external_user_id unknown" in report.read_text()
    assert mi.main(["copy", "--self", f"work={WORK_SELF}", "--probe-report", "/nonexistent",
                    "--out-dir", str(tmp_path)]) == 0
    assert mi.main(["verify", "--out-dir", str(tmp_path)]) == 0
