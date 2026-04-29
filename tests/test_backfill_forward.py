"""test_backfill_forward.py — PR #4: forward-fill backfill matrix.

Tests:
  1. forward fill uses min_id from sync_state.newest_message_id
  2. forward fill with NULL newest_message_id uses min_id=0
  3. forward fill does not touch oldest_message_id or is_fully_synced
  4. backward (default) behaviour unchanged — regression guard
  5. forward fill is idempotent (upsert, no duplicates)
  6. concurrent forward + backward for same chat → already_running
"""

import asyncio
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_msg(msg_id: int, text: str = "hello") -> MagicMock:
    """Minimal Telethon message mock."""
    msg = MagicMock()
    msg.id = msg_id
    msg.text = text
    msg.sender = None  # skip user upsert path
    return msg


def _make_sync_state(
    chat_id: int,
    newest_message_id: int | None = 100,
    oldest_message_id: int | None = 1,
    is_fully_synced: bool = True,
    total_messages_synced: int = 8720,
    last_backfill_at=None,
) -> MagicMock:
    state = MagicMock()
    state.chat_id = chat_id
    state.newest_message_id = newest_message_id
    state.oldest_message_id = oldest_message_id
    state.is_fully_synced = is_fully_synced
    state.total_messages_synced = total_messages_synced
    state.last_backfill_at = last_backfill_at
    return state


def _make_db_chat(chat_id: int, last_message_id: int = 200) -> MagicMock:
    chat = MagicMock()
    chat.id = chat_id
    chat.last_message_id = last_message_id
    return chat


@pytest_asyncio.fixture
async def mock_pool_and_client():
    """Mock pool with a single authorized 'work' session.

    pool is imported lazily inside start_backfill/start_backfill, so we patch
    'app.telegram.pool.pool' — the singleton imported by backfill_service at call time.
    """
    mock_client = MagicMock()
    mock_client.is_user_authorized = AsyncMock(return_value=True)
    mock_client.get_entity = AsyncMock()
    # Default: return empty (no messages)
    mock_client.get_messages = AsyncMock(return_value=[])

    mock_session = MagicMock()
    mock_session.client = mock_client

    mock_pool = MagicMock()
    mock_pool.get = AsyncMock(return_value=mock_session)

    with patch("app.telegram.pool.pool", mock_pool):
        yield mock_pool, mock_client


# ---------------------------------------------------------------------------
# Test 1: forward fill uses min_id from newest_message_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forward_fill_uses_min_id_from_sync_state(mock_pool_and_client):
    """_run_forward_fill must pass min_id=newest_message_id to get_messages with reverse=True."""
    mock_pool, mock_client = mock_pool_and_client
    chat_id = -100123456789

    sync_state = _make_sync_state(chat_id, newest_message_id=100)
    db_chat = _make_db_chat(chat_id, last_message_id=200)

    # Return 50 messages id 101..150, then empty to stop loop
    batch = [_make_msg(i) for i in range(101, 151)]
    mock_client.get_messages = AsyncMock(side_effect=[batch, []])

    with patch("app.services.backfill_service.async_session") as mock_async_session, \
         patch("app.services.backfill_service.asyncio.sleep", AsyncMock()):
        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)
        mock_db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: sync_state))
        mock_db.get = AsyncMock(return_value=db_chat)
        mock_db.flush = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_async_session.return_value = mock_db

        with patch("app.services.backfill_service.upsert_message", AsyncMock()):
            from app.services.backfill_service import _run_forward_fill
            await _run_forward_fill(
                mock_client, mock_db, sync_state, db_chat, chat_id, limit=100, count=0
            )

    # Verify first get_messages call uses min_id=100, reverse=True
    first_call_kwargs = mock_client.get_messages.call_args_list[0][1]
    assert first_call_kwargs.get("min_id") == 100, (
        f"Expected min_id=100, got {first_call_kwargs.get('min_id')}"
    )
    assert first_call_kwargs.get("reverse") is True, (
        "Expected reverse=True for forward fill"
    )

    # Verify newest_message_id updated to 150
    assert sync_state.newest_message_id == 150, (
        f"Expected newest_message_id=150, got {sync_state.newest_message_id}"
    )


