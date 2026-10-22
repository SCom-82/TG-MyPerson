"""MAX release 5, PR R5-3: scheduled messages (plan 2026-10-22 Р5 Р-73; QA R5-19, R5-21, R5-22, R5-24).

send_message with schedule_date → MSG_SEND with message.delayedAttributes (PyMax's own frame);
list / cancel / send now — CHAT_HISTORY / MSG_DELETE / MSG_EDIT with itemType DELAYED, the
web client's frames. MAX_SCHEDULED_ENABLED=false switches all of it back to 501.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pymax import ApiError
from pymax.api.messages.service import MessageService
from pymax.protocol import Opcode

import app.authz.middleware as mw
from app.max import frames, normalize
from app.max.config import max_settings
from app.mcp import tools as mcp_tools
from tests.isolation_env import sql
from tests.max_env import API_KEY, aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_bots import _frames
from tests.test_max_groups import _get
from tests.test_max_ingest import DIALOG, ME
from tests.test_max_write_limits import _events, clock, rw  # noqa: F401 — pytest fixtures
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture


def _later(minutes: int = 5) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


async def _schedule(w, text: str = "позже", **extra):
    return await w.http.post("/api/v1/messages", headers=w.env.h(),
                             json={"chat_id": DIALOG, "text": text, "schedule_date": _later(), **extra})


def _sends(w) -> list[tuple]:
    return [e for e in _events(w.id) if e[0] == "send"]


async def test_schedule_list_cancel_send_now(rw):
    resp = await _schedule(rw, "напомнить")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "scheduled" and body["schedule_date"].startswith(_later()[:13])
    frame = _frames(rw, int(Opcode.MSG_SEND))[-1]
    assert (frame["chatId"], frame["message"]["text"], frame["message"]["attaches"]) == (DIALOG, "напомнить", [])
    assert frame["message"]["delayedAttributes"]["notifySender"] is True
    assert len(_sends(rw)) == 1  # the send is counted when scheduled
    # not stored as sent: the agent does not see it before MAX sends it
    assert (await _get(rw, f"/messages/{DIALOG}/{body['message_id']}")).status_code == 404

    listed = (await _get(rw, "/messages/scheduled", chat_id=DIALOG)).json()
    assert listed["total"] == 1 and listed["items"][0]["message_id"] == body["message_id"]
    assert listed["items"][0]["text"] == "напомнить" and listed["items"][0]["date"] is not None
    assert _frames(rw, int(Opcode.CHAT_HISTORY))[-1] == {"chatId": DIALOG, "from": 1, "forward": 100,
                                                         "getMessages": True, "itemType": "DELAYED"}

    resp = await rw.http.post(f"/api/v1/messages/scheduled/{DIALOG}/{body['message_id']}/send-now",
                              headers=rw.env.h())
    assert resp.json() == {"status": "sent", "chat_id": DIALOG, "message_id": body["message_id"]}, resp.text
    frame = _frames(rw, int(Opcode.MSG_EDIT))[-1]
    assert (frame["messageId"], frame["text"], set(frame["delayedAttributes"])) == (
        body["message_id"], "напомнить", {"timeToFire"})
    assert len(_sends(rw)) == 1  # send now is not a second send: an action slot (review 19 R19-1)
    assert [e[1] for e in _events(rw.id) if e[0] == "action"] == ["send_scheduled_now"]
    assert (await _get(rw, "/messages/scheduled", chat_id=DIALOG)).json()["total"] == 0
    resp = await rw.http.post(f"/api/v1/messages/scheduled/{DIALOG}/{body['message_id']}/send-now",
                              headers=rw.env.h())
    assert resp.status_code == 404

    second = (await _schedule(rw, "отменить")).json()
    resp = await rw.http.delete(f"/api/v1/messages/scheduled/{DIALOG}/{second['message_id']}", headers=rw.env.h())
    assert resp.json() == {"status": "cancelled", "chat_id": DIALOG, "message_id": second["message_id"]}
    assert _frames(rw, int(Opcode.MSG_DELETE))[-1] == {"chatId": DIALOG, "messageIds": [second["message_id"]],
                                                       "forMe": False, "itemType": "DELAYED"}
    assert [e[1] for e in _events(rw.id) if e[0] == "action"] == ["send_scheduled_now", "cancel_scheduled"]


async def test_schedule_date_bounds_and_media_stay_501(rw):
    for when in (datetime.now(timezone.utc) + timedelta(seconds=30),
                 datetime.now(timezone.utc) + timedelta(days=366),
                 datetime(2026, 1, 1, tzinfo=timezone.utc)):
        resp = await rw.http.post("/api/v1/messages", headers=rw.env.h(),
                                  json={"chat_id": DIALOG, "text": "x", "schedule_date": when.isoformat()})
        assert resp.status_code == 422, resp.text
    unix = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    resp = await rw.http.post("/api/v1/messages", headers=rw.env.h(),
                              json={"chat_id": DIALOG, "text": "x", "schedule_date": unix})
    assert resp.status_code == 200 and resp.json()["status"] == "scheduled", resp.text
    assert len(_sends(rw)) == 1
    resp = await rw.http.post("/api/v1/messages/send-file", headers=rw.env.h(),
                              data={"chat_id": str(DIALOG), "schedule_date": _later()},
                              files={"file": ("a.pdf", b"%PDF", "application/pdf")})
    assert resp.status_code == 501 and resp.json()["parameter"] == "schedule_date"


async def test_errors_and_slots(rw):
    rw.env.server.group_errors = [ApiError(opcode=64, error="delayed.disabled", message="scheduled messages off")]
    resp = await _schedule(rw)
    assert resp.status_code == 502 and "scheduled messages off" in resp.text and _sends(rw) == []
    rw.env.server.group_errors = [TimeoutError("no answer")]
    resp = await _schedule(rw)
    assert resp.status_code == 504 and "list_scheduled" in resp.text and len(_sends(rw)) == 1
    rw.env.server.group_errors = [ApiError(opcode=66, error="x", message="refused")]
    resp = await rw.http.delete(f"/api/v1/messages/scheduled/{DIALOG}/1", headers=rw.env.h())
    assert resp.status_code == 502 and [e for e in _events(rw.id) if e[0] == "action"] == []


async def test_switch_off(rw, monkeypatch):
    monkeypatch.setattr(max_settings, "scheduled_enabled", False)
    resp = await _schedule(rw)
    assert resp.status_code == 501 and resp.json()["parameter"] == "schedule_date"
    for method, path in (("GET", f"/messages/scheduled?chat_id={DIALOG}"),
                         ("DELETE", f"/messages/scheduled/{DIALOG}/1"),
                         ("POST", f"/messages/scheduled/{DIALOG}/1/send-now")):
        resp = await rw.http.request(method, "/api/v1" + path, headers=rw.env.h())
        assert resp.status_code == 501 and "MAX_SCHEDULED_ENABLED" in resp.text, path
    assert _frames(rw, int(Opcode.MSG_SEND)) == [] and _events(rw.id) == []
    names = {t.name for t in mcp_tools.visible(mcp_tools.Caller("account", rw.id, rw.env.alias, "max", "rw",
                                                                "http://test"))}
    assert not names & mcp_tools.SCHEDULED_TOOLS and len(names) == 77
    tools = (await rw.http.get("/api/v1/tools", headers=rw.env.h())).json()["tools"]
    assert "list_scheduled" not in tools
    tg = {t.name for t in mcp_tools.visible(mcp_tools.Caller("account", 1, "x", "telegram", "rw", "http://test"))}
    assert mcp_tools.SCHEDULED_TOOLS <= tg  # TG untouched


async def test_ro_and_mcp(rw):
    sql("UPDATE accounts SET mode = 'ro' WHERE id = %s", (rw.id,))
    mw._mode_cache.clear()
    assert (await _schedule(rw)).status_code == 403
    for method, path in (("DELETE", f"/messages/scheduled/{DIALOG}/1"),
                         ("POST", f"/messages/scheduled/{DIALOG}/1/send-now")):
        assert (await rw.http.request(method, "/api/v1" + path, headers=rw.env.h())).status_code == 403
    assert (await _get(rw, "/messages/scheduled", chat_id=DIALOG)).status_code == 200
    assert _frames(rw, int(Opcode.MSG_SEND)) == [] and _events(rw.id) == []
    for _ in range(100):
        denied = sql("SELECT tool FROM audit_logs WHERE alias = %s AND status = 'denied'", (rw.env.alias,), fetch=True)
        if len(denied) >= 3:
            break
        await asyncio.sleep(0.05)
    assert sorted(t for (t,) in denied) == ["cancel_scheduled", "send_message", "send_scheduled_now"]
    schema = mcp_tools.visible(mcp_tools.Caller("account", rw.id, rw.env.alias, "max", "rw", "http://test"))
    send = next(t for t in schema if t.name == "send_message")
    assert "MAX: text only" in send.schema["properties"]["schedule_date"]["description"]


async def test_ingest_skips_a_message_until_it_fires(rw):
    fire = int((datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp() * 1000)
    msg = {"id": 31, "time": fire - 600_000, "type": "USER", "text": "позже", "sender": ME,
           "delayedAttributes": {"timeToFire": fire}}
    await rw.client.push({"opcode": 128, "cmd": 0, "payload": {"chatId": DIALOG, "message": msg}})
    assert (await _get(rw, f"/messages/{DIALOG}/31")).status_code == 404
    sent = {**msg, "delayedAttributes": {"timeToFire": int(datetime.now(timezone.utc).timestamp() * 1000)}}
    await rw.client.push({"opcode": 128, "cmd": 0, "payload": {"chatId": DIALOG, "message": sent}})
    assert (await _get(rw, f"/messages/{DIALOG}/31")).json()["text"] == "позже"
    assert normalize.is_delayed({"message": {"delayedAttributes": {"timeToFire": "x"}}}, 0) is False


# ---------------------------------------------------------------------------
# Canaries (R5-24): PyMax's 64 with send_at, our 49 / 66 / 67 against the appendix
# ---------------------------------------------------------------------------

class Sent(Exception):
    pass


async def test_pymax_send_at_frame_is_the_clients():
    sent = []

    async def invoke(opcode, payload):
        sent.append((int(opcode), payload))
        raise Sent()

    service = MessageService(SimpleNamespace(invoke=invoke))
    when = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
    with pytest.raises(Sent):
        await service.send_message(5, "t", send_at=when)
    opcode, payload = sent[0]
    # 64 message Js: delayedAttributes qs {timeToFire V, notifySender F?}
    assert opcode == int(Opcode.MSG_SEND)
    assert payload["message"]["delayedAttributes"] == {"timeToFire": int(when.timestamp() * 1000), "notifySender": True}


def test_delayed_frames_match_the_client_schemas():
    # 49: chatId z, from V?, forward B?, getMessages F?, itemType Ac (REGULAR | DELAYED)
    assert frames.list_delayed_frame(5) == {"chatId": 5, "from": 1, "forward": 100, "getMessages": True,
                                            "itemType": "DELAYED"}
    # 66: chatId z, messageIds I(z), forMe F?, itemType Ac
    assert frames.cancel_delayed_frame(5, [1]) == {"chatId": 5, "messageIds": [1], "forMe": False,
                                                   "itemType": "DELAYED"}
    # 67: chatId z, messageId z, text P?, elements I(gc)?, delayedAttributes qs
    frame = frames.fire_now_frame(5, {"id": "7", "text": "a_b", "elements": [{"type": "STRONG"}]}, 1000)
    assert frame == {"chatId": 5, "messageId": 7, "text": "a_b", "elements": [{"type": "STRONG"}],
                     "delayedAttributes": {"timeToFire": 1000}}
    assert set(frames.fire_now_frame(5, {"id": 7}, 1)) == {"chatId", "messageId", "delayedAttributes"}
    assert frames.delayed_attrs(5) == {"timeToFire": 5, "notifySender": True}



async def test_send_now_stops_with_writes(rw):
    """review 19 R19-1: the write stop holds send now."""
    from tests.test_max_write_limits import _limits

    mid = (await _schedule(rw, "текст")).json()["message_id"]
    await _limits(rw, enabled=False)
    resp = await rw.http.post(f"/api/v1/messages/scheduled/{DIALOG}/{mid}/send-now", headers=rw.env.h())
    assert resp.status_code == 403, resp.text
    assert _frames(rw, int(Opcode.MSG_EDIT)) == []
