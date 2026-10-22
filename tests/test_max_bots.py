"""MAX release 5, PR R5-2: bots — keyboards and press_button, start_bot, get_bot_info, block /
unblock (plan 2026-10-22 Р5 Р-64…Р-66, Р-72; QA R5-13…R5-15, R5-18, R5-21…R5-24).
"""

import asyncio

from pymax import ApiError
from pymax.protocol import Opcode

import app.authz.middleware as mw
from app.max import frames
from app.repo import chats
from app.repo.rows import ChatRow
from app.storage import account_db
from tests.isolation_env import sql
from tests.max_env import API_KEY, aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_groups import FOREIGN, _calls, _foreign, _get, _post
from tests.test_max_ingest import ME
from tests.test_max_rights import BOT, _actions, _bot
from tests.test_max_write_limits import _events, clock, rw  # noqa: F401 — pytest fixtures
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture

KEYBOARD = {"_type": "INLINE_KEYBOARD", "keyboard": {"callbackId": "cb-1", "buttons": [
    [{"text": "Да", "type": "CALLBACK", "payload": "yes"}, {"text": "Сайт", "type": "LINK", "url": "https://x.ru"}],
    [{"text": "Телефон", "type": "REQUEST_CONTACT"}, {"text": "Чат", "type": "CHAT", "payload": "c"},
     {"text": "Ещё", "type": "MESSAGE"}]]}}


def _frames(w, opcode: int) -> list[dict]:
    return [c[2] for c in _calls(w, "invoke") if c[1] == opcode]


async def _bot_message(w, mid: int = 81, keyboard: dict = KEYBOARD) -> int:
    dialog = ME ^ BOT
    w.env.server.history.setdefault(dialog, []).append(
        {"id": mid, "time": 1_800_000_000_000, "type": "USER", "text": "Выберите", "sender": BOT,
         "attaches": [keyboard]})
    async with account_db(w.id) as db:
        await chats.upsert_chat(db, w.id, ChatRow(dialog, "private", title="Бот"))
        await db.commit()
    return dialog


# ---------------------------------------------------------------------------
# keyboards in messages (R5-13)
# ---------------------------------------------------------------------------

async def test_keyboard_is_in_the_message(rw):
    dialog = await _bot_message(rw)
    await rw.client.push({"opcode": 128, "cmd": 0, "payload": {
        "chatId": dialog, "message": rw.env.server.history[dialog][0]}})
    msg = (await _get(rw, f"/messages/{dialog}/81")).json()
    assert msg["message_type"] == "text" and msg["keyboard"]["callback_id"] == "cb-1"
    assert msg["keyboard"]["buttons"][0] == [{"text": "Да", "type": "CALLBACK", "url": None, "payload": "yes"},
                                             {"text": "Сайт", "type": "LINK", "url": "https://x.ru", "payload": None}]
    listed = (await _get(rw, "/messages", chat_id=dialog)).json()["items"]
    assert listed[0]["keyboard"]["buttons"][1][2]["type"] == "MESSAGE"


def test_keyboard_view_takes_callback_id_next_to_the_keyboard():
    from pymax.types.domain import Message

    msg = Message.model_validate({"id": 1, "time": 1, "type": "USER", "text": "", "attaches": [
        {"_type": "INLINE_KEYBOARD", "callbackId": 77, "keyboard": {"buttons": [[{"text": "a", "type": "MESSAGE"}]]}}]})
    assert frames.keyboard_view(msg) == {"callback_id": "77", "buttons": [[{"text": "a", "type": "MESSAGE"}]]}
    assert frames.keyboard_view(Message.model_validate({"id": 1, "time": 1, "type": "USER", "text": ""})) is None


# ---------------------------------------------------------------------------
# press_button (R5-13)
# ---------------------------------------------------------------------------

