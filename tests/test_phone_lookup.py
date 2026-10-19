"""Find a person by phone, TG and MAX (plan 2026-10-19 §4 PR-D1: Р-13…Р-19, QA R2-1…R2-11).

POST /api/v1/contacts/by-phone, MCP contact_by_phone. STORAGE_MODE=new, the
fake MAX server (search_by_phone / add_contact), a fake Telethon client for
ImportContacts. The clock of write_limits is the test's.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest
import pytest_asyncio
from pymax import ApiError
from telethon.tl.types import User as TlUser

import app.authz.middleware as mw
from app.authz import write_limits
from app.mcp import tools as mcp_tools
from app.utils.phone import mask_phone
from tests.isolation_env import drop_account, sql
from tests.max_env import ADMIN_KEY, API_KEY, aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_ingest import ME
from tests.test_max_write_limits import OFF, _events, _limits, clock, rw  # noqa: F401 — pytest fixtures
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture

PHONE = "+79278975956"
MASKED = "+7******5956"
PEER = 197122769
TG_PEER = 9_400_000_219
TG_SELF = 9_400_000_201


def _max_user(uid: int = PEER, phone: int | None = 79278975956) -> dict:
    return {"id": uid, "names": [{"firstName": "Сергей", "lastName": "Личный"}],
            **({"phone": phone} if phone else {})}


async def _lookup(w, headers=None, **body):
    return await w.http.post("/api/v1/contacts/by-phone", headers=headers or w.env.h(),
                             json={"phone": PHONE, **body})


def _cleanup_people(*external_ids: int) -> None:
    rows = sql("SELECT DISTINCT person_id FROM identities WHERE external_user_id = ANY(%s)",
               (list(external_ids),), fetch=True)
    persons = [r[0] for r in rows]
    ids = "(SELECT id FROM identities WHERE external_user_id = ANY(%s))"
    sql(f"DELETE FROM messages m USING users u WHERE m.account_id = u.account_id AND m.sender_user_id = u.id"
        f" AND u.identity_id IN {ids}", (list(external_ids),))
    sql(f"DELETE FROM users WHERE identity_id IN {ids}", (list(external_ids),))
    sql("DELETE FROM identities WHERE external_user_id = ANY(%s)", (list(external_ids),))
    if persons:
        sql("DELETE FROM people_merge_log WHERE from_person_id = ANY(%s) OR to_person_id = ANY(%s)",
            (persons, persons))
        sql("DELETE FROM people p WHERE p.id = ANY(%s)"
            " AND NOT EXISTS (SELECT 1 FROM identities i WHERE i.person_id = p.id)", (persons,))


@pytest_asyncio.fixture
async def mx(rw):  # noqa: F811
    rw.env.server.phone_book[PHONE] = _max_user()
    yield rw
    _cleanup_people(PEER, TG_PEER)


class FakeTelethon:
    def __init__(self, users: list | None = None, error: BaseException | None = None) -> None:
        self.users = users if users is not None else [TlUser(id=TG_PEER, access_hash=777, first_name="Сергей",
                                                             phone="79278975956")]
        self.error = error
        self.retry: list[int] = []
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(users=self.users, imported=[], retry_contacts=self.retry)


@pytest_asyncio.fixture
async def tg(rw, aliases, monkeypatch):  # noqa: F811
    alias, account_id = aliases("tgl", platform="telegram", mode="rw")
    sql("UPDATE accounts SET external_user_id = %s WHERE id = %s", (TG_SELF, account_id))
    sql("SELECT create_account_partitions(%s)", (account_id,))
    mw._mode_cache.clear()
    client = FakeTelethon()

    async def authorized(request):
        return client

    monkeypatch.setattr("app.telegram.pool.require_authorized_client", authorized)
    yield SimpleNamespace(rw=rw, alias=alias, id=account_id, client=client,
                          h={**API_KEY, "x-session-alias": alias})
    drop_account(account_id)
    _cleanup_people(TG_PEER, PEER)


# ---------------------------------------------------------------------------
# Numbers (1)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["+7 927 897-59-56", "79278975956", "8 (927) 897 59 56", "+79278975956"])
async def test_phone_formats_are_one_number(mx, raw):
    resp = await mx.http.post("/api/v1/contacts/by-phone", headers=mx.env.h(), json={"phone": raw})
    assert resp.status_code == 200, resp.text
    assert mx.env.server.lookup_calls == [PHONE]


@pytest.mark.parametrize("raw", ["", "12345", "абв", "+7 927", "1" * 20])
async def test_bad_number_is_422_without_a_lookup(mx, raw):
    resp = await mx.http.post("/api/v1/contacts/by-phone", headers=mx.env.h(), json={"phone": raw})
    assert resp.status_code == 422, resp.text
    assert mx.env.server.lookup_calls == [] and _events(mx.id) == []


def test_mask_phone():
    assert mask_phone(PHONE) == MASKED
    assert mask_phone("8 927 897-59-56") == MASKED
    assert mask_phone("+442079460958") == "+4*******0958"
    assert mask_phone("123") is None


# ---------------------------------------------------------------------------
# MAX (6)
# ---------------------------------------------------------------------------

async def test_max_found_added_stored_with_dialog(mx):
    resp = await _lookup(mx)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    chat = ME ^ PEER
    assert {k: body[k] for k in ("found", "cached", "platform", "user_id", "chat_id", "phone", "is_contact",
                                 "lookups_left_today")} == {
        "found": True, "cached": False, "platform": "max", "user_id": PEER, "chat_id": chat, "phone": PHONE,
        "is_contact": True, "lookups_left_today": 19}
    assert body["name"] == "Сергей Личный" and isinstance(body["person_id"], int)
    assert mx.env.server.added_contacts == [PEER]
    assert sql("SELECT phone FROM identities WHERE platform = 'max' AND external_user_id = %s", (PEER,),
               fetch=True) == [(PHONE,)]
    chats = (await mx.http.get("/api/v1/chats", headers=mx.env.h(), params={"chat_type": "private"})).json()
    assert [(c["id"], c["title"]) for c in chats["items"]] == [(chat, "Сергей Личный")]
    assert [e[0:2] for e in _events(mx.id)] == [("lookup", "contact_by_phone")]


async def test_max_add_contact_failure_still_answers(mx):
    mx.env.server.add_contact_errors = [ApiError(opcode=34, error="contact.limit", message="Too many")]
    body = (await _lookup(mx)).json()
    assert body["found"] is True and body["is_contact"] is False and "add_contact failed" in body["warning"]


async def test_max_not_found_and_errors_spend_the_slot(mx):
    mx.env.server.phone_book.clear()
    resp = await _lookup(mx)
    assert resp.status_code == 404
    assert resp.json() == {"found": False, "phone": MASKED, "reason": "not registered or hidden by privacy",
                           "lookups_left_today": 19}
    mx.env.server.lookup_errors = [ApiError(opcode=46, error="too.many.requests", message="flood"),
                                   TimeoutError("no answer")]
    assert (await _lookup(mx)).status_code == 502
    assert (await _lookup(mx)).status_code == 502
    assert [e[0] for e in _events(mx.id)] == ["lookup"] * 3


# ---------------------------------------------------------------------------
# Cache (Р-15), limits (3, 4), parallel (R2-7)
# ---------------------------------------------------------------------------

async def test_cache_answers_without_a_lookup_refresh_looks_up(mx):
    first = (await _lookup(mx)).json()
    again = (await _lookup(mx)).json()
    assert again["cached"] is True and again["lookups_left_today"] == 19
    assert {k: again[k] for k in ("person_id", "user_id", "chat_id")} == {
        k: first[k] for k in ("person_id", "user_id", "chat_id")}
    assert mx.env.server.lookup_calls == [PHONE]
    refreshed = (await _lookup(mx, refresh=True)).json()
    assert refreshed["cached"] is False and refreshed["lookups_left_today"] == 18
    assert len(mx.env.server.lookup_calls) == 2


async def test_limit_20_a_day_then_429(mx):
    for i in range(20):
        mx.clock.at(i * 60)
        assert (await _lookup(mx, refresh=True)).status_code == 200
    mx.clock.at(20 * 60)
    resp = await _lookup(mx, refresh=True)
    assert resp.status_code == 429, resp.text
    body = resp.json()
    assert (body["limit"], body["max"], body["used"]) == ("phone_lookups_per_day", 20, 20)
    assert resp.headers["retry-after"] == str(86400 - 1200)
    assert "phone lookups" in body["message"]
    assert len(mx.env.server.lookup_calls) == 20
    mx.clock.at(86400 + 1)
    assert (await _lookup(mx, refresh=True)).status_code == 200


async def test_admin_patch_zero_null_delete(mx):
    status = (await mx.http.get(f"/api/v1/accounts/{mx.id}/write-limits", headers=ADMIN_KEY)).json()
    assert status["limits"]["phone_lookups_per_day"] == 20
    assert status["phone_lookups"] == {"limit": 20, "used": 0, "remaining": 20}

    await _limits(mx, phone_lookups_per_day=0)  # the switch (Р-16): lookups off without a restart
    resp = await _lookup(mx)
    assert resp.status_code == 429 and "retry-after" not in resp.headers
    assert resp.json()["message"] == f"phone lookups of account '{mx.env.alias}' are off: phone_lookups_per_day = 0"
    assert mx.env.server.lookup_calls == []

    await _limits(mx, phone_lookups_per_day=None)
    assert (await _lookup(mx)).json()["lookups_left_today"] is None
    await mx.http.delete(f"/api/v1/accounts/{mx.id}/write-limits", headers=ADMIN_KEY)
    status = (await mx.http.get(f"/api/v1/accounts/{mx.id}/write-limits", headers=ADMIN_KEY)).json()
    assert status["phone_lookups"] == {"limit": 20, "used": 1, "remaining": 19}


async def test_enabled_false_stops_lookups(mx):
    await _limits(mx, enabled=False)
    resp = await _lookup(mx)
    assert resp.status_code == 403 and resp.json()["error"] == "writes disabled"
    assert mx.env.server.lookup_calls == []


async def test_lookups_have_no_min_interval(mx):
    await _limits(mx, min_interval_sec=10, jitter_sec=0)
    for _ in range(3):
        assert (await _lookup(mx, refresh=True)).status_code == 200
    assert mx.clock.sleeps == []


async def test_parallel_lookups_take_exactly_the_remaining(mx):
    await _limits(mx, phone_lookups_per_day=3)
    results = await asyncio.gather(*(_lookup(mx, refresh=True) for _ in range(10)))
    assert sorted(r.status_code for r in results) == [200] * 3 + [429] * 7
    assert len(mx.env.server.lookup_calls) == 3


# ---------------------------------------------------------------------------
# ro (2), MCP, masking (9)
# ---------------------------------------------------------------------------

async def test_ro_is_403_and_does_not_see_the_tool(mx):
    sql("UPDATE accounts SET mode = 'ro' WHERE id = %s", (mx.id,))
    mw._mode_cache.clear()
    resp = await _lookup(mx)
    assert resp.status_code == 403, resp.text
    assert mx.env.server.lookup_calls == [] and _events(mx.id) == []
    for platform in ("max", "telegram"):
        caller = mcp_tools.Caller("account", mx.id, mx.env.alias, platform, "ro", "http://test")
        assert "contact_by_phone" not in {t.name for t in mcp_tools.visible(caller)}


def test_tools_by_platform_and_mode():
    names = lambda platform, mode: {t.name for t in mcp_tools.visible(  # noqa: E731
        mcp_tools.Caller("account", 1, "x", platform, mode, "http://test"))}
    max_rw, max_ro, tg_rw, tg_ro = names("max", "rw"), names("max", "ro"), names("telegram", "rw"), names("telegram", "ro")
    assert len(max_rw) == 30 and {"contact_by_phone", "send_to_person", "reachable_contacts"} <= max_rw
    assert (len(max_ro), len(tg_rw), len(tg_ro)) == (17, 46, 21)
    assert "contact_by_phone" in tg_rw and "contact_by_phone" not in max_ro | tg_ro


async def test_mcp_contact_by_phone(mx):
    caller = mcp_tools.Caller("account", mx.id, mx.env.alias, "max", "rw", "http://test")
    found = await mcp_tools.BY_NAME["contact_by_phone"].call(caller, {"phone": "8 927 897 59 56"})
    assert found["user_id"] == PEER and found["chat_id"] == ME ^ PEER
    mx.env.server.phone_book.clear()
    with pytest.raises(mcp_tools.ToolError) as exc:
        await mcp_tools.BY_NAME["contact_by_phone"].call(caller, {"phone": "+79001112244", "refresh": True})
    assert str(exc.value).startswith("HTTP 404") and "+7******2244" in str(exc.value)


async def test_full_number_is_not_logged_or_audited(mx, caplog):
    caplog.set_level(logging.DEBUG)
    await _lookup(mx)
    mx.env.server.phone_book.clear()
    await _lookup(mx, phone="+79001112244", refresh=True)
    for _ in range(100):
        rows = sql("SELECT status, error, params_digest, target_user_id FROM audit_logs"
                   " WHERE alias = %s AND tool = 'contact_by_phone' ORDER BY id", (mx.env.alias,), fetch=True)
        if len(rows) >= 2:
            break
        await asyncio.sleep(0.05)
    assert [(r[0], r[3]) for r in rows] == [("ok", PEER), ("error", None)]
    import hashlib
    import json

    full = hashlib.sha256(json.dumps({"phone": PHONE}, sort_keys=True).encode()).hexdigest()[:16]
    assert rows[0][2] != full  # REST: the digest (when the body is readable) is of the masked number
    from app.authz.middleware import _digest as rest_digest
    from app.mcp.server import _digest as mcp_digest

    same_mask = "+70000005956"  # another number with the same mask: the same digest, nothing to enumerate
    assert rest_digest(json.dumps({"phone": PHONE}).encode()) == rest_digest(json.dumps({"phone": same_mask}).encode())
    assert mcp_digest({"phone": PHONE, "refresh": True}) == mcp_digest({"phone": same_mask, "refresh": True})
    text = caplog.text + repr(rows)
    for number in ("79278975956", "79001112244", "927 897"):
        assert number not in text
    assert MASKED in caplog.text and "+7******2244" in caplog.text


# ---------------------------------------------------------------------------
# Telegram (5, 7, 8)
# ---------------------------------------------------------------------------

async def test_tg_found_stored_as_contact(tg):
    resp = await tg.rw.http.post("/api/v1/contacts/by-phone", headers=tg.h, json={"phone": PHONE})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["platform"], body["user_id"], body["chat_id"], body["is_contact"]) == ("telegram", TG_PEER, TG_PEER, True)
    [req] = tg.client.requests
    assert req.contacts[0].phone == PHONE and req.contacts[0].first_name == PHONE
    assert sql(f"SELECT access_hash, is_contact FROM users_a{tg.id} WHERE external_id = %s", (TG_PEER,),
               fetch=True) == [(777, True)]
    assert [e[0] for e in _events(tg.id)] == ["lookup"]


async def test_tg_not_found_and_flood_spend_the_slot(tg):
    tg.client.users = []
    resp = await tg.rw.http.post("/api/v1/contacts/by-phone", headers=tg.h, json={"phone": PHONE})
    assert resp.status_code == 404 and resp.json()["lookups_left_today"] == 19
    tg.client.error = RuntimeError("FloodWaitError: A wait of 300 seconds is required")
    resp = await tg.rw.http.post("/api/v1/contacts/by-phone", headers=tg.h, json={"phone": PHONE})
    assert resp.status_code == 502 and "FloodWait" in resp.text
    assert [e[0] for e in _events(tg.id)] == ["lookup", "lookup"]


async def test_tg_import_limit_is_429_not_not_found(tg):
    """review 14 R14-1: empty users + retry_contacts = Telegram's import limit, not 'no Telegram'."""
    tg.client.users = []
    tg.client.retry = [12345]
    resp = await tg.rw.http.post("/api/v1/contacts/by-phone", headers=tg.h, json={"phone": PHONE})
    assert resp.status_code == 429, resp.text
    assert "Telegram import limit, retry later" in resp.json()["detail"]
    assert "not registered" not in resp.text
    assert [e[0] for e in _events(tg.id)] == ["lookup"]  # spent (Р-14)


