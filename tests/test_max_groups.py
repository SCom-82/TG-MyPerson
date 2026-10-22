"""MAX groups (plan 2026-10-19 Р3 §3 PR-G2, QA R3-2…R3-9).

STORAGE_MODE=new, the fake MAX server (its group methods record every call),
the test clock of write_limits. PyMax's own frames are checked separately at
app.invoke (canary: a PyMax update that renames them fails here).
"""

import asyncio
from types import SimpleNamespace

import pytest
from pymax import ApiError
from pymax.api.chats.enums import ChannelPermissions
from pymax.api.chats.service import ChatService
from pymax.protocol import Opcode

import app.authz.middleware as mw
from app.authz import write_limits
from app.max import groups
from app.mcp import tools as mcp_tools
from app.repo import chats
from app.repo.rows import ChatRow
from app.storage import account_db
from tests.isolation_env import sql
from tests.max_env import ADMIN_KEY, API_KEY, aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_ingest import ME
from tests.test_max_write_limits import OFF, _events, _limits, _status, clock, rw  # noqa: F401 — pytest fixtures
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture

FOREIGN = -72_000_000_001  # someone else's group the account is a member of
A, B, C = 5001, 5002, 5003


async def _post(w, path, body=None, headers=None):
    return await w.http.post(f"/api/v1{path}", headers=headers or w.env.h(), json=body or {})


async def _patch(w, path, body):
    return await w.http.patch(f"/api/v1{path}", headers=w.env.h(), json=body)


async def _get(w, path, **params):
    return await w.http.get(f"/api/v1{path}", headers=w.env.h(), params=params)


async def _create(w, title="Тест", user_ids=(A,)) -> int:
    resp = await _post(w, "/groups", {"title": title, "user_ids": list(user_ids)})
    assert resp.status_code == 200, resp.text
    return resp.json()["chat_id"]


async def _chat_row(account_id: int, chat_id: int):
    async with account_db(account_id) as db:
        return await chats.get_chat(db, account_id, chat_id)


def _calls(w, name: str) -> list[tuple]:
    return [c for c in w.env.server.group_calls if c[0] == name]


def _admin_frames(w) -> list[tuple]:
    """(chat, user, mask) of every 77 ADMIN add (plan Р5 Р-52: our frame, not PyMax's add_admin)."""
    return [(c[2]["chatId"], c[2]["userIds"][0], c[2]["permissions"]) for c in _calls(w, "invoke")
            if c[1] == int(Opcode.CHAT_MEMBERS_UPDATE) and c[2].get("type") == "ADMIN"
            and c[2]["operation"] == "add"]


GROUP_PERSON = ["add_remove_member", "change_chat_info", "pin_message", "read_all_messages", "edit_link",
                "delete_message", "call"]  # 251, the MAX app's default for a person in a group (Р-53)


async def _foreign(w, kind: str = "group", owner: int = 9999) -> None:
    w.env.server.groups[FOREIGN] = {"id": FOREIGN, "type": "CHAT" if kind == "group" else "CHANNEL",
                                    "status": "ACTIVE", "owner": owner, "title": "Чужая",
                                    "participants": {str(ME): 1, str(owner): 1, str(A): 1}, "admins": [owner]}
    async with account_db(w.id) as db:
        await chats.upsert_chat(db, w.id, ChatRow(FOREIGN, kind, title="Чужая"))
        await db.commit()


# ---------------------------------------------------------------------------
# create / members (1, 2, 3)
# ---------------------------------------------------------------------------

