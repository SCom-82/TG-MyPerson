"""test_max_session.py — MAX adapter: session, auth, supervisor, store (PR-3, block S + F).

FakePyMaxClient (tests/max_fakes.py) replaces the client factory; no network.
REST goes through the real app + middleware (platform_dispatch → /_max router).

  S-1  QR login → authorized, JSON v1 in account_sessions, platform_user_id
  S-2  /auth/qr.png: PNG of the same link, Cache-Control: no-store
  S-3  QR expiry → expired; a new POST issues a new QR
  S-4  QR + 2FA password (max-work has 2FA): wrong → 400 attempts_left, right →
       authorized; 3 wrong → error + connection closed, no 4th check; timeout
  S-5  SMS login (tcp), no token in the response; SMS + 2FA
  S-6  wrong SMS code → 400 invalid code (login ends, see report)
  S-7  pool restart → token login without auth; rotated token persisted
  S-8  revoked token → unauthorized, no re-auth, no more connects, row deactivated
  S-9  ban → banned, no retries
  S-10 backoff 5 s → 10 min, reset after stable uptime
  S-11 /auth/session import → authorized, persisted
  S-12 bad import (JSON / transport / revoked) → error, nothing written
  S-13 config guards on the real PyMax client config
  S-14 MAX_REQUIRE_PROXY without URL → error, factory never called
  S-15 pymax logger ≥ INFO, no token in logs
  S-16 pymax import boundary
  F    MAX disabled → no pool; MAX start failure does not affect TG start / readyz
  +    status/me/logout/409s, admin runtime, "reading never marks read" sentinel
"""

import asyncio
import io
import json
import logging
import re
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import psycopg2
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import app.max.auth as auth_module
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
        conn.cursor().execute("DELETE FROM accounts WHERE alias LIKE %s", (f"mx-%-{suffix}",))
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


# ---------------------------------------------------------------------------
# S-1 / S-2 / S-3 — QR
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_s1_qr_login_authorized_and_session_persisted(env):
    resp = await env.client.post("/api/v1/auth/qr", headers=env.h())
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "awaiting_qr"
    assert body["alias"] == env.alias
    assert body["qr_link"] == "https://max.ru/:auth/qr-1"
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$", body["expires_at"])
    assert body["qr_png_url"] == f"/api/v1/auth/qr.png?session={env.alias}"

    status = await env.client.get("/api/v1/auth/qr", headers=env.h())
    assert status.json()["status"] == "awaiting_qr"

    env.server.qr_confirmed = True
    await env.wait_state("authorized")
    assert (await env.client.get("/api/v1/auth/qr", headers=env.h())).json()["status"] == "authorized"

    stored = _active_json(env.account_id)
    assert stored["v"] == 1
    assert stored["transport"] == "web"
    assert stored["token"] == "tok-1"
    assert stored["device_id"] == "dev-1"
    assert stored["mt_instance_id"] == "mt-1"
    assert stored["sync"]["chats_sync"] == 777  # sync markers saved by the login
    assert stored["pymax"] == "2.4.1"

    conn = _pg()
    try:
        cur = conn.cursor()
        cur.execute("SELECT platform_user_id FROM accounts WHERE id = %s", (env.account_id,))
        assert cur.fetchone()[0] == 4242
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_s2_qr_png(env):
    import qrcode
    import qrcode.image.pure

    await env.client.post("/api/v1/auth/qr", headers=env.h())
    resp = await env.client.get("/api/v1/auth/qr.png", headers=env.h())
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["cache-control"] == "no-store"
    expected = io.BytesIO()
    qrcode.make("https://max.ru/:auth/qr-1", image_factory=qrcode.image.pure.PyPNGImage).save(expected)
    assert resp.content == expected.getvalue()

    # ?api_key= works too (open in a browser on the Mac)
    resp = await env.client.get(f"/api/v1/auth/qr.png?session={env.alias}&api_key=test-api-key")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_s3_qr_expired_then_new_qr(env):
    env.server.qr_ttl_ms = 150
    first = await env.client.post("/api/v1/auth/qr", headers=env.h())
    assert first.json()["qr_link"] == "https://max.ru/:auth/qr-1"
    await env.wait_state("error")
    status = (await env.client.get("/api/v1/auth/qr", headers=env.h())).json()
    assert status["status"] == "expired"
    assert env.server.clients[0].closed

    env.server.qr_ttl_ms = 60_000
    second = await env.client.post("/api/v1/auth/qr", headers=env.h())
    assert second.status_code == 202
    assert second.json()["qr_link"] == "https://max.ru/:auth/qr-2"