async def test_press_button_kinds(rw):
    dialog = await _bot_message(rw)
    rw.env.server.callback_answer = {"success": True, "message": {"id": 82, "time": 1_800_000_000_001,
                                                                  "type": "USER", "text": "Принято", "sender": BOT}}
    resp = await _post(rw, f"/messages/{dialog}/81/press", {"row": 0, "column": 0})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["success"], resp.json()["message"]["text"]) == (True, "Принято")
    frame = _frames(rw, int(Opcode.MSG_SEND_CALLBACK))[-1]
    assert (frame["callbackId"], frame["type"], frame["payload"]) == ("cb-1", "CALLBACK", "yes")
    assert isinstance(frame["timestamp"], int)
    assert (await _get(rw, f"/messages/{dialog}/82")).json()["text"] == "Принято"  # the answer is stored
    rw.env.server.callback_answer = None
    for row, column in ((1, 1), (1, 2)):
        assert (await _post(rw, f"/messages/{dialog}/81/press", {"row": row, "column": column})).status_code == 200
    assert [f["type"] for f in _frames(rw, 118)] == ["CALLBACK", "CHAT", "MESSAGE"]
    assert "payload" not in _frames(rw, 118)[-1]
    assert _actions(rw) == ["press_button"] * 3

    resp = await _post(rw, f"/messages/{dialog}/81/press", {"row": 0, "column": 1})
    assert resp.status_code == 422 and resp.json()["detail"]["url"] == "https://x.ru"
    resp = await _post(rw, f"/messages/{dialog}/81/press", {"row": 1, "column": 0})
    assert resp.status_code == 422 and "REQUEST_CONTACT" in resp.text
    for row, column in ((2, 0), (0, 2)):
        resp = await _post(rw, f"/messages/{dialog}/81/press", {"row": row, "column": column})
        assert resp.status_code == 422 and "no button" in resp.text
    assert (await _post(rw, f"/messages/{dialog}/81/press", {"row": -1, "column": 0})).status_code == 422
    assert len(_frames(rw, 118)) == 3 and len(_actions(rw)) == 3  # refusals: no frame, no slot

    await _bot_message(rw, 83, {"_type": "INLINE_KEYBOARD", "keyboard": {"buttons": [[{"text": "x", "type": "MESSAGE"}]]}})
    resp = await _post(rw, f"/messages/{dialog}/83/press", {"row": 0, "column": 0})
    assert resp.status_code == 422 and "callbackId" in resp.text
    rw.env.server.history[dialog].append({"id": 84, "time": 1, "type": "USER", "text": "без кнопок", "sender": BOT})
    assert (await _post(rw, f"/messages/{dialog}/84/press", {"row": 0, "column": 0})).status_code == 422
    assert (await _post(rw, f"/messages/{dialog}/99/press", {"row": 0, "column": 0})).status_code == 404


async def test_press_errors_and_slot(rw):
    dialog = await _bot_message(rw)
    rw.env.server.group_errors = [ApiError(opcode=118, error="callback.expired", message="expired")]
    resp = await _post(rw, f"/messages/{dialog}/81/press", {"row": 0, "column": 0})
    assert resp.status_code == 502 and _actions(rw) == []
    rw.env.server.group_errors = [TimeoutError("no answer")]
    resp = await _post(rw, f"/messages/{dialog}/81/press", {"row": 0, "column": 0})
    assert resp.status_code == 504 and _actions(rw) == ["press_button"]


# ---------------------------------------------------------------------------
# start_bot (R5-14)
# ---------------------------------------------------------------------------

async def test_start_bot_to_the_dialog(rw):
    _bot(rw)
    resp = await _post(rw, "/bots/start", {"bot_id": BOT, "start_payload": "ref42"})
    assert resp.status_code == 200, resp.text
    dialog = ME ^ BOT
    body = resp.json()
    assert (body["status"], body["chat_id"], body["bot_id"]) == ("started", dialog, BOT)
    frame = _frames(rw, int(Opcode.MSG_SEND))[-1]
    assert frame["chatId"] == dialog and "userId" not in frame
    assert frame["message"]["attaches"] == [{"_type": "CONTROL", "event": "botStarted", "startPayload": "ref42"}]
    events = [e for e in _events(rw.id) if e[0] == "send"]
    assert [(e[1], e[2], e[3]) for e in events] == [("start_bot", dialog, True)]  # a new dialog
    msgs = (await _get(rw, "/messages", chat_id=dialog)).json()["items"]
    assert [m["message_id"] for m in msgs] == [body["message_id"]]

    await _post(rw, "/bots/start", {"bot_id": BOT})
    assert [e[3] for e in _events(rw.id) if e[0] == "send"] == [True, False]  # the dialog has history now
    assert "startPayload" not in _frames(rw, int(Opcode.MSG_SEND))[-1]["message"]["attaches"][0]


async def test_start_bot_falls_back_to_user_id_and_refuses_people(rw):
    _bot(rw)
    rw.env.server.group_errors = [ApiError(opcode=64, error="chat.not.found", message="no such chat")]
    resp = await _post(rw, "/bots/start", {"bot_id": BOT})
    assert resp.status_code == 200, resp.text
    first, second = _frames(rw, int(Opcode.MSG_SEND))[-2:]
    assert (first.get("chatId"), second.get("userId"), "chatId" in second) == (ME ^ BOT, BOT, False)
    assert len([e for e in _events(rw.id) if e[0] == "send"]) == 1

    sends = len(_frames(rw, int(Opcode.MSG_SEND)))
    resp = await _post(rw, "/bots/start", {"bot_id": 5001})  # a person
    assert resp.status_code == 422 and "not a bot" in resp.text
    assert len(_frames(rw, int(Opcode.MSG_SEND))) == sends and len(_events(rw.id)) == 1
    assert (await _post(rw, "/bots/start", {"bot_id": BOT, "start_payload": "x" * 513})).status_code == 422


