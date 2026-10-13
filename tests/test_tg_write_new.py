"""Telegram write path in STORAGE_MODE=new (ТЗ 2026-10-13 §5, PR-2): T9–T13, T20.

Real Telethon TL objects, account tables under the non-superuser app role
(tests/isolation_env.py). The shadow legacy write is off unless a test turns it on.
"""

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import telethon
from sqlalchemy import select, text
from telethon.sessions import StringSession
from telethon.tl.types import (
    Channel,
    ChatAdminRights,
    ChatPhotoEmpty,
    InputPeerChannel,
    InputPeerUser,
    Message,
    PeerChannel,
    PeerUser,
    User,
)

from app.config import settings
from app.models import Chat, Message as MessageRow_, SyncState, User as UserRow_
from app.repo import chats
from app.services import tg_store
from app.services.stream_service import stream_manager
from app.storage import account_db, owner_db
from tests.isolation_env import SELF_A, SELF_B, sql

DATE = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
CHANNEL = 9_400_000_300
CHAT_EXT = -1_000_000_000_000 - CHANNEL
PEER = 9_400_000_301


@pytest.fixture(autouse=True)
def _new_mode(monkeypatch):
    monkeypatch.setattr(settings, "storage_mode", "new")
    monkeypatch.setattr(settings, "shadow_legacy_write", False)


def channel(*, creator=False, admin=False, access_hash=777, min_=False):
    return Channel(
        id=CHANNEL, title="Чат", photo=ChatPhotoEmpty(), date=DATE, megagroup=True,
        creator=creator, min=min_, access_hash=access_hash,
        admin_rights=ChatAdminRights(delete_messages=True) if admin else None,
    )


def user(uid=PEER, *, access_hash=555, min_=False, username="peer", contact=False):
    return User(id=uid, first_name="Peer", username=username, access_hash=access_hash, min=min_,
                contact=contact, phone="79001234567")


def message(mid=1, *, sender=PEER, peer=None, text_="hello", out=False, edit_date=None):
    return Message(
        id=mid, peer_id=peer or PeerChannel(CHANNEL), date=DATE, message=text_,
        from_id=PeerUser(sender) if sender else None, out=out, edit_date=edit_date, views=3,
    )


def event(msg, chat_entity=None, sender_entity=None, chat_id=CHAT_EXT):
    ev = MagicMock()
    ev.message = msg
    ev.id = msg.id
    ev.chat_id = chat_id
    ev.get_chat = AsyncMock(return_value=chat_entity if chat_entity is not None else channel())
    ev.get_sender = AsyncMock(return_value=sender_entity)
    return ev


async def _messages(account_id):
    async with account_db(account_id) as db:
        return (await db.execute(
            select(MessageRow_).order_by(MessageRow_.external_id)
        )).scalars().all()


# ---------------------------------------------------------------------------
# Event ingest
# ---------------------------------------------------------------------------

async def test_store_event_writes_account_tables(accounts):
    payload = await tg_store.store_event(
        accounts.a, event(message(7), channel(creator=True), user())
    )

    assert payload["message_id"] == 7 and payload["chat_id"] == CHAT_EXT
    async with account_db(accounts.a) as db:
        chat = (await db.execute(select(Chat))).scalar_one()
        peer = (await db.execute(select(UserRow_).where(UserRow_.external_id == PEER))).scalar_one()
        [msg] = (await db.execute(select(MessageRow_))).scalars().all()
    assert (chat.external_id, chat.chat_type, chat.access_hash, chat.my_role) == (CHAT_EXT, "supergroup", 777, "creator")
    assert (peer.access_hash, peer.last_seen_via, peer.phone) == (555, "message", "+79001234567")
    assert msg.sender_user_id == peer.id
    # Full TL object in raw, not the 7 legacy fields.
    assert msg.raw["_"] == "Message" and msg.raw["peer_id"]["channel_id"] == CHANNEL
    assert msg.raw["views"] == 3


async def test_sender_without_entity_gets_stub_user(accounts):
    await tg_store.store_event(accounts.a, event(message(1, sender=PEER), sender_entity=None))
    async with account_db(accounts.a) as db:
        [msg] = (await db.execute(select(MessageRow_))).scalars().all()
        stub = (await db.execute(select(UserRow_).where(UserRow_.external_id == PEER))).scalar_one()
    assert msg.sender_user_id == stub.id and stub.first_name is None


async def test_t9_is_outgoing_per_account(accounts):
    """One message of A in a channel both accounts read."""
    msg = message(1, sender=SELF_A, out=True)
    await tg_store.store_event(accounts.a, event(msg, sender_entity=user(SELF_A)))
    await tg_store.store_event(accounts.b, event(msg, sender_entity=user(SELF_A)))

    [a_msg] = await _messages(accounts.a)
    [b_msg] = await _messages(accounts.b)
    assert (a_msg.is_outgoing, b_msg.is_outgoing) == (True, False)


