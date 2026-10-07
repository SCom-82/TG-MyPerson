"""Shared MAX test environment: accounts, fake pool, HTTP client (PR-3+).

Import the fixtures into a test module to use them:
    from tests.max_env import aliases, env  # noqa: F401  (pytest fixtures)
"""

import json
import uuid

import psycopg2
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import app.max.pool as max_pool_module
import app.max.session as session_module
from app.max.config import MaxSettings
from app.max.pool import MaxPool
from app.max.store import PgSessionStore
from tests.conftest import TEST_DB_URL
from tests.max_fakes import FakeMaxServer

API_KEY = {"x-api-key": "test-api-key"}
ADMIN_KEY = {"x-admin-key": "test-admin-key"}
PROXY = "socks5://svc:pw@127.0.0.1:1080"
PHONE = "+79001112233"


def _pg():
    conn = psycopg2.connect(TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://"))
    conn.autocommit = True
    return conn


def _create_account(alias: str, platform: str = "max", mode: str = "ro") -> int:
    conn = _pg()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO accounts (alias, phone, mode, platform) VALUES (%s, %s, %s, %s) RETURNING id",
            (alias, PHONE, mode, platform),
        )
        return cur.fetchone()[0]
    finally:
        conn.close()


def _sessions(account_id: int) -> list[tuple]:
    conn = _pg()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT is_active, session_plaintext FROM account_sessions WHERE account_id = %s ORDER BY id",
            (account_id,),
        )
        return cur.fetchall()
    finally:
        conn.close()


def _active_json(account_id: int) -> dict | None:
    rows = [r for r in _sessions(account_id) if r[0]]
    return json.loads(rows[0][1]) if rows else None


class Env:
    def __init__(self, server, pool, client, alias, account_id, settings):
        self.server = server
        self.pool = pool
        self.client = client
        self.alias = alias
        self.account_id = account_id
        self.settings = settings

    def h(self, alias: str | None = None) -> dict:
        return {**API_KEY, "x-session-alias": alias or self.alias}

    async def session(self, alias: str | None = None):
        return await self.pool.get(alias or self.alias)

    async def wait_state(self, *states: str, alias: str | None = None, timeout: float = 5.0) -> str:
        session = await self.session(alias)
        ok = await session.wait_until(lambda: session.state in states, timeout)
        assert ok, f"state {session.state!r} (last_error={session.last_error!r}), expected {states}"
        return session.state


@pytest.fixture
def aliases():
    suffix = uuid.uuid4().hex[:8]
    made = []

    def make(name: str, **kw) -> tuple[str, int]:
        alias = f"mx-{name}-{suffix}"
        made.append(alias)
        return alias, _create_account(alias, **kw)

    yield make
    conn = _pg()
    try:
        cur = conn.cursor()
        # max_raw_events.account_id references accounts (no cascade): journal first.
        cur.execute(
            "DELETE FROM max_raw_events WHERE account_id IN (SELECT id FROM accounts WHERE alias LIKE %s)",
            (f"mx-%-{suffix}",),
        )
        cur.execute("DELETE FROM accounts WHERE alias LIKE %s", (f"mx-%-{suffix}",))
    finally:
        conn.close()


@pytest_asyncio.fixture
async def env(monkeypatch, aliases):
    import app.authz.middleware as mw
    import app.main as main_module

    monkeypatch.setattr(session_module, "BACKOFF_START_S", 0.01)
    alias, account_id = aliases("work")
    server = FakeMaxServer()
    settings = MaxSettings(proxy_url=PROXY, require_proxy=True)
    pool = MaxPool(settings, client_factory=server.factory)
    monkeypatch.setattr(max_pool_module, "max_pool", pool)
    mw._alias_cache.clear()
    mw._mode_cache.clear()
    async with AsyncClient(transport=ASGITransport(app=main_module.app), base_url="http://test") as c:
        yield Env(server, pool, c, alias, account_id, settings)
    await pool.stop_all()
    mw._alias_cache.clear()
    mw._mode_cache.clear()


async def _qr_login(env: Env) -> dict:
    resp = await env.client.post("/api/v1/auth/qr", headers=env.h())
    assert resp.status_code == 202, resp.text
    env.server.qr_confirmed = True
    await env.wait_state("authorized", "awaiting_password")
    return resp.json()


async def _store_session(account_id: int, transport: str = "web", token: str = "tok-stored") -> None:
    from pymax.session.models import SessionInfo

    await PgSessionStore(account_id, transport).save_session(
        SessionInfo(token=token, device_id="dev-s", phone="", mt_instance_id="mt-s")
    )