# ---------------------------------------------------------------------------
# get_bot_info (R5-15)
# ---------------------------------------------------------------------------

async def test_bot_info_by_bot_and_by_chat(rw):
    contact = {"id": BOT, "names": [{"firstName": "Бот"}], "options": ["BOT"], "link": "some_bot",
               "description": "Заказы"}
    rw.env.server.bot_infos[BOT] = {"contact": contact, "commands": [{"botId": BOT, "name": "start",
                                                                      "description": "Начать"}],
                                    "startMessage": {"text": {"text": "Привет!"}}}
    body = (await _get(rw, "/bots/info", bot_id=BOT)).json()
    assert body["commands"] == [{"name": "start", "description": "Начать"}] and body["start_message"] == "Привет!"
    assert (body["profile"]["username"], body["profile"]["is_bot"], body["profile"]["description"]) == (
        "some_bot", True, "Заказы")
    assert _frames(rw, int(Opcode.BOT_INFO)) == [{"botId": BOT}]

    await _foreign(rw)
    rw.env.server.chat_bots[FOREIGN] = {"commands": [{"botId": BOT, "name": "help"}], "contacts": {str(BOT): contact}}
    body = (await _get(rw, "/bots/info", chat_id=FOREIGN)).json()
    assert body["bots"] == [{"bot_id": BOT, "profile": body["bots"][0]["profile"],
                             "commands": [{"name": "help", "description": None}]}]
    assert _frames(rw, int(Opcode.CHAT_BOT_COMMANDS)) == [{"chatId": FOREIGN}]
    for params in ({}, {"bot_id": BOT, "chat_id": FOREIGN}):
        assert (await _get(rw, "/bots/info", **params)).status_code == 422
    assert (await _get(rw, "/bots/info", bot_id=1)).status_code == 502  # MAX: no such bot


# ---------------------------------------------------------------------------
# block / unblock (R5-18)
# ---------------------------------------------------------------------------

async def test_block_and_unblock(rw):
    _bot(rw)
    resp = await _post(rw, f"/users/{BOT}/block")
    assert resp.json() == {"status": "blocked", "user_id": BOT, "blocked": True}, resp.text
    assert _frames(rw, int(Opcode.CONTACT_UPDATE)) == [{"contactId": BOT, "action": "BLOCK"}]
    assert _frames(rw, int(Opcode.CONTACT_LIST)) == []  # confirmed by the ack, no list read (Р5.1 Р-83)
    resp = await _post(rw, f"/users/{BOT}/unblock")
    assert resp.json() == {"status": "unblocked", "user_id": BOT, "blocked": False}
    assert _actions(rw) == ["block_user", "unblock_user"]
    assert (await _post(rw, f"/users/{ME}/block")).status_code == 422
    assert len(_frames(rw, int(Opcode.CONTACT_UPDATE))) == 2


async def test_block_ack_cases_and_timeout(rw):
    """Р5.1 Р-83: the ack's contact decides; no contact → warning; no answer → 504 applied unknown, no repeat."""
    _bot(rw)
    rw.env.server.block_ack = {}
    body = (await _post(rw, f"/users/{BOT}/block")).json()
    assert body["blocked"] is None and "not confirmed" in body["warning"]
    rw.env.server.block_ack = {"contact": {"id": BOT, "status": "ACTIVE"}}
    body = (await _post(rw, f"/users/{BOT}/block")).json()
    assert body["blocked"] is False and "not blocked" in body["warning"]
    sent = len(_frames(rw, int(Opcode.CONTACT_UPDATE)))
    rw.env.server.group_errors = [TimeoutError("keepalive ping timeout")]
    resp = await _post(rw, f"/users/{BOT}/unblock")
    assert resp.status_code == 504 and resp.json()["detail"]["applied"] == "unknown"
    assert len(_frames(rw, int(Opcode.CONTACT_UPDATE))) == sent + 1  # one frame, no repeat
    assert _actions(rw)[-1] == "unblock_user"  # kept: it may be applied