async def test_tg_lookups_limited_sends_not(tg):
    status = (await tg.rw.http.get(f"/api/v1/accounts/{tg.id}/write-limits", headers=ADMIN_KEY)).json()
    assert (status["source"], status["limits"]) == ("none", None)
    assert status["phone_lookups"] == {"limit": 20, "used": 0, "remaining": 20}
    for i in range(20):
        tg.rw.clock.at(i)
        r = await tg.rw.http.post("/api/v1/contacts/by-phone", headers=tg.h, json={"phone": PHONE, "refresh": True})
        assert r.status_code == 200, r.text
    r = await tg.rw.http.post("/api/v1/contacts/by-phone", headers=tg.h, json={"phone": PHONE, "refresh": True})
    assert r.status_code == 429 and r.json()["limit"] == "phone_lookups_per_day"
    assert all(e[0] == "lookup" for e in _events(tg.id))
    # a PATCH of another TG limit keeps lookups at 20 (NO_LIMITS carries the default)
    await tg.rw.http.patch(f"/api/v1/accounts/{tg.id}/write-limits", headers=ADMIN_KEY, json={**OFF, "per_minute": 5})
    status = (await tg.rw.http.get(f"/api/v1/accounts/{tg.id}/write-limits", headers=ADMIN_KEY)).json()
    assert status["limits"]["phone_lookups_per_day"] == 20 and status["limits"]["per_hour"] is None


