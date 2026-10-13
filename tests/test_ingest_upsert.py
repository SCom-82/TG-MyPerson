"""Ingest upsert hotfix (PR-H): no message is lost on a duplicate chat/user/message.

Ticket «Потеря входящих сообщений: INSERT без upsert в обработчике» and
«UniqueViolation uq_tg_messages_msg_chat в хендлере». The handler used to do
select-then-insert for tg_chats, tg_users and tg_messages: whenever another
writer (second account in the same chat, catch-up after reconnect, backfill)
inserted the same row first, the commit failed and the new message was rolled
back with it.

The race tests are deterministic: a blocker transaction inserts the row and
keeps it uncommitted while the code under test runs, then commits. The old
code could not see the uncommitted row, inserted its own and failed on the
unique key; INSERT … ON CONFLICT waits for the blocker and updates instead.

Live DB, rows are committed (the services commit themselves) and removed by
the fixture.
"""

import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from telethon.tl.types import Channel, ChatPhotoEmpty, User

from tests.conftest import _TestSessionFactory

USER_ID = 9_100_000_001
OTHER_USER_ID = 9_100_000_002
CHANNEL_ID = 9_200_000_001
CHAT_ID = -1_000_000_000_000 - CHANNEL_ID
DATE = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


async def _cleanup() -> None:
    async with _TestSessionFactory() as s:
        await s.execute(
            text(
                "DELETE FROM tg_media WHERE message_pk IN "
                "(SELECT id FROM tg_messages WHERE chat_id = :c)"
            ),
            {"c": CHAT_ID},
        )
        await s.execute(text("DELETE FROM tg_messages WHERE chat_id = :c"), {"c": CHAT_ID})
        await s.execute(text("DELETE FROM tg_chats WHERE id = :c"), {"c": CHAT_ID})
        await s.execute(
            text("DELETE FROM tg_users WHERE id IN (:u1, :u2)"),
            {"u1": USER_ID, "u2": OTHER_USER_ID},
        )
        await s.commit()


@pytest_asyncio.fixture(autouse=True)
async def _clean_rows():
    await _cleanup()
    yield
    await _cleanup()


def _user(username: str = "fotya", first_name: str = "Fotya", user_id: int = USER_ID) -> User:
    return User(id=user_id, first_name=first_name, username=username)


def _channel(title: str = "Чат Радара") -> Channel:
    return Channel(
        id=CHANNEL_ID, title=title, photo=ChatPhotoEmpty(), date=DATE, megagroup=True
    )


def _message(msg_id: int, text_: str = "hello", sender_id: int = USER_ID,
             edit_date=None, views=None) -> MagicMock:
    msg = MagicMock()
    msg.id = msg_id
    msg.sender_id = sender_id
    msg.text = text_
    msg.message = text_
    msg.media = None
    msg.entities = None
    msg.date = DATE
    msg.out = False
    msg.edit_date = edit_date
    msg.views = views
    msg.fwd_from = None
    msg.reply_to = None
    msg.forwards = None
    msg.grouped_id = None
    return msg


def _event(message, chat=None, sender=None) -> MagicMock:
    ev = MagicMock()
    ev.message = message
    ev.id = message.id
    ev.chat_id = CHAT_ID
    ev.get_chat = AsyncMock(return_value=chat or _channel())
    ev.get_sender = AsyncMock(return_value=sender or _user())
    return ev


async def _scalar(sql: str, **params):
    async with _TestSessionFactory() as s:
        return (await s.execute(text(sql), params)).scalar()


async def _ingest(event, is_edit: bool = False):
    from app.services.message_service import upsert_message_from_event

    async with _TestSessionFactory() as s:
        return await upsert_message_from_event(s, event, is_edit=is_edit)


async def _while_blocked(blocker_sql: str, params: dict, coro):
    """Run coro while a concurrent transaction holds an uncommitted duplicate row."""
    async with _TestSessionFactory() as blocker:
        await blocker.execute(text(blocker_sql), params)
        task = asyncio.create_task(coro)
        await asyncio.sleep(0.3)
        await blocker.commit()
    return await asyncio.wait_for(task, timeout=10)