async def test_block_waits_for_a_history_page(rw):
    """Р5.1 Р-83: the session's frame lock — a block does not go out while a history page is read."""
    import asyncio

    _bot(rw)
    session = await rw.env.session()
    order = []
    gate = asyncio.Event()

    async def page():
        async with session.frame_lock:
            order.append("history")
            await gate.wait()
            order.append("history done")

    task = asyncio.create_task(page())
    await asyncio.sleep(0)
    block = asyncio.create_task(_post(rw, f"/users/{BOT}/block"))
    await asyncio.sleep(0.2)
    assert _frames(rw, int(Opcode.CONTACT_UPDATE)) == []
    gate.set()
    await task
    assert (await block).status_code == 200 and order == ["history", "history done"]
    assert len(_frames(rw, int(Opcode.CONTACT_UPDATE))) == 1


# ---------------------------------------------------------------------------
# ro, TG, route names, MCP (R5-20…R5-22)
# ---------------------------------------------------------------------------

async def test_access_of_the_r5_2_routes(rw, aliases):  # noqa: F811
    dialog = await _bot_message(rw)
    writes = [("POST", f"/messages/{dialog}/81/press", {"row": 0, "column": 0}, "press_button"),
              ("POST", "/bots/start", {"bot_id": BOT}, "start_bot"),
              ("POST", f"/users/{BOT}/block", None, "block_user"),
              ("POST", f"/users/{BOT}/unblock", None, "unblock_user")]
    table = __import__("app.main", fromlist=["app"]).app.state.route_table
    for method, path, _, tool in writes:
        assert table.resolve(method, "/api/v1/_max" + path) == tool
    tg_alias, _ = aliases("tgr52", platform="telegram", mode="rw")
    for method, path, body in (("POST", f"/messages/{dialog}/81/press", {"row": 0, "column": 0}),
                               ("POST", "/bots/start", {"bot_id": BOT}), ("GET", "/bots/info?bot_id=1", None)):
        resp = await rw.http.request(method, "/api/v1" + path, json=body, headers={**API_KEY, "x-session-alias": tg_alias})
        assert resp.status_code == 501, (path, resp.text)
    sql("UPDATE accounts SET mode = 'ro' WHERE id = %s", (rw.id,))
    mw._mode_cache.clear()
    for method, path, body, tool in writes:
        resp = await rw.http.request(method, "/api/v1" + path, json=body, headers=rw.env.h())
        assert resp.status_code == 403, (tool, resp.text)
    assert _calls(rw, "invoke") == [] and _events(rw.id) == []
    rw.env.server.bot_infos[BOT] = {"commands": []}
    assert (await _get(rw, "/bots/info", bot_id=BOT)).status_code == 200
    for _ in range(100):
        denied = sql("SELECT tool FROM audit_logs WHERE alias = %s AND status = 'denied'", (rw.env.alias,), fetch=True)
        if len(denied) >= len(writes):
            break
        await asyncio.sleep(0.05)
    assert sorted(t for (t,) in denied) == sorted(w[3] for w in writes)


# ---------------------------------------------------------------------------
# Canaries against the appendix (R5-24)
# ---------------------------------------------------------------------------

def test_bot_frames_match_the_client_schemas():
    # 118: callbackId P, type P?, payload P?, timestamp V?
    frame = frames.callback_frame("cb", "CALLBACK", "p")
    assert set(frame) == {"callbackId", "type", "payload", "timestamp"} and isinstance(frame["timestamp"], int)
    # 64: chatId z? | userId z?, message Js {cid, attaches [CONTROL botStarted startPayload?]}
    frame = frames.bot_start_frame(chat_id=5, bot_id=7, start_payload="s")
    assert set(frame) == {"chatId", "message"} and set(frame["message"]) == {"cid", "attaches"}
    assert frame["message"]["attaches"] == [{"_type": "CONTROL", "event": "botStarted", "startPayload": "s"}]
    assert set(frames.bot_start_frame(chat_id=None, bot_id=7, start_payload=None)) == {"userId", "message"}
    # 34: contactId z, action gme (ADD BLOCK REMOVE UNBLOCK UPDATE ...); 36: status Es, from B?, count B?
    assert frames.block_frame(5, block=True) == {"contactId": 5, "action": "BLOCK"}
    assert frames.block_frame(5, block=False)["action"] == "UNBLOCK"
    assert frames.blocked_frame() == {"status": "BLOCKED", "count": 100, "from": 0}  # the client's dme, Р5.1 Р-82


async def test_start_bot_second_frame_only_for_a_chat_error(rw):
    """review 19 R19-3: flood, a stopped bot ... → 502 at once, no userId frame."""
    _bot(rw)
    rw.env.server.group_errors = [ApiError(opcode=64, error="too.many.requests", message="slow down")]
    resp = await _post(rw, "/bots/start", {"bot_id": BOT})
    assert resp.status_code == 502 and "slow down" in resp.text
    assert len(_frames(rw, int(Opcode.MSG_SEND))) == 1 and _events(rw.id) == []