async def test_one_person_across_tg_and_max(tg):
    tg_body = (await tg.rw.http.post("/api/v1/contacts/by-phone", headers=tg.h, json={"phone": PHONE})).json()
    tg.rw.env.server.phone_book[PHONE] = _max_user()
    max_body = (await _lookup(tg.rw)).json()
    assert tg_body["person_id"] == max_body["person_id"]
    assert write_limits.LOOKUP_DEFAULT == 20


# ---------------------------------------------------------------------------
# send_to_person on MAX (plan §4 PR-D2, QA R2-9)
# ---------------------------------------------------------------------------

async def _to_person(w, person_id: int, text: str = "тест tg-myperson R2, не отвечать", headers=None):
    return await w.http.post("/api/v1/messages/to-person", headers=headers or w.env.h(),
                             json={"person_id": person_id, "text": text})


async def test_max_send_to_person_new_dialog_once_and_listed(mx):
    person = (await _lookup(mx)).json()["person_id"]
    chat = ME ^ PEER
    resp = await _to_person(mx, person)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {k: body[k] for k in ("status", "person_id", "method", "chat_id")} == {
        "status": "sent", "person_id": person, "method": "direct", "chat_id": chat}
    assert mx.env.server.writes[-1][:3] == ("send_message", chat, "тест tg-myperson R2, не отвечать")
    listed = (await mx.http.get("/api/v1/messages", headers=mx.env.h(), params={"chat_id": chat})).json()["items"]
    assert [m["message_id"] for m in listed] == [body["message_id"]] and listed[0]["is_outgoing"] is True

    mx.clock.at(60)
    assert (await _to_person(mx, person, "второе")).status_code == 200
    sends = [e for e in _events(mx.id) if e[0] == "send"]
    assert [(e[1], e[2], e[3]) for e in sends] == [("send_to_person", chat, True), ("send_to_person", chat, False)]
    status = (await mx.http.get(f"/api/v1/accounts/{mx.id}/write-limits", headers=ADMIN_KEY)).json()
    assert status["counters"]["new_dialogs_last_day"] == 1


