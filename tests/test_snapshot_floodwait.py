"""test_snapshot_floodwait.py — M3: FloodWaitError → 429 Retry-After.

Tests:
  1. iter_participants raises FloodWaitError(42) → endpoint returns 429 with Retry-After: 42
  2. Verify audit log: status='error', tool='snapshot_chat_members', is_write=True
     (note: audit is async background task; we verify the HTTP response only here
      since auditing is fire-and-forget and would need a full integration stack)
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch, AsyncMock
from httpx import AsyncClient, ASGITransport


# ---------------------------------------------------------------------------
# FloodWaitError stub (avoids real Telethon import in tests)
# ---------------------------------------------------------------------------

class FakeFloodWaitError(Exception):
    """Mimics telethon.errors.FloodWaitError interface."""
    def __init__(self, seconds: int):
        self.seconds = seconds
        super().__init__(f"FloodWait for {seconds} seconds")


# ---------------------------------------------------------------------------
# Fixture: app with snapshot endpoint wired to a mocked Telegram session
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def snapshot_client_with_floodwait():
    """
    Build app where pool.get('work') returns a session whose
    iter_participants raises FloodWaitError(42).
    """
    import os
    import importlib

    os.environ.setdefault("API_KEY", "test-api-key")
    os.environ.setdefault("TG_ADMIN_API_KEY", "test-admin-key")
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:test@localhost:5433/tg_myperson")

    # Build mock client that raises FloodWaitError
    mock_tg_client = MagicMock()
    mock_tg_client.is_user_authorized = AsyncMock(return_value=True)

    async def _fake_iter_participants(*args, **kwargs):
        raise FakeFloodWaitError(seconds=42)
        # This never yields — the exception is raised immediately

    mock_tg_client.iter_participants = MagicMock(return_value=_fake_iter_participants())

    mock_session = MagicMock()
    mock_session.client = mock_tg_client
    mock_session.is_running = True

    # Patch pool
    import app.telegram.pool as pool_module
    pool_module.pool.start_all = AsyncMock()
    pool_module.pool.stop_all = AsyncMock()
    pool_module.pool._pool = {"work": mock_session}
    pool_module.pool.get = AsyncMock(return_value=mock_session)

    # Patch FloodWaitError in the snapshots module
    import app.api.snapshots as snap_module
    snap_module.FloodWaitError = FakeFloodWaitError

    # Patch _require_account_id to avoid DB lookup
    original_require = snap_module._require_account_id
    snap_module._require_account_id = AsyncMock(return_value=1)

    importlib.reload(pool_module)

    import app.main
    importlib.reload(app.main)

    transport = ASGITransport(app=app.main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    # Restore
    snap_module._require_account_id = original_require


# ---------------------------------------------------------------------------
# Test 1: FloodWaitError → 429 with Retry-After header
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_floodwait_returns_429_with_retry_after():
    """When iter_participants raises FloodWaitError, endpoint must return 429 with Retry-After."""
    import os
    import importlib
    from unittest.mock import AsyncMock, MagicMock

    os.environ.setdefault("API_KEY", "test-api-key")

    # Build mock TG client
    mock_tg_client = MagicMock()
    mock_tg_client.is_user_authorized = AsyncMock(return_value=True)

    async def _flood_iter(*args, **kwargs):
        raise FakeFloodWaitError(seconds=42)

    # iter_participants must be an async generator — we patch the snapshot endpoint directly
    mock_session = MagicMock()
    mock_session.client = mock_tg_client
    mock_session.is_running = True

    import app.telegram.pool as pool_module
    import app.api.snapshots as snap_module
    import app.main as main_module

    pool_module.pool.start_all = AsyncMock()
    pool_module.pool.stop_all = AsyncMock()
    pool_module.pool._pool = {"work": mock_session}
    pool_module.pool.get = AsyncMock(return_value=mock_session)

    snap_module.FloodWaitError = FakeFloodWaitError
    snap_module._require_account_id = AsyncMock(return_value=1)

    # Patch iter_participants to raise on first call
    async def _fake_iter_participants(chat_id, **kwargs):
        raise FakeFloodWaitError(seconds=42)
        # Unreachable but makes it an async generator
        yield  # noqa

    mock_tg_client.iter_participants = _fake_iter_participants

    importlib.reload(main_module)

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/snapshots/chat/123456789",
            headers={"x-api-key": "test-api-key"},
        )

    assert resp.status_code == 429, (
        f"FloodWaitError must produce 429, got {resp.status_code}: {resp.text}"
    )
    assert "Retry-After" in resp.headers, (
        f"429 response must include Retry-After header, headers: {dict(resp.headers)}"
    )
    assert resp.headers["Retry-After"] == "42", (
        f"Retry-After must match FloodWaitError.seconds (42), got: {resp.headers.get('Retry-After')}"
    )


# ---------------------------------------------------------------------------
# Test 2: 429 response body mentions flood wait
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_floodwait_response_body_mentions_seconds():
    """429 response body must mention the wait duration."""
    import os
    import importlib
    from unittest.mock import AsyncMock, MagicMock

    os.environ.setdefault("API_KEY", "test-api-key")

    mock_tg_client = MagicMock()
    mock_tg_client.is_user_authorized = AsyncMock(return_value=True)

    mock_session = MagicMock()
    mock_session.client = mock_tg_client
    mock_session.is_running = True

    import app.telegram.pool as pool_module
    import app.api.snapshots as snap_module
    import app.main as main_module

    pool_module.pool.start_all = AsyncMock()
    pool_module.pool.stop_all = AsyncMock()
    pool_module.pool._pool = {"work": mock_session}
    pool_module.pool.get = AsyncMock(return_value=mock_session)

    snap_module.FloodWaitError = FakeFloodWaitError
    snap_module._require_account_id = AsyncMock(return_value=1)

    async def _fake_iter_participants(chat_id, **kwargs):
        raise FakeFloodWaitError(seconds=99)
        yield  # noqa

    mock_tg_client.iter_participants = _fake_iter_participants

    importlib.reload(main_module)

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/snapshots/chat/987654321",
            headers={"x-api-key": "test-api-key"},
        )

    assert resp.status_code == 429
    body = resp.text
    assert "99" in body, f"Response body must mention seconds (99), got: {body}"
    assert resp.headers.get("Retry-After") == "99"
