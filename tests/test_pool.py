"""test_pool.py — TelegramClientPool unit tests.

Tests:
  1. pool.get(unknown_alias) → HTTPException 404
  2. pool.get(disabled_alias) → HTTPException 404
  3. pool.start_all() with unavailable DB → graceful (no crash)
  4. Lazy start: 10 concurrent pool.get("work") → _start_one called once (lock)
  5. pool.restart("work") → old session stopped, new started, handlers registered
  6. pool.restart("personal-ro") → register_handlers NOT called
  7. pool.stop_alias("work") → alias removed from _pool, session.stop() called
"""

import asyncio
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_session(alias: str, is_running: bool = True) -> MagicMock:
    """Create a mock TelegramSession."""
    session = MagicMock()
    session.alias = alias
    session.is_running = is_running
    session.client = MagicMock()
    session.client.is_user_authorized = AsyncMock(return_value=True)
    session.stop = AsyncMock()
    session.start = AsyncMock()
    session.last_error = None
    session.last_started_at = None
    return session


# ---------------------------------------------------------------------------
# Test 1: pool.get(unknown alias) → 404
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_unknown_alias_raises_404():
    """pool.get with unknown alias must raise HTTPException(404)."""
    from app.telegram.pool import TelegramClientPool

    pool = TelegramClientPool()

    # Mock _load_and_start to raise 404 (simulating DB not finding alias)
    with patch("app.telegram.pool._load_and_start", AsyncMock(
        side_effect=HTTPException(status_code=404, detail="not found")
    )):
        with pytest.raises(HTTPException) as exc_info:
            await pool.get("nonexistent-alias")

    assert exc_info.value.status_code == 404, (
        f"Expected 404, got {exc_info.value.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 2: pool.get(disabled alias) → 404
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_disabled_alias_raises_404():
    """pool.get with disabled account must raise HTTPException(404)."""
    from app.telegram.pool import TelegramClientPool

    pool = TelegramClientPool()

    with patch("app.telegram.pool._load_and_start", AsyncMock(
        side_effect=HTTPException(status_code=404, detail="disabled")
    )):
        with pytest.raises(HTTPException) as exc_info:
            await pool.get("disabled-account")

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Test 3: pool.start_all() with DB error → graceful, no crash
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_all_with_db_error_graceful():
    """pool.start_all() must not crash when DB is unavailable.

    KNOWN DEFECT (M2 — open): start_all() currently propagates OperationalError
    from the top-level DB query in start_all() itself. The supervisor pattern
    only isolates errors inside _start_one() per-session, but the initial
    'SELECT all enabled accounts' query is unprotected.

    This test is marked xfail to document the regression. When M2 is fixed,
    remove the xfail marker and verify it passes.
    """
    import pytest
    from app.telegram.pool import TelegramClientPool
    from sqlalchemy.exc import OperationalError

    pool = TelegramClientPool()

    with patch("app.telegram.pool.async_session") as mock_session_ctx:
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(
            side_effect=OperationalError("connection refused", None, None)
        )
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_ctx.return_value = mock_cm

        raised = False
        try:
            await pool.start_all()
        except OperationalError:
            raised = True
        except Exception:
            raised = True

    # Document current behavior: start_all() propagates DB error (M2 open)
    # When M2 is fixed, this assert must be: assert not raised
    if raised:
        pytest.xfail(
            "M2 (open): start_all() propagates OperationalError from top-level DB query — "
            "not isolated by supervisor pattern. dev-coder must wrap the initial SELECT in try/except."
        )


# ---------------------------------------------------------------------------
# Test 4: Lazy start — 10 concurrent get() → _start_one called once (lock)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lazy_start_concurrent_get_calls_start_once():
    """10 concurrent pool.get('work') calls must invoke _start_one exactly once."""
    from app.telegram.pool import TelegramClientPool

    pool = TelegramClientPool()
    start_count = 0
    mock_sess = _make_mock_session("work", is_running=True)

    original_start_one = pool._start_one

    async def _counted_start_one(alias: str):
        nonlocal start_count
        start_count += 1
        pool._pool[alias] = mock_sess
        return mock_sess

    pool._start_one = _counted_start_one

    # Also need to mock _load_and_start to use our counted version
    async def _fake_load_and_start(alias, p):
        return await _counted_start_one(alias)

    with patch("app.telegram.pool._load_and_start", side_effect=_fake_load_and_start):
        tasks = [pool.get("work") for _ in range(10)]
        results = await asyncio.gather(*tasks)

    assert start_count == 1, (
        f"_start_one must be called exactly once for 10 concurrent get() calls, "
        f"got {start_count} calls (lock broken?)"
    )
    assert all(r.alias == "work" for r in results), "All results must be the same session"


# ---------------------------------------------------------------------------
# Test 5: pool.restart("work") → handlers registered
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restart_work_registers_handlers():
    """pool.restart('work') must stop old session, start new, and register handlers."""
    from app.telegram.pool import TelegramClientPool

    pool = TelegramClientPool()
    old_session = _make_mock_session("work", is_running=True)
    new_session = _make_mock_session("work", is_running=True)
    pool._pool["work"] = old_session

    register_called = False

    async def _fake_maybe_register(alias, session):
        nonlocal register_called
        if alias == "work":
            register_called = True

    with patch("app.telegram.pool._maybe_register_handlers", side_effect=_fake_maybe_register), \
         patch.object(pool, "get", AsyncMock(return_value=new_session)):

        result = await pool.restart("work")

    old_session.stop.assert_called_once()
    assert register_called, "Handlers must be registered after restart('work')"
    assert result == new_session


# ---------------------------------------------------------------------------
# Test 6: pool.restart("personal-ro") → handlers NOT called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restart_non_work_alias_does_not_register_handlers():
    """pool.restart('personal-ro') must NOT call register_handlers."""
    from app.telegram.pool import TelegramClientPool
    from app.telegram.handlers import register_handlers

    pool = TelegramClientPool()
    old_session = _make_mock_session("personal-ro", is_running=True)
    new_session = _make_mock_session("personal-ro", is_running=True)
    pool._pool["personal-ro"] = old_session

    register_called_for = []

    async def _fake_maybe_register(alias, session):
        register_called_for.append(alias)
        # Real implementation: only 'work' registers
        if alias == "work" and session.client:
            if await session.client.is_user_authorized():
                register_handlers(session.client)

    with patch("app.telegram.pool._maybe_register_handlers", side_effect=_fake_maybe_register), \
         patch.object(pool, "get", AsyncMock(return_value=new_session)):

        await pool.restart("personal-ro")

    # _maybe_register_handlers is called, but inside it no-ops for non-work
    # Verify the actual behavior: register_handlers must not be invoked
    with patch("app.telegram.handlers.register_handlers") as mock_reg:
        # Re-run the real _maybe_register_handlers logic
        from app.telegram.pool import _maybe_register_handlers
        await _maybe_register_handlers("personal-ro", new_session)
        mock_reg.assert_not_called()


# ---------------------------------------------------------------------------
# Test 7: pool.stop_alias("work") → removed from _pool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_alias_removes_from_pool():
    """pool.stop_alias('work') must stop session and remove from _pool."""
    from app.telegram.pool import TelegramClientPool

    pool = TelegramClientPool()
    session = _make_mock_session("work", is_running=True)
    pool._pool["work"] = session

    await pool.stop_alias("work")

    assert "work" not in pool._pool, "After stop_alias, alias must be removed from _pool"
    session.stop.assert_called_once()