# ---------------------------------------------------------------------------
# Test 2: forward fill with NULL newest_message_id uses min_id=0
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forward_fill_with_null_newest_uses_min_id_zero(mock_pool_and_client):
    """When newest_message_id is None, forward fill must use min_id=0 (no crash)."""
    mock_pool, mock_client = mock_pool_and_client
    chat_id = -100111222333

    sync_state = _make_sync_state(chat_id, newest_message_id=None)
    db_chat = _make_db_chat(chat_id)

    # Return empty immediately
    mock_client.get_messages = AsyncMock(return_value=[])

    with patch("app.services.backfill_service.async_session") as mock_async_session, \
         patch("app.services.backfill_service.asyncio.sleep", AsyncMock()):
        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)
        mock_db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: sync_state))
        mock_db.get = AsyncMock(return_value=db_chat)
        mock_db.flush = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_async_session.return_value = mock_db

        with patch("app.services.backfill_service.upsert_message", AsyncMock()):
            from app.services.backfill_service import _run_forward_fill
            # Must not raise
            await _run_forward_fill(
                mock_client, mock_db, sync_state, db_chat, chat_id, limit=100, count=0
            )

    first_call_kwargs = mock_client.get_messages.call_args_list[0][1]
    assert first_call_kwargs.get("min_id") == 0, (
        f"Expected min_id=0 when newest_message_id is None, got {first_call_kwargs.get('min_id')}"
    )


# ---------------------------------------------------------------------------
# Test 3: forward fill does not touch oldest_message_id or is_fully_synced
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forward_fill_does_not_touch_oldest(mock_pool_and_client):
    """Forward fill must never modify oldest_message_id or is_fully_synced."""
    mock_pool, mock_client = mock_pool_and_client
    chat_id = -100999888777

    sync_state = _make_sync_state(
        chat_id,
        newest_message_id=100,
        oldest_message_id=50,
        is_fully_synced=False,
    )
    db_chat = _make_db_chat(chat_id)

    batch = [_make_msg(i) for i in range(101, 111)]
    mock_client.get_messages = AsyncMock(side_effect=[batch, []])

    with patch("app.services.backfill_service.async_session") as mock_async_session, \
         patch("app.services.backfill_service.asyncio.sleep", AsyncMock()):
        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)
        mock_db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: sync_state))
        mock_db.get = AsyncMock(return_value=db_chat)
        mock_db.flush = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_async_session.return_value = mock_db

        with patch("app.services.backfill_service.upsert_message", AsyncMock()):
            from app.services.backfill_service import _run_forward_fill
            await _run_forward_fill(
                mock_client, mock_db, sync_state, db_chat, chat_id, limit=100, count=0
            )

    # oldest_message_id must remain 50
    assert sync_state.oldest_message_id == 50, (
        f"oldest_message_id must not change, got {sync_state.oldest_message_id}"
    )
    # is_fully_synced must remain False (we didn't set it)
    assert sync_state.is_fully_synced is False, (
        "is_fully_synced must not change during forward fill"
    )

    # Also verify get_messages does NOT receive offset_id (forward uses min_id not offset_id)
    first_call_kwargs = mock_client.get_messages.call_args_list[0][1]
    assert "offset_id" not in first_call_kwargs, (
        "Forward fill must not pass offset_id to get_messages"
    )


