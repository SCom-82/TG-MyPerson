"""test_broadcast_ingest.py — SPEC §8 tests 5-18 (broadcast-sender-chat-id fix).

Tests:
  5.  upsert_message broadcast post → from_user_id IS NULL, sender_chat_id set, no FK error.
  6.  upsert_message broadcast post with MessageMediaDocument → message + media saved.
  7.  upsert_message user post in supergroup → from_user_id=sender_id, sender_chat_id IS NULL.
  8.  SSE dict contains sender_chat_id and correct from_user_id.
  9.  upsert_message_from_event Channel sender → upsert_user NOT called, upsert_message called.
  10. upsert_message_from_event User sender → upsert_user called (regression guard).
  11. upsert_message_from_event None sender → no crash, message saved.
  12. Backfill batch of 3, second raises → other 2 saved, count==2, rollback called once.
  13. Regression: existing test_backfill_forward.py tests remain green (manual guard note).
  14. Broadcast backfill end-to-end mock → total_messages_synced > 0, sync_state committed.
  15. MessageResponse.model_validate with sender_chat_id → serializes; without → None.
  16. (Skipped — requires live DB) alembic upgrade/downgrade 007.
  17. Anon-admin: sender_id == chat_id (negative) → from_user_id NULL, sender_chat_id == chat_id.
  18. Linked-channel re-post: sender_chat_id != chat_id → both values saved separately.
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers — minimal Telethon message mock
# ---------------------------------------------------------------------------

def _make_tg_msg(
    msg_id: int,
    sender_id: int | None = None,
    text: str = "hello",
    media=None,
    entities=None,
    date=None,
) -> MagicMock:
    from datetime import datetime, timezone
    msg = MagicMock()
    msg.id = msg_id
    msg.sender_id = sender_id
    msg.sender = None
    msg.text = text
    msg.message = text
    msg.media = media
    msg.entities = entities
    msg.date = date or datetime(2024, 1, 1, tzinfo=timezone.utc)
    msg.out = False
    msg.edit_date = None
    msg.views = None
    msg.fwd_from = None
    msg.reply_to = None
    msg.forwards = None
    msg.grouped_id = None
    return msg


def _make_db_chat(chat_id: int) -> MagicMock:
    chat = MagicMock()
    chat.id = chat_id
    chat.last_message_id = None
    chat.last_message_at = None
    chat.is_monitored = True
    return chat


# ---------------------------------------------------------------------------
# Test 5: broadcast post → from_user_id NULL, sender_chat_id set
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_message_broadcast_no_fk_error():
    """Broadcast post: from_user_id IS NULL, sender_chat_id set, no FK violation."""
    from app.services.message_service import upsert_message

    channel_id = -1002175364727
    msg = _make_tg_msg(msg_id=1001, sender_id=channel_id)
    db_chat = _make_db_chat(channel_id)

    captured_obj = {}

    def _capture_add(obj):
        captured_obj["obj"] = obj

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=lambda: None)
    )
    mock_session.add = MagicMock(side_effect=_capture_add)
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()

    result = await upsert_message(mock_session, msg, db_chat)

    obj = captured_obj.get("obj")
    assert obj is not None, "TgMessage was not added to session"
    assert obj.from_user_id is None, f"Expected from_user_id=None, got {obj.from_user_id}"
    assert obj.sender_chat_id == channel_id, (
        f"Expected sender_chat_id={channel_id}, got {obj.sender_chat_id}"
    )


# ---------------------------------------------------------------------------
# Test 6: broadcast post with media → message + tg_media saved
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_message_broadcast_with_media():
    """Broadcast post with MessageMediaDocument → TgMessage + TgMedia both created."""
    from telethon.tl.types import MessageMediaDocument, Document
    from app.services.message_service import upsert_message

    channel_id = -1002175364727

    # Build minimal media mock
    doc = MagicMock(spec=Document)
    doc.id = 999888777
    doc.access_hash = 123456
    doc.size = 1024
    doc.mime_type = "application/pdf"
    doc.attributes = []

    media = MagicMock(spec=MessageMediaDocument)
    media.document = doc

    msg = _make_tg_msg(msg_id=1002, sender_id=channel_id, media=media)
    db_chat = _make_db_chat(channel_id)

    added_objects = []

    def _capture_add(obj):
        added_objects.append(obj)

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=lambda: None)
    )
    mock_session.add = MagicMock(side_effect=_capture_add)
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()

    # _upsert_media does a second execute for TgMedia existence check
    execute_results = [
        MagicMock(scalar_one_or_none=lambda: None),  # for TgMessage check
        MagicMock(scalar_one_or_none=lambda: None),  # for TgMedia check
    ]
    mock_session.execute = AsyncMock(side_effect=execute_results)

    await upsert_message(mock_session, msg, db_chat)

    from app.models import TgMessage, TgMedia
    added_types = [type(o).__name__ for o in added_objects]
    assert "TgMessage" in added_types, f"TgMessage not added. Got: {added_types}"
    assert "TgMedia" in added_types, f"TgMedia not added. Got: {added_types}"

    tg_msg = next(o for o in added_objects if isinstance(o, TgMessage))
    assert tg_msg.sender_chat_id == channel_id
    assert tg_msg.from_user_id is None


# ---------------------------------------------------------------------------
# Test 7: user post → from_user_id set, sender_chat_id NULL (regression guard)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_message_user_post_regression():
    """Ordinary user post: from_user_id=sender_id, sender_chat_id IS NULL."""
    from app.services.message_service import upsert_message

    user_id = 123456789
    chat_id = -100111222333
    msg = _make_tg_msg(msg_id=2001, sender_id=user_id)
    db_chat = _make_db_chat(chat_id)

    captured_obj = {}

    def _capture_add(obj):
        captured_obj["obj"] = obj

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=lambda: None)
    )
    mock_session.add = MagicMock(side_effect=_capture_add)
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()

    await upsert_message(mock_session, msg, db_chat)

    obj = captured_obj.get("obj")
    assert obj is not None
    assert obj.from_user_id == user_id, (
        f"Expected from_user_id={user_id}, got {obj.from_user_id}"
    )
    assert obj.sender_chat_id is None, (
        f"Expected sender_chat_id=None for user post, got {obj.sender_chat_id}"
    )


# ---------------------------------------------------------------------------
# Test 8: SSE dict contains sender_chat_id and correct from_user_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_message_sse_dict_contains_sender_chat_id():
    """SSE result dict has sender_chat_id and correct from_user_id."""
    from app.services.message_service import upsert_message

    channel_id = -1002175364727
    msg = _make_tg_msg(msg_id=3001, sender_id=channel_id)
    db_chat = _make_db_chat(channel_id)

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=lambda: None)
    )
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()

    result = await upsert_message(mock_session, msg, db_chat)

    assert result is not None
    assert "sender_chat_id" in result, "sender_chat_id missing from SSE dict"
    assert result["sender_chat_id"] == channel_id
    assert result["from_user_id"] is None


# ---------------------------------------------------------------------------
# Test 9: real-time guard — Channel sender → upsert_user NOT called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_message_from_event_channel_sender_no_upsert_user():
    """Channel sender in real-time event: upsert_user must NOT be called."""
    from telethon.tl.types import Channel
    from app.services import message_service

    channel_sender = MagicMock(spec=Channel)
    channel_sender.id = -1002175364727

    event = AsyncMock()
    event.message = _make_tg_msg(msg_id=4001, sender_id=-1002175364727)
    event.get_sender = AsyncMock(return_value=channel_sender)
    event.get_chat = AsyncMock(return_value=MagicMock())

    with patch.object(message_service, "upsert_chat", AsyncMock(return_value=_make_db_chat(-1002175364727))), \
         patch.object(message_service, "upsert_user", AsyncMock()) as mock_upsert_user, \
         patch.object(message_service, "upsert_message", AsyncMock(return_value={"event": "message"})):

        mock_session = AsyncMock()
        result = await message_service.upsert_message_from_event(mock_session, event)

    mock_upsert_user.assert_not_called()


# ---------------------------------------------------------------------------
# Test 10: real-time guard — User sender → upsert_user IS called (regression)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_message_from_event_user_sender_calls_upsert_user():
    """User sender in real-time event: upsert_user must be called."""
    from telethon.tl.types import User
    from app.services import message_service

    user_sender = MagicMock(spec=User)
    user_sender.id = 123456789

    event = AsyncMock()
    event.message = _make_tg_msg(msg_id=5001, sender_id=123456789)
    event.get_sender = AsyncMock(return_value=user_sender)
    event.get_chat = AsyncMock(return_value=MagicMock())

    with patch.object(message_service, "upsert_chat", AsyncMock(return_value=_make_db_chat(-100111222))), \
         patch.object(message_service, "upsert_user", AsyncMock()) as mock_upsert_user, \
         patch.object(message_service, "upsert_message", AsyncMock(return_value={"event": "message"})):

        mock_session = AsyncMock()
        await message_service.upsert_message_from_event(mock_session, event)

    mock_upsert_user.assert_called_once_with(mock_session, user_sender)


# ---------------------------------------------------------------------------
# Test 11: real-time guard — None sender → no crash, message saved
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_message_from_event_none_sender_no_crash():
    """None sender in real-time event: must not crash, upsert_message called."""
    from app.services import message_service

    event = AsyncMock()
    event.message = _make_tg_msg(msg_id=6001, sender_id=None)
    event.get_sender = AsyncMock(return_value=None)
    event.get_chat = AsyncMock(return_value=MagicMock())

    with patch.object(message_service, "upsert_chat", AsyncMock(return_value=_make_db_chat(-100111222))), \
         patch.object(message_service, "upsert_user", AsyncMock()) as mock_upsert_user, \
         patch.object(message_service, "upsert_message", AsyncMock(return_value={"event": "message"})) as mock_upsert_msg:

        mock_session = AsyncMock()
        result = await message_service.upsert_message_from_event(mock_session, event)

    mock_upsert_user.assert_not_called()
    mock_upsert_msg.assert_called_once()


# ---------------------------------------------------------------------------
# Test 12: backfill robustness — one failing message doesn't kill the batch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backfill_per_message_error_isolation():
    """Batch of 3: second raises → other 2 saved, count==2, rollback called once."""
    from datetime import datetime, timezone
    from app.services.backfill_service import _run_backward_fill

    chat_id = -100111222333

    msg1 = _make_tg_msg(1, sender_id=None)
    msg2 = _make_tg_msg(2, sender_id=None)
    msg3 = _make_tg_msg(3, sender_id=None)

    sync_state = MagicMock()
    sync_state.oldest_message_id = None
    sync_state.newest_message_id = None
    sync_state.total_messages_synced = 0
    sync_state.last_backfill_at = None
    sync_state.is_fully_synced = False

    db_chat = _make_db_chat(chat_id)

    saved_ids = []
    call_count = 0

    async def _mock_upsert(session, msg, chat):
        nonlocal call_count
        call_count += 1
        if msg.id == 2:
            raise Exception("Simulated FK violation")
        saved_ids.append(msg.id)

    mock_client = MagicMock()
    mock_client.get_messages = AsyncMock(side_effect=[[msg1, msg2, msg3], []])

    mock_session = AsyncMock()
    mock_session.rollback = AsyncMock()
    mock_session.commit = AsyncMock()

    with patch("app.services.backfill_service.upsert_message", side_effect=_mock_upsert), \
         patch("app.services.backfill_service.asyncio.sleep", AsyncMock()):
        # Pass count=0; function updates it internally via reference (count is local)
        # We check the sync_state.total_messages_synced to verify count
        await _run_backward_fill(
            mock_client, mock_session, sync_state, db_chat, chat_id, limit=100, count=0
        )

    assert 1 in saved_ids, "Message 1 should have been saved"
    assert 2 not in saved_ids, "Message 2 (failing) should NOT be in saved list"
    assert 3 in saved_ids, "Message 3 should have been saved"
    assert len(saved_ids) == 2, f"Expected 2 saved, got {len(saved_ids)}: {saved_ids}"
    mock_session.rollback.assert_called_once()
    # sync_state.total_messages_synced should reflect count=2 added to initial 0
    assert sync_state.total_messages_synced == 2, (
        f"Expected total_messages_synced=2, got {sync_state.total_messages_synced}"
    )


# ---------------------------------------------------------------------------
# Test 14: broadcast backfill end-to-end mock → sync_state committed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_broadcast_backfill_end_to_end():
    """Broadcast channel backfill mock: total_messages_synced>0, sync_state committed."""
    from app.services.backfill_service import _run_backward_fill

    chat_id = -1002175364727

    # 3 broadcast posts from the channel itself
    msgs = [_make_tg_msg(i, sender_id=chat_id) for i in range(1, 4)]

    sync_state = MagicMock()
    sync_state.oldest_message_id = None
    sync_state.newest_message_id = None
    sync_state.total_messages_synced = 0
    sync_state.last_backfill_at = None
    sync_state.is_fully_synced = False

    db_chat = _make_db_chat(chat_id)

    mock_client = MagicMock()
    mock_client.get_messages = AsyncMock(side_effect=[msgs, []])

    mock_session = AsyncMock()
    mock_session.rollback = AsyncMock()
    mock_session.commit = AsyncMock()

    # upsert_message should succeed for all (sender_chat_id path, no FK)
    async def _mock_upsert(session, msg, chat):
        pass  # success, no FK violation

    with patch("app.services.backfill_service.upsert_message", side_effect=_mock_upsert), \
         patch("app.services.backfill_service.asyncio.sleep", AsyncMock()):
        await _run_backward_fill(
            mock_client, mock_session, sync_state, db_chat, chat_id, limit=100, count=0
        )

    assert sync_state.total_messages_synced == 3, (
        f"Expected 3 synced, got {sync_state.total_messages_synced}"
    )
    mock_session.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Test 15: MessageResponse schema — sender_chat_id serializes
# ---------------------------------------------------------------------------

def test_message_response_sender_chat_id_serializes():
    """MessageResponse.model_validate with sender_chat_id → field present."""
    from app.schemas import MessageResponse
    from datetime import datetime, timezone

    # Simulate ORM object with sender_chat_id
    orm_obj = MagicMock()
    orm_obj.id = 1
    orm_obj.message_id = 100
    orm_obj.chat_id = -1002175364727
    orm_obj.from_user_id = None
    orm_obj.sender_chat_id = -1002175364727
    orm_obj.from_user = None
    orm_obj.reply_to_message_id = None
    orm_obj.forward_from_chat_id = None
    orm_obj.forward_from_message_id = None
    orm_obj.message_type = "text"
    orm_obj.text = "hello"
    orm_obj.text_html = None
    orm_obj.tg_date = datetime(2024, 1, 1, tzinfo=timezone.utc)
    orm_obj.is_outgoing = False
    orm_obj.is_edited = False
    orm_obj.edit_date = None
    orm_obj.views = None
    orm_obj.media = []
    orm_obj.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)

    resp = MessageResponse.model_validate(orm_obj)
    assert resp.sender_chat_id == -1002175364727
    assert resp.from_user_id is None


def test_message_response_sender_chat_id_backward_compat():
    """MessageResponse without sender_chat_id (old row) → field defaults to None."""
    from app.schemas import MessageResponse
    from datetime import datetime, timezone

    orm_obj = MagicMock()
    orm_obj.id = 2
    orm_obj.message_id = 200
    orm_obj.chat_id = -100111222333
    orm_obj.from_user_id = 123456789
    orm_obj.sender_chat_id = None
    orm_obj.from_user = None
    orm_obj.reply_to_message_id = None
    orm_obj.forward_from_chat_id = None
    orm_obj.forward_from_message_id = None
    orm_obj.message_type = "text"
    orm_obj.text = "hi"
    orm_obj.text_html = None
    orm_obj.tg_date = datetime(2024, 1, 1, tzinfo=timezone.utc)
    orm_obj.is_outgoing = False
    orm_obj.is_edited = False
    orm_obj.edit_date = None
    orm_obj.views = None
    orm_obj.media = []
    orm_obj.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)

    resp = MessageResponse.model_validate(orm_obj)
    assert resp.sender_chat_id is None
    assert resp.from_user_id == 123456789


# ---------------------------------------------------------------------------
# Test 16: Migration (skipped — requires live DB)
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="Requires live test DB — run via: alembic upgrade 007; alembic downgrade -1")
def test_alembic_migration_007():
    """Skipped: integration test for alembic upgrade/downgrade 007. Run manually."""
    pass


# ---------------------------------------------------------------------------
# Test 17: Anon-admin — sender_id == chat_id (both negative)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_anon_admin_sender_chat_id_equals_chat_id():
    """Anon-admin in supergroup: sender_id is the group id → from_user_id NULL, sender_chat_id == chat_id."""
    from app.services.message_service import upsert_message

    supergroup_id = -1001234567890
    # Anon-admin posts: sender_id is the supergroup itself
    msg = _make_tg_msg(msg_id=7001, sender_id=supergroup_id)
    db_chat = _make_db_chat(supergroup_id)

    captured_obj = {}

    def _capture_add(obj):
        captured_obj["obj"] = obj

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=lambda: None)
    )
    mock_session.add = MagicMock(side_effect=_capture_add)
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()

    await upsert_message(mock_session, msg, db_chat)

    obj = captured_obj["obj"]
    assert obj.from_user_id is None
    assert obj.sender_chat_id == supergroup_id
    # sender_chat_id == chat_id is acceptable (anon-admin case)
    assert obj.sender_chat_id == supergroup_id


# ---------------------------------------------------------------------------
# Test 18: Linked-channel auto-repost — sender_chat_id != chat_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_linked_channel_sender_chat_id_differs_from_chat_id():
    """Auto-repost from linked channel: sender_chat_id != chat_id — stored separately."""
    from app.services.message_service import upsert_message

    discussion_chat_id = -1009999888777   # the discussion chat/group
    linked_channel_id = -1002175364727    # the original broadcast channel

    # Message appears in discussion group but sender_id is the linked channel
    msg = _make_tg_msg(msg_id=8001, sender_id=linked_channel_id)
    db_chat = _make_db_chat(discussion_chat_id)

    captured_obj = {}

    def _capture_add(obj):
        captured_obj["obj"] = obj

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=lambda: None)
    )
    mock_session.add = MagicMock(side_effect=_capture_add)
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()

    await upsert_message(mock_session, msg, db_chat)

    obj = captured_obj["obj"]
    assert obj.from_user_id is None
    assert obj.sender_chat_id == linked_channel_id
    assert obj.chat_id == discussion_chat_id
    # Key assertion: they differ
    assert obj.sender_chat_id != obj.chat_id, (
        "sender_chat_id must differ from chat_id in linked-channel repost scenario"
    )