async def test_create_group_stores_chat_message_and_counts(rw):
    resp = await _post(rw, "/groups", {"title": "Штаб тест", "user_ids": [A, B]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    gid = body["chat_id"]
    assert (body["title"], body["my_role"], body["members_count"], body["owner"]) == ("Штаб тест", "creator", 3, ME)
    assert _calls(rw, "create_group") == [("create_group", "Штаб тест", [A, B], True)]
    row = await _chat_row(rw.id, gid)
    assert (row.chat_type, row.my_role, row.title) == ("group", "creator", "Штаб тест")
    assert set(row.raw["participants"]) == {str(ME), str(A), str(B)}
    msgs = (await _get(rw, "/messages", chat_id=gid)).json()["items"]
    assert [m["message_id"] for m in msgs] == [body["message_id"]]
    assert sorted(e[0] for e in _events(rw.id)) == ["group_create", "group_invite", "group_invite"]


async def test_create_group_without_a_chat_in_the_answer_keeps_the_slots(rw):
    """review 15 R15-1: MAX took the frame, answered without the chat — the group likely exists."""
    async def no_chat(name, participant_ids=None, notify=True):
        rw.env.server.group_calls.append(("create_group", name))
        return None

    rw.client.create_group = no_chat
    resp = await _post(rw, "/groups", {"title": "x", "user_ids": [A, B]})
    assert resp.status_code == 502 and "may have been created, check list_chats" in resp.text
    assert sorted(e[0] for e in _events(rw.id)) == ["group_create", "group_invite", "group_invite"]


async def test_create_group_limits(rw):
    await _limits(rw, group_invites_per_day=3)
    resp = await _post(rw, "/groups", {"title": "x", "user_ids": [A, B, C, 5004, 5005]})
    assert resp.status_code == 429 and resp.json()["limit"] == "group_invites_per_day"
    assert _calls(rw, "create_group") == [] and _events(rw.id) == []
    for i in range(3):
        rw.clock.at(i * 10)
        await _create(rw, f"g{i}", ())
    resp = await _post(rw, "/groups", {"title": "4th"})
    assert resp.status_code == 429 and resp.json()["limit"] == "groups_created_per_day"
    assert (await _post(rw, "/groups", {"title": "x", "user_ids": list(range(1, 22))})).status_code == 422
    assert (await _post(rw, "/groups", {"title": "x", "user_ids": [ME]})).status_code == 422


async def test_add_members_limits_and_slots(rw):
    gid = await _create(rw, user_ids=())
    resp = await _post(rw, f"/groups/{gid}/members", {"user_ids": list(range(1, 22))})
    assert resp.status_code == 422
    assert (await _post(rw, f"/groups/{gid}/members", {"user_ids": list(range(1100, 1120))})).status_code == 200
    resp = await _post(rw, f"/groups/{gid}/members", {"user_ids": list(range(1200, 1210))})
    assert resp.status_code == 200 and resp.json()["members_count"] == 31
    resp = await _post(rw, f"/groups/{gid}/members", {"user_ids": [1300]})
    assert resp.status_code == 429 and resp.json()["limit"] == "group_invites_per_day"
    assert _calls(rw, "invite_users_to_group")[-1][2] == list(range(1200, 1210))

    await _limits(rw, group_invites_per_day=100)
    rw.env.server.group_errors = [ApiError(opcode=77, error="chat.denied", message="denied")]
    assert (await _post(rw, f"/groups/{gid}/members", {"user_ids": [1400, 1401]})).status_code == 502
    assert len([e for e in _events(rw.id) if e[0] == "group_invite"]) == 30  # given back
    rw.env.server.group_errors = [TimeoutError("no answer")]
    resp = await _post(rw, f"/groups/{gid}/members", {"user_ids": [1400, 1401]})
    assert resp.status_code == 504 and "the change may have been applied" in resp.text
    assert len([e for e in _events(rw.id) if e[0] == "group_invite"]) == 32  # kept: it may be done


async def test_remove_members_keeps_their_messages_and_updates_the_chat(rw):
    gid = await _create(rw, user_ids=(A, B))
    resp = await _post(rw, f"/groups/{gid}/members/remove", {"user_ids": [A]})
    assert resp.status_code == 200, resp.text
    assert _calls(rw, "remove_users_from_group") == [("remove_users_from_group", gid, [A], 0)]
    assert set((await _chat_row(rw.id, gid)).raw["participants"]) == {str(ME), str(B)}
    assert resp.json()["removed"] == [A] and resp.json()["members_count"] == 2
    assert (await _post(rw, f"/groups/{gid}/members/remove", {"user_ids": [ME]})).status_code == 422


# ---------------------------------------------------------------------------
# admins, profile, settings, link, join requests (4, 5, 6, 7)
# ---------------------------------------------------------------------------

async def test_admins(rw):
    gid = await _create(rw, user_ids=(A, B))
    resp = await _post(rw, f"/groups/{gid}/admins", {"user_id": A})
    assert resp.status_code == 200, resp.text
    assert _admin_frames(rw)[-1] == (gid, A, 251)
    assert resp.json()["permissions"] == GROUP_PERSON
    assert resp.json()["admins"] == [A]
    await _post(rw, f"/groups/{gid}/admins", {"user_id": B, "permissions": ["add_admin"]})
    assert _admin_frames(rw)[-1] == (gid, B, 4)
    resp = await _post(rw, f"/groups/{gid}/admins", {"user_id": B, "permissions": ["post_message"]})
    assert resp.status_code == 422 and "channel rights" in resp.text  # a channel right (Р4 Р-40)
    resp = await _post(rw, f"/groups/{gid}/admins", {"user_id": B, "permissions": ["fly"]})
    assert resp.status_code == 422 and "unknown permissions" in resp.text
    assert (await _post(rw, f"/groups/{gid}/admins", {"user_id": ME})).status_code == 422

    resp = await _post(rw, f"/groups/{gid}/admins/remove", {"user_id": A})
    assert resp.status_code == 200, resp.text
    assert _calls(rw, "invoke")[-1] == ("invoke", int(Opcode.CHAT_MEMBERS_UPDATE),
                                        {"chatId": gid, "userIds": [A], "type": "ADMIN", "operation": "remove"})
    assert resp.json()["admins"] == [B] and (await _chat_row(rw.id, gid)).raw["admins"] == [B]
    assert [e[1] for e in _events(rw.id) if e[0] == "action"] == ["set_group_admin"] * 2 + ["remove_group_admin"]


async def test_profile_and_settings(rw):
    gid = await _create(rw, user_ids=())
    assert (await _patch(rw, f"/groups/{gid}", {})).status_code == 422
    resp = await _patch(rw, f"/groups/{gid}", {"description": "тест, будет удалена"})
    assert resp.status_code == 200 and resp.json()["description"] == "тест, будет удалена"
    assert _calls(rw, "change_group_profile")[-1] == ("change_group_profile", gid, None, "тест, будет удалена")
    await _patch(rw, f"/groups/{gid}", {"title": "Новое"})
    assert (await _chat_row(rw.id, gid)).title == "Новое"

    assert (await _patch(rw, f"/groups/{gid}/settings", {})).status_code == 422
    resp = await _patch(rw, f"/groups/{gid}/settings", {"only_admin_can_add_member": True})
    assert resp.status_code == 200, resp.text
    assert _calls(rw, "change_group_settings")[-1] == ("change_group_settings", gid,
                                                       {"only_admin_can_add_member": True})
    assert resp.json()["changed"] == {"only_admin_can_add_member": True}


async def test_invite_link(rw):
    gid = await _create(rw, user_ids=())
    resp = await _get(rw, f"/groups/{gid}/invite-link")
    assert resp.json() == {"chat_id": gid, "link": None,
                           "hint": "the group has no invite link: reset_invite_link creates one"}
    resp = await _post(rw, f"/groups/{gid}/invite-link")
    body = resp.json()
    assert body["link"].startswith("https://max.ru/join/new-") and "revoked" in body["warning"]
    assert (await _get(rw, f"/groups/{gid}/invite-link")).json()["link"] == body["link"]
    assert _calls(rw, "rework_invite_link") == [("rework_invite_link", gid)]


async def test_join_requests(rw):
    gid = await _create(rw, user_ids=())
    rw.env.server.users[A] = {"id": A, "names": [{"firstName": "Аня", "lastName": "К"}], "link": "anya"}
    rw.env.server.join_queue[gid] = [A, B]
    listed = (await _get(rw, f"/groups/{gid}/join-requests")).json()
    assert listed["total"] == 2 and listed["items"][0] == {"user_id": A, "first_name": "Аня", "last_name": "К",
                                                           "username": "anya"}
    assert sql(f"SELECT first_name, last_seen_via FROM users_a{rw.id} WHERE external_id = %s", (A,),
               fetch=True) == [("Аня", "member")]
    resp = await _post(rw, f"/groups/{gid}/join-requests/approve", {"user_ids": [A]})
    assert resp.status_code == 200 and str(A) in (await _chat_row(rw.id, gid)).raw["participants"]
    assert (await _post(rw, f"/groups/{gid}/join-requests/decline", {"user_ids": [B]})).status_code == 200
    assert rw.env.server.join_queue[gid] == []
    assert [e[0] for e in _events(rw.id)].count("group_invite") == 0  # approval is an action, not an invite


# ---------------------------------------------------------------------------
# delete, leave, members, rights (8, 9)
# ---------------------------------------------------------------------------

async def test_delete_group_owner_only_confirmed(rw):
    await _foreign(rw)
    assert (await _post(rw, f"/groups/{FOREIGN}/delete", {"confirm_title": "Чужая"})).status_code == 403
    async with account_db(rw.id) as db:
        await chats.upsert_chat(db, rw.id, ChatRow(100500, "private"))
        await db.commit()
    assert (await _post(rw, "/groups/100500/delete", {"confirm_title": "x"})).status_code == 422
    gid = await _create(rw, "Удалить меня", ())
    resp = await _post(rw, f"/groups/{gid}/delete", {"confirm_title": "удалить меня"})
    assert resp.status_code == 409 and _calls(rw, "delete_chat") == []
    resp = await _post(rw, f"/groups/{gid}/delete", {"confirm_title": "Удалить меня"})
    assert resp.json() == {"chat_id": gid, "deleted": True, "for_all": True}
    assert _calls(rw, "delete_chat") == [("delete_chat", gid, True)]
    row = await _chat_row(rw.id, gid)
    assert row is not None and row.is_monitored is False
    assert len((await _get(rw, "/messages", chat_id=gid)).json()["items"]) == 1  # history stays
    assert (await _post(rw, "/groups/-1/delete", {"confirm_title": "x"})).status_code == 404


async def test_leave_members_rights(rw):
    await _foreign(rw)
    rw.env.server.users[A] = {"id": A, "names": [{"firstName": "Аня"}], "link": "anya"}
    members = (await _get(rw, f"/chats/{FOREIGN}/members")).json()
    assert {(m["user_id"], m["role"]) for m in members["items"]} == {(ME, "member"), (9999, "creator"), (A, "member")}
    assert len([c for c in _calls(rw, "get_chat_members")]) == 2  # page of 2 + the rest
    found = (await _get(rw, f"/chats/{FOREIGN}/members", search="аня")).json()
    assert [m["user_id"] for m in found["items"]] == [A]

    rights = (await _get(rw, f"/chats/{FOREIGN}/my_rights")).json()
    assert (rights["is_member"], rights["is_admin"], rights["is_creator"]) == (True, False, False)
    assert rights["can_post_messages"] is True  # a group member writes
    gid = await _create(rw, user_ids=(A,))
    mine = (await _get(rw, f"/chats/{gid}/my_rights")).json()
    assert (mine["is_creator"], mine["is_admin"], mine["can_add_admins"]) == (True, True, True)

    resp = await _post(rw, "/chats/leave", {"chat_id": FOREIGN})
    assert resp.json() == {"status": "left", "chat_id": FOREIGN}
    assert _calls(rw, "leave_group") == [("leave_group", FOREIGN)]
    assert (await _chat_row(rw.id, FOREIGN)).is_monitored is False


async def test_my_rights_of_an_admin_by_mask(rw):
    await _foreign(rw)
    rw.env.server.groups[FOREIGN]["admins"] = [9999, ME]
    rw.env.server.groups[FOREIGN]["adminParticipants"] = {str(ME): {"permissions": 2 | 16}}
    rights = (await _get(rw, f"/chats/{FOREIGN}/my_rights")).json()
    assert (rights["is_admin"], rights["can_pin_messages"], rights["can_change_info"], rights["can_add_admins"]) == (
        True, True, False, False)
    assert rights["raw"] == {"permissions": 18}
    assert (await _chat_row(rw.id, FOREIGN)).my_role == "admin"


async def test_channel_leave_and_private_refused(rw):
    await _foreign(rw, kind="channel")
    rights = (await _get(rw, f"/chats/{FOREIGN}/my_rights")).json()
    assert rights["can_post_messages"] is False  # review 15 R15-2: a channel member does not post
    assert (await _post(rw, "/chats/leave", {"chat_id": FOREIGN})).status_code == 200
    assert _calls(rw, "leave_channel") == [("leave_channel", FOREIGN)]
    # a channel takes the group routes since Р4 (Р-40); a private chat does not
    dialog = ME ^ A
    async with account_db(rw.id) as db:
        await chats.upsert_chat(db, rw.id, ChatRow(dialog, "private", title="A"))
        await db.commit()
    resp = await _post(rw, f"/groups/{dialog}/members", {"user_ids": [B]})
    assert resp.status_code == 422 and "not a group or channel" in resp.text


# ---------------------------------------------------------------------------
# ro, TG, enabled=false, MCP (10, 11, 12)
# ---------------------------------------------------------------------------

WRITES = [("POST", "/groups", {"title": "x"}), ("POST", "/groups/{g}/members", {"user_ids": [A]}),
          ("POST", "/groups/{g}/members/remove", {"user_ids": [A]}), ("POST", "/groups/{g}/admins", {"user_id": A}),
          ("POST", "/groups/{g}/admins/remove", {"user_id": A}), ("PATCH", "/groups/{g}", {"title": "y"}),
          ("PATCH", "/groups/{g}/settings", {"only_admin_can_call": True}), ("POST", "/groups/{g}/invite-link", {}),
          ("POST", "/groups/{g}/join-requests/approve", {"user_ids": [A]}),
          ("POST", "/groups/{g}/join-requests/decline", {"user_ids": [A]}),
          ("POST", "/groups/{g}/delete", {"confirm_title": "Чужая"}), ("POST", "/chats/leave", {"chat_id": FOREIGN})]


async def _call(w, method, path, body, headers=None):
    return await w.http.request(method, "/api/v1" + path.format(g=FOREIGN), headers=headers or w.env.h(), json=body)


async def test_ro_cannot_write_reads_work(rw):
    await _foreign(rw)
    sql("UPDATE accounts SET mode = 'ro' WHERE id = %s", (rw.id,))
    mw._mode_cache.clear()
    for method, path, body in WRITES:
        resp = await _call(rw, method, path, body)
        assert resp.status_code == 403, (path, resp.text)
    assert rw.env.server.group_calls == []
    assert (await _get(rw, f"/chats/{FOREIGN}/members")).status_code == 200
    assert (await _get(rw, f"/groups/{FOREIGN}/invite-link")).status_code == 200
    assert (await _get(rw, f"/groups/{FOREIGN}/join-requests")).status_code == 200
    for _ in range(100):
        denied = sql("SELECT count(*) FROM audit_logs WHERE alias = %s AND status = 'denied'", (rw.env.alias,),
                     fetch=True)[0][0]
        if denied >= len(WRITES):
            break
        await asyncio.sleep(0.05)
    assert denied == len(WRITES)


async def test_telegram_gets_501_for_groups(rw, aliases):  # noqa: F811
    tg_alias, _ = aliases("tgg", platform="telegram", mode="rw")
    h = {**API_KEY, "x-session-alias": tg_alias}
    for method, path, body in WRITES[:-1] + [("GET", "/groups/{g}/invite-link", None),
                                             ("GET", "/groups/{g}/join-requests", None)]:
        resp = await _call(rw, method, path, body, headers=h)
        assert resp.status_code == 501, (path, resp.text)
        assert resp.json()["platform"] == "telegram"


async def test_tool_names_reach_the_audit(rw):
    gid = await _create(rw, user_ids=())
    await _post(rw, f"/groups/{gid}/members", {"user_ids": [A]})
    for _ in range(100):
        rows = sql("SELECT tool, status, chat_id, target_user_id FROM audit_logs WHERE alias = %s"
                   " AND tool LIKE '%%group%%' ORDER BY id", (rw.env.alias,), fetch=True)
        if len(rows) >= 2:
            break
        await asyncio.sleep(0.05)
    assert rows == [("create_group", "ok", None, None), ("add_group_members", "ok", gid, A)]


async def test_enabled_false_stops_group_writes(rw):
    gid = await _create(rw, user_ids=())
    await _limits(rw, enabled=False)
    for path, body in ((f"/groups/{gid}/members", {"user_ids": [A]}), (f"/groups/{gid}/invite-link", {}),
                       ("/groups", {"title": "x"})):
        assert (await _post(rw, path, body)).status_code == 403
    await _limits(rw, enabled=True)
    await _post(rw, f"/groups/{gid}/invite-link")
    assert (await _status(rw))["counters"]["actions_last_hour"] == 1


async def test_mcp_tools_by_platform_and_mode(rw):
    names = lambda platform, mode: {t.name for t in mcp_tools.visible(  # noqa: E731
        mcp_tools.Caller("account", rw.id, rw.env.alias, platform, mode, "http://test"))}
    max_rw, max_ro, tg_rw, tg_ro = names("max", "rw"), names("max", "ro"), names("telegram", "rw"), names("telegram", "ro")
    assert (len(max_rw), len(max_ro), len(tg_rw), len(tg_ro)) == (77, 31, 46, 21)  # R5-1 +4/+1, R5-2 +10/+6
    assert {"create_group", "delete_group", "get_chat_members", "leave_chat", "get_my_rights"} <= max_rw
    assert {"get_chat_members", "get_my_rights", "get_invite_link", "list_join_requests"} <= max_ro
    assert not {"create_group", "leave_chat"} & max_ro and not {"create_group", "get_invite_link"} & tg_rw
    resp = await rw.http.get("/api/v1/tools", headers=rw.env.h())
    assert set(resp.json()["tools"]) == max_rw

    caller = mcp_tools.Caller("account", rw.id, rw.env.alias, "max", "rw", "http://test")
    made = await mcp_tools.BY_NAME["create_group"].call(caller, {"title": "MCP", "user_ids": [A]})
    added = await mcp_tools.BY_NAME["add_group_members"].call(caller, {"chat_id": made["chat_id"], "user_ids": [B]})
    assert added["added"] == [B] and added["members_count"] == 3
    schema = mcp_tools.BY_NAME["add_group_members"].schema["properties"]
    assert schema["chat_id"]["type"] == ["string", "integer"] and schema["user_ids"]["items"]["type"] == ["string", "integer"]


# ---------------------------------------------------------------------------
# Canary: PyMax 2.4.1's own group frames, at app.invoke
# ---------------------------------------------------------------------------

class Sent(Exception):
    pass


def _service():
    frames = []

    async def invoke(opcode, payload):
        frames.append((int(opcode), payload))
        raise Sent()

    return ChatService(SimpleNamespace(invoke=invoke, chats={})), frames


@pytest.mark.parametrize("call,opcode,payload", [
    (lambda s: s.invite_users_to_group(-5, [1, 2], show_history=True), Opcode.CHAT_MEMBERS_UPDATE,
     {"chatId": -5, "userIds": [1, 2], "showHistory": True, "operation": "add"}),
    (lambda s: s.remove_users_from_group(-5, [1], clean_msg_period=0), Opcode.CHAT_MEMBERS_UPDATE,
     {"chatId": -5, "userIds": [1], "operation": "remove", "cleanMsgPeriod": 0}),
    (lambda s: s.add_admin(-5, 1, [ChannelPermissions.ADD_ADMIN, ChannelPermissions.PIN_MESSAGE]),
     Opcode.CHAT_MEMBERS_UPDATE, groups.admin_frame(-5, 1, 20)),  # our frame (Р5) has PyMax's shape
    (lambda s: s.rework_invite_link(-5), Opcode.CHAT_UPDATE, {"chatId": -5, "revokePrivateLink": True}),
    (lambda s: s.leave_group(-5), Opcode.CHAT_LEAVE, {"chatId": -5}),
])
async def test_pymax_group_frames(call, opcode, payload):
    service, frames = _service()
    with pytest.raises(Sent):
        await call(service)
    assert frames == [(int(opcode), payload)]


def test_remove_admin_frame_mirrors_add_admin():
    assert groups.remove_admin_frame(-5, 1) == {"chatId": -5, "userIds": [1], "type": "ADMIN", "operation": "remove"}
    assert groups.resolve_mask(["add_admin"], None, channel=False, bot=False) == 4
    assert groups.permission_names(251) == GROUP_PERSON
    with pytest.raises(groups.UnknownPermission):
        groups.resolve_mask(["post_message"], None, channel=False, bot=False)
    assert write_limits.DEFAULT_LIMITS.groups_created_per_day == 3 and write_limits.NO_LIMITS.group_invites_per_day is None


# ---------------------------------------------------------------------------
# Channels: the same routes (plan 2026-10-19 Р4 Р-40, QA R4-9)
# ---------------------------------------------------------------------------

async def test_channel_takes_the_group_tools(rw):
    await _foreign(rw, kind="channel", owner=ME)
    resp = await _post(rw, f"/groups/{FOREIGN}/members", {"user_ids": [B, C]})
    assert resp.status_code == 200, resp.text
    assert _calls(rw, "invite_users_to_channel") == [("invite_users_to_channel", FOREIGN, [B, C], True)]
    assert _calls(rw, "invite_users_to_group") == []
    assert [e[0] for e in _events(rw.id)] == ["group_invite", "group_invite"]

    resp = await _post(rw, f"/groups/{FOREIGN}/admins", {"user_id": A})
    assert resp.status_code == 200 and resp.json()["permissions"] == [
        "add_remove_member", "change_chat_info", "pin_message", "read_all_messages", "edit_link", "delete_message",
        "post_message", "edit_message", "view_stats"], resp.text
    assert _admin_frames(rw) == [(FOREIGN, A, 4026)]  # the MAX app's default for a person in a channel (Р-53)
    resp = await _post(rw, f"/groups/{FOREIGN}/admins", {"user_id": B, "permissions": ["post_message"]})
    assert resp.status_code == 200 and _admin_frames(rw)[-1] == (FOREIGN, B, 256)
    for path, body in ((f"/groups/{FOREIGN}/members/remove", {"user_ids": [C]}),
                       (f"/groups/{FOREIGN}/admins/remove", {"user_id": B}),
                       (f"/groups/{FOREIGN}/invite-link", {}),
                       (f"/groups/{FOREIGN}/join-requests/approve", {"user_ids": [C]}),
                       (f"/groups/{FOREIGN}/join-requests/decline", {"user_ids": [C]})):
        assert (await _post(rw, path, body)).status_code == 200, path
    assert (await _patch(rw, f"/groups/{FOREIGN}", {"description": "канал"})).status_code == 200
    assert (await _patch(rw, f"/groups/{FOREIGN}/settings", {"all_can_pin_message": False})).status_code == 200
    assert (await _get(rw, f"/groups/{FOREIGN}/invite-link")).status_code == 200
    assert (await _get(rw, f"/groups/{FOREIGN}/join-requests")).status_code == 200

    resp = await _post(rw, f"/groups/{FOREIGN}/delete", {"confirm_title": "Чужая"})
    assert resp.status_code == 422 and "a channel with subscribers" in resp.text  # Р5 Р-63


async def test_channel_rights_are_refused_for_a_group(rw):
    gid = await _create(rw, user_ids=(A,))
    resp = await _post(rw, f"/groups/{gid}/admins", {"user_id": A, "permissions": ["post_message"]})
    assert resp.status_code == 422 and "channel rights" in resp.text
    assert _admin_frames(rw) == []
    resp = await _post(rw, f"/groups/{gid}/admins", {"user_id": A})
    assert _admin_frames(rw) == [(gid, A, 251)]