async def test_t11_monitoring_of_a_does_not_affect_b(accounts):
    await tg_store.store_event(accounts.a, event(message(1)))
    async with account_db(accounts.a) as db:
        await chats.set_monitored(db, accounts.a, CHAT_EXT, False)
        await db.commit()

    assert await tg_store.store_event(accounts.a, event(message(2))) is None
    assert await tg_store.store_event(accounts.b, event(message(2))) is not None
    assert [m.external_id for m in await _messages(accounts.a)] == [1]
    assert [m.external_id for m in await _messages(accounts.b)] == [2]


async def test_edit_and_replay(accounts):
    await tg_store.store_event(accounts.a, event(message(1, text_="draft")))
    await tg_store.store_event(accounts.a, event(message(1, text_="final", edit_date=DATE)))
    await tg_store.store_event(accounts.a, event(message(1, text_="draft")))  # stale replay
    [msg] = await _messages(accounts.a)
    assert (msg.text, msg.is_edited) == ("final", True)


# ---------------------------------------------------------------------------
# Handlers: SSE per account (T12), deletions (T20), shadow write
# ---------------------------------------------------------------------------

class _Client:
    def __init__(self):
        self.handlers = {}

    def on(self, builder):
        def deco(fn):
            self.handlers[fn.__name__] = fn
            return fn
        return deco


def _handlers(account_id):
    from app.telegram.handlers import register_handlers

    client = _Client()
    register_handlers(client, account_id=account_id)
    return client.handlers


def test_register_handlers_needs_account_in_new_mode():
    from app.telegram.handlers import register_handlers

    with pytest.raises(ValueError):
        register_handlers(_Client())


async def test_t12_sse_subscriber_gets_only_own_account(accounts):
    qa, qb, q_legacy = stream_manager.subscribe(accounts.a), stream_manager.subscribe(accounts.b), stream_manager.subscribe()
    try:
        await _handlers(accounts.a)["on_new_message"](event(message(1, text_="for A")))
        await _handlers(accounts.b)["on_new_message"](event(message(2, text_="for B")))
        got_a = [qa.get_nowait() for _ in range(qa.qsize())]
        got_b = [qb.get_nowait() for _ in range(qb.qsize())]
    finally:
        for q in (qa, qb, q_legacy):
            stream_manager.unsubscribe(q)
    assert len(got_a) == 1 and "for A" in got_a[0]
    assert len(got_b) == 1 and "for B" in got_b[0]
    assert q_legacy.qsize() == 0


async def test_t12_max_events_scoped_by_account(accounts):
    from app.max.ingest import MaxIngest

    qa, qm = stream_manager.subscribe(accounts.a), stream_manager.subscribe(accounts.m)
    try:
        ingest = MaxIngest(SimpleNamespace(account_id=accounts.m, alias="iso-max"))
        await ingest._broadcast({"event": "message", "platform": "max"})
        assert (qa.qsize(), qm.qsize()) == (0, 1)
    finally:
        stream_manager.unsubscribe(qa)
        stream_manager.unsubscribe(qm)


async def test_t20_deleted_without_chat_marks_only_own_private_chat(accounts):
    for acc in (accounts.a, accounts.b):
        private = user(PEER)
        await tg_store.store_event(acc, event(message(42, peer=PeerUser(PEER)), private, private, chat_id=PEER))
        await tg_store.store_event(acc, event(message(42)))  # same id in the channel

    deleted = SimpleNamespace(chat_id=None, deleted_ids=[42])
    await _handlers(accounts.a)["on_message_deleted"](deleted)

    async with owner_db() as db:
        rows = (await db.execute(text(
            "SELECT m.account_id, c.chat_type, m.deleted_at IS NOT NULL FROM messages m"
            " JOIN chats c ON c.account_id = m.account_id AND c.id = m.chat_id"
            " WHERE m.account_id IN (:a, :b) ORDER BY 1, 2"
        ), {"a": accounts.a, "b": accounts.b})).all()
    assert [tuple(r) for r in rows] == [
        (accounts.a, "private", True), (accounts.a, "supergroup", False),
        (accounts.b, "private", False), (accounts.b, "supergroup", False),
    ]


async def test_deleted_with_chat_marks_that_chat(accounts):
    await tg_store.store_event(accounts.a, event(message(5)))
    await _handlers(accounts.a)["on_message_deleted"](SimpleNamespace(chat_id=CHAT_EXT, deleted_ids=[5, 999]))
    [msg] = await _messages(accounts.a)
    assert msg.deleted_at is not None