# ---------------------------------------------------------------------------
# Test 4: backward (default) unchanged — regression guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backward_default_unchanged(mock_pool_and_client):
    """start_backfill without direction uses backward (default) — regression guard.

    Verifies that offset_id=oldest_message_id is passed, NOT min_id/reverse.
    """
    mock_pool, mock_client = mock_pool_and_client
    chat_id = -100555444333

    sync_state = _make_sync_state(
        chat_id,
        newest_message_id=500,
        oldest_message_id=200,
        is_fully_synced=False,
    )
    db_chat = _make_db_chat(chat_id, last_message_id=500)

    # Return empty → loop terminates immediately, is_fully_synced set True
    mock_client.get_messages = AsyncMock(return_value=[])

    with patch("app.services.backfill_service.async_session") as mock_async_session, \
         patch("app.services.backfill_service.asyncio.sleep", AsyncMock()):
        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)
        mock_db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: sync_state))
        mock_db.get = AsyncMock(return_value=db_chat)
        mock_db.flush = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_async_session.return_value = mock_db

        with patch("app.services.backfill_service.upsert_message", AsyncMock()):
            from app.services.backfill_service import _run_backward_fill
            await _run_backward_fill(
                mock_client, mock_db, sync_state, db_chat, chat_id, limit=100, count=0
            )

    first_call_kwargs = mock_client.get_messages.call_args_list[0][1]
    # backward uses offset_id, NOT min_id/reverse
    assert first_call_kwargs.get("offset_id") == 200, (
        f"Backward fill must pass offset_id=oldest_message_id=200, got {first_call_kwargs.get('offset_id')}"
    )
    assert "reverse" not in first_call_kwargs, (
        "Backward fill must not pass reverse=True"
    )
    assert "min_id" not in first_call_kwargs, (
        "Backward fill must not pass min_id"
    )


# ---------------------------------------------------------------------------
# Test 5: forward fill is idempotent (upsert handles re-fetched messages)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forward_dedup_idempotent(mock_pool_and_client):
    """Forward fill must call upsert_message for all returned messages (incl. duplicates).

    upsert_message itself is idempotent; we verify it's called for every message.
    """
    mock_pool, mock_client = mock_pool_and_client
    chat_id = -100777666555

    sync_state = _make_sync_state(chat_id, newest_message_id=104)
    db_chat = _make_db_chat(chat_id)

    # Return 3 messages including id=105 (already in DB conceptually)
    batch = [_make_msg(105), _make_msg(106), _make_msg(107)]
    mock_client.get_messages = AsyncMock(side_effect=[batch, []])

    upsert_calls = []

    async def _mock_upsert(session, msg, chat):
        upsert_calls.append(msg.id)

    with patch("app.services.backfill_service.async_session") as mock_async_session, \
         patch("app.services.backfill_service.asyncio.sleep", AsyncMock()), \
         patch("app.services.backfill_service.upsert_message", side_effect=_mock_upsert):
        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)
        mock_db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: sync_state))
        mock_db.get = AsyncMock(return_value=db_chat)
        mock_db.flush = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_async_session.return_value = mock_db

        from app.services.backfill_service import _run_forward_fill
        await _run_forward_fill(
            mock_client, mock_db, sync_state, db_chat, chat_id, limit=100, count=0
        )

    assert upsert_calls == [105, 106, 107], (
        f"upsert_message must be called for all 3 messages (incl. id=105), got {upsert_calls}"
    )
    assert sync_state.newest_message_id == 107, (
        f"newest_message_id must be updated to 107, got {sync_state.newest_message_id}"
    )


# ---------------------------------------------------------------------------
# Test 6: concurrent forward + backward for same chat → already_running
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forward_concurrent_with_backward_blocked(mock_pool_and_client):
    """_backfill_tasks per-chat lock: second start_backfill returns already_running."""
    mock_pool, mock_client = mock_pool_and_client
    chat_id = -100333222111

    # Make the first call hang long enough for the second to arrive
    started_event = asyncio.Event()
    proceed_event = asyncio.Event()

    async def _slow_run(*args, **kwargs):
        started_event.set()
        await proceed_event.wait()

    with patch("app.services.backfill_service._run_backfill", side_effect=_slow_run):
        from app.services.backfill_service import start_backfill, _backfill_tasks

        # Clear any stale state
        _backfill_tasks.pop(chat_id, None)

        # Start backward
        result1 = await start_backfill(chat_id, limit=100, alias="work", direction="backward")
        # Wait until the task is actually running
        await started_event.wait()

        # Try forward while backward is running
        result2 = await start_backfill(chat_id, limit=100, alias="work", direction="forward")

        # Cleanup
        proceed_event.set()
        _backfill_tasks.pop(chat_id, None)

    assert result1["status"] == "started", f"First call should start, got {result1}"
    assert result2["status"] == "already_running", (
        f"Second concurrent call must return already_running, got {result2}"
    )