# ---------------------------------------------------------------------------
# Acceptance 2: the same message twice — no error, no duplicate
# ---------------------------------------------------------------------------

async def test_same_message_twice_no_error_no_duplicate():
    first = await _ingest(_event(_message(111520)))
    second = await _ingest(_event(_message(111520)))

    assert first["message_id"] == second["message_id"] == 111520
    assert await _scalar(
        "SELECT count(*) FROM tg_messages WHERE chat_id = :c AND message_id = 111520", c=CHAT_ID
    ) == 1


async def test_replay_does_not_mark_message_edited():
    """Telethon replays after a reconnect are not edits."""
    await _ingest(_event(_message(5)))
    await _ingest(_event(_message(5)))

    assert await _scalar(
        "SELECT is_edited FROM tg_messages WHERE chat_id = :c AND message_id = 5", c=CHAT_ID
    ) is False


# ---------------------------------------------------------------------------
# Acceptance 3: a message from an already known user is saved
# ---------------------------------------------------------------------------

async def test_message_from_known_user_is_saved():
    await _ingest(_event(_message(1)))
    await _ingest(_event(_message(2)))

    assert await _scalar(
        "SELECT count(*) FROM tg_messages WHERE chat_id = :c AND from_user_id = :u",
        c=CHAT_ID, u=USER_ID,
    ) == 2
    assert await _scalar("SELECT count(*) FROM tg_users WHERE id = :u", u=USER_ID) == 1


# ---------------------------------------------------------------------------
# Acceptance 4: profile changes reach tg_users / tg_chats (DO UPDATE, not DO NOTHING)
# ---------------------------------------------------------------------------

async def test_username_change_is_picked_up():
    await _ingest(_event(_message(1), sender=_user(username="old_name")))
    await _ingest(_event(_message(2), sender=_user(username="new_name", first_name="Renamed")))

    async with _TestSessionFactory() as s:
        row = (await s.execute(
            text("SELECT username, first_name, raw_data->>'username' FROM tg_users WHERE id = :u"),
            {"u": USER_ID},
        )).one()
    assert tuple(row) == ("new_name", "Renamed", "new_name")


async def test_user_phone_is_kept_when_update_has_none():
    from app.services.user_service import upsert_user

    with_phone = _user()
    with_phone.phone = "79270000000"
    async with _TestSessionFactory() as s:
        await upsert_user(s, with_phone)
    async with _TestSessionFactory() as s:
        db_user = await upsert_user(s, _user(username="again"))

    assert db_user.phone == "79270000000"
    assert db_user.username == "again"


async def test_chat_title_change_keeps_monitoring_flag():
    await _ingest(_event(_message(1), chat=_channel("Old title")))
    async with _TestSessionFactory() as s:
        await s.execute(text("UPDATE tg_chats SET is_monitored = false WHERE id = :c"), {"c": CHAT_ID})
        await s.commit()

    result = await _ingest(_event(_message(2), chat=_channel("New title")))

    assert result is None  # not monitored → not stored, as before
    assert await _scalar("SELECT title FROM tg_chats WHERE id = :c", c=CHAT_ID) == "New title"
    assert await _scalar("SELECT is_monitored FROM tg_chats WHERE id = :c", c=CHAT_ID) is False


# ---------------------------------------------------------------------------
# Races with a concurrent writer (the actual production failure)
# ---------------------------------------------------------------------------

async def test_race_on_new_user_message_is_saved():
    """tg_users_pkey: the author is inserted concurrently (14 lost/day in the ticket)."""
    await _while_blocked(
        "INSERT INTO tg_users (id, first_name, is_bot, is_self) VALUES (:u, 'Other writer', false, false)",
        {"u": USER_ID},
        _ingest(_event(_message(42), sender=_user(first_name="From event"))),
    )

    assert await _scalar(
        "SELECT count(*) FROM tg_messages WHERE chat_id = :c AND message_id = 42", c=CHAT_ID
    ) == 1
    assert await _scalar("SELECT first_name FROM tg_users WHERE id = :u", u=USER_ID) == "From event"