async def test_shadow_write_reaches_legacy_tables(accounts, monkeypatch):
    monkeypatch.setattr(settings, "shadow_legacy_write", True)
    try:
        await _handlers(accounts.a)["on_new_message"](event(message(11), sender_entity=user()))
        assert sql("SELECT count(*) FROM tg_messages WHERE chat_id = %s AND message_id = 11",
                   (CHAT_EXT,), fetch=True) == [(1,)]
        assert len(await _messages(accounts.a)) == 1
    finally:
        sql("DELETE FROM tg_messages WHERE chat_id = %s", (CHAT_EXT,))
        sql("DELETE FROM tg_chats WHERE id = %s", (CHAT_EXT,))
        sql("DELETE FROM tg_users WHERE id = %s", (PEER,))


async def test_failing_shadow_does_not_break_primary(accounts, monkeypatch, caplog):
    monkeypatch.setattr(settings, "shadow_legacy_write", True)

    async def broken(*a, **kw):
        raise RuntimeError("legacy down")

    monkeypatch.setattr("app.telegram.handlers.upsert_message_from_event", broken)
    caplog.set_level(logging.WARNING)
    await _handlers(accounts.a)["on_new_message"](event(message(12)))

    assert [m.external_id for m in await _messages(accounts.a)] == [12]
    assert "shadow legacy write failed (message)" in caplog.text
    assert "Error handling new message" not in caplog.text


# ---------------------------------------------------------------------------
# T13: access_hash and the entity cache preload
# ---------------------------------------------------------------------------

async def test_t13_access_hash_kept_per_account_and_not_overwritten_by_min(accounts):
    await tg_store.store_event(accounts.a, event(message(1), sender_entity=user(access_hash=111)))
    await tg_store.store_event(accounts.b, event(message(1), sender_entity=user(access_hash=222)))
    await tg_store.store_event(accounts.b, event(message(2), channel(min_=True, access_hash=1),
                                                 user(access_hash=999, min_=True)))
    hashes = {}
    for acc in (accounts.a, accounts.b):
        async with account_db(acc) as db:
            hashes[acc] = (
                (await db.execute(select(UserRow_.access_hash).where(UserRow_.external_id == PEER))).scalar_one(),
                (await db.execute(select(Chat.access_hash))).scalar_one(),
            )
    assert hashes == {accounts.a: (111, 777), accounts.b: (222, 777)}


async def test_t13_preload_gives_input_entities_without_network(accounts):
    assert telethon.__version__.startswith("1.45"), "preload uses MemorySession._entities — recheck on upgrade"
    await tg_store.store_event(accounts.b, event(message(1), channel(access_hash=4242), user(access_hash=5151)))

    client = telethon.TelegramClient(StringSession(), 1, "x")  # never connected
    assert await tg_store.preload(accounts.b, client) == 2

    peer = await client.get_input_entity(PEER)
    chan = await client.get_input_entity(CHAT_EXT)
    assert peer == InputPeerUser(PEER, 5151)
    assert chan == InputPeerChannel(CHANNEL, 4242)
    assert await client.get_input_entity("peer") == InputPeerUser(PEER, 5151)  # by username


def test_preload_refuses_unknown_session_layout():
    with pytest.raises(RuntimeError, match="preload unsupported"):
        tg_store.preload_entities(SimpleNamespace(session=object()), [(1, 2, None, None, None)])


async def test_attach_handlers_preloads_then_registers(accounts):
    from app.telegram import pool as pool_module

    await tg_store.store_event(accounts.a, event(message(1), sender_entity=user(access_hash=7)))
    client = telethon.TelegramClient(StringSession(), 1, "x")
    session = SimpleNamespace(account_id=accounts.a, client=client, me_id=SELF_A)
    with patch("app.telegram.handlers.register_handlers") as reg:
        await pool_module.attach_handlers("iso-a", session)
    reg.assert_called_once_with(client, account_id=accounts.a)
    assert await client.get_input_entity(PEER) == InputPeerUser(PEER, 7)


# ---------------------------------------------------------------------------
# Backfill (T10 + cursor)
# ---------------------------------------------------------------------------

def _pool_with(sessions: dict):
    pool = MagicMock()

    async def get(alias):
        return sessions[alias]
    pool.get = get
    return pool


def _tg_session(account_id, client):
    client.is_user_authorized = AsyncMock(return_value=True)
    # identity already confirmed (R3-2 is covered in test_account_identity.py)
    return SimpleNamespace(account_id=account_id, client=client, identity_ok=True)


