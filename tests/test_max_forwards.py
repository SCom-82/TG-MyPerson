"""Forwarded attachments and contact cards (plan 2026-10-19, PR-C: Р-6…Р-9, QA R1-9…R1-11).

STORAGE_MODE=new, the fake MAX server, a local CDN. Fixtures n14/n15/n16 are
shaped like the prod raw of 19.10 (keys and types), values synthesized.
"""

import pytest
from pymax import ApiError
from pymax.types.domain.attachments.file import FileRequest

from app.max import normalize
from app.mcp import tools as mcp_tools
from app.repo import users
from app.repo.rows import UserRow
from app.storage import account_db
from tests.max_env import aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_backfill import BLOB, cdn  # noqa: F401 — pytest fixture
from tests.test_max_ingest import _authorized, fx
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture

DIALOG = 100500
FWD_PHOTO, OWN_PHOTO_FWD_FILE, CONTACT = 14, 15, 16
ORIGIN_CHAT, ORIGIN_MSG = -75183661482741, 116641336752745999


async def _get(env, message_id: int, chat: int = DIALOG) -> dict:
    resp = await env.client.get(f"/api/v1/messages/{chat}/{message_id}", headers=env.h())
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _ready(new_env, cdn, *names):  # noqa: F811
    client = await _authorized(new_env)
    new_env.settings.proxy_url = ""  # the test CDN is local
    new_env.server.file_url = cdn("/file")
    for name in names:
        await client.push(fx(name)["frame"])
    return client


# ---------------------------------------------------------------------------
# Normalization (Р-6, Р-8)
# ---------------------------------------------------------------------------

def test_all_attaches_own_then_forwarded():
    msg = normalize.parse_message(fx("n15_own_photo_forward_file")["frame"]["payload"])
    assert [(i, normalize._attachment_kind(a), fwd) for i, a, fwd in normalize.all_attaches(msg)] == [
        (0, "photo", False), (1, "document", True)]
    reply = normalize.parse_message(fx("n09_reply")["frame"]["payload"])
    assert all(not fwd for _, _, fwd in normalize.all_attaches(reply))  # a reply's attachments are not ours


def test_contact_cards_phone_from_card_or_lookup():
    with_phone = normalize.parse_message(fx("n16_contact_with_phone")["frame"]["payload"])
    assert normalize.contact_cards(with_phone) == [{
        "user_id": 301, "name": "Пётр Окнов", "first_name": "Пётр", "last_name": "Окнов",
        "phone": "+79001234567", "forwarded": False}]
    bare = normalize.parse_message(fx("n07b_contact")["frame"]["payload"])
    assert normalize.contact_cards(bare)[0]["phone"] is None
    assert normalize.contact_cards(bare, {300: "+79005554433"}.get)[0]["phone"] == "+79005554433"
    assert normalize.cards_text(normalize.contact_cards(bare)) == "Контакт: Иван"


def test_forwarded_contact_card_and_text():
    payload = fx("n10_forward_empty_text")["frame"]["payload"]
    payload["message"]["link"]["message"]["text"] = ""
    payload["message"]["link"]["message"]["attaches"] = [
        {"_type": "CONTACT", "contactId": 302, "name": "Ольга", "firstName": "Ольга", "phone": "8 900 111-22-33"}]
    row, media = normalize.normalize_message(normalize.parse_message(payload), me_id=100)
    assert (row["message_type"], row["text"], media) == ("contact", "Контакт: Ольга, +79001112233", [])


# ---------------------------------------------------------------------------
# Answers: media with attach_index, forward, contacts (R1-9, R1-11)
# ---------------------------------------------------------------------------

async def test_forwarded_photo_is_stored_and_described(new_env, cdn):  # noqa: F811
    await _ready(new_env, cdn, "n14_forward_photo")
    async with account_db(new_env.account_id) as db:
        await users.upsert_user(db, new_env.account_id, "max", UserRow(300, first_name="Автор", last_name="Ф"),
                                "contact")
        await db.commit()
    msg = await _get(new_env, FWD_PHOTO)
    assert (msg["message_type"], msg["text"]) == ("photo", "визитка")
    assert [(m["attach_index"], m["file_type"], m["file_id"]) for m in msg["media"]] == [(0, "photo", "9101")]
    assert msg["forward"] == {"from_user_id": 300, "from_user_name": "Автор Ф", "chat_id": 0, "chat_name": None,
                              "message_id": 117465863381610294, "date": "2026-10-07T18:33:20Z"}
    assert msg["contacts"] == []


async def test_own_photo_and_forwarded_file_by_index(new_env, cdn):  # noqa: F811
    client = await _ready(new_env, cdn, "n15_own_photo_forward_file")
    msg = await _get(new_env, OWN_PHOTO_FWD_FILE)
    assert [(m["attach_index"], m["file_type"]) for m in msg["media"]] == [(0, "photo"), (1, "document")]
    assert msg["forward"]["chat_id"] == ORIGIN_CHAT and msg["forward"]["chat_name"] == "Поставщики"
    assert msg["forward"]["from_user_name"] is None  # 301 is not known to the account

    resp = await new_env.client.get(f"/api/v1/messages/{DIALOG}/{OWN_PHOTO_FWD_FILE}/media?index=1",
                                    headers=new_env.h())
    assert resp.status_code == 200, resp.text
    assert resp.content == BLOB and "filename*=UTF-8''%D0%B4%D0%BE%D0%B3" in resp.headers["content-disposition"]
    assert "get_file_by_id" in client.calls
    assert (await new_env.client.get(f"/api/v1/messages/{DIALOG}/{OWN_PHOTO_FWD_FILE}/media?index=2",
                                     headers=new_env.h())).status_code == 404


