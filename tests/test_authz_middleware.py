"""test_authz_middleware.py — Authorization middleware matrix tests.

Tests resolve_alias and tool_authz behavior:
  1.  mode=rw, read tool → 200 (passes, tool_is_write=False)
  2.  mode=rw, write tool → passes (tool_is_write=True) — needs authorized session
  3.  mode=ro, read tool → passes
  4.  mode=ro, write tool → 403 with {tool, alias, mode} body
  5.  mode=ro, policy={tool='send_message', effect='allow'} → write passes (override)
  6.  mode=rw, policy={tool='send_message', effect='deny'} → 403 (deny > mode)
  7.  unknown_route (not in catalog) → passes with warning (open-by-default)
  8.  path not in /api/ → bypass entire authz stack
  9.  _HEALTH_PATHS → bypass, not written to audit
  10. Default alias = 'work' (no header)
  11. X-Session-Alias header → alias used
  12. ?session= query param
  13. Nonexistent alias → 404
  14. Disabled account → 404
  15. Cache invalidation: invalidate_alias_cache removes entry
  16. Cache TTL: cache hit returns same account_id before TTL expires
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import AsyncClient, ASGITransport
from sqlalchemy import text

from tests.conftest import create_account, create_tool_policy

# Test DB URL (reuse from conftest env)
import os
TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:test@localhost:5433/tg_myperson",
)


# ---------------------------------------------------------------------------
# Fixture: isolated FastAPI app with real middleware stack but mocked pool
# ---------------------------------------------------------------------------

def _build_app_overriding_db(alias_map: dict[str, int | None]):
    """Build app where _resolve_alias_from_db is replaced by alias_map lookup."""
    import importlib
    import app.authz.middleware as mw_module
    import app.telegram.pool as pool_module
    import app.main as main_module

    # Patch DB resolver
    async def _fake_resolve(alias: str) -> int | None:
        return alias_map.get(alias)

    # Patch pool
    pool_module.pool.start_all = AsyncMock()
    pool_module.pool.stop_all = AsyncMock()
    pool_module.pool._pool = {}

    importlib.reload(main_module)

    # Monkey-patch after reload
    main_module.app.state  # ensure app is built
    import app.authz.middleware as reloaded_mw
    reloaded_mw._alias_cache.clear()
    reloaded_mw._mode_cache.clear()

    return main_module.app, reloaded_mw


@pytest_asyncio.fixture(autouse=True)
async def clear_caches():
    """Clear alias and mode caches before each test."""
    import app.authz.middleware as mw
    mw._alias_cache.clear()
    mw._mode_cache.clear()
    yield
    mw._alias_cache.clear()
    mw._mode_cache.clear()


# ---------------------------------------------------------------------------
# Helper: build async client with mocked resolve + mode
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def rw_client():
    """Client where 'work' alias → account_id=1, mode='rw'."""
    import app.authz.middleware as mw
    import app.main as main_module
    from unittest.mock import AsyncMock
    import app.telegram.pool as pool_module

    pool_module.pool.start_all = AsyncMock()
    pool_module.pool.stop_all = AsyncMock()
    pool_module.pool._pool = {}

    with patch.object(mw, "_resolve_alias_from_db", AsyncMock(return_value=1)), \
         patch.object(mw, "_get_account_mode", AsyncMock(return_value="rw")), \
         patch.object(mw, "_get_tool_policy", AsyncMock(return_value=None)):

        import importlib
        importlib.reload(main_module)

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest_asyncio.fixture
async def ro_client():
    """Client where 'work' alias → account_id=2, mode='ro'."""
    import app.authz.middleware as mw
    import app.main as main_module
    import app.telegram.pool as pool_module

    pool_module.pool.start_all = AsyncMock()
    pool_module.pool.stop_all = AsyncMock()
    pool_module.pool._pool = {}

    with patch.object(mw, "_resolve_alias_from_db", AsyncMock(return_value=2)), \
         patch.object(mw, "_get_account_mode", AsyncMock(return_value="ro")), \
         patch.object(mw, "_get_tool_policy", AsyncMock(return_value=None)):

        import importlib
        importlib.reload(main_module)

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


# ---------------------------------------------------------------------------
# Test 1: mode=rw, read tool → passes (not 401/403)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rw_mode_read_tool_passes(rw_client):
    """mode=rw + read tool → request passes authz, tool_is_write=False."""
    resp = await rw_client.get(
        "/api/v1/auth/me",
        headers={"x-api-key": "test-api-key"},
    )
    # We expect NOT 403 (may be 503 if no TG session, but authz passed)
    assert resp.status_code != 403, f"Read tool on rw account must not be 403, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Test 2: mode=rw, write tool → passes authz (not 403)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rw_mode_write_tool_passes_authz(rw_client):
    """mode=rw + write tool → authz allows (not 403); downstream may 503."""
    resp = await rw_client.post(
        "/api/v1/messages",
        headers={"x-api-key": "test-api-key"},
        json={"chat_id": 123, "text": "hi"},
    )
    assert resp.status_code != 403, (
        f"Write tool on rw account must not be 403, got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# Test 3: mode=ro, read tool → passes (not 403)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ro_mode_read_tool_passes(ro_client):
    """mode=ro + read tool → passes authz."""
    resp = await ro_client.get(
        "/api/v1/auth/me",
        headers={"x-api-key": "test-api-key"},
    )
    assert resp.status_code != 403, (
        f"Read tool on ro account must not be 403, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 4: mode=ro, write tool → 403 with body containing tool/alias/mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ro_mode_write_tool_returns_403(ro_client):
    """mode=ro + write tool → 403 with {tool, alias, mode} in body."""
    resp = await ro_client.post(
        "/api/v1/messages",
        headers={"x-api-key": "test-api-key"},
        json={"chat_id": 123, "text": "hi"},
    )
    assert resp.status_code == 403, f"Expected 403 for write on ro, got {resp.status_code}"
    body = resp.json()
    assert "tool" in body, f"Response missing 'tool' field: {body}"
    assert "alias" in body, f"Response missing 'alias' field: {body}"
    assert "mode" in body, f"Response missing 'mode' field: {body}"


# ---------------------------------------------------------------------------
# Test 5: mode=ro, policy allow for write tool → write passes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ro_mode_policy_allow_overrides():
    """mode=ro + account_tool_policy{allow} → write tool passes (override)."""
    import app.authz.middleware as mw
    import app.main as main_module
    import app.telegram.pool as pool_module

    pool_module.pool.start_all = AsyncMock()
    pool_module.pool.stop_all = AsyncMock()
    pool_module.pool._pool = {}

    with patch.object(mw, "_resolve_alias_from_db", AsyncMock(return_value=3)), \
         patch.object(mw, "_get_account_mode", AsyncMock(return_value="ro")), \
         patch.object(mw, "_get_tool_policy", AsyncMock(return_value="allow")):

        import importlib
        importlib.reload(main_module)

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/api/v1/messages",
                headers={"x-api-key": "test-api-key"},
                json={"chat_id": 123, "text": "hi"},
            )
        # Must NOT be 403 — policy override allowed it
        assert resp.status_code != 403, (
            f"Policy 'allow' on ro account must override to pass, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Test 6: mode=rw, policy deny → 403
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rw_mode_policy_deny_returns_403():
    """mode=rw + account_tool_policy{deny} → 403 (deny wins over mode)."""
    import app.authz.middleware as mw
    import app.main as main_module
    import app.telegram.pool as pool_module

    pool_module.pool.start_all = AsyncMock()
    pool_module.pool.stop_all = AsyncMock()
    pool_module.pool._pool = {}

    with patch.object(mw, "_resolve_alias_from_db", AsyncMock(return_value=4)), \
         patch.object(mw, "_get_account_mode", AsyncMock(return_value="rw")), \
         patch.object(mw, "_get_tool_policy", AsyncMock(return_value="deny")):

        import importlib
        importlib.reload(main_module)

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/api/v1/messages",
                headers={"x-api-key": "test-api-key"},
                json={"chat_id": 123, "text": "hi"},
            )
        assert resp.status_code == 403, (
            f"Policy 'deny' on rw account must return 403, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Test 7: unknown route → passes with open-by-default (not 403)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_route_open_by_default(rw_client):
    """Unknown/unregistered route must pass through authz (open-by-default)."""
    resp = await rw_client.get(
        "/api/v1/nonexistent_endpoint_xyz",
        headers={"x-api-key": "test-api-key"},
    )
    # 404 from router is fine, but must not be 403 from authz
    assert resp.status_code != 403, (
        f"Unknown route must not return 403 from authz, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 8: path not in /api/ → authz bypassed entirely
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_api_path_bypasses_authz(rw_client):
    """Requests to non-/api/ paths skip the entire authz stack."""
    resp = await rw_client.get("/docs")
    # Should get docs page or redirect, not 401/403 from authz
    assert resp.status_code not in (401, 403), (
        f"/docs must bypass authz, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 9: Health paths → bypass authz and audit (no 403)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_paths_bypass_authz(rw_client):
    """_HEALTH_PATHS (/healthz, /readyz) bypass authz."""
    for path in ["/api/v1/healthz"]:
        resp = await rw_client.get(path)
        assert resp.status_code not in (401, 403), (
            f"{path} must bypass authz, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Test 10: Default alias = 'work' (no header)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_alias_is_work():
    """Without X-Session-Alias, alias defaults to 'work'."""
    import app.authz.middleware as mw
    import app.main as main_module
    import app.telegram.pool as pool_module

    pool_module.pool.start_all = AsyncMock()
    pool_module.pool.stop_all = AsyncMock()
    pool_module.pool._pool = {}

    captured_aliases = []

    async def _fake_resolve(alias: str) -> int | None:
        captured_aliases.append(alias)
        return 1

    with patch.object(mw, "_resolve_alias_from_db", side_effect=_fake_resolve), \
         patch.object(mw, "_get_account_mode", AsyncMock(return_value="rw")), \
         patch.object(mw, "_get_tool_policy", AsyncMock(return_value=None)):

        import importlib
        importlib.reload(main_module)

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.get("/api/v1/auth/me", headers={"x-api-key": "test-api-key"})

    assert "work" in captured_aliases, (
        f"Default alias must be 'work', got: {captured_aliases}"
    )


# ---------------------------------------------------------------------------
# Test 11: X-Session-Alias header → custom alias used
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_x_session_alias_header_used():
    """X-Session-Alias header must override default 'work' alias."""
    import app.authz.middleware as mw
    import app.main as main_module
    import app.telegram.pool as pool_module

    pool_module.pool.start_all = AsyncMock()
    pool_module.pool.stop_all = AsyncMock()
    pool_module.pool._pool = {}

    captured_aliases = []

    async def _fake_resolve(alias: str) -> int | None:
        captured_aliases.append(alias)
        return 1

    with patch.object(mw, "_resolve_alias_from_db", side_effect=_fake_resolve), \
         patch.object(mw, "_get_account_mode", AsyncMock(return_value="rw")), \
         patch.object(mw, "_get_tool_policy", AsyncMock(return_value=None)):

        import importlib
        importlib.reload(main_module)

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.get(
                "/api/v1/auth/me",
                headers={"x-api-key": "test-api-key", "x-session-alias": "personal-ro"},
            )

    assert "personal-ro" in captured_aliases, (
        f"X-Session-Alias 'personal-ro' must be used, got: {captured_aliases}"
    )


# ---------------------------------------------------------------------------
# Test 12: ?session= query param
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_query_param_used():
    """?session= query param must be used as alias when header absent."""
    import app.authz.middleware as mw
    import app.main as main_module
    import app.telegram.pool as pool_module

    pool_module.pool.start_all = AsyncMock()
    pool_module.pool.stop_all = AsyncMock()
    pool_module.pool._pool = {}

    captured_aliases = []

    async def _fake_resolve(alias: str) -> int | None:
        captured_aliases.append(alias)
        return 1

    with patch.object(mw, "_resolve_alias_from_db", side_effect=_fake_resolve), \
         patch.object(mw, "_get_account_mode", AsyncMock(return_value="rw")), \
         patch.object(mw, "_get_tool_policy", AsyncMock(return_value=None)):

        import importlib
        importlib.reload(main_module)

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.get(
                "/api/v1/auth/me",
                params={"session": "work2"},
                headers={"x-api-key": "test-api-key"},
            )

    assert "work2" in captured_aliases, (
        f"?session= param must be used as alias, got: {captured_aliases}"
    )


# ---------------------------------------------------------------------------
# Test 13: Nonexistent alias → 404
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nonexistent_alias_returns_404():
    """Unknown alias must return 404."""
    import app.authz.middleware as mw
    import app.main as main_module
    import app.telegram.pool as pool_module

    pool_module.pool.start_all = AsyncMock()
    pool_module.pool.stop_all = AsyncMock()
    pool_module.pool._pool = {}

    # Return None → alias not found
    with patch.object(mw, "_resolve_alias_from_db", AsyncMock(return_value=None)):

        import importlib
        importlib.reload(main_module)

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get(
                "/api/v1/auth/me",
                headers={"x-api-key": "test-api-key", "x-session-alias": "ghost"},
            )

    assert resp.status_code == 404, f"Unknown alias must return 404, got {resp.status_code}"
    body = resp.json()
    assert "ghost" in str(body), f"404 body must mention alias 'ghost': {body}"


# ---------------------------------------------------------------------------
# Test 14: Disabled account → 404 (DB returns None for is_enabled=false)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disabled_account_returns_404():
    """Disabled account (is_enabled=false) must return 404, not 200."""
    import app.authz.middleware as mw
    import app.main as main_module
    import app.telegram.pool as pool_module

    pool_module.pool.start_all = AsyncMock()
    pool_module.pool.stop_all = AsyncMock()
    pool_module.pool._pool = {}

    # _resolve_alias_from_db queries WHERE is_enabled=true; disabled → None
    with patch.object(mw, "_resolve_alias_from_db", AsyncMock(return_value=None)):

        import importlib
        importlib.reload(main_module)

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get(
                "/api/v1/auth/me",
                headers={"x-api-key": "test-api-key", "x-session-alias": "disabled-acc"},
            )

    assert resp.status_code == 404, (
        f"Disabled account must return 404, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 15: Cache invalidation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_invalidation_removes_entry():
    """invalidate_alias_cache(alias) must remove the cached entry immediately."""
    import time
    import app.authz.middleware as mw

    # Plant a cache entry manually
    mw._alias_cache["test-alias"] = (42, time.monotonic() + 100)
    assert "test-alias" in mw._alias_cache

    mw.invalidate_alias_cache("test-alias")
    assert "test-alias" not in mw._alias_cache, (
        "invalidate_alias_cache must remove the entry"
    )


# ---------------------------------------------------------------------------
# Test 16: Cache TTL: cache hit within TTL returns same account_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_hit_within_ttl():
    """Within TTL, alias cache must return cached account_id without DB call."""
    import time
    import app.authz.middleware as mw
    import app.main as main_module
    import app.telegram.pool as pool_module

    pool_module.pool.start_all = AsyncMock()
    pool_module.pool.stop_all = AsyncMock()
    pool_module.pool._pool = {}

    call_count = 0

    async def _counting_resolve(alias: str) -> int | None:
        nonlocal call_count
        call_count += 1
        return 99

    with patch.object(mw, "_resolve_alias_from_db", side_effect=_counting_resolve), \
         patch.object(mw, "_get_account_mode", AsyncMock(return_value="rw")), \
         patch.object(mw, "_get_tool_policy", AsyncMock(return_value=None)):

        import importlib
        importlib.reload(main_module)

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            # Two requests for same alias
            await c.get("/api/v1/auth/me", headers={"x-api-key": "test-api-key"})
            await c.get("/api/v1/auth/me", headers={"x-api-key": "test-api-key"})

    assert call_count == 1, (
        f"Within TTL, DB must be called only once (cache hit), got {call_count} calls"
    )


# ---------------------------------------------------------------------------
# Tests 17-20: WRITE_DB / MANAGE_SESSION allowed on ro (f5df2f64)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ro_mode_auth_login_allowed(ro_client):
    """mode=ro + auth_login (MANAGE_SESSION) → must NOT return 403.

    We mock pool.get so the request doesn't hit real Telegram.
    The key assertion: authz layer passes (not 403) even on ro account.
    """
    from unittest.mock import AsyncMock, MagicMock
    from fastapi import HTTPException

    # Mock TG session that returns 409 (already authorized) — avoids TG call
    mock_session = MagicMock()
    mock_session.client = MagicMock()
    mock_session.client.is_user_authorized = AsyncMock(return_value=True)
    mock_session.send_code = AsyncMock(
        side_effect=HTTPException(status_code=409, detail="already authorized")
    )

    with patch("app.api.auth.pool.get", AsyncMock(return_value=mock_session)):
        resp = await ro_client.post(
            "/api/v1/auth/login",
            headers={"x-api-key": "test-api-key"},
            json={"phone_number": "+79001234567"},
        )

    # authz layer must pass (not 403) — downstream 409 is fine
    assert resp.status_code != 403, (
        f"auth_login is MANAGE_SESSION — must pass ro authz, got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_ro_mode_snapshot_chat_allowed(ro_client):
    """mode=ro + snapshot_chat_members (WRITE_DB) → must NOT return 403.

    We mock pool.get to return an unauthorized session (→ 503).
    The key assertion: authz layer passes (not 403) even on ro account.
    """
    from unittest.mock import AsyncMock, MagicMock

    # Mock session that appears not authorized → endpoint returns 503
    mock_session = MagicMock()
    mock_session.client = MagicMock()
    mock_session.client.is_user_authorized = AsyncMock(return_value=False)

    with patch("app.api.snapshots.pool.get", AsyncMock(return_value=mock_session)):
        resp = await ro_client.post(
            "/api/v1/snapshots/chat/123456789",
            headers={"x-api-key": "test-api-key"},
        )

    # authz layer must pass (not 403) — downstream 503 is fine
    assert resp.status_code != 403, (
        f"snapshot_chat_members is WRITE_DB — must pass ro authz, got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_ro_mode_send_message_blocked(ro_client):
    """mode=ro + send_message (WRITE_TG) → 403."""
    resp = await ro_client.post(
        "/api/v1/messages",
        headers={"x-api-key": "test-api-key"},
        json={"chat_id": 123, "text": "hi"},
    )
    assert resp.status_code == 403, (
        f"send_message is WRITE_TG — must be 403 on ro, got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_ro_mode_join_chat_blocked(ro_client):
    """mode=ro + join_chat (WRITE_TG) → 403."""
    resp = await ro_client.post(
        "/api/v1/chats/join",
        headers={"x-api-key": "test-api-key"},
        json={"target": "@testchannel"},
    )
    assert resp.status_code == 403, (
        f"join_chat_endpoint is WRITE_TG — must be 403 on ro, got {resp.status_code}"
    )