@pytest.mark.asyncio
async def test_repeated_post_returns_current_qr(env):
    first = (await env.client.post("/api/v1/auth/qr", headers=env.h())).json()
    second = (await env.client.post("/api/v1/auth/qr", headers=env.h())).json()
    assert second["qr_link"] == first["qr_link"]
    assert env.server.qr_requests == 1


# ---------------------------------------------------------------------------
# S-4 — QR + 2FA password (the main pilot scenario: max-work has a password)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_s4_qr_with_password(env):
    env.server.password = "secret"
    await _qr_login(env)
    assert await env.wait_state("awaiting_password") == "awaiting_password"

    status = (await env.client.get("/api/v1/auth/qr", headers=env.h())).json()
    assert status["status"] == "awaiting_password"
    assert status["password_hint"] == "pet name"
    assert status["qr_link"] is None

    wrong = await env.client.post("/api/v1/auth/code", headers=env.h(), json={"code": "", "password": "nope"})
    assert wrong.status_code == 400
    assert wrong.json() == {"error": "invalid password", "attempts_left": 2}

    ok = await env.client.post("/api/v1/auth/code", headers=env.h(), json={"code": "", "password": "secret"})
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"status": "authorized", "alias": env.alias, "user_id": 4242, "username": "sergey"}
    assert env.server.password_checks == ["nope", "secret"]
    assert _active_json(env.account_id)["transport"] == "web"


@pytest.mark.asyncio
async def test_s4_password_max_three_attempts_then_error_and_closed(env):
    env.server.password = "secret"
    await _qr_login(env)
    await env.wait_state("awaiting_password")

    for left in (2, 1):
        resp = await env.client.post("/api/v1/auth/code", headers=env.h(), json={"code": "", "password": f"bad{left}"})
        assert resp.json() == {"error": "invalid password", "attempts_left": left}

    last = await env.client.post("/api/v1/auth/code", headers=env.h(), json={"code": "", "password": "bad0"})
    assert last.status_code == 400
    assert last.json()["error"] == "password attempts exceeded"
    assert await env.wait_state("error") == "error"

    assert env.server.password_checks == ["bad2", "bad1", "bad0"]  # no 4th check
    assert env.server.clients[-1].closed
    assert _active_json(env.account_id) is None
    # nothing is retried by itself
    await asyncio.sleep(0.1)
    assert len(env.server.factory_calls) == 1

    # A further code post is a conflict, not a hang
    again = await env.client.post("/api/v1/auth/code", headers=env.h(), json={"code": "", "password": "secret"})
    assert again.status_code == 409


@pytest.mark.asyncio
async def test_s4_password_timeout_closes_connection(env, monkeypatch):
    monkeypatch.setattr(auth_module, "PASSWORD_TIMEOUT_S", 0.2)
    env.server.password = "secret"
    await _qr_login(env)
    await env.wait_state("awaiting_password")
    assert await env.wait_state("error") == "error"
    session = await env.session()
    assert "in time" in session.last_error
    assert env.server.clients[-1].closed
    assert env.server.password_checks == []


@pytest.mark.asyncio
async def test_s4_empty_password_is_rejected_without_using_an_attempt(env):
    env.server.password = "secret"
    await _qr_login(env)
    await env.wait_state("awaiting_password")
    resp = await env.client.post("/api/v1/auth/code", headers=env.h(), json={"code": ""})
    assert resp.status_code == 400
    assert resp.json() == {"error": "password required", "attempts_left": 3}


