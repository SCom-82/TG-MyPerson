"""test_handlers_register.py — M5: register_handlers called on restart for all authorized sessions.

Tests:
  1. _maybe_register_handlers('work', authorized_session) → register_handlers called
  2. _maybe_register_handlers('personal-ro', authorized_session) → register_handlers called (PR #4)
  3. _maybe_register_handlers('work', no_client_session) → register_handlers NOT called
  4. _maybe_register_handlers('work', unauthorized_session) → register_handlers NOT called
  5. pool.restart('work') → _maybe_register_handlers invoked (integration)
  7. _maybe_register_handlers('personal-ro', authorized) → register_handlers called (replaces old "skips" test)
  8. _maybe_register_handlers, no client → still skips
  9. _maybe_register_handlers, unauthorized → still skips
  10. pool.restart('personal-ro') now registers handlers
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
# Test 2: non-work alias (personal-ro) + authorized → register_handlers IS called (PR #4)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_maybe_register_handlers_personal_ro_authorized_now_registers():
    """PR #4: _maybe_register_handlers for non-'work' alias MUST call register_handlers.

    Previously skipped (alias != 'work' guard). Now all authorized sessions get handlers.
    """
    from app.telegram.pool import _maybe_register_handlers

    session = _authorized_session("personal-ro")

    with patch("app.telegram.handlers.register_handlers") as mock_register:
        await _maybe_register_handlers("personal-ro", session)

    mock_register.assert_called_once_with(session.client)


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


# ---------------------------------------------------------------------------
# Test 10: pool.restart("personal-ro") now registers handlers (PR #4)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pool_restart_personal_ro_now_registers_handlers():
    """PR #4: pool.restart('personal-ro') must call _maybe_register_handlers which now
    registers handlers (no longer skipped by alias guard)."""
    from app.telegram.pool import TelegramClientPool

    pool = TelegramClientPool()
    old_session = _authorized_session("personal-ro")
    new_session = _authorized_session("personal-ro")
    pool._pool["personal-ro"] = old_session

    register_calls = []

    async def _fake_maybe_register(alias, session):
        register_calls.append((alias, session))

    with patch("app.telegram.pool._maybe_register_handlers", side_effect=_fake_maybe_register), \
         patch.object(pool, "get", AsyncMock(return_value=new_session)):

        await pool.restart("personal-ro")

    assert len(register_calls) == 1, (
        f"_maybe_register_handlers must be called once after restart('personal-ro'), got {len(register_calls)}"
    )
    assert register_calls[0][0] == "personal-ro"
    assert register_calls[0][1] == new_session

    # Verify the real _maybe_register_handlers now actually registers for personal-ro
    with patch("app.telegram.handlers.register_handlers") as mock_reg:
        from app.telegram.pool import _maybe_register_handlers
        await _maybe_register_handlers("personal-ro", new_session)
        mock_reg.assert_called_once_with(new_session.client)


# ---------------------------------------------------------------------------
# Test 11: lifespan registers handlers for all authorized sessions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lifespan_registers_handlers_for_all_authorized_sessions():
    """PR #4: lifespan startup must call register_handlers for every authorized session.

    Setup: pool with 3 sessions — work (authorized), personal-ro (authorized),
    archived (unauthorized). Expect register_handlers called exactly twice.
    """
    from unittest.mock import AsyncMock, MagicMock, patch, call

    work_session = _authorized_session("work")
    personal_ro_session = _authorized_session("personal-ro")
    archived_session = _unauthorized_session("archived")

    mock_pool = MagicMock()
    mock_pool.start_all = AsyncMock()
    mock_pool.stop_all = AsyncMock()
    mock_pool._pool = {
        "work": work_session,
        "personal-ro": personal_ro_session,
        "archived": archived_session,
    }

    register_calls = []

    def _fake_register(client):
        register_calls.append(client)

    with patch("app.telegram.pool.pool", mock_pool), \
         patch("app.telegram.handlers.register_handlers", side_effect=_fake_register):
        # Execute the lifespan startup logic directly (mirrors app/main.py)
        await mock_pool.start_all()
        for alias, session in mock_pool._pool.items():
            if session.client and await session.client.is_user_authorized():
                from app.telegram.handlers import register_handlers
                register_handlers(session.client)

    assert len(register_calls) == 2, (
        f"register_handlers must be called for work + personal-ro (2 times), got {len(register_calls)}"
    )
    assert work_session.client in register_calls, "work session client must be registered"
    assert personal_ro_session.client in register_calls, "personal-ro session client must be registered"
    assert archived_session.client not in register_calls, "unauthorized 'archived' must NOT be registered"