async def test_race_on_new_chat_message_is_saved():
    """tg_chats_pkey: the chat is inserted concurrently (2 lost/day in the ticket)."""
    await _while_blocked(
        "INSERT INTO tg_chats (id, chat_type, title, is_monitored) VALUES (:c, 'supergroup', 'x', true)",
        {"c": CHAT_ID},
        _ingest(_event(_message(43))),
    )

    assert await _scalar(
        "SELECT count(*) FROM tg_messages WHERE chat_id = :c AND message_id = 43", c=CHAT_ID
    ) == 1


async def test_race_on_same_message_no_error():
    """uq_tg_messages_msg_chat: two accounts store the same message at once."""
    await _ingest(_event(_message(1)))  # chat and user exist

    result = await _while_blocked(
        "INSERT INTO tg_messages (message_id, chat_id, from_user_id, message_type, tg_date,"
        " is_outgoing, is_edited) VALUES (44, :c, :u, 'text', :d, false, false)",
        {"c": CHAT_ID, "u": USER_ID, "d": DATE},
        _ingest(_event(_message(44, "hello"))),
    )

    assert result["message_id"] == 44
    assert await _scalar(
        "SELECT count(*) FROM tg_messages WHERE chat_id = :c AND message_id = 44", c=CHAT_ID
    ) == 1
    assert await _scalar(
        "SELECT text FROM tg_messages WHERE chat_id = :c AND message_id = 44", c=CHAT_ID
    ) == "hello"


async def test_parallel_ingest_from_two_sessions():
    """Two accounts receive the same new message in a new chat from a new user."""
    results = await asyncio.gather(
        _ingest(_event(_message(77))), _ingest(_event(_message(77))),
        return_exceptions=True,
    )

    assert not [r for r in results if isinstance(r, BaseException)], results
    assert await _scalar(
        "SELECT count(*) FROM tg_messages WHERE chat_id = :c AND message_id = 77", c=CHAT_ID
    ) == 1


# ---------------------------------------------------------------------------
# Edits
# ---------------------------------------------------------------------------

async def test_edit_updates_and_stale_copy_does_not_overwrite():
    edited_at = datetime(2026, 10, 13, 12, 5, tzinfo=timezone.utc)
    await _ingest(_event(_message(9, "draft", views=10)))
    await _ingest(_event(_message(9, "final", edit_date=edited_at)), is_edit=True)
    # A stale non-edited copy (catch-up replay) must not roll the edit back.
    await _ingest(_event(_message(9, "draft")))

    async with _TestSessionFactory() as s:
        row = (await s.execute(
            text("SELECT text, is_edited, edit_date, views FROM tg_messages"
                 " WHERE chat_id = :c AND message_id = 9"),
            {"c": CHAT_ID},
        )).one()
    assert row.text == "final"
    assert row.is_edited is True
    assert row.edit_date == edited_at
    assert row.views == 10  # the edit carried no views → kept


# ---------------------------------------------------------------------------
# Acceptance 1: the NewMessage handler logs no error for duplicates
# ---------------------------------------------------------------------------

async def test_handler_logs_no_error_on_duplicates(monkeypatch, caplog):
    import app.telegram.handlers as handlers

    monkeypatch.setattr(handlers, "async_session", _TestSessionFactory)
    broadcast = AsyncMock()
    monkeypatch.setattr(handlers.stream_manager, "broadcast", broadcast)

    registered = {}

    class _Client:
        def on(self, event_builder):
            def deco(fn):
                registered[fn.__name__] = fn
                return fn
            return deco

    handlers.register_handlers(_Client())
    on_new_message = registered["on_new_message"]

    caplog.set_level(logging.ERROR, logger="app.telegram.handlers")
    await asyncio.gather(
        on_new_message(_event(_message(100))),
        on_new_message(_event(_message(100))),
    )
    await on_new_message(_event(_message(100)))
    await on_new_message(_event(_message(101), sender=_user(user_id=OTHER_USER_ID)))

    assert "Error handling new message" not in caplog.text
    assert broadcast.await_count == 4
    assert await _scalar(
        "SELECT count(*) FROM tg_messages WHERE chat_id = :c", c=CHAT_ID
    ) == 2
