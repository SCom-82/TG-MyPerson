"""MAX write path in STORAGE_MODE=new (ТЗ 2026-10-13 §5.3, PR-2).

The fake MAX server of the MAX suites; the session's account gets its
partitions for the test. The shadow legacy write is off: max_* stay empty.
"""

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.config import settings
from app.max import normalize
from app.max.store_new import NewMaxStore
from app.models import Chat, Message, RawEvent, SyncState, User
from app.repo import chats
from app.repo.rows import ChatRow
from app.storage import account_db
from tests.isolation_env import create_account, drop_account, sql
from tests.max_env import aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_ingest import GROUP, ME, _authorized, fx

T0_MS = 1_760_000_000_000


@pytest_asyncio.fixture
async def new_env(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "storage_mode", "new")
    monkeypatch.setattr(settings, "shadow_legacy_write", False)
    sql("SELECT create_account_partitions(%s)", (env.account_id,))
    yield env
    await env.pool.stop_all()
    for t in ("raw_events", "snapshot_members", "member_snapshots", "sync_state", "media", "messages", "chats", "users"):
        part = f"{t}_a{env.account_id}"
        if sql("SELECT to_regclass(%s) IS NOT NULL", (part,), fetch=True) == [(True,)]:
            sql(f'ALTER TABLE "{t}" DETACH PARTITION "{part}"')
            sql(f'DROP TABLE "{part}"')
    sql("DELETE FROM identities WHERE platform = 'max' AND external_user_id IN (%s, 200, 300)", (ME,))


async def _rows(account_id, model, *order):
    async with account_db(account_id) as db:
        return (await db.execute(select(model).order_by(*order))).scalars().all()


async def test_frame_goes_to_account_tables(new_env):
    client = await _authorized(new_env)
    await client.push(fx("n03_photo")["frame"])

    [msg] = await _rows(new_env.account_id, Message, Message.id)
    assert (msg.external_id, msg.message_type, msg.text) == (3, "photo", "фото")
    [chat] = await _rows(new_env.account_id, Chat, Chat.id)
    assert (chat.external_id, chat.chat_type) == (GROUP, "group")
    [event] = await _rows(new_env.account_id, RawEvent, RawEvent.id)
    assert (event.kind, event.normalized, event.error) == (f"max:{normalize.OP_MESSAGE}", True, None)
    async with account_db(new_env.account_id) as db:
        media = (await db.execute(text("SELECT attach_index, file_type, file_external_id FROM media"))).all()
    assert [tuple(m) for m in media] == [(0, "photo", "9001")]
    assert sql("SELECT count(*) FROM max_messages", fetch=True) == [(0,)]  # shadow off
    assert sql("SELECT external_user_id FROM accounts WHERE id = %s", (new_env.account_id,), fetch=True) == [(ME,)]


async def test_outgoing_by_account_user_and_sender_row(new_env):
    client = await _authorized(new_env)
    case = fx("n02_own_message")
    assert case["me_id"] == ME
    await client.push(case["frame"])
    [msg] = await _rows(new_env.account_id, Message, Message.id)
    users = {u.id: u for u in await _rows(new_env.account_id, User, User.id)}
    assert msg.is_outgoing is True
    assert users[msg.sender_user_id].external_id == ME and users[msg.sender_user_id].is_self


async def test_delete_and_edit_rules(new_env):
    client = await _authorized(new_env)
    await client.push(fx("e01_edit")["frame"])
    await client.push(fx("n01_text_private_incoming")["frame"])
    [msg] = await _rows(new_env.account_id, Message, Message.id)
    assert (msg.text, msg.is_edited) == ("Привет!", True)

    await client.push(fx("d01_delete")["frame"])
    [msg] = await _rows(new_env.account_id, Message, Message.id)
    assert msg.deleted_at is not None and msg.text == "Привет!"


async def test_login_snapshot_contacts(new_env):
    new_env.server.chats = [fx("c01_chat")["frame"]["payload"]["chat"]]
    new_env.server.contacts = [{"id": 200, "names": [{"firstName": "Анна"}], "link": "anna"}]
    await _authorized(new_env)
    users = {u.external_id: u for u in await _rows(new_env.account_id, User, User.id)}
    assert users[200].is_contact and users[200].first_name == "Анна"
    assert users[ME].is_self
    [chat] = await _rows(new_env.account_id, Chat, Chat.id)
    assert (chat.external_id, chat.title) == (GROUP, "Рабочий чат")


async def test_cursor_moves_in_sync_state(new_env):
    client = await _authorized(new_env)
    (await new_env.session()).caught_up.add(GROUP)
    await client.push(fx("n03_photo")["frame"])
    [state] = await _rows(new_env.account_id, SyncState, SyncState.chat_id)
    msg_time = normalize.parse_message(fx("n03_photo")["frame"]["payload"]).time
    assert normalize.dt_to_ms(state.newest_ts) == msg_time and state.newest_external_id == 3


async def test_catchup_sees_only_own_chats(new_env):
    other = create_account("iso-max-other", platform="max")
    try:
        for acc, ext in ((new_env.account_id, GROUP), (other, GROUP - 1)):
            async with account_db(acc) as db:
                await chats.upsert_chat(db, acc, ChatRow(ext, "group", last_message_external_id=1,
                                                         last_message_at=normalize.ms_to_dt(T0_MS)))
                await db.commit()
        rows = await NewMaxStore(new_env.account_id).catchup_rows()
        assert [r[0] for r in rows] == [GROUP]
    finally:
        drop_account(other)


async def test_same_chat_id_two_accounts_independent_cursors(new_env):
    other = create_account("iso-max-other2", platform="max")
    try:
        for acc, newest in ((new_env.account_id, T0_MS), (other, T0_MS + 5000)):
            async with account_db(acc) as db:
                pk = await chats.upsert_chat(db, acc, ChatRow(GROUP, "group"))
                await db.commit()
            await NewMaxStore(acc).store_message(
                {**_row(1), "chat_id": GROUP}, [], chat_type="group", advance_cursor=True, time_ms=newest
            )
        a = await NewMaxStore(new_env.account_id).cursor(GROUP)
        b = await NewMaxStore(other).cursor(GROUP)
        assert (a.newest_time_ms, b.newest_time_ms) == (T0_MS, T0_MS + 5000)
    finally:
        drop_account(other)


def _row(mid):
    return {
        "message_id": mid, "chat_id": GROUP, "from_user_id": None, "sender_chat_id": None,
        "reply_to_message_id": None, "forward_from_chat_id": None, "forward_from_message_id": None,
        "message_type": "text", "text": "x", "text_html": None, "tg_date": normalize.ms_to_dt(T0_MS),
        "is_outgoing": False, "is_edited": False, "edit_date": None, "views": None, "raw_data": {},
        "deleted_at": None,
    }
