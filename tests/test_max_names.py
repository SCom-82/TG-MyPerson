"""MAX names (plan 2026-10-19, PR-B: Р-3, Р-4, Р-5, Р-11, QA R1-5…R1-8).

- from_user in MAX message answers (TG stays null: golden legacy == new);
- private chats get the peer's name as title (chat 0 → «Избранное"),
  recomputed when the name changes, only for MAX;
- unknown dialog peers are fetched after login, a stub without a name is
  asked again once a session (first sight + one retry).
"""

import asyncio

import pytest

from app.max.store_new import NewMaxStore
from app.repo import chats, users
from app.repo.chats import SAVED_MESSAGES_TITLE, fill_dialog_titles
from app.repo.rows import ChatRow, UserRow
from app.storage import account_db
from tests.isolation_env import SELF_M, create_account, drop_account
from tests.max_env import aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_ingest import ME, _authorized, fx
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture

PEER = 200
STRANGER = 777_001
T = 1791398400000


def _user(uid: int, first: str | None, last: str | None = None, link: str | None = None) -> dict:
    names = [{"firstName": first, "lastName": last}] if first else []
    return {"id": uid, "names": names, **({"link": link} if link else {})}


def _dialog(peer: int, *, participants: bool = True) -> dict:
    chat = {"id": ME ^ peer, "type": "DIALOG", "status": "ACTIVE", "owner": ME, "lastEventTime": T}
    if participants:
        chat["participants"] = {str(ME): T, str(peer): T}
    return chat


def _frame(chat_id: int, message_id: int, sender: int | None, text: str = "привет") -> dict:
    message = {"id": str(message_id), "time": T + message_id, "type": "USER", "text": text, "attaches": []}
    if sender is not None:
        message["sender"] = sender
    return {"opcode": 128, "cmd": 0, "payload": {"chatId": chat_id, "message": message}}


