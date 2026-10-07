"""test_max_routing.py — platform-aware routing and authz (MAX plan PR-2, block A).

Real middleware stack against the test DB (accounts are committed and removed):
  A-1  max ro  + send_message        → 403, audit tool=send_message status=denied
  A-2  max ro  + join_chat_endpoint  → 403, not 501
  A-3  max     + list_members (read) → 501 {tool, platform:max}, audit status=error
  A-4  TG      + /auth/qr*           → 501 (MAX-only tools)
  A-5  external /api/v1/_max/...     → 404 for any alias
  A-6  max     + list_chats (PR-2)   → 501
  A-8  disabled max alias            → 404
  A-9/A-10, §1.1–1.3                 → admin API: platform field, PATCH guards
  plus: alias cache keeps the platform; TG pool never loads MAX accounts;
        tool catalog (WRITE_MESSENGER_TOOLS, MAX_ONLY_TOOLS).
"""

import asyncio
import uuid

import psycopg2
import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from tests.conftest import TEST_DB_URL

API_KEY = {"x-api-key": "test-api-key"}
ADMIN_KEY = {"x-admin-key": "test-admin-key"}


def _pg():
    conn = psycopg2.connect(TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://"))
    conn.autocommit = True
    return conn


@pytest.fixture
def accounts():
    """Create committed accounts with unique aliases; remove them afterwards."""
    suffix = uuid.uuid4().hex[:8]
    spec = {
        "max_ro": ("max", "ro", True),
        "max_rw": ("max", "rw", True),
        "max_off": ("max", "ro", False),
        "tg_rw": ("telegram", "rw", True),
    }
    conn = _pg()
    cur = conn.cursor()
    out = {}
    for key, (platform, mode, enabled) in spec.items():
        alias = f"rt-{key.replace('_', '-')}-{suffix}"
        cur.execute(
            "INSERT INTO accounts (alias, phone, mode, is_enabled, platform) "
            "VALUES (%s, '+79000000099', %s, %s, %s) RETURNING id",
            (alias, mode, enabled, platform),
        )
        out[key] = {"alias": alias, "id": cur.fetchone()[0]}
    try:
        yield out
    finally:
        cur.execute("DELETE FROM accounts WHERE alias LIKE %s", (f"rt-%-{suffix}",))
        conn.close()


@pytest_asyncio.fixture
async def client(monkeypatch):
    import app.authz.middleware as mw
    import app.main as main_module
    import app.telegram.pool as pool_module

    async def _no_tg_session(alias: str):
        raise HTTPException(status_code=404, detail=f"Session alias '{alias}' not registered or disabled")

    monkeypatch.setattr(pool_module.pool, "get", _no_tg_session)
    mw._alias_cache.clear()
    mw._mode_cache.clear()
    async with AsyncClient(transport=ASGITransport(app=main_module.app), base_url="http://test") as c:
        yield c
    mw._alias_cache.clear()
    mw._mode_cache.clear()


async def _audit_rows(alias: str, expected: int = 1, timeout: float = 5.0) -> list[tuple]:
    """audit_log is written by a background task — poll until it lands."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        conn = _pg()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT tool, is_write, status, error FROM audit_logs WHERE alias = %s ORDER BY ts",
                (alias,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        if len(rows) >= expected or asyncio.get_running_loop().time() > deadline:
            return rows
        await asyncio.sleep(0.05)


def _h(alias: str) -> dict:
    return {**API_KEY, "x-session-alias": alias}


# ---------------------------------------------------------------------------
# A-1 / A-2: ro check happens before the platform check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_max_ro_send_message_403_and_audit_denied(client, accounts):
    alias = accounts["max_ro"]["alias"]
    resp = await client.post("/api/v1/messages", headers=_h(alias), json={"chat_id": 1, "text": "x"})

    assert resp.status_code == 403, resp.text
    assert resp.json() == {
        "error": f"tool 'send_message' not allowed on read-only account '{alias}'",
        "tool": "send_message",
        "alias": alias,
        "mode": "ro",
        "reason": "read-only account",
    }
    assert await _audit_rows(alias) == [("send_message", True, "denied", "HTTP 403")]


@pytest.mark.asyncio
async def test_max_ro_join_chat_403_not_501(client, accounts):
    alias = accounts["max_ro"]["alias"]
    resp = await client.post("/api/v1/chats/join", headers=_h(alias), json={"target": "@x"})
    assert resp.status_code == 403
    assert resp.json()["tool"] == "join_chat_endpoint"


# ---------------------------------------------------------------------------
# A-3 / A-6: tools not implemented for MAX → 501 with the real tool name
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_max_list_members_501_and_audit_error(client, accounts):
    alias = accounts["max_ro"]["alias"]
    resp = await client.get("/api/v1/chats/-100123/members", headers=_h(alias))

    assert resp.status_code == 501
    assert resp.json() == {
        "error": "tool 'list_members' is not supported for platform max",
        "tool": "list_members",
        "platform": "max",
        "alias": alias,
    }
    assert await _audit_rows(alias) == [("list_members", False, "error", "HTTP 501")]


@pytest.mark.parametrize(
    "method,path,tool",
    [
        ("GET", "/api/v1/messages/scheduled?chat_id=1", "list_scheduled"),
        ("GET", "/api/v1/snapshots/chat/-100123", "list_chat_snapshots"),
        ("GET", "/api/v1/chats/-100123/my_rights", "get_my_rights"),
    ],
)
@pytest.mark.asyncio
async def test_max_tools_not_yet_implemented_501(client, accounts, method, path, tool):
    alias = accounts["max_ro"]["alias"]
    resp = await client.request(method, path, headers=_h(alias), json={"chat_id": 1} if method == "POST" else None)
    assert resp.status_code == 501, resp.text
    assert resp.json()["tool"] == tool
    assert resp.json()["platform"] == "max"


@pytest.mark.asyncio
async def test_max_rw_write_passes_authz_then_501(client, accounts):
    alias = accounts["max_rw"]["alias"]
    resp = await client.post("/api/v1/messages", headers=_h(alias), json={"chat_id": 1, "text": "x"})
    assert resp.status_code == 501
    assert resp.json()["tool"] == "send_message"
    assert await _audit_rows(alias) == [("send_message", True, "error", "HTTP 501")]


@pytest.mark.asyncio
async def test_max_alias_via_query_param(client, accounts):
    alias = accounts["max_ro"]["alias"]
    resp = await client.get(f"/api/v1/chats/-100123/members?session={alias}", headers=API_KEY)
    assert resp.status_code == 501
    assert resp.json()["alias"] == alias


@pytest.mark.asyncio
async def test_max_unknown_path_404_like_telegram(client, accounts):
    resp = await client.get("/api/v1/no-such-endpoint", headers=_h(accounts["max_ro"]["alias"]))
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("path", ["/api/v1/messages", "/api/v1/chats", "/api/v1/sync/status"])
@pytest.mark.asyncio
async def test_max_unresolved_tool_404_never_reaches_tg_routes(client, accounts, path):
    """Review 07.10: if name resolution breaks, a MAX alias must get 404, not TG data.

    The TG read services are replaced by mocks that must never be awaited.
    """
    import app.authz.middleware as mw

    tg_services = {
        "app.api.messages.get_messages": AsyncMock(return_value=([], 0)),
        "app.api.chats.get_chats": AsyncMock(return_value=([], 0)),
        "app.api.sync.get_sync_states": AsyncMock(return_value=[]),
    }
    patches = [patch(target, mock) for target, mock in tg_services.items()]
    for p in patches:
        p.start()
    try:
        with patch.object(mw, "_resolve_route_name", return_value=None):
            resp = await client.get(path, headers=_h(accounts["max_ro"]["alias"]))
    finally:
        for p in patches:
            p.stop()

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}
    for target, mock in tg_services.items():
        mock.assert_not_awaited(), target


@pytest.mark.asyncio
async def test_max_route_table_contains_internal_router(accounts):
    """The resolver sees /_max routes, so PR-3+ MAX endpoints are found after the rewrite."""
    import app.main as main_module

    table = main_module.app.state.route_table
    assert table.resolve("GET", "/api/v1/_max/_unsupported") == "max_unsupported"
    assert table.resolve("GET", "/api/v1/chats") == "list_chats"


# ---------------------------------------------------------------------------
# A-4: MAX-only tools on a Telegram alias → 501
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "method,path,tool",
    [
        ("POST", "/api/v1/auth/qr", "auth_qr_start"),
        ("GET", "/api/v1/auth/qr", "auth_qr_status"),
        ("GET", "/api/v1/auth/qr.png", "auth_qr_status"),
    ],
)
@pytest.mark.asyncio
async def test_tg_alias_max_only_tool_501(client, accounts, method, path, tool):
    alias = accounts["tg_rw"]["alias"]
    resp = await client.request(method, path, headers=_h(alias))
    assert resp.status_code == 501
    assert resp.json() == {
        "error": f"tool '{tool}' is not supported for platform telegram",
        "tool": tool,
        "platform": "telegram",
        "alias": alias,
    }
    assert await _audit_rows(alias) == [(tool, False, "error", "HTTP 501")]


# ---------------------------------------------------------------------------
# A-5: the internal router is not reachable from outside
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["tg_rw", "max_ro"])
@pytest.mark.parametrize("path", ["/api/v1/_max/chats", "/api/v1/_max/_unsupported", "/api/v1/_max"])
@pytest.mark.asyncio
async def test_external_max_prefix_404(client, accounts, key, path):
    resp = await client.get(path, headers=_h(accounts[key]["alias"]))
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}


@pytest.mark.asyncio
async def test_max_router_rejects_requests_without_rewrite_flag():
    """Defense in depth: the router dependency 404s without the middleware flag."""
    from starlette.requests import Request

    from app.max.api.router import require_max_rewrite

    request = Request({"type": "http", "method": "GET", "path": "/api/v1/_max/_unsupported", "headers": []})
    with pytest.raises(HTTPException) as exc_info:
        await require_max_rewrite(request)
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# A-8 and Telegram regression on the same stack
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disabled_max_alias_404(client, accounts):
    alias = accounts["max_off"]["alias"]
    resp = await client.get("/api/v1/chats", headers=_h(alias))
    assert resp.status_code == 404
    assert resp.json() == {"error": f"Session alias '{alias}' not registered or disabled"}


@pytest.mark.asyncio
async def test_tg_alias_is_not_rewritten(client, accounts):
    """TG read tool keeps hitting the TG route (no 501, no MAX catch-all)."""
    resp = await client.get("/api/v1/chats?limit=1", headers=_h(accounts["tg_rw"]["alias"]))
    assert resp.status_code == 200
    assert set(resp.json()) == {"items", "total", "limit", "offset"}


# ---------------------------------------------------------------------------
# Alias cache carries the platform (ADR §2.B p.1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_alias_cache_keeps_platform(client, accounts):
    import app.authz.middleware as mw

    alias = accounts["max_ro"]["alias"]
    first = await client.get("/api/v1/chats/-100123/members", headers=_h(alias))
    assert mw._alias_cache[alias][0] == (accounts["max_ro"]["id"], "max")

    with patch.object(mw, "_resolve_alias_from_db", AsyncMock(side_effect=AssertionError("cache miss"))):
        second = await client.get("/api/v1/chats/-100123/members", headers=_h(alias))
    assert first.status_code == second.status_code == 501


@pytest.mark.asyncio
async def test_resolve_alias_from_db_returns_platform(accounts):
    import app.authz.middleware as mw

    assert await mw._resolve_alias_from_db(accounts["max_ro"]["alias"]) == (accounts["max_ro"]["id"], "max")
    assert await mw._resolve_alias_from_db(accounts["tg_rw"]["alias"]) == (accounts["tg_rw"]["id"], "telegram")
    assert await mw._resolve_alias_from_db(accounts["max_off"]["alias"]) is None


# ---------------------------------------------------------------------------
# Telegram pool never loads MAX accounts (ADR §2.C)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tg_pool_start_all_skips_max_accounts(accounts):
    from app.telegram.pool import TelegramClientPool

    pool = TelegramClientPool()
    with patch.object(pool, "_start_one", AsyncMock(return_value=None)) as start_one:
        await pool.start_all()

    started = {c.args[0] for c in start_one.call_args_list}
    assert accounts["tg_rw"]["alias"] in started
    assert accounts["max_ro"]["alias"] not in started
    assert accounts["max_rw"]["alias"] not in started


@pytest.mark.asyncio
async def test_tg_pool_get_max_alias_404(accounts):
    from app.telegram.pool import TelegramClientPool

    pool = TelegramClientPool()
    with pytest.raises(HTTPException) as exc_info:
        await pool.get(accounts["max_ro"]["alias"])
    assert exc_info.value.status_code == 404
    assert accounts["max_ro"]["alias"] not in pool._pool

    with pytest.raises(HTTPException) as exc_info:
        await pool._load_session(accounts["max_ro"]["alias"])
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Admin API: platform field (API spec §1, A-9, A-10)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def admin_aliases():
    suffix = uuid.uuid4().hex[:8]
    yield lambda name: f"adm-{name}-{suffix}"
    conn = _pg()
    try:
        conn.cursor().execute("DELETE FROM accounts WHERE alias LIKE %s", (f"adm-%-{suffix}",))
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_admin_create_max_account(client, admin_aliases):
    alias = admin_aliases("max")
    resp = await client.post(
        "/api/v1/accounts",
        headers=ADMIN_KEY,
        json={"alias": alias, "phone": "+79000000077", "mode": "ro", "platform": "max", "watch_chat_ids": [1]},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["platform"] == "max"
    assert body["platform_user_id"] is None
    assert body["write_chat_ids"] is None
    assert body["write_rate_per_hour"] is None
    assert body["runtime"]["state"] == "stopped"
    assert body["runtime"]["pymax"] == "2.4.1"
    assert body["watch_chat_ids"] == []  # ignored for MAX
    assert "warning" not in body


@pytest.mark.asyncio
async def test_admin_create_max_rw_warns(client, admin_aliases):
    resp = await client.post(
        "/api/v1/accounts",
        headers=ADMIN_KEY,
        json={"alias": admin_aliases("maxrw"), "phone": "+79000000078", "mode": "rw", "platform": "max"},
    )
    assert resp.status_code == 201
    assert resp.json()["warning"] == "max accounts should start in ro"


@pytest.mark.asyncio
async def test_admin_create_defaults_to_telegram_without_max_fields(client, admin_aliases):
    resp = await client.post(
        "/api/v1/accounts",
        headers=ADMIN_KEY,
        json={"alias": admin_aliases("tg"), "phone": "+79000000079", "mode": "rw"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["platform"] == "telegram"
    for field in ("platform_user_id", "write_chat_ids", "write_rate_per_hour", "runtime", "warning"):
        assert field not in body


@pytest.mark.asyncio
async def test_admin_create_unknown_platform_422(client, admin_aliases):
    resp = await client.post(
        "/api/v1/accounts",
        headers=ADMIN_KEY,
        json={"alias": admin_aliases("vk"), "phone": "+79000000080", "platform": "vk"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_admin_patch_platform_400(client, accounts):
    resp = await client.patch(
        f"/api/v1/accounts/{accounts['max_ro']['id']}", headers=ADMIN_KEY, json={"platform": "telegram"}
    )
    assert resp.status_code == 400
    assert resp.json() == {"detail": "platform cannot be changed via PATCH"}


@pytest.mark.asyncio
async def test_admin_patch_write_guards_set_and_clear(client, accounts):
    url = f"/api/v1/accounts/{accounts['max_rw']['id']}"

    resp = await client.patch(url, headers=ADMIN_KEY, json={"write_chat_ids": [-100, 5], "write_rate_per_hour": 20})
    assert resp.status_code == 200
    assert resp.json()["write_chat_ids"] == [-100, 5]
    assert resp.json()["write_rate_per_hour"] == 20

    # Unrelated PATCH keeps them
    resp = await client.patch(url, headers=ADMIN_KEY, json={"notes": "n"})
    assert resp.json()["write_chat_ids"] == [-100, 5]

    # Explicit null clears
    resp = await client.patch(url, headers=ADMIN_KEY, json={"write_chat_ids": None, "write_rate_per_hour": None})
    assert resp.json()["write_chat_ids"] is None
    assert resp.json()["write_rate_per_hour"] is None


@pytest.mark.parametrize("rate", [0, 201])
@pytest.mark.asyncio
async def test_admin_patch_write_rate_bounds_422(client, accounts, rate):
    resp = await client.patch(
        f"/api/v1/accounts/{accounts['max_rw']['id']}", headers=ADMIN_KEY, json={"write_rate_per_hour": rate}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_admin_list_shows_platform(client, accounts):
    resp = await client.get("/api/v1/accounts", headers=ADMIN_KEY)
    by_alias = {a["alias"]: a for a in resp.json()}
    assert by_alias[accounts["tg_rw"]["alias"]]["platform"] == "telegram"
    assert "runtime" not in by_alias[accounts["tg_rw"]["alias"]]
    assert by_alias[accounts["max_ro"]["alias"]]["runtime"]["state"] == "stopped"


# ---------------------------------------------------------------------------
# Tool catalog (ADR §2.D)
# ---------------------------------------------------------------------------

def test_catalog_messenger_write_category():
    from app.authz import tool_catalog as tc

    assert tc.WRITE_TG_TOOLS is tc.WRITE_MESSENGER_TOOLS
    assert "send_message" in tc.WRITE_MESSENGER_TOOLS
    assert {"auth_qr_start", "auth_qr_status"} <= tc.MANAGE_SESSION_TOOLS
    assert tc.MAX_ONLY_TOOLS == {"auth_qr_start", "auth_qr_status"}
    assert tc.tool_is_write("auth_qr_start") is False
    assert tc.tool_is_write("send_message") is True