# ---------------------------------------------------------------------------
# S-5 / S-6 — SMS (fallback transport)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_s5_sms_login(env):
    resp = await env.client.post("/api/v1/auth/login", headers=env.h(), json={"phone_number": "+70000000000"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "code_sent", "phone": PHONE, "alias": env.alias}

    resp = await env.client.post("/api/v1/auth/code", headers=env.h(), json={"code": "12345"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "authorized", "alias": env.alias, "user_id": 4242, "username": "sergey"}
    assert "session_string" not in resp.text and "tok-" not in resp.text

    assert _active_json(env.account_id)["transport"] == "tcp"
    assert env.server.factory_calls[0]["transport"] == "tcp"
    assert env.server.factory_calls[0]["phone"] == PHONE


@pytest.mark.asyncio
async def test_s5_sms_with_password(env):
    env.server.password = "secret"
    await env.client.post("/api/v1/auth/login", headers=env.h(), json={"phone_number": PHONE})
    resp = await env.client.post("/api/v1/auth/code", headers=env.h(), json={"code": "12345"})
    assert resp.json() == {"status": "2fa_required", "alias": env.alias, "hint": "pet name"}
    resp = await env.client.post("/api/v1/auth/code", headers=env.h(), json={"code": "", "password": "secret"})
    assert resp.json()["status"] == "authorized"


@pytest.mark.asyncio
async def test_s5_sms_code_and_password_in_one_request(env):
    env.server.password = "secret"
    await env.client.post("/api/v1/auth/login", headers=env.h(), json={"phone_number": PHONE})
    resp = await env.client.post("/api/v1/auth/code", headers=env.h(), json={"code": "12345", "password": "secret"})
    assert resp.json()["status"] == "authorized"


@pytest.mark.asyncio
async def test_s6_wrong_sms_code(env):
    await env.client.post("/api/v1/auth/login", headers=env.h(), json={"phone_number": PHONE})
    resp = await env.client.post("/api/v1/auth/code", headers=env.h(), json={"code": "00000"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid code"
    assert await env.wait_state("error") == "error"
    assert env.server.clients[-1].closed


# ---------------------------------------------------------------------------
# S-7 … S-10 — supervisor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_s7_restart_logs_in_by_token_and_persists_rotation(env):
    await _qr_login(env)
    assert env.server.authenticate_calls == 1

    env.server.rotate_token = True
    await env.pool.restart(env.alias)
    await env.wait_state("authorized")
    assert env.server.authenticate_calls == 1  # no QR, no SMS
    assert _active_json(env.account_id)["token"] == "tok-2"


@pytest.mark.asyncio
async def test_s7_start_all_resumes_stored_sessions_only(env, aliases):
    other_alias, _ = aliases("fresh")
    await _store_session(env.account_id)
    await env.pool.start_all()
    await env.wait_state("authorized")
    fresh = await env.pool.get(other_alias)
    assert fresh.state == "stopped"  # no stored session → waits for a login
    assert env.server.authenticate_calls == 0


@pytest.mark.asyncio
async def test_s8_revoked_token_unauthorized_without_relogin(env):
    await _qr_login(env)
    env.server.revoked.add("tok-1")
    env.server.clients[-1].drop()

    assert await env.wait_state("unauthorized") == "unauthorized"
    calls = len(env.server.factory_calls)
    await asyncio.sleep(0.2)
    assert len(env.server.factory_calls) == calls  # no further attempts
    assert env.server.authenticate_calls == 1  # never re-authenticated
    assert _active_json(env.account_id) is None  # row deactivated
    assert [r[0] for r in _sessions(env.account_id)] == [False]


@pytest.mark.asyncio
async def test_s8_revoked_at_startup(env):
    await _store_session(env.account_id, token="tok-dead")
    env.server.revoked.add("tok-dead")
    await env.pool.start_all()
    assert await env.wait_state("unauthorized") == "unauthorized"
    assert env.server.authenticate_calls == 0
    assert len(env.server.factory_calls) == 1


@pytest.mark.asyncio
async def test_s9_ban_stops_supervisor(env):
    await _store_session(env.account_id)
    env.server.banned = True
    await env.pool.start_all()
    assert await env.wait_state("banned") == "banned"
    await asyncio.sleep(0.1)
    assert len(env.server.factory_calls) == 1


@pytest.mark.asyncio
async def test_s10_backoff_grows_to_ten_minutes(env, monkeypatch):
    monkeypatch.setattr(session_module, "BACKOFF_START_S", 5.0)
    delays: list[float] = []

    async def record(seconds):
        delays.append(seconds)

    monkeypatch.setattr(session_module, "_sleep", record)
    await _store_session(env.account_id)
    env.server.connect_errors = [OSError("network down") for _ in range(9)]
    await env.pool.start_all()
    await env.wait_state("authorized")
    assert delays == [5, 10, 20, 40, 80, 160, 320, 600, 600]


@pytest.mark.asyncio
async def test_s10_backoff_resets_after_stable_uptime(env, monkeypatch):
    monkeypatch.setattr(session_module, "BACKOFF_START_S", 5.0)
    monkeypatch.setattr(session_module, "STABLE_RESET_S", 0.0)
    delays: list[float] = []

    async def record(seconds):
        delays.append(seconds)

    monkeypatch.setattr(session_module, "_sleep", record)
    await _store_session(env.account_id)
    env.server.connect_errors = [OSError("x") for _ in range(3)]
    await env.pool.start_all()
    await env.wait_state("authorized")
    assert delays == [5, 5, 5]


@pytest.mark.asyncio
async def test_network_drop_reconnects_with_token(env):
    await _qr_login(env)
    env.server.clients[-1].drop()
    session = await env.session()
    assert await session.wait_until(
        lambda: len(env.server.factory_calls) == 2 and session.state == "authorized", 5
    ), (session.state, session.last_error)
    assert env.server.authenticate_calls == 1


# ---------------------------------------------------------------------------
# S-11 / S-12 — session import
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_s11_import_session(env, aliases):
    await _qr_login(env)
    exported = _sessions(env.account_id)[0][1]
    other_alias, other_id = aliases("restore")

    resp = await env.client.post(
        "/api/v1/auth/session", headers=env.h(other_alias), json={"session_string": exported}
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "authorized", "alias": other_alias, "user_id": 4242, "username": "sergey"}
    stored = _active_json(other_id)
    assert stored["token"] == "tok-1"
    assert stored["transport"] == "web"
    assert env.server.authenticate_calls == 1  # import never runs an interactive login


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        json.dumps({"v": 2, "transport": "web", "token": "t", "device_id": "d"}),
        json.dumps({"v": 1, "transport": "vk", "token": "t", "device_id": "d"}),
        json.dumps({"v": 1, "transport": "web", "device_id": "d"}),
    ],
)
@pytest.mark.asyncio
async def test_s12_bad_import_writes_nothing(env, payload):
    resp = await env.client.post("/api/v1/auth/session", headers=env.h(), json={"session_string": payload})
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"
    assert _sessions(env.account_id) == []
    assert env.server.factory_calls == []


@pytest.mark.asyncio
async def test_s12_import_of_revoked_session_keeps_previous(env):
    await _store_session(env.account_id, token="tok-good")
    await env.pool.start_all()
    await env.wait_state("authorized")

    env.server.revoked.add("tok-dead")
    bad = json.dumps({"v": 1, "transport": "web", "token": "tok-dead", "device_id": "d", "phone": ""})
    resp = await env.client.post("/api/v1/auth/session", headers=env.h(), json={"session_string": bad})
    assert resp.json() == {"status": "error", "detail": "Session is invalid or expired"}
    assert _active_json(env.account_id)["token"] == "tok-good"  # nothing overwritten
    await env.wait_state("authorized")  # previous session resumed


# ---------------------------------------------------------------------------
# S-13 … S-16 — guards
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("transport", ["web", "tcp"])
@pytest.mark.asyncio
async def test_s13_pymax_config_guards(transport):
    from pymax import QrAuthFlow

    store = PgSessionStore(1, transport)
    client = session_module.build_client(
        transport=transport,
        phone=PHONE,
        extra_config=session_module.build_extra_config(proxy=PROXY, store=store),
        auth_flow=QrAuthFlow(qr_provider=None),
    )
    config = await client._prepare_config()  # offline: no connection is opened
    assert config.telemetry is False
    assert config.relogin is False
    assert config.interactive is False
    assert config.registration_config is None
    assert config.proxy == PROXY
    assert config.store is store
    assert config.persist_session is True
    assert config.password_max_attempts == 3
    assert client.extra_config.reconnect is False
    if transport == "tcp":
        assert client.catalog.remote is False  # no fetch from hashes.pymax.org
        assert config.phone == PHONE
    else:
        assert type(client).__name__ == "AdapterWebClient"


@pytest.mark.asyncio
async def test_s14_proxy_required(env, monkeypatch):
    env.settings.proxy_url = ""
    resp = await env.client.post("/api/v1/auth/qr", headers=env.h())
    assert resp.status_code == 502
    assert "proxy required" in resp.json()["error"]
    assert env.server.factory_calls == []

    await _store_session(env.account_id)
    await env.pool.restart(env.alias)
    session = await env.session()
    await session.wait_until(lambda: not session.running, 2)
    assert session.state == "error"
    assert "proxy required" in session.last_error
    assert env.server.factory_calls == []


@pytest.mark.asyncio
async def test_s15_pymax_logger_never_debug_and_no_token_in_logs(env, caplog):
    root = logging.getLogger()
    old = root.level
    root.setLevel(logging.DEBUG)
    logging.getLogger("pymax").setLevel(logging.NOTSET)
    try:
        session_module.guard_pymax_logger()
        assert logging.getLogger("pymax").getEffectiveLevel() >= logging.INFO
        with caplog.at_level(logging.DEBUG):
            await _qr_login(env)
            env.server.rotate_token = True
            await env.pool.restart(env.alias)
            await env.wait_state("authorized")
        assert "tok-1" not in caplog.text and "tok-2" not in caplog.text
    finally:
        root.setLevel(old)


def test_s16_pymax_import_boundary():
    allowed = {"session.py", "store.py", "auth.py", "normalize.py", "media.py"}
    app_dir = Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*(import pymax|from pymax)", text, re.MULTILINE):
            rel = path.relative_to(app_dir)
            if rel.parts[0] != "max" or len(rel.parts) != 2 or rel.name not in allowed:
                offenders.append(str(rel))
    assert offenders == []


# ---------------------------------------------------------------------------
# status / me / logout / conflicts / admin runtime
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_and_me(env):
    status = await env.client.get("/api/v1/auth/status", headers=env.h())
    assert status.json() == {"connected": False, "phone_number": PHONE, "user_id": None, "username": None}
    me = await env.client.get("/api/v1/auth/me", headers=env.h())
    assert me.status_code == 503
    assert me.json() == {"detail": f"Session '{env.alias}' not available", "state": "stopped"}

    await _qr_login(env)
    status = await env.client.get("/api/v1/auth/status", headers=env.h())
    assert status.json() == {"connected": True, "phone_number": PHONE, "user_id": 4242, "username": "sergey"}
    me = (await env.client.get("/api/v1/auth/me", headers=env.h())).json()
    assert me == {
        "user_id": 4242, "username": "sergey", "first_name": "Сергей", "last_name": "С",
        "phone": "+79001112233", "is_premium": False, "is_verified": False, "is_bot": False,
        "dc_id": None, "lang_code": None,
    }


@pytest.mark.asyncio
async def test_logout(env):
    await _qr_login(env)
    resp = await env.client.post("/api/v1/auth/logout", headers=env.h())
    assert resp.json() == {"status": "logged_out", "alias": env.alias}
    assert "logout" in env.server.clients[-1].calls
    assert env.server.logged_out
    assert _active_json(env.account_id) is None
    assert (await env.session()).state == "stopped"


@pytest.mark.asyncio
async def test_conflicts_409(env):
    await env.client.post("/api/v1/auth/qr", headers=env.h())
    sms = await env.client.post("/api/v1/auth/login", headers=env.h(), json={"phone_number": PHONE})
    assert sms.status_code == 409
    assert sms.json() == {"error": "login already in progress", "state": "awaiting_qr"}

    env.server.qr_confirmed = True
    await env.wait_state("authorized")
    again = await env.client.post("/api/v1/auth/qr", headers=env.h())
    assert again.status_code == 409
    assert again.json()["error"] == "already authorized"

    no_login = await env.client.post("/api/v1/auth/code", headers=env.h(), json={"code": "1"})
    assert no_login.status_code == 409


@pytest.mark.asyncio
async def test_qr_refused_while_stored_session_exists(env):
    env.settings.proxy_url = ""  # stored session cannot start → state error
    await _store_session(env.account_id)
    await env.pool.restart(env.alias)
    resp = await env.client.post("/api/v1/auth/qr", headers=env.h())
    assert resp.status_code == 409
    assert "stored session exists" in resp.json()["error"]


@pytest.mark.asyncio
async def test_admin_accounts_runtime(env):
    await _qr_login(env)
    accounts = (await env.client.get("/api/v1/accounts", headers=ADMIN_KEY)).json()
    acc = next(a for a in accounts if a["alias"] == env.alias)
    assert acc["platform_user_id"] == 4242
    assert acc["is_running"] is True
    assert acc["last_started_at_pool"] is not None
    runtime = acc["runtime"]
    assert runtime["state"] == "authorized"
    assert runtime["connected"] is True
    assert runtime["authorized"] is True
    assert runtime["transport"] == "web"
    assert runtime["proxy"] is True
    assert runtime["pymax"] == "2.4.1"
    assert set(runtime) == {
        "state", "connected", "authorized", "transport", "proxy", "last_event_at",
        "last_catchup_at", "catchup_backlog_chats", "last_error", "pymax",
    }


@pytest.mark.asyncio
async def test_max_disabled_answers_503(env, monkeypatch):
    monkeypatch.setattr(max_pool_module, "max_pool", None)
    resp = await env.client.get("/api/v1/auth/status", headers=env.h())
    assert resp.status_code == 503
    assert resp.json()["state"] == "stopped"


@pytest.mark.asyncio
async def test_login_never_marks_anything_read(env):
    """Watchdog (ADR §1.2): the adapter never calls read/presence on its own."""
    env.server.password = "secret"
    await _qr_login(env)
    await env.client.post("/api/v1/auth/code", headers=env.h(), json={"code": "", "password": "secret"})
    await env.wait_state("authorized")
    await env.pool.restart(env.alias)
    await env.wait_state("authorized")
    for client in env.server.clients:
        assert not {"read_message", "set_presence"} & set(client.calls)


# ---------------------------------------------------------------------------
# F — isolation in lifespan
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def lifespan_app(monkeypatch):
    import app.main as main_module
    import app.telegram.pool as tg_pool_module

    tg_start = AsyncMock()
    monkeypatch.setattr(tg_pool_module.pool, "start_all", tg_start)
    monkeypatch.setattr(tg_pool_module.pool, "stop_all", AsyncMock())
    monkeypatch.setattr(tg_pool_module.pool, "_pool", {})
    monkeypatch.setattr(max_pool_module, "max_pool", None)
    return main_module, tg_start


@pytest.mark.asyncio
async def test_f_max_disabled_creates_no_pool(lifespan_app, monkeypatch):
    main_module, tg_start = lifespan_app
    monkeypatch.setattr(main_module.max_settings, "enabled", False)
    created = []
    monkeypatch.setattr(max_pool_module, "MaxPool", lambda *a, **k: created.append(1))
    async with main_module.app.router.lifespan_context(main_module.app):
        assert max_pool_module.max_pool is None
    assert created == []
    tg_start.assert_awaited_once()


@pytest.mark.asyncio
async def test_f_max_start_failure_does_not_affect_tg_or_readyz(lifespan_app, monkeypatch):
    main_module, tg_start = lifespan_app
    monkeypatch.setattr(main_module.max_settings, "enabled", True)
    monkeypatch.setattr(MaxPool, "start_all", AsyncMock(side_effect=RuntimeError("MAX down")))
    async with main_module.app.router.lifespan_context(main_module.app):
        assert isinstance(max_pool_module.max_pool, MaxPool)
        tg_start.assert_awaited_once()
        async with AsyncClient(transport=ASGITransport(app=main_module.app), base_url="http://test") as c:
            ready = await c.get("/api/v1/readyz")
        assert ready.status_code == 200
        assert set(ready.json()) == {"status", "database", "telegram_connected", "telegram_authorized"}
    assert max_pool_module.max_pool is None  # stopped and cleared on shutdown