async def test_t10_backfill_of_a_does_not_block_b(accounts, monkeypatch):
    from app.services import backfill_service

    gate = asyncio.Event()

    async def slow(*args, **kwargs):
        await gate.wait()

    pool = _pool_with({"a": _tg_session(accounts.a, MagicMock()), "b": _tg_session(accounts.b, MagicMock())})
    monkeypatch.setattr("app.telegram.pool.pool", pool)
    monkeypatch.setattr(backfill_service, "_run_backfill_new", slow)
    try:
        assert (await backfill_service.start_backfill(CHAT_EXT, alias="a"))["status"] == "started"
        assert (await backfill_service.start_backfill(CHAT_EXT, alias="b"))["status"] == "started"
        assert (await backfill_service.start_backfill(CHAT_EXT, alias="a"))["status"] == "already_running"
        assert backfill_service.is_backfill_running(CHAT_EXT, accounts.b)
    finally:
        gate.set()
        await asyncio.sleep(0)


async def test_backfill_writes_messages_and_cursor(accounts, monkeypatch):
    from app.services import backfill_service

    pages = [[message(i) for i in (30, 29, 28)], [message(i) for i in (27, 26)], []]
    client = MagicMock()
    client.get_messages = AsyncMock(side_effect=pages)
    client.get_entity = AsyncMock(return_value=channel())
    monkeypatch.setattr("app.telegram.pool.pool", _pool_with({"a": _tg_session(accounts.a, client)}))
    monkeypatch.setattr(backfill_service.asyncio, "sleep", AsyncMock())

    await backfill_service._run_backfill_new(accounts.a, CHAT_EXT, 100, "a", "backward")

    assert [m.external_id for m in await _messages(accounts.a)] == [26, 27, 28, 29, 30]
    async with account_db(accounts.a) as db:
        state = (await db.execute(select(SyncState))).scalar_one()
    assert (state.oldest_external_id, state.newest_external_id, state.total_synced, state.is_fully_synced) == (
        26, 30, 5, True
    )
    assert client.get_messages.await_args_list[1].kwargs["offset_id"] == 28

    # forward from the cursor
    client.get_messages = AsyncMock(side_effect=[[message(31)], []])
    await backfill_service._run_backfill_new(accounts.a, CHAT_EXT, 100, "a", "forward")
    assert client.get_messages.await_args_list[0].kwargs["min_id"] == 30
    async with account_db(accounts.a) as db:
        state = (await db.execute(select(SyncState).execution_options(populate_existing=True))).scalar_one()
    assert (state.newest_external_id, state.oldest_external_id, state.total_synced) == (31, 26, 6)


# ---------------------------------------------------------------------------
# Dialogs, contacts, snapshots
# ---------------------------------------------------------------------------

async def test_store_dialogs_role_hash_and_private_peers(accounts):
    async def dialogs():
        yield SimpleNamespace(entity=channel(admin=True, access_hash=31), message=message(9))
        yield SimpleNamespace(entity=user(access_hash=32), message=message(4, peer=PeerUser(PEER)))

    client = SimpleNamespace(iter_dialogs=dialogs)
    assert await tg_store.store_dialogs(accounts.b, client) == 2
    async with account_db(accounts.b) as db:
        rows = {c.external_id: (c.chat_type, c.my_role, c.access_hash, c.last_message_external_id)
                for c in (await db.execute(select(Chat))).scalars()}
        peer = (await db.execute(select(UserRow_))).scalar_one()
    assert rows == {CHAT_EXT: ("supergroup", "admin", 31, 9), PEER: ("private", None, 32, 4)}
    assert (peer.last_seen_via, peer.access_hash) == ("dialog", 32)


async def test_store_contacts_full_sync_resets_missing(accounts):
    other = user(PEER + 1, username="other")
    await tg_store.store_contacts(accounts.a, [user(), other], full=True)
    await tg_store.store_contacts(accounts.a, [user()], full=True)
    async with account_db(accounts.a) as db:
        flags = dict((await db.execute(select(UserRow_.external_id, UserRow_.is_contact))).all())
    assert flags == {PEER: True, PEER + 1: False}


async def test_store_snapshot_members(accounts):
    client = MagicMock()
    client.get_entity = AsyncMock(return_value=channel())
    members = [{"tg_user_id": PEER, "username": "peer", "first_name": "Peer", "role": "member"}]
    snapshot_id = await tg_store.store_snapshot(accounts.a, client, CHAT_EXT, [user()], members, note="n")
    async with account_db(accounts.a) as db:
        row = (await db.execute(text(
            "SELECT s.note, s.members_count, m.username, u.last_seen_via FROM member_snapshots s"
            " JOIN snapshot_members m ON m.account_id = s.account_id AND m.snapshot_id = s.id"
            " JOIN users u ON u.account_id = m.account_id AND u.id = m.user_id WHERE s.id = :s"
        ), {"s": snapshot_id})).one()
    assert tuple(row) == ("n", 1, "peer", "member")
