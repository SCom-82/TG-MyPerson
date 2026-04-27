"""test_handlers_register.py — M5: register_handlers called on restart for 'work'.

Tests:
  1. _maybe_register_handlers('work', authorized_session) → register_handlers called
  2. _maybe_register_handlers('personal-ro', session) → register_handlers NOT called
  3. _maybe_register_handlers('work', no_client_session) → register_handlers NOT called
  4. _maybe_register_handlers('work', unauthorized_session) → register_handlers NOT called
  5. pool.restart('work') → _maybe_register_handlers invoked (integration)
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _authorized_session(alias: str) -> MagicMock:
    """Session with is_running=True, authorized client."""
    session = MagicMock()
    session.alias = alias
    session.is_running = True
    session.client = MagicMock()
    session.client.is_user_authorized = AsyncMock(return_value=True)
    session.stop = AsyncMock()
    return session


def _unauthorized_session(alias: str) -> MagicMock:
    """Session with is_running=True but not authorized."""
    session = MagicMock()
    session.alias = alias
    session.is_running = True
    session.client = MagicMock()
    session.client.is_user_authorized = AsyncMock(return_value=False)
    session.stop = AsyncMock()
    return session


def _no_client_session(alias: str) -> MagicMock:
    """Session without a Telethon client (never connected)."""
    session = MagicMock()
    session.alias = alias
    session.is_running = False
    session.client = None
    session.stop = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# Test 1: 'work' + authorized → register_handlers called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_maybe_register_handlers_work_authorized():
    """_maybe_register_handlers('work', auth_session) must call register_handlers.

    register_handlers is imported lazily inside _maybe_register_handlers, so we
    patch it at the source module (app.telegram.handlers) level.
    """
    from app.telegram.pool import _maybe_register_handlers

    session = _authorized_session("work")

    with patch("app.telegram.handlers.register_handlers") as mock_register:
        await _maybe_register_handlers("work", session)

    mock_register.assert_called_once_with(session.client)


# ---------------------------------------------------------------------------
# Test 2: non-work alias → register_handlers NOT called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_maybe_register_handlers_non_work_alias_skips():
    """_maybe_register_handlers for non-'work' alias must NOT call register_handlers."""
    from app.telegram.pool import _maybe_register_handlers

    session = _authorized_session("personal-ro")

    with patch("app.telegram.handlers.register_handlers") as mock_register:
        await _maybe_register_handlers("personal-ro", session)

    mock_register.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: 'work' + no client → register_handlers NOT called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_maybe_register_handlers_no_client_skips():
    """_maybe_register_handlers('work') with no client must skip handler registration."""
    from app.telegram.pool import _maybe_register_handlers

    session = _no_client_session("work")

    with patch("app.telegram.handlers.register_handlers") as mock_register:
        await _maybe_register_handlers("work", session)

    mock_register.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: 'work' + unauthorized → register_handlers NOT called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_maybe_register_handlers_unauthorized_skips():
    """_maybe_register_handlers('work') when not authorized must skip."""
    from app.telegram.pool import _maybe_register_handlers

    session = _unauthorized_session("work")

    with patch("app.telegram.handlers.register_handlers") as mock_register:
        await _maybe_register_handlers("work", session)

    mock_register.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: pool.restart('work') invokes _maybe_register_handlers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pool_restart_work_calls_maybe_register():
    """pool.restart('work') must call _maybe_register_handlers after re-start."""
    from app.telegram.pool import TelegramClientPool

    pool = TelegramClientPool()
    old_session = _authorized_session("work")
    new_session = _authorized_session("work")
    pool._pool["work"] = old_session

    register_calls = []

    async def _fake_maybe_register(alias, session):
        register_calls.append((alias, session))

    with patch("app.telegram.pool._maybe_register_handlers", side_effect=_fake_maybe_register), \
         patch.object(pool, "get", AsyncMock(return_value=new_session)):

        await pool.restart("work")

    assert len(register_calls) == 1, (
        f"_maybe_register_handlers must be called once after restart, got {len(register_calls)}"
    )
    assert register_calls[0][0] == "work", "Must be called with alias='work'"
    assert register_calls[0][1] == new_session, "Must be called with the new session"