async def test_max_send_to_person_reply_is_not_a_new_dialog(mx):
    person = (await _lookup(mx)).json()["person_id"]
    chat = ME ^ PEER
    await mx.client.push({"opcode": 128, "cmd": 0, "payload": {"chatId": chat, "message": {
        "id": "5", "time": 1_760_000_000_000, "type": "USER", "text": "привет", "attaches": [], "sender": PEER}}})
    assert (await _to_person(mx, person)).status_code == 200
    assert [e[3] for e in _events(mx.id) if e[0] == "send"] == [False]


async def test_max_send_to_person_errors_and_slot(mx):
    person = (await _lookup(mx)).json()["person_id"]
    mx.env.server.write_errors = [ApiError(opcode=64, error="chat.denied", message="denied")]
    resp = await _to_person(mx, person)
    assert resp.status_code == 502, resp.text
    assert [e for e in _events(mx.id) if e[0] == "send"] == []  # given back
    mx.env.server.write_errors = [TimeoutError("no answer")]
    resp = await _to_person(mx, person)
    assert resp.status_code == 504 and "may have been sent" in resp.text
    assert len([e for e in _events(mx.id) if e[0] == "send"]) == 1  # kept: it may be out


async def test_max_send_to_person_refusals(mx, aliases):  # noqa: F811
    assert (await _to_person(mx, 999_999_999)).status_code == 404
    person = (await _lookup(mx)).json()["person_id"]
    sql("UPDATE accounts SET external_user_id = NULL WHERE id = %s", (mx.id,))
    try:
        resp = await _to_person(mx, person)
        assert resp.status_code == 409 and "identity unknown" in resp.text
    finally:
        sql("UPDATE accounts SET external_user_id = %s WHERE id = %s", (ME, mx.id))
    sql("UPDATE accounts SET mode = 'ro' WHERE id = %s", (mx.id,))
    mw._mode_cache.clear()
    assert (await _to_person(mx, person)).status_code == 403
    assert [w for w in mx.env.server.writes if w[0] == "send_message"] == []