async def test_contact_card_in_text_and_contacts(new_env, cdn):  # noqa: F811
    await _ready(new_env, cdn, "n16_contact_with_phone", "n07b_contact")
    async with account_db(new_env.account_id) as db:
        await users.upsert_user(db, new_env.account_id, "max", UserRow(300, first_name="Иван", phone="+79005554433"),
                                "contact")
        await db.commit()
    card = await _get(new_env, CONTACT)
    assert (card["message_type"], card["text"]) == ("contact", "Контакт: Пётр Окнов, +79001234567")
    assert card["contacts"][0]["phone"] == "+79001234567" and card["contacts"][0]["user_id"] == 301
    bare = await _get(new_env, 72)
    assert bare["text"] == "Контакт: Иван"  # ingest does no lookups
    assert bare["contacts"][0]["phone"] == "+79005554433"  # the account's users.phone, on read
    found = (await new_env.client.get("/api/v1/messages", headers=new_env.h(),
                                      params={"search": "79001234567"})).json()["items"]
    assert [m["message_id"] for m in found] == [CONTACT]


# ---------------------------------------------------------------------------
# Which ids fetch a forwarded file (Р-7, R1-10)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hidden_source", [False, True])
async def test_forwarded_file_tries_the_original_only_when_named(new_env, cdn, hidden_source):  # noqa: F811
    frame = fx("n15_own_photo_forward_file")["frame"]
    if hidden_source:
        frame["payload"]["message"]["link"]["chatId"] = 0
    client = await _authorized(new_env)
    new_env.settings.proxy_url = ""
    await client.push(frame)
    calls = []

    async def get_file_by_id(chat_id, message_id, file_id):
        calls.append((chat_id, message_id, file_id))
        if message_id == OWN_PHOTO_FWD_FILE:
            raise ApiError(opcode=88, error="file.not.found", message="File not found")
        return FileRequest(url=cdn("/file"), unsafe=False)

    client.get_file_by_id = get_file_by_id
    resp = await new_env.client.get(f"/api/v1/messages/{DIALOG}/{OWN_PHOTO_FWD_FILE}/media?index=1",
                                    headers=new_env.h())
    if hidden_source:
        assert resp.status_code == 502 and "file.not.found" in resp.text
        assert calls == [(DIALOG, OWN_PHOTO_FWD_FILE, 9201)]
    else:
        assert resp.status_code == 200 and resp.content == BLOB
        assert calls == [(DIALOG, OWN_PHOTO_FWD_FILE, 9201), (ORIGIN_CHAT, ORIGIN_MSG, 9201)]


async def test_forwarded_file_by_the_forward_first(new_env, cdn):  # noqa: F811
    client = await _ready(new_env, cdn, "n15_own_photo_forward_file")
    calls = []
    real = client.get_file_by_id

    async def spy(chat_id, message_id, file_id):
        calls.append((chat_id, message_id))
        return await real(chat_id, message_id, file_id)

    client.get_file_by_id = spy
    resp = await new_env.client.get(f"/api/v1/messages/{DIALOG}/{OWN_PHOTO_FWD_FILE}/media?index=1",
                                    headers=new_env.h())
    assert resp.status_code == 200 and calls == [(DIALOG, OWN_PHOTO_FWD_FILE)]


# ---------------------------------------------------------------------------
# MCP download_media by attach_index
# ---------------------------------------------------------------------------

def _caller(env):
    return mcp_tools.Caller("account", env.account_id, env.alias, "max", "ro", "http://test")


async def test_mcp_download_media_by_attach_index(new_env, cdn, monkeypatch):  # noqa: F811
    from app.config import settings
    from app.mcp import files

    monkeypatch.setattr(settings, "file_link_secret", "test-secret")
    await _ready(new_env, cdn, "n15_own_photo_forward_file", "n08b_keyboard_then_photo")
    dl = mcp_tools.BY_NAME["download_media"].call
    first = await dl(_caller(new_env), {"chat_id": -70000000001, "message_id": 81})  # [INLINE_KEYBOARD, PHOTO]
    assert files.verify(first["url"].rsplit("/", 1)[1], "file")["i"] == 1
    doc = await dl(_caller(new_env), {"chat_id": DIALOG, "message_id": OWN_PHOTO_FWD_FILE, "index": 1})
    assert doc["file_name"] == "договор.pdf" and files.verify(doc["url"].rsplit("/", 1)[1], "file")["i"] == 1
    with pytest.raises(mcp_tools.ToolError, match=r"attach_index 5.*\[0, 1\]"):
        await dl(_caller(new_env), {"chat_id": DIALOG, "message_id": OWN_PHOTO_FWD_FILE, "index": 5})
