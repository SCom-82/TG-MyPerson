"""test_golden_tg.py — golden responses of the Telegram user API (MAX plan, R-2).

Invariant (API spec §0.1): for Telegram aliases every user-API response stays
byte-for-byte the same — status, body, field order, error texts. The snapshots
in tests/golden/ were recorded BEFORE the platform-routing change (PR-2) and must
match after every following PR.

The test runs the real middleware stack (alias resolution, tool authz, audit)
against an isolated database `tg_myperson_golden` in the same test Postgres.
Reads from tg_* are global (not filtered by account), so a shared DB would leak
rows from other tests into the snapshots.

Only the Telegram pool is stubbed (auth_status needs a session object).

Re-record (only when a TG response change is intended and approved):
    GOLDEN_UPDATE=1 pytest tests/test_golden_tg.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg2
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import TEST_DB_URL

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
GOLDEN_DB = "tg_myperson_golden"
GOLDEN_URL = TEST_DB_URL.rsplit("/", 1)[0] + "/" + GOLDEN_DB
UPDATE = os.environ.get("GOLDEN_UPDATE") == "1"

API_KEY = {"x-api-key": "test-api-key"}

# (snapshot name, method, path, alias header or None, json body or None)
CASES = [
    ("list_chats", "GET", "/api/v1/chats", None, None),
    ("list_chats_filtered", "GET", "/api/v1/chats?chat_type=group&limit=1&offset=0", None, None),
    ("list_chats_search", "GET", "/api/v1/chats?search=golden", "golden-ro", None),
    ("get_chat_detail", "GET", "/api/v1/chats/-1001000000001", None, None),
    ("get_chat_detail_404", "GET", "/api/v1/chats/-1009999999999", None, None),
    ("list_messages", "GET", "/api/v1/messages", None, None),
    ("list_messages_by_chat", "GET", "/api/v1/messages?chat_id=-1001000000001&limit=2", None, None),
    ("list_messages_search", "GET", "/api/v1/messages?search=hello", "golden-ro", None),
    ("get_single_message", "GET", "/api/v1/messages/-1001000000001/11", None, None),
    ("get_single_message_404", "GET", "/api/v1/messages/-1001000000001/999", None, None),
    ("list_users", "GET", "/api/v1/users", None, None),
    ("list_users_search", "GET", "/api/v1/users?search=alice", "golden-ro", None),
    ("sync_status", "GET", "/api/v1/sync/status", None, None),
    ("auth_status", "GET", "/api/v1/auth/status", None, None),
    ("auth_status_ro", "GET", "/api/v1/auth/status", "golden-ro", None),
    ("ro_send_message_403", "POST", "/api/v1/messages", "golden-ro", {"chat_id": 1, "text": "x"}),
    ("ro_join_chat_403", "POST", "/api/v1/chats/join", "golden-ro", {"target": "@x"}),
    ("unknown_alias_404", "GET", "/api/v1/chats", "no-such-alias", None),
    ("disabled_alias_404", "GET", "/api/v1/chats", "golden-off", None),
    ("unknown_path_404", "GET", "/api/v1/no-such-endpoint", None, None),
    ("unknown_max_prefix_404", "GET", "/api/v1/_max/chats", None, None),
]

_SEED_SQL = """
INSERT INTO accounts (alias, phone, mode, is_enabled) VALUES
  ('golden-ro',  '+79000000002', 'ro', true),
  ('golden-off', '+79000000003', 'rw', false);

INSERT INTO tg_users (id, username, first_name, last_name, phone, is_bot, is_self, first_seen_at, updated_at) VALUES
  (101, 'alice', 'Alice', 'A', '+70000000101', false, false, '2026-01-01 10:00:00+00', '2026-01-02 10:00:00+00'),
  (102, 'bob',   'Bob',   NULL, NULL,          false, true,  '2026-01-01 11:00:00+00', '2026-01-02 11:00:00+00'),
  (103, NULL,    'Bot',   NULL, NULL,          true,  false, '2026-01-01 12:00:00+00', '2026-01-02 12:00:00+00');

INSERT INTO tg_chats (id, chat_type, title, username, description, members_count, is_monitored,
                      last_message_id, last_message_at, created_at, updated_at) VALUES
  (-1001000000001, 'group',   'Golden Group',   'golden_group', 'desc', 42, true,
   12, '2026-02-01 09:00:00+00', '2026-01-01 00:00:00+00', '2026-02-01 09:00:00+00'),
  (-1001000000002, 'channel', 'Golden Channel', NULL,           NULL,   1000, false,
   21, '2026-02-02 09:00:00+00', '2026-01-01 00:00:00+00', '2026-02-02 09:00:00+00'),
  (101,            'private', 'Alice',          'alice',        NULL,   NULL, true,
   NULL, NULL, '2026-01-01 00:00:00+00', '2026-01-01 00:00:00+00');

