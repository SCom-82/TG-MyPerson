"""MAX writes (PR-7) and per-account write limits (brief 2026-10-17).

The fake MAX server of the MAX suites, STORAGE_MODE=new (the account's
partitions). The clock, the pause and the jitter of app/authz/write_limits.py
are replaced: windows and Retry-After are checked to the second, waits are
recorded instead of slept.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from pymax import ApiError

import app.authz.middleware as mw
import app.max.pool as max_pool_module
from app.authz import tool_catalog, write_limits
from app.max.pool import MaxPool
from app.mcp import tools as mcp_tools
from tests.isolation_env import sql
from tests.max_env import ADMIN_KEY, API_KEY, aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_ingest import DIALOG, GROUP, _authorized
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture

T0 = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
OFF = {k: None for k in ("per_minute", "per_hour", "per_day", "new_dialogs_per_day", "min_interval_sec",
                         "jitter_sec", "actions_per_hour")}


class Clock:
    def __init__(self) -> None:
        self.t = T0
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.t

    def at(self, seconds: float) -> None:
        self.t = T0 + timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 3))
        self.t += timedelta(seconds=seconds)


@pytest_asyncio.fixture
async def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(write_limits, "_now", c.now)
    monkeypatch.setattr(write_limits, "_sleep", c.sleep)
    monkeypatch.setattr(write_limits, "_jitter", lambda upper: 0.0)
    return c


@pytest_asyncio.fixture
async def rw(new_env, clock):  # noqa: F811
    sql("UPDATE accounts SET mode = 'rw' WHERE id = %s", (new_env.account_id,))
    mw._mode_cache.clear()
    client = await _authorized(new_env)
    return SimpleNamespace(env=new_env, http=new_env.client, client=client, clock=clock, id=new_env.account_id)


async def _limits(w, **changes) -> dict:
    resp = await w.http.patch(f"/api/v1/accounts/{w.id}/write-limits", headers=ADMIN_KEY, json=changes)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _status(w) -> dict:
    resp = await w.http.get(f"/api/v1/accounts/{w.id}/write-limits", headers=ADMIN_KEY)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _send(w, chat: int = DIALOG, text: str = "привет", **extra):
    return await w.http.post("/api/v1/messages", headers=w.env.h(), json={"chat_id": chat, "text": text, **extra})


async def _react(w, chat: int, message_id: int, emoticon: str | None = "👍"):
    return await w.http.post(f"/api/v1/messages/{chat}/{message_id}/react", headers=w.env.h(),
                             json={"emoticon": emoticon})


def _events(account_id: int) -> list[tuple]:
    return sql("SELECT kind, tool, chat_id, new_dialog, at FROM write_events WHERE account_id = %s ORDER BY id",
               (account_id,), fetch=True)


# ---------------------------------------------------------------------------
# ro / rw, what is sent is stored
# ---------------------------------------------------------------------------

async def test_ro_account_cannot_write(new_env, clock):  # noqa: F811
    client = await _authorized(new_env)
    resp = await new_env.client.post("/api/v1/messages", headers=new_env.h(), json={"chat_id": DIALOG, "text": "x"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["mode"] == "ro"
    assert new_env.server.writes == [] and "send_message" not in client.calls
    assert _events(new_env.account_id) == []


async def test_send_message_is_stored_and_listed_at_once(rw):
    resp = await _send(rw, text="первое")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"status": "sent", "message_id": body["message_id"], "chat_id": DIALOG, "schedule_date": None}
    assert rw.env.server.writes == [("send_message", DIALOG, "первое", None, [], [])]

    listed = (await rw.http.get(f"/api/v1/messages?chat_id={DIALOG}", headers=rw.env.h())).json()["items"]
    assert [(m["message_id"], m["text"], m["is_outgoing"]) for m in listed] == [(body["message_id"], "первое", True)]

    status = await _status(rw)
    assert status["source"] == "default" and status["limits"] == write_limits.DEFAULT_LIMITS.as_dict()
    assert status["counters"]["sends_last_minute"] == 1 and status["counters"]["new_dialogs_last_day"] == 1
    assert status["remaining"]["per_hour"] == 19 and status["remaining"]["new_dialogs_per_day"] == 9


async def test_parameters_without_max_counterpart_501(rw):
    resp = await _send(rw, schedule_date="2026-10-18T10:00:00Z")  # scheduled text: Р5 Р-73, the past → 422
    assert resp.status_code == 422 and "schedule_date" in resp.text, resp.text
    resp = await rw.http.post("/api/v1/messages/send-file", headers=rw.env.h(),
                              data={"chat_id": str(DIALOG), "schedule_date": "2026-10-18T10:00:00Z"},
                              files={"file": ("v.ogg", b"OggS", "audio/ogg")})
    assert resp.status_code == 501 and resp.json()["parameter"] == "schedule_date", resp.text
    resp = await rw.http.post("/api/v1/messages/edit", headers=rw.env.h(),
                              json={"chat_id": DIALOG, "message_id": 1, "text": "x", "scheduled": True})
    assert resp.status_code == 501 and resp.json()["parameter"] == "scheduled", resp.text
    resp = await rw.http.post("/api/v1/messages/poll", headers=rw.env.h(),
                              json={"chat_id": DIALOG, "question": "q", "options": ["a", "b"], "quiz_correct_option": 1})
    assert resp.status_code == 501 and resp.json()["parameter"] == "quiz_correct_option", resp.text
    assert rw.env.server.writes == [] and _events(rw.id) == []


async def test_send_file_as_photo_or_file(rw):
    await _limits(rw, **OFF)
    h = rw.env.h()
    for name, extra in (("a.PNG", {}), ("report.pdf", {}), ("b.jpg", {"force_document": "true"})):
        resp = await rw.http.post("/api/v1/messages/send-file", headers=h,
                                  data={"chat_id": str(GROUP), "caption": "подпись", **extra},
                                  files={"file": (name, b"\x89PNG data", "application/octet-stream")})
        assert resp.status_code == 200, resp.text
    assert [(w[1], w[2], w[4], w[5]) for w in rw.env.server.writes] == [
        (GROUP, "подпись", ["Photo"], ["a.PNG"]),
        (GROUP, "подпись", ["File"], ["report.pdf"]),
        (GROUP, "подпись", ["File"], ["b.jpg"]),
    ]


async def test_forward_edit_react_delete_mark_read(rw):
    await _limits(rw, **OFF)
    h = rw.env.h()
    sent = (await _send(rw, text="исходное")).json()["message_id"]

    resp = await rw.http.post("/api/v1/messages/edit", headers=h,
                              json={"chat_id": DIALOG, "message_id": sent, "text": "исправлено"})
    assert resp.status_code == 200, resp.text
    msg = (await rw.http.get(f"/api/v1/messages/{DIALOG}/{sent}", headers=h)).json()
    assert (msg["text"], msg["is_edited"]) == ("исправлено", True)

    assert (await _react(rw, DIALOG, sent)).json()["status"] == "reacted"
    assert (await _react(rw, DIALOG, sent, None)).json()["status"] == "unreacted"

    resp = await rw.http.post("/api/v1/messages/forward", headers=h,
                              json={"from_chat_id": DIALOG, "to_chat_id": GROUP, "message_ids": [sent]})
    assert resp.status_code == 200, resp.text
    [fwd] = resp.json()["message_ids"]
    listed = (await rw.http.get(f"/api/v1/messages?chat_id={GROUP}", headers=h)).json()["items"]
    assert [m["message_id"] for m in listed] == [fwd]

    resp = await rw.http.post(f"/api/v1/chats/{DIALOG}/read", headers=h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["message_id"] == sent

    resp = await rw.http.delete(f"/api/v1/messages/{DIALOG}/{sent}", headers=h)
    assert resp.status_code == 200 and resp.json()["for_all"] is True, resp.text
    msg = (await rw.http.get(f"/api/v1/messages/{DIALOG}/{sent}", headers=h)).json()
    assert msg["deleted_at"] is not None

    assert [w[0] for w in rw.env.server.writes] == [
        "send_message", "edit_message", "add_reaction", "remove_reaction", "forward_message",
        "read_message", "delete_message",
    ]
    assert rw.env.server.writes[2] == ("add_reaction", DIALOG, sent, "👍")
    assert rw.env.server.writes[5] == ("read_message", sent, DIALOG)  # a number on web too (MAX rejects a str id)
    assert rw.env.server.writes[6] == ("delete_message", DIALOG, [sent], False)
    counters = (await _status(rw))["counters"]
    assert (counters["sends_last_hour"], counters["actions_last_hour"]) == (2, 5)


async def test_forward_batch_stopped_by_a_limit_reports_what_went_out(rw):
    await _limits(rw, **{**OFF, "per_minute": 2})
    first = (await _send(rw)).json()["message_id"]
    resp = await rw.http.post("/api/v1/messages/forward", headers=rw.env.h(),
                              json={"from_chat_id": DIALOG, "to_chat_id": GROUP, "message_ids": [first, 7, 8]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "partial" and len(body["message_ids"]) == 1
    assert body["not_forwarded"] == [7, 8] and body["stopped_by"]["limit"] == "per_minute"


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

def _check_429(resp, limit: str, used: int, retry_after: int) -> dict:
    assert resp.status_code == 429, resp.text
    body = resp.json()
    assert resp.headers["retry-after"] == str(retry_after)
    assert (body["error"], body["limit"], body["used"], body["remaining"], body["retry_after"]) == (
        "rate limit", limit, used, 0, retry_after)
    assert f"retry in {retry_after} s" in body["message"]
    return body


@pytest.mark.parametrize("limit,seconds", [("per_minute", 60), ("per_hour", 3600), ("per_day", 86400)])
async def test_send_windows_429_with_retry_after(rw, limit, seconds):
    await _limits(rw, **{**OFF, limit: 2})
    step = seconds // 10
    for i in range(2):
        rw.clock.at(i * step)
        assert (await _send(rw, chat=DIALOG + i)).status_code == 200
    rw.clock.at(2 * step)
    _check_429(await _send(rw, chat=DIALOG + 5), limit, 2, seconds - 2 * step)
    rw.clock.at(seconds + 1)  # the first send has left the window
    assert (await _send(rw, chat=DIALOG + 5)).status_code == 200
    assert len(rw.env.server.writes) == 3


async def test_new_dialogs_counted_once_per_chat(rw):
    await _limits(rw, **{**OFF, "new_dialogs_per_day": 2})
    assert (await _send(rw, chat=1001)).status_code == 200
    rw.clock.at(600)
    assert (await _send(rw, chat=1002)).status_code == 200
    rw.clock.at(1200)
    _check_429(await _send(rw, chat=1003), "new_dialogs_per_day", 2, 86400 - 1200)
    for _ in range(3):  # chats we already wrote to are not new
        assert (await _send(rw, chat=1001)).status_code == 200
    assert [e[3] for e in _events(rw.id)] == [True, True, False, False, False]


def _frame(chat: int, message_id: int, sender: int, text: str) -> dict:
    return {"opcode": 128, "cmd": 0, "payload": {"chatId": chat, "message": {
        "id": str(message_id), "time": 1_760_000_000_000, "type": "USER", "text": text, "attaches": [],
        "sender": sender}}}


async def test_new_dialog_is_a_chat_without_any_history(rw):
    """Only a cold first message is new (owner's clarification 17.10): a reply to someone
    who wrote first, or a chat with our own earlier message, is not counted."""
    await _limits(rw, **{**OFF, "new_dialogs_per_day": 0})
    await rw.client.push(_frame(2001, 77, 100, "давнее наше"))  # outgoing (sender = ME)
    await rw.client.push(_frame(2003, 78, 555, "здравствуйте"))  # incoming only: they wrote first
    assert (await _send(rw, chat=2001)).status_code == 200
    assert (await _send(rw, chat=2003)).status_code == 200
    resp = await _send(rw, chat=2002)  # limit 0: new dialogs are off, no Retry-After (W-D2)
    assert resp.status_code == 429 and "retry-after" not in resp.headers
    assert (resp.json()["limit"], resp.json()["retry_after"]) == ("new_dialogs_per_day", None)
    assert resp.json()["message"] == f"new dialogs of account '{rw.env.alias}' are off: new_dialogs_per_day = 0"
    assert [e[3] for e in _events(rw.id)] == [False, False]


async def test_actions_per_hour(rw):
    await _limits(rw, **{**OFF, "actions_per_hour": 2})
    sent = (await _send(rw)).json()["message_id"]
    rw.clock.at(10)
    assert (await _react(rw, DIALOG, sent)).status_code == 200
    rw.clock.at(20)
    assert (await _react(rw, DIALOG, sent, None)).status_code == 200
    rw.clock.at(30)
    _check_429(await rw.http.post(f"/api/v1/chats/{DIALOG}/read", headers=rw.env.h()), "actions_per_hour", 2, 3580)
    assert (await _send(rw)).status_code == 200  # sends have their own counters


async def test_min_interval_waits_then_refuses(rw, monkeypatch):
    await _limits(rw, **{**OFF, "min_interval_sec": 3, "jitter_sec": 2})
    monkeypatch.setattr(write_limits, "_jitter", lambda upper: 1.5)
    assert (await _send(rw)).status_code == 200
    assert (await _send(rw)).status_code == 200  # right after: waits 3 + 1.5 s, then sends
    assert rw.clock.sleeps == [4.5]
    assert [e[4] for e in _events(rw.id)] == [T0, T0 + timedelta(seconds=4.5)]

    await _limits(rw, min_interval_sec=20, jitter_sec=None)
    body = _check_429_min_interval(await _send(rw), retry_after=5)
    assert body["queued_wait_sec"] == 20
    assert rw.clock.sleeps == [4.5] and len(rw.env.server.writes) == 2


def _check_429_min_interval(resp, retry_after: int) -> dict:
    assert resp.status_code == 429, resp.text
    assert resp.headers["retry-after"] == str(retry_after)
    body = resp.json()
    assert (body["limit"], body["retry_after"]) == ("min_interval_sec", retry_after)
    return body


async def test_counters_survive_a_restart(rw, monkeypatch):
    await _limits(rw, **{**OFF, "per_hour": 2})
    for _ in range(2):
        assert (await _send(rw)).status_code == 200
    await rw.env.pool.stop_all()
    restarted = MaxPool(rw.env.settings, client_factory=rw.env.server.factory)
    monkeypatch.setattr(max_pool_module, "max_pool", restarted)
    try:
        await restarted.restart(rw.env.alias)
        session = await restarted.get(rw.env.alias)
        assert await session.wait_until(lambda: session.state == "authorized", 5.0), session.state
        rw.clock.at(60)
        _check_429(await _send(rw), "per_hour", 2, 3540)
    finally:
        await restarted.stop_all()



async def test_null_turns_a_limit_off_and_changes_apply_at_once(rw):
    await _limits(rw, **{**OFF, "per_minute": 1})
    assert (await _send(rw)).status_code == 200
    assert (await _send(rw)).status_code == 429
    status = await _limits(rw, per_minute=None)
    assert status["source"] == "account" and status["limits"]["per_minute"] is None
    for _ in range(3):
        assert (await _send(rw)).status_code == 200
    await _limits(rw, per_minute=10)
    assert (await _send(rw)).status_code == 200


async def test_enabled_false_stops_writes_without_mode_change(rw):
    await _limits(rw, enabled=False)
    resp = await _send(rw)
    assert resp.status_code == 403 and resp.json()["error"] == "writes disabled", resp.text
    assert sql("SELECT mode FROM accounts WHERE id = %s", (rw.id,), fetch=True) == [("rw",)]
    assert (await rw.http.get("/api/v1/messages", headers=rw.env.h())).status_code == 200  # reads go on
    await _limits(rw, enabled=True)
    assert (await _send(rw)).status_code == 200


async def test_failed_write_gives_its_slot_back(rw):
    await _limits(rw, **{**OFF, "per_minute": 1})
    rw.env.server.write_errors.append(ApiError(opcode=64, error="chat.not.found", message="Chat not found"))
    resp = await _send(rw)
    assert resp.status_code == 502, resp.text
    assert _events(rw.id) == []
    assert (await _send(rw)).status_code == 200


async def test_uncertain_failure_keeps_the_slot(rw):
    """No answer from MAX (timeout, dropped connection): the message may be out → counted."""
    await _limits(rw, **{**OFF, "per_minute": 1})
    rw.env.server.write_errors.append(TimeoutError("request timed out"))
    resp = await _send(rw)
    assert resp.status_code == 504 and "may have been sent" in resp.text, resp.text
    assert [e[1] for e in _events(rw.id)] == ["send_message"]
    _check_429(await _send(rw), "per_minute", 1, 60)


async def test_parallel_sends_do_not_drain_the_pool(rw):
    """Review R10-1: 25 parallel sends to 25 new chats with the default pool (5 + 10):
    no QueuePool timeout, quick, every call ends in ok or 429, no connection left out."""
    from app.database import engine

    await _limits(rw, **{**OFF, "per_minute": 10, "new_dialogs_per_day": 100})
    loop = asyncio.get_running_loop()
    started = loop.time()

    async def one(i: int):
        try:
            return await write_limits.reserve(rw.id, write_limits.SEND, "send_message", 5000 + i)
        except write_limits.WriteLimited as exc:
            return exc.status

    results = await asyncio.wait_for(asyncio.gather(*(one(i) for i in range(25))), 20)
    assert loop.time() - started < 10
    assert sum(isinstance(r, write_limits.Slot) for r in results) == 10
    assert results.count(429) == 15
    assert engine.pool.checkedout() == 0
    assert sum(e[3] for e in _events(rw.id)) == 10  # each a new dialog


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------

async def test_admin_write_limits_api(rw, aliases):  # noqa: F811
    tg_alias, tg_id = aliases("tg", platform="telegram", mode="rw")
    listed = {i["account_id"]: i for i in
              (await rw.http.get("/api/v1/accounts/write-limits", headers=ADMIN_KEY)).json()["items"]}
    assert listed[rw.id]["source"] == "default"
    assert (listed[tg_id]["source"], listed[tg_id]["limits"], listed[tg_id]["remaining"]) == ("none", None, None)

    assert (await rw.http.get(f"/api/v1/accounts/{rw.id}/write-limits", headers=API_KEY)).status_code == 401
    assert (await rw.http.get("/api/v1/accounts/999999999/write-limits", headers=ADMIN_KEY)).status_code == 404
    resp = await rw.http.patch(f"/api/v1/accounts/{rw.id}/write-limits", headers=ADMIN_KEY, json={"per_week": 1})
    assert resp.status_code == 422
    resp = await rw.http.patch(f"/api/v1/accounts/{rw.id}/write-limits", headers=ADMIN_KEY, json={"per_hour": -1})
    assert resp.status_code == 422
    resp = await rw.http.patch(f"/api/v1/accounts/{rw.id}/write-limits", headers=ADMIN_KEY, json={"enabled": None})
    assert resp.status_code == 422

    # The first change of a TG account does not bring MAX's defaults along (review 10).
    resp = await rw.http.patch(f"/api/v1/accounts/{tg_id}/write-limits", headers=ADMIN_KEY, json={"per_hour": 7})
    assert resp.json()["source"] == "account"
    assert resp.json()["limits"] == {**write_limits.NO_LIMITS.as_dict(), "per_hour": 7}

    status = await _limits(rw, per_hour=7)
    assert status["source"] == "account"
    assert status["limits"] == {**write_limits.DEFAULT_LIMITS.as_dict(), "per_hour": 7}
    resp = await rw.http.delete(f"/api/v1/accounts/{rw.id}/write-limits", headers=ADMIN_KEY)
    assert resp.json()["source"] == "default" and resp.json()["limits"]["per_hour"] == 20

    # The first PATCH of an account turning a limit off: NULL, not the column DEFAULT (013/014).
    off = {"phone_lookups_per_day": None, "groups_created_per_day": None, "group_invites_per_day": None}
    status = await _limits(rw, **off)
    assert {k: status["limits"][k] for k in off} == off


# ---------------------------------------------------------------------------
# Telegram: the same limits, off unless set
# ---------------------------------------------------------------------------

async def test_telegram_writes_unlimited_by_default_limited_by_setting(rw, aliases, monkeypatch):  # noqa: F811
    import app.api.messages as tg_messages

    tg_alias, tg_id = aliases("tgw", platform="telegram", mode="rw")
    sent = []

    class FakeTelethon:
        async def send_message(self, entity, message, **kw):
            sent.append((entity, message))
            return SimpleNamespace(id=len(sent))

    async def authorized_client(request):
        return FakeTelethon()

    monkeypatch.setattr(tg_messages, "require_authorized_client", authorized_client)
    h = {**API_KEY, "x-session-alias": tg_alias}
    for _ in range(25):
        resp = await rw.http.post("/api/v1/messages", headers=h, json={"chat_id": 5, "text": "tg"})
        assert resp.status_code == 200, resp.text
    assert _events(tg_id) == [] and (await _status_of(rw, tg_id))["source"] == "none"

    await rw.http.patch(f"/api/v1/accounts/{tg_id}/write-limits", headers=ADMIN_KEY, json={**OFF, "per_minute": 1})
    assert (await rw.http.post("/api/v1/messages", headers=h, json={"chat_id": 5, "text": "tg"})).status_code == 200
    resp = await rw.http.post("/api/v1/messages", headers=h, json={"chat_id": 5, "text": "tg"})
    _check_429(resp, "per_minute", 1, 60)
    assert resp.json()["platform"] == "telegram" and len(sent) == 26


# ---------------------------------------------------------------------------
# QA PR-7 W-D1…W-D3 (plan 2026-10-19 Р-12, QA R1-14)
# ---------------------------------------------------------------------------

async def test_wd1_the_longest_wait_decides(rw):
    await _limits(rw, **{**OFF, "per_minute": 1, "per_hour": 1})
    assert (await _send(rw)).status_code == 200
    rw.clock.at(1)
    body = _check_429(await _send(rw), "per_hour", 1, 3599)
    assert body["also"] == ["per_minute"]
    rw.clock.at(60)  # per_minute has passed, per_hour has not: the same answer, no surprise
    body = _check_429(await _send(rw), "per_hour", 1, 3540)
    assert body["also"] == []
    rw.clock.at(3600)
    assert (await _send(rw)).status_code == 200


async def test_wd2_limit_zero_is_off_without_retry_after(rw):
    await _limits(rw, **{**OFF, "per_minute": 1, "per_hour": 0})
    for _ in range(2):  # zero wins over a full per_minute too
        resp = await _send(rw)
        assert resp.status_code == 429, resp.text
        assert "retry-after" not in resp.headers
        body = resp.json()
        assert (body["limit"], body["max"], body["retry_after"]) == ("per_hour", 0, None)
        assert body["message"] == f"sends of account '{rw.env.alias}' are off: per_hour = 0"
    assert rw.env.server.writes == [] and _events(rw.id) == []

    await _limits(rw, **{**OFF, "actions_per_hour": 0})
    resp = await rw.http.post(f"/api/v1/chats/{DIALOG}/read", headers=rw.env.h())
    assert resp.status_code == 429 and "retry-after" not in resp.headers
    assert resp.json()["message"].endswith("are off: actions_per_hour = 0")

    with pytest.raises(mcp_tools.ToolError) as exc:
        await mcp_tools.BY_NAME["mark_chat_read"].call(_caller(rw), {"chat_id": DIALOG})
    assert "are off: actions_per_hour = 0" in str(exc.value) and '"retry_after": null' in str(exc.value)


async def test_wd3_mark_read_refused_before_max(rw):
    await _limits(rw, enabled=False)
    resp = await rw.http.post(f"/api/v1/chats/{DIALOG}/read", headers=rw.env.h())
    assert resp.status_code == 403, resp.text
    await _limits(rw, **{**OFF, "actions_per_hour": 1, "enabled": True})
    sent = (await _send(rw)).json()["message_id"]
    assert (await _react(rw, DIALOG, sent)).status_code == 200
    resp = await rw.http.post(f"/api/v1/chats/{DIALOG}/read", headers=rw.env.h())
    assert resp.status_code == 429, resp.text
    assert "fetch_history" not in rw.client.calls and rw.env.server.history_calls == []


@pytest.mark.parametrize("error,status", [(TimeoutError("timed out"), 504), (ConnectionError("dropped"), 504),
                                          (ApiError(opcode=49, error="chat.not.found", message="x"), 502)])
async def test_wd3_failed_history_read_gives_the_slot_back(rw, error, status):
    await _limits(rw, **{**OFF, "actions_per_hour": 5})
    rw.env.server.history[DIALOG] = [{"id": "1", "time": 1, "type": "USER", "text": "x", "attaches": [], "sender": 200}]
    rw.env.server.history_errors[DIALOG] = [error]
    resp = await rw.http.post(f"/api/v1/chats/{DIALOG}/read", headers=rw.env.h())
    assert resp.status_code == status, resp.text
    assert "may have been sent" not in resp.text
    if status == 504:
        assert "no answer from MAX (read)" in resp.text
    assert _events(rw.id) == [] and (await _status(rw))["counters"]["actions_last_hour"] == 0
    assert "read_message" not in rw.client.calls

    resp = await rw.http.post(f"/api/v1/chats/{DIALOG}/read", headers=rw.env.h())  # the next one works
    assert resp.status_code == 200, resp.text
    assert [e[0] for e in _events(rw.id)] == ["action"]


async def test_wd3_empty_chat_404_gives_the_slot_back(rw):
    await _limits(rw, **{**OFF, "actions_per_hour": 5})
    resp = await rw.http.post("/api/v1/chats/424242/read", headers=rw.env.h())
    assert resp.status_code == 404 and resp.json()["detail"] == "Chat has no messages"
    assert _events(rw.id) == []


# ---------------------------------------------------------------------------
# Bundles: create_group = 1 group + n invites under one lock (plan 2026-10-19 Р3 PR-G1, Р-23)
# ---------------------------------------------------------------------------

G, I = write_limits.GROUP_CREATE, write_limits.GROUP_INVITE


async def test_bundle_all_or_nothing(rw):
    await _limits(rw, group_invites_per_day=3)
    with pytest.raises(write_limits.WriteLimited) as exc:
        await write_limits.reserve_bundle(rw.id, "create_group", {G: 1, I: 5}, -1)
    body = exc.value.body
    assert (exc.value.status, body["limit"], body["used"], body["requested"]) == (429, "group_invites_per_day", 0, 5)
    assert body["retry_after"] is None and "never fit" in body["message"]  # 5 > 3: waiting does not help
    assert _events(rw.id) == []

    await _limits(rw, group_invites_per_day=30)
    slot = await write_limits.reserve_bundle(rw.id, "create_group", {G: 1, I: 5}, -1)
    assert sorted(e[0] for e in _events(rw.id)) == [G] + [I] * 5
    status = (await _status(rw))["counters"]
    assert (status["groups_created_last_day"], status["group_invites_last_day"]) == (1, 5)
    await slot.release()
    assert _events(rw.id) == []


async def test_bundle_waits_for_the_oldest_that_frees_enough(rw):
    await _limits(rw, group_invites_per_day=6)
    for i in range(3):
        rw.clock.at(i * 100)
        await write_limits.reserve_bundle(rw.id, "add_group_members", {I: 2}, -1)
    rw.clock.at(1000)
    with pytest.raises(write_limits.WriteLimited) as exc:
        await write_limits.reserve_bundle(rw.id, "add_group_members", {I: 3}, -1)
    # 6 used, 3 more need 3 to leave: the 3rd oldest (t=100) leaves at 86500
    assert exc.value.retry_after == 86400 + 100 - 1000 and exc.value.body["requested"] == 3


async def test_parallel_group_creations_take_exactly_the_limit(rw):
    results = await asyncio.gather(*(write_limits.reserve_bundle(rw.id, "create_group", {G: 1}, -i)
                                     for i in range(10)), return_exceptions=True)
    assert sum(isinstance(r, write_limits.Slot) for r in results) == 3
    assert all(isinstance(r, write_limits.WriteLimited) for r in results if not isinstance(r, write_limits.Slot))
    assert [e[0] for e in _events(rw.id)] == [G] * 3


async def test_group_limits_zero_null_delete_disabled(rw):
    await _limits(rw, groups_created_per_day=0)
    with pytest.raises(write_limits.WriteLimited) as exc:
        await write_limits.reserve_bundle(rw.id, "create_group", {G: 1}, -1)
    assert exc.value.retry_after is None and exc.value.response().headers.get("retry-after") is None
    assert exc.value.body["message"] == f"groups created of account '{rw.env.alias}' are off: groups_created_per_day = 0"

    await _limits(rw, groups_created_per_day=None, group_invites_per_day=None)
    for _ in range(5):
        await write_limits.reserve_bundle(rw.id, "create_group", {G: 1, I: 20}, -1)
    await rw.http.delete(f"/api/v1/accounts/{rw.id}/write-limits", headers=ADMIN_KEY)
    limits = (await _status(rw))["limits"]
    assert (limits["groups_created_per_day"], limits["group_invites_per_day"]) == (3, 30)

    await _limits(rw, enabled=False)
    with pytest.raises(write_limits.WriteLimited) as exc:
        await write_limits.reserve_bundle(rw.id, "remove_group_members", {write_limits.ACTION: 1}, -1)
    assert exc.value.status == 403


async def test_bundle_for_telegram_is_unlimited(rw, aliases):  # noqa: F811
    _, tg_id = aliases("tgb", platform="telegram", mode="rw")
    assert await write_limits.reserve_bundle(tg_id, "create_group", {G: 1, I: 20}) is None
    assert _events(tg_id) == []


async def _status_of(w, account_id: int) -> dict:
    return (await w.http.get(f"/api/v1/accounts/{account_id}/write-limits", headers=ADMIN_KEY)).json()


# ---------------------------------------------------------------------------
# MCP: the refusal as text, tools by platform (QA D-3)
# ---------------------------------------------------------------------------

def _caller(w, platform: str = "max", mode: str = "rw", account_id: int | None = None, alias: str | None = None):
    return mcp_tools.Caller("account", account_id or w.id, alias or w.env.alias, platform, mode, "http://test")


async def test_mcp_send_and_limit_text(rw):
    await _limits(rw, **{**OFF, "per_minute": 1})
    caller = _caller(rw)
    result = await mcp_tools.BY_NAME["send_message"].call(caller, {"chat_id": DIALOG, "text": "из MCP"})
    assert result["status"] == "sent"
    with pytest.raises(mcp_tools.ToolError) as exc:
        await mcp_tools.BY_NAME["send_message"].call(caller, {"chat_id": DIALOG, "text": "ещё"})
    text = str(exc.value)
    assert text.startswith("HTTP 429: write limit per_minute of account")
    assert "retry in 60 s" in text and '"retry_after": 60' in text


MAX_501_TOOLS = {"archive_chat"}  # block_user / unblock_user (Р-72), scheduled (Р-73): MAX since Р5
MAX_WRITES = {"send_message", "send_photo", "send_file", "create_upload", "forward_message", "edit_message",
              "delete_message", "react_to_message", "mark_chat_read", "contact_by_phone", "send_to_person",
              "leave_chat",
              # release 4 (plan 2026-10-19 Р4 Р-32): the TG tools served by MAX routes
              "send_album", "send_voice", "send_poll", "pin_message", "unpin_message", "join_chat",
              # MAX-only (Р4)
              "send_video_note", "vote_poll", "import_contacts", "remove_contact", "delete_dialog",
              "create_folder", "update_folder", "delete_folder"}


async def test_mcp_tools_depend_on_platform(rw):
    names = lambda c: {t.name for t in mcp_tools.visible(c)}  # noqa: E731
    max_rw, max_ro = names(_caller(rw)), names(_caller(rw, mode="ro"))
    assert MAX_WRITES <= max_rw and not MAX_501_TOOLS & max_rw
    assert not MAX_WRITES & max_ro and not MAX_501_TOOLS & max_ro and "list_messages" in max_ro
    tg_rw = names(_caller(rw, platform="telegram"))
    assert len(tg_rw) == 46 and MAX_501_TOOLS <= tg_rw and not tool_catalog.MAX_ONLY_TOOLS & tg_rw
    assert len(max_rw) == 77 and "reachable_contacts" in max_rw  # reach (Р-17) + groups (Р3) + Р4 + Р5 on MAX

    # Every tool shown to a MAX account has its route on MAX (no 501 by construction).
    for name in max_rw:
        assert mcp_tools.BY_NAME[name].route in mcp_tools.route_names("max"), name



async def test_available_tools_endpoint_for_the_stdio_wrapper(rw, aliases):  # noqa: F811
    resp = await rw.http.get("/api/v1/tools", headers=rw.env.h())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["platform"], body["mode"]) == ("max", "rw")
    assert set(body["tools"]) == {t.name for t in mcp_tools.visible(_caller(rw))}

    tg_alias, tg_id = aliases("tgt", platform="telegram", mode="ro")
    body = (await rw.http.get("/api/v1/tools", headers={**API_KEY, "x-session-alias": tg_alias})).json()
    assert body["platform"] == "telegram" and "send_message" not in body["tools"] and "list_scheduled" in body["tools"]
    assert set(body["tools"]) == {t.name for t in mcp_tools.ACCOUNT_TOOLS
                                  if not t.is_write and t.route not in tool_catalog.MAX_ONLY_TOOLS}


async def test_session_stop_waits_for_cancelled_jobs(new_env):  # noqa: F811
    """stop() returns after the background jobs ran their cleanup (rollback in a DB job)."""
    session = await new_env.session()
    cleaned = []

    async def job():
        try:
            await asyncio.sleep(3600)
        finally:
            await asyncio.sleep(0.05)  # e.g. the rollback of an open transaction
            cleaned.append(True)

    session.spawn_background("probe", job())
    await asyncio.sleep(0)
    await session.stop()
    assert cleaned == [True]


async def test_join_and_import_windows(rw):
    """Р4 Р-39: chats_joined_per_day 5 and contacts_imported_per_day 50, own counters; 0 switches them off."""
    status = await _status(rw)
    assert (status["limits"]["chats_joined_per_day"], status["limits"]["contacts_imported_per_day"]) == (5, 50)
    assert (status["counters"]["chats_joined_last_day"], status["counters"]["contacts_imported_last_day"]) == (0, 0)
    await write_limits.reserve_bundle(rw.id, "join_chat", {write_limits.CHAT_JOIN: 1})
    await write_limits.reserve_bundle(rw.id, "import_contacts", {write_limits.CONTACT_IMPORT: 48})
    with pytest.raises(write_limits.WriteLimited) as exc:
        await write_limits.reserve_bundle(rw.id, "import_contacts", {write_limits.CONTACT_IMPORT: 3})
    assert exc.value.body["limit"] == "contacts_imported_per_day" and exc.value.body["remaining"] == 2
    status = await _status(rw)
    assert (status["counters"]["chats_joined_last_day"], status["counters"]["contacts_imported_last_day"]) == (1, 48)
    assert status["remaining"]["chats_joined_per_day"] == 4

    await _limits(rw, chats_joined_per_day=0)
    with pytest.raises(write_limits.WriteLimited) as exc:
        await write_limits.reserve_bundle(rw.id, "join_chat", {write_limits.CHAT_JOIN: 1})
    assert exc.value.status == 429 and exc.value.retry_after is None and "chats joined" in exc.value.body["message"]
    rw.clock.at(86_401)
    assert (await _status(rw))["counters"]["contacts_imported_last_day"] == 0
    assert write_limits.NO_LIMITS.chats_joined_per_day is None and write_limits.NO_LIMITS.contacts_imported_per_day is None
