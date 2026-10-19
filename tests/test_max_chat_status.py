"""R3.1 (19.10, after the live R3 check): a group the account lost stops being monitored;
MCP audit target of user_ids tools; TG /groups/<not a number> → 404.

Live frames of max-personal-ro when max-work deleted the test group: a message frame
(opcode 128) carrying chat.status CLOSED; when the member was removed — NOTIF_CHAT
(135) with status LEFT. Catch-up and backfill of such a chat get chat.denied.
"""

import asyncio

from pymax import ApiError

import app.main as main_module
from app.authz import keys
from app.max.store_new import NewMaxStore
from tests.isolation_env import sql
from tests.max_env import API_KEY, aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_groups import FOREIGN, _chat_row, _foreign
from tests.test_max_ingest import ME
from tests.test_max_write_limits import OFF, _limits, clock, rw  # noqa: F401 — pytest fixtures
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture
from tests.test_mcp_http import session

T = 1_792_420_039_833


def _chat(status: str, **extra) -> dict:
    return {"id": FOREIGN, "type": "CHAT", "status": status, "owner": 9999, "title": "Чужая",
            "participants": {str(ME): 1}, "lastEventTime": T, **extra}


async def _caught_up(rw) -> set[int]:
    return {ext for ext, _, _ in await NewMaxStore(rw.id).catchup_rows()}


async def _wait(predicate, timeout: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


async def test_group_deleted_by_owner_stops_monitoring(rw):
    await _foreign(rw)
    assert FOREIGN in await _caught_up(rw)
    # the owner's control message, its frame carries a partial chat (owner 0 at times)
    await rw.client.push({"opcode": 128, "cmd": 0, "payload": {
        "chatId": FOREIGN, "chat": _chat("CLOSED", owner=0),
        "message": {"id": "77", "time": T, "type": "USER", "text": "", "sender": 9999,
                    "attaches": [{"_type": "CONTROL", "event": "remove"}]}}})
    row = await _chat_row(rw.id, FOREIGN)
    assert (row.is_monitored, row.raw["status"], row.title) == (False, "CLOSED", "Чужая")
    assert FOREIGN not in await _caught_up(rw)
    chats = (await rw.http.get("/api/v1/chats", headers=rw.env.h())).json()["items"]
    assert [c["is_monitored"] for c in chats if c["id"] == FOREIGN] == [False]


async def test_removed_member_and_added_again(rw):
    await _foreign(rw)
    await rw.client.push({"opcode": 135, "cmd": 0, "payload": {"chat": _chat("LEFT", participants={})}})
    assert (await _chat_row(rw.id, FOREIGN)).is_monitored is False
    await rw.client.push({"opcode": 135, "cmd": 0, "payload": {"chat": _chat("ACTIVE")}})
    row = await _chat_row(rw.id, FOREIGN)
    assert (row.is_monitored, row.raw["status"]) == (True, "ACTIVE")
    assert FOREIGN in await _caught_up(rw)


async def test_active_updates_keep_the_owners_setting(rw):
    await _foreign(rw)
    resp = await rw.http.patch(f"/api/v1/chats/{FOREIGN}", headers=rw.env.h(), json={"is_monitored": False})
    assert resp.status_code == 200, resp.text
    await rw.client.push({"opcode": 135, "cmd": 0, "payload": {"chat": _chat("ACTIVE", title="Новое имя")}})
    row = await _chat_row(rw.id, FOREIGN)
    assert (row.is_monitored, row.title) == (False, "Новое имя")


async def test_chat_denied_marks_the_chat(rw):
    await _foreign(rw)
    rw.env.server.history_errors[FOREIGN] = [ApiError(opcode=49, error="chat.denied", message="Access denied")]
    resp = await rw.http.post("/api/v1/sync/backfill", headers=rw.env.h(), json={"chat_id": FOREIGN, "limit": 10})
    assert resp.status_code == 200, resp.text

    async def denied():
        return ((await _chat_row(rw.id, FOREIGN)).raw or {}).get("status") == "DENIED"
    assert await _wait(denied)
    assert (await _chat_row(rw.id, FOREIGN)).is_monitored is False
    assert FOREIGN not in await _caught_up(rw)
    await NewMaxStore(rw.id).set_chat_status(-1, "DENIED")  # a chat the account does not have: nothing


async def test_mcp_audit_target_of_user_ids_tools(rw):
    await _limits(rw, **OFF)
    _, key = await keys.create_key(scope="account", account_id=rw.id, label="mcp-r31")
    try:
        async with session(main_module.app, key) as s:
            made = await s.call_tool("create_group", {"title": "MCP", "user_ids": ["5001", "5002"]})
            assert not made.isError, made.content[0].text
    finally:
        sql("DELETE FROM api_keys WHERE label = 'mcp-r31'")
    for _ in range(100):
        rows = sql("SELECT tool, target_user_id FROM audit_logs WHERE alias = %s AND tool = 'create_group'",
                   (rw.env.alias,), fetch=True)
        if rows:
            break
        await asyncio.sleep(0.05)
    assert rows == [("create_group", 5001)]


async def test_groups_with_a_non_numeric_id_are_404(rw, aliases):  # noqa: F811
    tg_alias, _ = aliases("tgn", platform="telegram", mode="rw")
    tg = {**API_KEY, "x-session-alias": tg_alias}
    for path in ("/api/v1/groups/abc/members", "/api/v1/groups/1.5/delete", "/api/v1/groups/x"):
        assert (await rw.http.post(path, headers=tg, json={})).status_code == 404, path
        assert (await rw.http.post(path, headers=rw.env.h(), json={})).status_code == 404, path
    resp = await rw.http.post("/api/v1/groups/-5/members", headers=tg, json={"user_ids": [1]})
    assert resp.status_code == 501 and resp.json()["tool"] == "add_group_members"