# ---------------------------------------------------------------------------
# QA-R2-D1: MCP error texts of contact_by_phone carry no full number
# ---------------------------------------------------------------------------

def test_mask_phones_in_text():
    from app.utils.phone import mask_phones_in_text

    assert mask_phones_in_text("Input validation error: 79278975956 is not of type 'string'") == \
        "Input validation error: +7******5956 is not of type 'string'"
    assert mask_phones_in_text("bad +7 (927) 897-59-56 and ['+79001234567']") == \
        "bad +7******5956 and ['+7******4567']"
    assert mask_phones_in_text("chat 12345, id 117461068428223441") == "chat 12345, id 117461068428223441"


async def test_mcp_errors_of_contact_by_phone_are_masked(mx, monkeypatch):
    import app.main as main_module
    from app.authz import keys
    from tests.test_mcp_http import session

    _, key = await keys.create_key(scope="account", account_id=mx.id, label="mcp-phone-mask")
    mx.env.server.phone_book.clear()
    mx.env.server.lookup_errors = [ApiError(opcode=46, error="search.failed",
                                            message="no contact for +7 927 897-59-56 (79278975956)")]
    try:
        async with session(main_module.app, key) as s:
            texts = [(await s.call_tool("contact_by_phone", args)).content[0].text for args in (
                {"phone": 79278975956}, {"phone": ["+79001234567"]}, {"phone": PHONE, "refresh": True})]
    finally:
        sql("DELETE FROM api_keys WHERE label = 'mcp-phone-mask'")
    for _ in range(100):
        rows = sql("SELECT status, error FROM audit_logs WHERE alias = %s AND tool = 'contact_by_phone'"
                   " ORDER BY id", (mx.env.alias,), fetch=True)
        if len(rows) >= 3:
            break
        await asyncio.sleep(0.05)
    assert [r[0] for r in rows] == ["error"] * 3
    assert texts[0].startswith("Input validation error") and MASKED in texts[0]
    assert "+7******4567" in texts[1]
    assert texts[2].startswith("HTTP 502") and MASKED in texts[2]
    blob = " ".join(texts) + repr(rows)
    for number in ("79278975956", "79001234567", "927 897", "897-59-56"):
        assert number not in blob, number


def test_mask_phones_in_text_two_numbers_in_a_row():
    from app.utils.phone import mask_phones_in_text

    assert mask_phones_in_text("no contact for +7 927 897-59-56 (79278975956)") == \
        "no contact for +7******5956 (+7******5956)"