INSERT INTO tg_messages (id, message_id, chat_id, from_user_id, sender_chat_id, reply_to_message_id,
                         forward_from_chat_id, forward_from_message_id, message_type, text, text_html,
                         tg_date, is_outgoing, is_edited, edit_date, views, created_at) VALUES
  (1, 11, -1001000000001, 101, NULL, NULL, NULL, NULL, 'text', 'hello world', NULL,
   '2026-02-01 08:00:00+00', false, false, NULL, NULL, '2026-02-01 08:00:01+00'),
  (2, 12, -1001000000001, 102, NULL, 11,   NULL, NULL, 'photo', 'hello photo', '<b>hello</b> photo',
   '2026-02-01 09:00:00+00', true, true, '2026-02-01 09:05:00+00', NULL, '2026-02-01 09:00:01+00'),
  (3, 21, -1001000000002, NULL, -1001000000002, NULL, -1001000000001, 11, 'text', 'post', NULL,
   '2026-02-02 09:00:00+00', false, false, NULL, 77, '2026-02-02 09:00:01+00');

INSERT INTO tg_media (id, message_pk, file_id, file_unique_id, file_type, file_name, file_size,
                      mime_type, local_path, created_at) VALUES
  (1, 2, 'f-1', 'u-1', 'photo', NULL, 12345, 'image/jpeg', NULL, '2026-02-01 09:00:02+00');

INSERT INTO tg_sync_state (chat_id, oldest_message_id, newest_message_id, is_fully_synced,
                           total_messages_synced, last_backfill_at, created_at, updated_at) VALUES
  (-1001000000001, 11, 12, true, 2, '2026-02-03 00:00:00+00', '2026-01-01 00:00:00+00', '2026-02-03 00:00:00+00');
"""


def _admin_conn():
    dsn = TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)[0] + "/postgres"
    conn = psycopg2.connect(dsn)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


def _alembic(*args: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": GOLDEN_URL},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"alembic {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}"


def _golden_psql(sql: str) -> None:
    conn = psycopg2.connect(GOLDEN_URL.replace("postgresql+asyncpg://", "postgresql://"))
    conn.autocommit = True
    try:
        conn.cursor().execute(sql)
    finally:
        conn.close()


@pytest.fixture(scope="module")
def golden_db():
    """Fresh tg_myperson_golden: migrated to head and seeded deterministically."""
    admin = _admin_conn()
    cur = admin.cursor()
    cur.execute(f'DROP DATABASE IF EXISTS "{GOLDEN_DB}" WITH (FORCE)')
    cur.execute(f'CREATE DATABASE "{GOLDEN_DB}"')
    try:
        # Migration 004 refuses to run without a 'work' account (see its docstring).
        _alembic("upgrade", "003")
        _golden_psql("INSERT INTO accounts (alias, phone, mode, is_enabled) VALUES ('work', '+79000000001', 'rw', true)")
        _alembic("upgrade", "head")
        _golden_psql(_SEED_SQL)
        yield GOLDEN_URL
    finally:
        cur.execute(f'DROP DATABASE IF EXISTS "{GOLDEN_DB}" WITH (FORCE)')
        admin.close()


@pytest_asyncio.fixture
async def golden_client(golden_db, monkeypatch):
    import app.authz.middleware as mw
    import app.database as database
    import app.main as main_module
    import app.telegram.pool as pool_module
    from app.database import get_db
    from app.schemas import AuthStatusResponse

    engine = create_async_engine(golden_db, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # Middleware + audit import async_session at call time → point them at the golden DB.
    monkeypatch.setattr(database, "async_session", factory)

    async def _golden_db_dep():
        async with factory() as session:
            yield session

    class _StubSession:
        def __init__(self, alias: str):
            self.alias = alias

        async def get_auth_status(self):
            return AuthStatusResponse(
                connected=True,
                phone_number="+79000000001" if self.alias == "work" else "+79000000002",
                user_id=102,
                username="bob",
            )

    async def _stub_get(alias: str):
        return _StubSession(alias)

    monkeypatch.setattr(pool_module.pool, "get", _stub_get)

    mw._alias_cache.clear()
    mw._mode_cache.clear()

    app = main_module.app
    app.dependency_overrides[get_db] = _golden_db_dep
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)
        mw._alias_cache.clear()
        mw._mode_cache.clear()
        await engine.dispose()


def _snapshot(resp) -> dict:
    return {
        "status": resp.status_code,
        "content_type": resp.headers.get("content-type"),
        "body": resp.content.decode("utf-8"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("name,method,path,alias,body", CASES, ids=[c[0] for c in CASES])
async def test_tg_response_matches_golden(golden_client, name, method, path, alias, body):
    headers = dict(API_KEY)
    if alias:
        headers["x-session-alias"] = alias
    resp = await golden_client.request(method, path, headers=headers, json=body)
    actual = _snapshot(resp)

    golden_file = GOLDEN_DIR / f"{name}.json"
    if UPDATE:
        GOLDEN_DIR.mkdir(exist_ok=True)
        golden_file.write_text(json.dumps(actual, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return

    assert golden_file.exists(), f"missing golden snapshot {golden_file.name}; record with GOLDEN_UPDATE=1"
    expected = json.loads(golden_file.read_text(encoding="utf-8"))
    assert actual == expected, f"TG response for {name} changed"
