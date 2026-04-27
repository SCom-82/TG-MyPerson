"""test_users_resolve.py — Tests for POST /users/resolve_by_id (f3b7f645).

Tests:
  1. Successful resolve: mock get_entity returns User → appears in resolved.
  2. ValueError from get_entity → appears in unresolved with error text.
  3. FloodWaitError(60) for all attempts → 429 response.
  4. Non-User entity (Channel) → unresolved with NotAUser error.
  5. More than 500 ids → 400.
  6. persist=true → registry_service.upsert_user_from_member called.
  7. persist=false → registry_service.upsert_user_from_member NOT called.
  8. Unauth session → 503.
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport


# ---------------------------------------------------------------------------
# Fixture: ro client that mocks the pool so get_entity can be patched
# ---------------------------------------------------------------------------

def _make_user(user_id: int, username: str = None, first_name: str = "Test", last_name: str = None) -> MagicMock:
    """Return a mock Telethon User object."""
    from telethon.tl.types import User
    user = MagicMock(spec=User)
    user.id = user_id
    user.username = username
    user.first_name = first_name
    user.last_name = last_name
    user.phone = None
    user.premium = False
    return user


def _make_channel(channel_id: int) -> MagicMock:
    """Return a mock Telethon Channel object (not a User)."""
    from telethon.tl.types import Channel
    ch = MagicMock(spec=Channel)
    ch.id = channel_id
    ch.title = "Test Channel"
    return ch


@pytest_asyncio.fixture
async def resolve_client():
    """Client with a mocked rw account and a controllable TG client."""
    import app.authz.middleware as mw
    import app.main as main_module
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


# ---------------------------------------------------------------------------
# Helper: patch pool.get to return a mock session with controllable client
# ---------------------------------------------------------------------------

def _mock_pool_session(get_entity_side_effect):
    """Return a patcher for pool.get that yields a mock session."""
    mock_client = MagicMock()
    mock_client.is_user_authorized = AsyncMock(return_value=True)
    mock_client.get_entity = AsyncMock(side_effect=get_entity_side_effect)

    mock_session = MagicMock()
    mock_session.client = mock_client

    return patch("app.api.users.pool.get", AsyncMock(return_value=mock_session))


# ---------------------------------------------------------------------------
# Test 1: successful resolve — User in resolved
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_by_id_success(resolve_client):
    """get_entity returns User → user_id in resolved, stats.resolved=1."""
    user = _make_user(219309009, username="scomit", first_name="Sergey")

    with _mock_pool_session([user]):
        resp = await resolve_client.post(
            "/api/v1/users/resolve_by_id",
            headers={"x-api-key": "test-api-key"},
            json={"user_ids": [219309009], "persist": False},
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["stats"]["resolved"] == 1
    assert body["stats"]["unresolved"] == 0
    assert len(body["resolved"]) == 1
    assert body["resolved"][0]["user_id"] == 219309009
    assert body["resolved"][0]["username"] == "scomit"


# ---------------------------------------------------------------------------
# Test 2: ValueError from get_entity → unresolved
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_by_id_value_error(resolve_client):
    """get_entity raises ValueError → user_id in unresolved with error."""
    with _mock_pool_session([ValueError("Cannot find any entity corresponding to 99999999")]):
        resp = await resolve_client.post(
            "/api/v1/users/resolve_by_id",
            headers={"x-api-key": "test-api-key"},
            json={"user_ids": [99999999], "persist": False},
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["stats"]["unresolved"] == 1
    assert body["stats"]["resolved"] == 0
    assert len(body["unresolved"]) == 1
    assert body["unresolved"][0]["user_id"] == 99999999
    assert "ValueError" in body["unresolved"][0]["error"]


# ---------------------------------------------------------------------------
# Test 3: FloodWaitError(60) → 429 with Retry-After header
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_by_id_flood_wait_large(resolve_client):
    """FloodWaitError with seconds>30 → 429 with Retry-After header."""
    from telethon.errors import FloodWaitError

    flood_error = FloodWaitError(request=None)
    flood_error.seconds = 60

    with _mock_pool_session([flood_error, flood_error]):
        resp = await resolve_client.post(
            "/api/v1/users/resolve_by_id",
            headers={"x-api-key": "test-api-key"},
            json={"user_ids": [123456], "persist": False},
        )

    assert resp.status_code == 429, f"Expected 429, got {resp.status_code}: {resp.text}"
    assert "Retry-After" in resp.headers


# ---------------------------------------------------------------------------
# Test 4: Channel entity → unresolved with NotAUser error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_by_id_channel_entity(resolve_client):
    """get_entity returns Channel (not User) → unresolved with NotAUser."""
    channel = _make_channel(-1001234567890)

    with _mock_pool_session([channel]):
        resp = await resolve_client.post(
            "/api/v1/users/resolve_by_id",
            headers={"x-api-key": "test-api-key"},
            json={"user_ids": [-1001234567890], "persist": False},
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["stats"]["unresolved"] == 1
    assert "NotAUser" in body["unresolved"][0]["error"]


# ---------------------------------------------------------------------------
# Test 5: more than 500 ids → 400
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_by_id_too_many_ids(resolve_client):
    """More than 500 user_ids → 400 Bad Request."""
    ids = list(range(501))

    mock_client = MagicMock()
    mock_client.is_user_authorized = AsyncMock(return_value=True)
    mock_session = MagicMock()
    mock_session.client = mock_client

    with patch("app.api.users.pool.get", AsyncMock(return_value=mock_session)):
        resp = await resolve_client.post(
            "/api/v1/users/resolve_by_id",
            headers={"x-api-key": "test-api-key"},
            json={"user_ids": ids, "persist": False},
        )

    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# Test 6: persist=True → upsert_user_from_member called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_by_id_persist_true_calls_upsert(resolve_client):
    """persist=True → registry_service.upsert_user_from_member is called."""
    user = _make_user(111222333, username="testuser")

    with _mock_pool_session([user]), \
         patch("app.api.users.registry_service.upsert_user_from_member", new_callable=AsyncMock) as mock_upsert:
        resp = await resolve_client.post(
            "/api/v1/users/resolve_by_id",
            headers={"x-api-key": "test-api-key"},
            json={"user_ids": [111222333], "persist": True},
        )

    assert resp.status_code == 200
    mock_upsert.assert_called_once()
    call_kwargs = mock_upsert.call_args.kwargs
    assert call_kwargs["member_data"]["tg_user_id"] == 111222333


# ---------------------------------------------------------------------------
# Test 7: persist=False → upsert_user_from_member NOT called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_by_id_persist_false_skips_upsert(resolve_client):
    """persist=False → registry_service.upsert_user_from_member is NOT called."""
    user = _make_user(444555666, username="nostore")

    with _mock_pool_session([user]), \
         patch("app.api.users.registry_service.upsert_user_from_member", new_callable=AsyncMock) as mock_upsert:
        resp = await resolve_client.post(
            "/api/v1/users/resolve_by_id",
            headers={"x-api-key": "test-api-key"},
            json={"user_ids": [444555666], "persist": False},
        )

    assert resp.status_code == 200
    mock_upsert.assert_not_called()


# ---------------------------------------------------------------------------
# Test 8: unauth session → 503
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_by_id_unauth_session(resolve_client):
    """Unauthorized session → 503."""
    mock_client = MagicMock()
    mock_client.is_user_authorized = AsyncMock(return_value=False)
    mock_session = MagicMock()
    mock_session.client = mock_client

    with patch("app.api.users.pool.get", AsyncMock(return_value=mock_session)):
        resp = await resolve_client.post(
            "/api/v1/users/resolve_by_id",
            headers={"x-api-key": "test-api-key"},
            json={"user_ids": [123], "persist": False},
        )

    assert resp.status_code == 503, f"Expected 503, got {resp.status_code}: {resp.text}"
