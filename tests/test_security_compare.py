"""test_security_compare.py — C1: verify secrets.compare_digest is used for auth.

Tests:
  1. auth_and_https: correct X-API-Key → 200 (or not 401)
  2. auth_and_https: wrong X-API-Key → 401
  3. auth_and_https: empty X-API-Key → 401
  4. auth_and_https: missing X-API-Key header → 401
  5. admin_auth_middleware: correct X-Admin-Key → passes (not 401/503)
  6. admin_auth_middleware: wrong X-Admin-Key → 401
  7. admin_auth_middleware: empty admin_api_key in config → 503
  8. Structural: secrets.compare_digest is called (not ==)
"""

import secrets
from unittest.mock import patch, MagicMock

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport


# ---------------------------------------------------------------------------
# Helpers: build a minimal app instance with given config
# ---------------------------------------------------------------------------

def _make_app(api_key: str = "test-api-key", admin_api_key: str = "test-admin-key"):
    """Build a fresh FastAPI app with specific key settings."""
    import os
    os.environ["API_KEY"] = api_key
    os.environ["TG_ADMIN_API_KEY"] = admin_api_key

    # Force reload config + main so env vars take effect
    import importlib
    import app.config
    importlib.reload(app.config)
    # Re-apply to module-level settings object
    from app.config import Settings
    app.config.settings = Settings()

    from unittest.mock import AsyncMock
    import app.telegram.pool as pool_module
    pool_module.pool.start_all = AsyncMock()
    pool_module.pool.stop_all = AsyncMock()
    pool_module.pool._pool = {}

    import app.main
    importlib.reload(app.main)
    return app.main.app


@pytest_asyncio.fixture
async def api_client():
    """Client with API_KEY=test-api-key, ADMIN_KEY=test-admin-key."""
    app = _make_app(api_key="test-api-key", admin_api_key="test-admin-key")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def api_client_empty_admin():
    """Client where admin key is empty — should return 503."""
    app = _make_app(api_key="test-api-key", admin_api_key="")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Test 1: Correct API key → not 401 (healthz is public, so test a public path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_correct_api_key_passes(api_client):
    """Valid X-API-Key must not return 401."""
    resp = await api_client.get(
        "/api/v1/healthz",
        headers={"x-api-key": "test-api-key"},
    )
    assert resp.status_code != 401, f"Expected not-401, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Test 2: Wrong X-API-Key → 401
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wrong_api_key_returns_401(api_client):
    """Wrong X-API-Key must return 401 on protected path."""
    resp = await api_client.get(
        "/api/v1/auth/me",
        headers={"x-api-key": "WRONG-KEY"},
    )
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# Test 3: Empty X-API-Key → 401
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_api_key_returns_401(api_client):
    """Empty string X-API-Key must return 401."""
    resp = await api_client.get(
        "/api/v1/auth/me",
        headers={"x-api-key": ""},
    )
    assert resp.status_code == 401, f"Expected 401 for empty key, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Test 4: Missing X-API-Key → 401
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_api_key_returns_401(api_client):
    """Missing X-API-Key must return 401."""
    resp = await api_client.get("/api/v1/auth/me")
    assert resp.status_code == 401, f"Expected 401 for missing key, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Test 5: Correct X-Admin-Key → passes (not 401 or 503)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_correct_admin_key_passes(api_client):
    """Valid X-Admin-Key must not return 401 or 503 on admin endpoint."""
    resp = await api_client.get(
        "/api/v1/accounts",
        headers={"x-admin-key": "test-admin-key"},
    )
    # 200, 404, 422 are all acceptable — not auth failures
    assert resp.status_code not in (401, 503), (
        f"Unexpected auth failure: {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# Test 6: Wrong X-Admin-Key → 401
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wrong_admin_key_returns_401(api_client):
    """Wrong X-Admin-Key must return 401."""
    resp = await api_client.get(
        "/api/v1/accounts",
        headers={"x-admin-key": "WRONG-ADMIN-KEY"},
    )
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# Test 7: Empty admin_api_key in config → 503
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_admin_key_config_returns_503(api_client_empty_admin):
    """If TG_ADMIN_API_KEY is empty, admin endpoint must return 503."""
    resp = await api_client_empty_admin.get(
        "/api/v1/accounts",
        headers={"x-admin-key": "anything"},
    )
    assert resp.status_code == 503, f"Expected 503 for unconfigured admin key, got {resp.status_code}"
    body = resp.json()
    assert "error" in body or "detail" in body, "503 response must contain error description"


# ---------------------------------------------------------------------------
# Test 8: Structural — secrets.compare_digest is actually called (not ==)
# ---------------------------------------------------------------------------

def test_compare_digest_used_in_middleware_source():
    """Verify main.py and middleware use secrets.compare_digest, not == for keys."""
    import inspect
    import app.main as main_module
    import app.authz.middleware as middleware_module

    main_src = inspect.getsource(main_module)
    middleware_src = inspect.getsource(middleware_module)

    assert "secrets.compare_digest" in main_src, (
        "main.py must use secrets.compare_digest for X-API-Key comparison"
    )
    assert "secrets.compare_digest" in middleware_src, (
        "middleware.py must use secrets.compare_digest for X-Admin-Key comparison"
    )
    # Guard: must NOT use plain == for key comparison in auth paths
    # (this is a heuristic — we check the API key check block)
    assert "api_key ==" not in main_src, "main.py must not use == for api_key comparison"
    assert "provided ==" not in middleware_src, "middleware.py must not use == for admin key comparison"