async def _wait(predicate, timeout: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


async def _chats(env, **query) -> dict:
    resp = await env.client.get("/api/v1/chats", headers=env.h(), params=query)
    assert resp.status_code == 200, resp.text
    return {c["id"]: c for c in resp.json()["items"]}


# ---------------------------------------------------------------------------
# from_user (R1-5)
# ---------------------------------------------------------------------------

async def test_from_user_in_max_answers(new_env):  # noqa: F811
    new_env.server.users[PEER] = _user(PEER, "Анна", "Ким", "anna")
    client = await _authorized(new_env)
    await client.push(fx("n01_text_private_incoming")["frame"])  # sender 200
    await client.push(fx("n02_own_message")["frame"])  # sender = me
    await client.push(fx("n11_channel_post")["frame"])  # no sender

    async def named():
        async with account_db(new_env.account_id) as db:
            u = await users.get_user(db, new_env.account_id, PEER)
            return u is not None and u.first_name == "Анна"
    assert await _wait(named)

    items = (await new_env.client.get("/api/v1/messages", headers=new_env.h())).json()["items"]
    by_text = {m["text"]: m for m in items}
    incoming = by_text["Привет"]
    assert incoming["from_user_id"] == PEER
    assert {k: incoming["from_user"][k] for k in ("id", "first_name", "last_name", "username", "is_self")} == {
        "id": PEER, "first_name": "Анна", "last_name": "Ким", "username": "anna", "is_self": False}
    own = next(m for m in items if m["from_user_id"] == ME)
    assert own["from_user"]["is_self"] is True and own["from_user"]["first_name"] == "Сергей"
    post = by_text["пост"]
    assert post["from_user_id"] is None and post["from_user"] is None

    one = (await new_env.client.get(f"/api/v1/messages/{incoming['chat_id']}/{incoming['message_id']}",
                                    headers=new_env.h())).json()
    assert one["from_user"]["first_name"] == "Анна"


# ---------------------------------------------------------------------------
# fill_dialog_titles (R1-7)
# ---------------------------------------------------------------------------

P1, P2, P3, P4 = 9_400_000_511, 9_400_000_512, 9_400_000_513, 9_400_000_514


async def _seed_dialogs(account_id: int, me: int) -> None:
    async with account_db(account_id) as db:
        await chats.upsert_chat(db, account_id, ChatRow(me ^ P1, "private",
                                                        raw={"participants": {str(me): 1, str(P1): 1}}))
        await chats.upsert_chat(db, account_id, ChatRow(me ^ P2, "private"))  # no raw: XOR
        await chats.upsert_chat(db, account_id, ChatRow(me ^ P3, "private"))  # peer unknown
        await chats.upsert_chat(db, account_id, ChatRow(0, "private"))
        await chats.upsert_chat(db, account_id, ChatRow(-5, "group"))
        await chats.upsert_chat(db, account_id, ChatRow(-6, "channel", title="Канал"))
        # participants win over XOR: the key that is not us
        await chats.upsert_chat(db, account_id, ChatRow(12345, "private",
                                                        raw={"participants": {str(me): 1, str(P4): 1}}))
        await users.upsert_user(db, account_id, "max", UserRow(P1, first_name="Анна", last_name="Ким"), "contact")
        await users.upsert_user(db, account_id, "max", UserRow(P2, username="petya"), "contact")
        await users.upsert_user(db, account_id, "max", UserRow(P4, first_name="Олег"), "contact")
        await db.commit()


async def _titles(account_id: int) -> dict:
    async with account_db(account_id) as db:
        rows, _ = await chats.list_chats(db, account_id, limit=200)
    return {c.external_id: c.title for c in rows}


async def test_fill_dialog_titles(accounts):
    m = accounts.m
    await _seed_dialogs(m, SELF_M)
    async with account_db(m) as db:
        assert await fill_dialog_titles(db, m) == 4
        await db.commit()
    assert await _titles(m) == {
        SELF_M ^ P1: "Анна Ким", SELF_M ^ P2: "petya", SELF_M ^ P3: None, 0: SAVED_MESSAGES_TITLE,
        -5: None, -6: "Канал", 12345: "Олег",
    }
    async with account_db(m) as db:
        assert await fill_dialog_titles(db, m) == 0  # nothing changed → nothing written
        await users.upsert_user(db, m, "max", UserRow(P1, first_name="Анна", last_name="Петрова"), "message")
        assert await fill_dialog_titles(db, m, [SELF_M ^ P2]) == 0  # only the chats asked for
        assert await fill_dialog_titles(db, m, [SELF_M ^ P1]) == 1
        await db.commit()
    assert (await _titles(m))[SELF_M ^ P1] == "Анна Петрова"


async def test_fill_dialog_titles_only_max_and_only_own_rows(accounts):
    a = accounts.a  # telegram
    await _seed_dialogs(a, 9_400_000_001)
    other = create_account("iso-max-2", platform="max", external_user_id=9_400_000_009)
    try:
        await _seed_dialogs(other, 9_400_000_009)
        async with account_db(a) as db:
            assert await fill_dialog_titles(db, a) == 0
        async with account_db(accounts.m) as db:  # app role, RLS: the other MAX account is not visible
            assert await fill_dialog_titles(db, accounts.m) == 0
            await db.commit()
        assert set((await _titles(a)).values()) == {None, "Канал"}
        assert set((await _titles(other)).values()) == {None, "Канал"}
    finally:
        drop_account(other)


# ---------------------------------------------------------------------------
# Ingest: titles appear without a script (R1-8)
# ---------------------------------------------------------------------------

async def test_new_dialog_from_a_stranger_gets_a_title(new_env):  # noqa: F811
    new_env.server.users[STRANGER] = _user(STRANGER, "Незнакомец")
    client = await _authorized(new_env)
    await client.push(_frame(ME ^ STRANGER, 1, STRANGER))

    async def titled():
        return (await _chats(new_env)).get(ME ^ STRANGER, {}).get("title") == "Незнакомец"
    assert await _wait(titled)
    found = await _chats(new_env, search="Незнак")
    assert list(found) == [ME ^ STRANGER] and found[ME ^ STRANGER]["chat_type"] == "private"
    assert await _chats(new_env, chat_type="private", search="нет такого") == {}


async def test_login_fetches_unknown_dialog_peers(new_env):  # noqa: F811
    new_env.server.chats = [_dialog(PEER), _dialog(STRANGER, participants=False),
                            {"id": 0, "type": "DIALOG", "status": "ACTIVE", "owner": ME, "lastEventTime": T}]
    new_env.server.contacts = [_user(PEER, "Анна")]
    new_env.server.users[STRANGER] = _user(STRANGER, "Борис")
    await _authorized(new_env)

    async def titled():
        got = await _chats(new_env)
        return got.get(ME ^ STRANGER, {}).get("title") == "Борис"
    assert await _wait(titled)
    got = await _chats(new_env)
    assert got[ME ^ PEER]["title"] == "Анна" and got[0]["title"] == SAVED_MESSAGES_TITLE
    assert new_env.server.get_users_calls == [[STRANGER]]  # the contact is known: not asked
    assert await NewMaxStore(new_env.account_id).dialog_peers_missing() == set()


async def test_stub_without_a_name_is_asked_again_once_a_session(new_env):  # noqa: F811
    client = await _authorized(new_env)
    calls = []
    real = client.get_users

    async def flaky(ids):
        calls.append(list(ids))
        if len(calls) == 1:
            raise TimeoutError("MAX did not answer")
        return await real(ids)

    client.get_users = flaky
    chat = ME ^ STRANGER
    await client.push(_frame(chat, 1, STRANGER))
    assert await _wait(lambda: _true(len(calls) == 1))
    new_env.server.users[STRANGER] = _user(STRANGER, "Вика")
    await client.push(_frame(chat, 2, STRANGER))  # the stub has no name: asked again

    async def named():
        return (await _chats(new_env)).get(chat, {}).get("title") == "Вика"
    assert await _wait(named)
    await client.push(_frame(chat, 3, STRANGER))
    await asyncio.sleep(0.1)
    assert calls == [[STRANGER], [STRANGER]]

    # a sender MAX has no name for: first sight + one retry per session, not on every message
    nameless = 777_002
    new_env.server.users[nameless] = _user(nameless, None)
    for i in range(4, 7):
        await client.push(_frame(ME ^ nameless, i, nameless))
    await asyncio.sleep(0.2)
    assert calls.count([nameless]) == 2


async def _true(value: bool) -> bool:
    return value


@pytest.mark.parametrize("raw,expected", [
    ({"participants": {"100": 1, "555": 2}}, 555),
    ({"participants": {"100": 1}}, 7 ^ 100),  # only us listed: XOR fallback
    (None, 7 ^ 100),
    ({"participants": ["junk"]}, 7 ^ 100),
])
def test_dialog_peer(raw, expected):
    from app.max.normalize import dialog_peer

    assert dialog_peer(7, raw, 100) == expected
    assert dialog_peer(0, raw, 100) is None
