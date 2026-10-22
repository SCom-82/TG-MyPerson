"""MAX release 5, PR R5-1: admin rights by the MAX web client, members, settings, extra links,
ownership, channels (plan 2026-10-22 Р5 §4 PR-R5-1, Р-52…Р-63; QA R5-1…R5-12, R5-23, R5-24).

Our own frames are checked against the appendix «r5-protocol» (canaries at the end): the
field names and types there are the client's zod schemas.
"""

import pytest
from pymax import ApiError
from pymax.protocol import Opcode
from pymax.types.domain import Message

from app.max import groups
from app.mcp import tools as mcp_tools
from tests.isolation_env import sql
from tests.max_env import API_KEY, aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_groups import (
    FOREIGN, GROUP_PERSON, A, B, _admin_frames, _calls, _chat_row, _create, _foreign, _get, _patch, _post,
)
from tests.test_max_ingest import ME
from tests.test_max_write_limits import _events, _limits, clock, rw  # noqa: F401 — pytest fixtures
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture

BOT = 5099


def _bot(w, uid: int = BOT) -> None:
    w.env.server.users[uid] = {"id": uid, "names": [{"firstName": "Бот"}], "options": ["BOT"], "link": "some_bot"}


def _actions(w) -> list[str]:
    return [e[1] for e in _events(w.id) if e[0] == "action"]


async def _channel(w, owner: int = ME, members=(A,)) -> int:
    await _foreign(w, kind="channel", owner=owner)
    w.env.server.groups[FOREIGN]["participants"] = {str(u): 1 for u in {ME, owner, *members}}
    return FOREIGN


# ---------------------------------------------------------------------------
# masks: the pure part (R5-1, R5-2, R5-3)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("channel,bot,mask", [(False, True, 123), (False, False, 251), (True, True, 1850),
                                              (True, False, 4026)])
def test_defaults_are_the_clients(channel, bot, mask):
    assert groups.resolve_mask(None, None, channel=channel, bot=bot) == mask


def test_delete_message_bit_by_chat_type():
    assert groups.resolve_mask(["delete_message"], None, channel=False, bot=False) == 1
    assert groups.resolve_mask(["delete_message"], None, channel=True, bot=False) == 1024
    assert groups.resolve_mask(["view_stats", "post_message", "edit_message"], None, channel=True, bot=True) == 2816
    assert groups.resolve_mask(["call"], None, channel=False, bot=False) == 64
    for names, channel in ((["post_message"], False), (["view_stats"], False), (["edit_message"], False),
                           (["call"], True)):
        with pytest.raises(groups.UnknownPermission, match="rights, not for a"):
            groups.resolve_mask(names, None, channel=channel, bot=False)
    assert groups.permission_names(1 | 1024, channel=True) == ["delete_message"]
    assert groups.permission_names(4095, channel=False) == list(groups.allowed_names(channel=False))


def test_dependencies_by_name_and_by_mask():
    with pytest.raises(groups.UnknownPermission, match="edit_link needs add_remove_member"):
        groups.resolve_mask(["edit_link"], None, channel=False, bot=False)
    with pytest.raises(groups.UnknownPermission, match="edit_link needs add_remove_member"):
        groups.resolve_mask(None, 128, channel=True, bot=False)
    assert groups.resolve_mask(["edit_link", "add_remove_member"], None, channel=False, bot=False) == 130
    for names, mask in ((["delete_message"], None), (["pin_message"], None), (None, 17)):
        with pytest.raises(groups.UnknownPermission, match="a bot needs read_all_messages"):
            groups.resolve_mask(names, mask, channel=False, bot=True)
    assert groups.resolve_mask(None, 17, channel=False, bot=False) == 17  # a person: no such rule
    assert groups.resolve_mask(["pin_message"], None, channel=True, bot=True) == 16  # a channel: no such rule
    assert groups.resolve_mask(None, 49, channel=False, bot=True) == 49
    with pytest.raises(groups.UnknownPermission, match="not both"):
        groups.resolve_mask(["pin_message"], 16, channel=False, bot=False)
    for mask in (0, 4096):
        with pytest.raises(groups.UnknownPermission, match="1…4095"):
            groups.resolve_mask(None, mask, channel=False, bot=False)
    with pytest.raises(groups.UnknownPermission, match=r"\[256\] are not rights of a group"):
        groups.resolve_mask(None, 384, channel=False, bot=False)
    with pytest.raises(groups.UnknownPermission, match=r"\[1, 64\] are not rights of a channel"):
        groups.resolve_mask(None, 65 | 2, channel=True, bot=False)


# ---------------------------------------------------------------------------
# set_group_admin (R5-1…R5-4)
# ---------------------------------------------------------------------------

async def test_bot_and_person_get_the_clients_defaults(rw):
    _bot(rw)
    gid = await _create(rw, user_ids=(A, BOT))
    resp = await _post(rw, f"/groups/{gid}/admins", {"user_id": BOT})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["mask"], body["is_bot"], body["previous_mask"]) == (123, True, None)
    assert body["permissions"] == ["add_remove_member", "change_chat_info", "pin_message", "read_all_messages",
                                   "delete_message", "call"]
    assert _admin_frames(rw) == [(gid, BOT, 123)]
    assert rw.env.server.get_users_calls[-1] == [BOT]
    resp = await _post(rw, f"/groups/{gid}/admins", {"user_id": A})
    assert (resp.json()["mask"], resp.json()["is_bot"], resp.json()["permissions"]) == (251, False, GROUP_PERSON)
    raw = (await _chat_row(rw.id, gid)).raw["adminParticipants"]
    assert (raw[str(BOT)]["permissions"], raw[str(A)]["permissions"]) == (123, 251)


async def test_mask_replaced_and_refusals_before_the_slot(rw):
    _bot(rw)
    gid = await _create(rw, user_ids=(A, BOT))
    await _post(rw, f"/groups/{gid}/admins", {"user_id": A})
    resp = await _post(rw, f"/groups/{gid}/admins", {"user_id": A, "permissions": ["delete_message", "pin_message"]})
    assert (resp.json()["mask"], resp.json()["previous_mask"]) == (17, 251)
    resp = await _post(rw, f"/groups/{gid}/admins", {"user_id": A, "permissions_mask": 255})
    assert (resp.json()["mask"], resp.json()["previous_mask"], resp.json()["permissions"][-1]) == (255, 17, "call")
    sent, actions = len(_admin_frames(rw)), len(_actions(rw))
    for body in ({"user_id": A, "permissions_mask": 384},  # 256 is a channel bit
                 {"user_id": A, "permissions": ["edit_link"]},
                 {"user_id": A, "permissions_mask": 0}, {"user_id": A, "permissions_mask": 4096},
                 {"user_id": A, "permissions_mask": "x"},
                 {"user_id": A, "permissions": ["pin_message"], "permissions_mask": 16},
                 {"user_id": BOT, "permissions": ["delete_message"]},  # a bot without read_all_messages
                 {"user_id": BOT, "permissions_mask": 16}):
        resp = await _post(rw, f"/groups/{gid}/admins", body)
        assert resp.status_code == 422, (body, resp.text)
    assert "a bot needs read_all_messages" in resp.text
    assert (len(_admin_frames(rw)), len(_actions(rw))) == (sent, actions)  # no frame, no slot


async def test_is_bot_read_failure_and_max_refusal(rw):
    gid = await _create(rw, user_ids=(A,))

    async def broken(user_ids):
        raise ApiError(opcode=32, error="contact.not.found", message="no")

    rw.client.get_users = broken
    resp = await _post(rw, f"/groups/{gid}/admins", {"user_id": A})
    assert resp.status_code == 502 and _admin_frames(rw) == [] and _actions(rw) == []
    del rw.client.get_users
    rw.env.server.group_errors = [ApiError(opcode=77, error="chat.admin.denied", message="not allowed")]
    resp = await _post(rw, f"/groups/{gid}/admins", {"user_id": A})
    assert resp.status_code == 502 and "not allowed" in resp.text
    assert _actions(rw) == []  # given back: MAX refused
    rw.env.server.group_errors = [TimeoutError("no answer")]
    resp = await _post(rw, f"/groups/{gid}/admins", {"user_id": A})
    assert resp.status_code == 504 and _actions(rw) == ["set_group_admin"]  # kept: it may be done (R17-1)


async def test_channel_admin_defaults_and_bits(rw):
    _bot(rw)
    cid = await _channel(rw, members=(A, BOT))
    resp = await _post(rw, f"/groups/{cid}/admins", {"user_id": BOT})
    assert (resp.json()["mask"], resp.json()["is_bot"]) == (1850, True)
    resp = await _post(rw, f"/groups/{cid}/admins", {"user_id": A, "permissions": ["delete_message"]})
    assert resp.json()["mask"] == 1024
    resp = await _post(rw, f"/groups/{cid}/admins", {"user_id": A, "permissions": ["call"]})
    assert resp.status_code == 422 and "group rights, not for a channel" in resp.text


# ---------------------------------------------------------------------------
# get_my_rights, list_members, get_chat (R5-5, R5-6)
# ---------------------------------------------------------------------------

async def test_my_rights_by_bits(rw):
    gid = await _create(rw, user_ids=(A,))
    mine = (await _get(rw, f"/chats/{gid}/my_rights")).json()
    assert (mine["mask"], mine["is_creator"], mine["can_delete_messages"], mine["can_view_stats"]) == (
        4095, True, True, True)
    await _foreign(rw)
    rw.env.server.groups[FOREIGN]["admins"] = [9999, ME]
    rw.env.server.groups[FOREIGN]["adminParticipants"] = {str(ME): {"permissions": 17}}
    rights = (await _get(rw, f"/chats/{FOREIGN}/my_rights")).json()
    assert (rights["mask"], rights["can_delete_messages"], rights["can_pin_messages"],
            rights["can_read_all_messages"], rights["can_change_invite_link"]) == (17, True, True, False, False)
    assert rights["permissions"] == ["pin_message", "delete_message"]
    rw.env.server.groups[FOREIGN]["adminParticipants"] = {str(ME): {"permissions": 1024}}  # a group: not delete
    assert (await _get(rw, f"/chats/{FOREIGN}/my_rights")).json()["can_delete_messages"] is False


async def test_my_rights_in_a_channel(rw):
    await _channel(rw, owner=9999)
    rw.env.server.groups[FOREIGN]["admins"] = [9999, ME]
    rw.env.server.groups[FOREIGN]["adminParticipants"] = {str(ME): {"permissions": 1024 | 2048}}
    rights = (await _get(rw, f"/chats/{FOREIGN}/my_rights")).json()
    assert (rights["can_delete_messages"], rights["can_view_stats"], rights["permissions"]) == (
        True, True, ["delete_message", "view_stats"])
    rw.env.server.groups[FOREIGN]["adminParticipants"] = {str(ME): {"permissions": 1}}  # the channel's spare bit
    assert (await _get(rw, f"/chats/{FOREIGN}/my_rights")).json()["can_delete_messages"] is True


async def test_members_admins_and_server_search(rw):
    _bot(rw)
    rw.env.server.users[A] = {"id": A, "names": [{"firstName": "Аня"}], "link": "anya"}
    gid = await _create(rw, user_ids=(A, B, BOT))
    await _post(rw, f"/groups/{gid}/admins", {"user_id": BOT})
    rw.env.server.groups[gid]["adminParticipants"][str(BOT)]["inviterId"] = ME
    admins = (await _get(rw, f"/chats/{gid}/members", role="admins")).json()
    assert [(m["user_id"], m["role"], m["mask"], m["is_bot"]) for m in admins["items"]] == [
        (ME, "creator", 4095, False), (BOT, "admin", 123, True)]
    assert admins["items"][1]["inviter_id"] == ME and "read_all_messages" in admins["items"][1]["permissions"]
    frames = [c[2] for c in _calls(rw, "invoke") if c[1] == int(Opcode.CHAT_MEMBERS)]
    assert frames[-1] == {"chatId": gid, "type": "ADMIN", "count": 100}
    found = (await _get(rw, f"/chats/{gid}/members", search="аня")).json()
    assert [m["user_id"] for m in found["items"]] == [A] and found["items"][0]["mask"] is None
    frames = [c[2] for c in _calls(rw, "invoke") if c[1] == int(Opcode.CHAT_MEMBERS)]
    assert frames[-1] == {"chatId": gid, "type": "MEMBER", "count": 100, "query": "аня"}
    plain = (await _get(rw, f"/chats/{gid}/members")).json()
    assert {m["user_id"] for m in plain["items"]} == {ME, A, B, BOT}
    assert (await _get(rw, f"/chats/{gid}/members", role="owners")).status_code == 422


async def test_get_chat_has_access_and_options(rw):
    gid = await _create(rw, user_ids=())
    await _patch(rw, f"/groups/{gid}/settings", {"join_request": True})
    body = (await _get(rw, f"/chats/{gid}")).json()
    assert body["options"] == {"JOIN_REQUEST": True} and "access" in body


# ---------------------------------------------------------------------------
# members: bot payload, removal with messages (R5-7, R5-12)
# ---------------------------------------------------------------------------

async def test_add_bot_with_start_payload(rw):
    _bot(rw)
    gid = await _create(rw, user_ids=())
    resp = await _post(rw, f"/groups/{gid}/members", {"user_ids": [BOT], "start_payload": "ref42"})
    assert resp.status_code == 200 and resp.json()["added"] == [BOT], resp.text
    frame = [c[2] for c in _calls(rw, "invoke") if c[1] == int(Opcode.CHAT_MEMBERS_UPDATE)][-1]
    assert frame == {"chatId": gid, "userIds": [BOT], "showHistory": True, "operation": "add", "startPayload": "ref42"}
    assert _calls(rw, "invite_users_to_group") == []
    invites = len([e for e in _events(rw.id) if e[0] == "group_invite"])
    rw.env.server.group_errors = [ApiError(opcode=77, error="bot.restricted", message="bot is restricted")]
    resp = await _post(rw, f"/groups/{gid}/members", {"user_ids": [BOT + 1], "start_payload": "x"})
    assert resp.status_code == 502 and "bot is restricted" in resp.text
    assert len([e for e in _events(rw.id) if e[0] == "group_invite"]) == invites  # refused: given back
    assert (await _post(rw, f"/groups/{gid}/members", {"user_ids": [BOT], "start_payload": "x" * 513})).status_code == 422


async def test_remove_with_messages_marks_them_deleted(rw):
    gid = await _create(rw, user_ids=(A, B))
    now = 1_800_000_000_000
    rw.env.server.history.setdefault(gid, []).extend([
        {"id": 71, "time": now, "type": "USER", "text": "от A", "sender": A},
        {"id": 72, "time": now + 1, "type": "USER", "text": "от B", "sender": B}])
    session = await rw.env.session()
    for m in rw.env.server.history[gid][-2:]:
        await session.ingest.store_sent(Message.model_validate({**m, "chatId": gid}), gid)
    resp = await _post(rw, f"/groups/{gid}/members/remove", {"user_ids": [A], "delete_messages": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted_message_ids"] == [71]
    frame = [c[2] for c in _calls(rw, "invoke") if c[1] == int(Opcode.CHAT_MEMBERS_UPDATE)][-1]
    assert frame == {"chatId": gid, "userIds": [A], "operation": "remove", "cleanMsgPeriod": -1}
    msgs = {m["message_id"]: m["deleted_at"] for m in (await _get(rw, "/messages", chat_id=gid)).json()["items"]}
    assert msgs[71] is not None and msgs[72] is None
    resp = await _post(rw, f"/groups/{gid}/members/remove", {"user_ids": [B]})
    assert resp.json()["deleted_message_ids"] == [] and _calls(rw, "remove_users_from_group")[-1][3] == 0


# ---------------------------------------------------------------------------
# settings (R5-8)
# ---------------------------------------------------------------------------

async def test_new_settings_and_comments_only_in_a_channel(rw):
    gid = await _create(rw, user_ids=())
    resp = await _patch(rw, f"/groups/{gid}/settings", {"join_request": True, "disable_forward": True,
                                                        "only_admin_can_call": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["options"] == {"JOIN_REQUEST": True, "DISABLE_FORWARD": True, "ONLY_ADMIN_CAN_CALL": False}
    frame = [c[2] for c in _calls(rw, "invoke") if c[1] == int(Opcode.CHAT_UPDATE)][-1]
    assert frame == {"chatId": gid, "options": {"JOIN_REQUEST": True, "DISABLE_FORWARD": True,
                                                "ONLY_ADMIN_CAN_CALL": False}}
    actions = len(_actions(rw))
    resp = await _patch(rw, f"/groups/{gid}/settings", {"comments": True})
    assert resp.status_code == 422 and "channel setting" in resp.text and len(_actions(rw)) == actions
    await _channel(rw)
    resp = await _patch(rw, f"/groups/{FOREIGN}/settings", {"comments": True})
    assert resp.status_code == 200 and resp.json()["options"]["COMMENTS"] is True


# ---------------------------------------------------------------------------
# extra invite links (R5-9)
# ---------------------------------------------------------------------------

async def test_extra_invite_links(rw):
    gid = await _create(rw, user_ids=())
    resp = await _get(rw, f"/groups/{gid}/invite-links")
    assert resp.json() == {"chat_id": gid, "main_link": None, "links": []}
    made = (await _post(rw, f"/groups/{gid}/invite-links", {"title": "Директ"})).json()
    assert (made["title"], made["link"].startswith("https://max.ru/join/")) == ("Директ", True)
    await _post(rw, f"/groups/{gid}/invite-links", {})
    listed = (await _get(rw, f"/groups/{gid}/invite-links")).json()["links"]
    assert [(i["title"], i["join_count"]) for i in listed] == [("Директ", 0), (None, 0)]
    resp = await _post(rw, f"/groups/{gid}/invite-links/revoke", {"link_ids": [made["link_id"], 999]})
    assert resp.json()["revoked"] == [made["link_id"]]
    frames = [c[1:] for c in _calls(rw, "invoke") if c[1] in (221, 222, 223)]
    assert frames[0] == (221, {"chatId": {"type": 1, "primaryId": gid}})
    assert frames[1] == (222, {"chatId": {"type": 1, "primaryId": gid}, "title": "Директ"})
    assert frames[-1] == (223, {"chatId": {"type": 1, "primaryId": gid}, "linkIds": [made["link_id"], 999]})
    assert _actions(rw).count("create_invite_link") == 2 and _actions(rw).count("revoke_invite_links") == 1
    assert (await _post(rw, f"/groups/{gid}/invite-links/revoke", {"link_ids": list(range(21))})).status_code == 422
    assert (await _post(rw, f"/groups/{gid}/invite-links", {"title": "x" * 65})).status_code == 422

    await _channel(rw)
    await _get(rw, f"/groups/{FOREIGN}/invite-links")
    assert [c[2] for c in _calls(rw, "invoke") if c[1] == 221][-1] == {"chatId": {"type": 2, "primaryId": FOREIGN}}

    rw.env.server.group_errors = [ApiError(opcode=222, error="feature.disabled", message="additional links off")]
    resp = await _post(rw, f"/groups/{gid}/invite-links", {"title": "x"})
    assert resp.status_code == 502 and "additional links off" in resp.text


# ---------------------------------------------------------------------------
# ownership (R5-10)
# ---------------------------------------------------------------------------

async def test_transfer_ownership_checks_then_frame(rw):
    gid = await _create(rw, "Передача", user_ids=(A, B))
    good = {"user_id": A, "confirm_title": "Передача", "confirm_user_id": A}
    for body in ({**good, "confirm_title": "передача"}, {**good, "confirm_user_id": B},
                 {**good, "user_id": ME, "confirm_user_id": ME}, good):  # the last: A is not an admin yet
        resp = await _post(rw, f"/groups/{gid}/owner", body)
        assert resp.status_code == 422, (body, resp.text)
    assert "to an admin only" in resp.text
    await _foreign(rw)
    rw.env.server.groups[FOREIGN]["admins"] = [9999, A]
    resp = await _post(rw, f"/groups/{FOREIGN}/owner", {**good, "confirm_title": "Чужая"})
    assert resp.status_code == 422 and "owner only" in resp.text
    assert not [c for c in _calls(rw, "invoke") if c[1] == int(Opcode.CHAT_UPDATE)] and _actions(rw) == []

    await _post(rw, f"/groups/{gid}/admins", {"user_id": A})
    resp = await _post(rw, f"/groups/{gid}/owner", good)
    assert resp.status_code == 200, resp.text
    assert (resp.json()["owner"], resp.json()["new_owner"], resp.json()["my_role"]) == (A, A, "member")
    assert [c[2] for c in _calls(rw, "invoke") if c[1] == int(Opcode.CHAT_UPDATE)] == [
        {"chatId": gid, "changeOwnerId": A}]
    assert (await _chat_row(rw.id, gid)).my_role == "member"
    assert _actions(rw)[-1] == "transfer_group_ownership"


# ---------------------------------------------------------------------------
# channel creation and deletion (R5-11)
# ---------------------------------------------------------------------------

async def test_create_and_delete_a_channel(rw):
    resp = await _post(rw, "/groups", {"title": "Канал R5", "chat_type": "channel"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    cid = body["chat_id"]
    assert (body["chat_type"], body["my_role"], body["members_count"]) == ("channel", "creator", 1)
    frame = [c[2] for c in _calls(rw, "invoke") if c[1] == int(Opcode.MSG_SEND)][-1]
    assert frame["notify"] is True and frame["message"]["attaches"] == [
        {"_type": "CONTROL", "event": "new", "chatType": "CHANNEL", "title": "Канал R5", "userIds": []}]
    assert (await _chat_row(rw.id, cid)).chat_type == "channel" and _calls(rw, "create_group") == []
    assert sorted(e[0] for e in _events(rw.id)) == ["group_create"]
    assert (await _post(rw, f"/groups/{cid}/delete", {"confirm_title": "Канал R5"})).json()["deleted"] is True

    resp = await _post(rw, "/groups", {"title": "Канал 2", "chat_type": "channel", "user_ids": [A]})
    cid = resp.json()["chat_id"]
    resp = await _post(rw, f"/groups/{cid}/delete", {"confirm_title": "Канал 2"})
    assert resp.status_code == 422 and "a channel with subscribers" in resp.text
    await _limits(rw, groups_created_per_day=2)
    resp = await _post(rw, "/groups", {"title": "3", "chat_type": "channel"})
    assert resp.status_code == 429 and resp.json()["limit"] == "groups_created_per_day"
    assert (await _post(rw, "/groups", {"title": "x", "chat_type": "chat"})).status_code == 422


async def test_channel_answer_without_a_chat_keeps_the_slots(rw):
    rw.env.server.frame_answers[int(Opcode.MSG_SEND)] = {"chatId": 1}
    resp = await _post(rw, "/groups", {"title": "x", "chat_type": "channel", "user_ids": [A]})
    assert resp.status_code == 502 and "may have been created" in resp.text
    assert sorted(e[0] for e in _events(rw.id)) == ["group_create", "group_invite"]


# ---------------------------------------------------------------------------
# access: ro, TG, catalog, MCP (R5-20…R5-22 for R5-1)
# ---------------------------------------------------------------------------

async def test_ro_tg_and_mcp_for_the_new_routes(rw, aliases):  # noqa: F811
    await _foreign(rw)
    writes = [("POST", f"/groups/{FOREIGN}/owner", {"user_id": A, "confirm_title": "Чужая", "confirm_user_id": A}),
              ("POST", f"/groups/{FOREIGN}/invite-links", {"title": "x"}),
              ("POST", f"/groups/{FOREIGN}/invite-links/revoke", {"link_ids": [1]})]
    tg_alias, _ = aliases("tgr5", platform="telegram", mode="rw")
    for method, path, body in [*writes, ("GET", f"/groups/{FOREIGN}/invite-links", None)]:
        resp = await rw.http.request(method, "/api/v1" + path, json=body,
                                     headers={**API_KEY, "x-session-alias": tg_alias})
        assert resp.status_code == 501, (path, resp.text)
    sql("UPDATE accounts SET mode = 'ro' WHERE id = %s", (rw.id,))
    import app.authz.middleware as mw

    mw._mode_cache.clear()
    for method, path, body in writes:
        resp = await rw.http.request(method, "/api/v1" + path, json=body, headers=rw.env.h())
        assert resp.status_code == 403, (path, resp.text)
    assert rw.env.server.group_calls == []
    assert (await _get(rw, f"/groups/{FOREIGN}/invite-links")).status_code == 200

    names = lambda mode: {t.name for t in mcp_tools.visible(  # noqa: E731
        mcp_tools.Caller("account", rw.id, rw.env.alias, "max", mode, "http://test"))}
    new = {"transfer_group_ownership", "list_invite_links", "create_invite_link", "revoke_invite_links"}
    assert new <= names("rw") and names("ro") & new == {"list_invite_links"}
    schema = mcp_tools.BY_NAME["set_group_admin"].schema["properties"]
    assert len(schema["permissions"]["items"]["enum"]) == 11 and schema["permissions_mask"]["maximum"] == 4095
    assert mcp_tools.BY_NAME["revoke_invite_links"].schema["properties"]["link_ids"]["items"]["type"] == [
        "string", "integer"]
    assert "set_group_admin without permissions" in mcp_tools.BY_NAME["set_group_admin"].description


# ---------------------------------------------------------------------------
# Canaries: our frames against the appendix's zod schemas (R5-24)
# ---------------------------------------------------------------------------

# 77 CHAT_MEMBERS_UPDATE: chatId z, operation add|remove, userIds I(z), type Gc, showHistory F,
# permissions B, cleanMsgPeriod B, startPayload P
MEMBERS_UPDATE = {"chatId": int, "operation": str, "userIds": list, "type": str, "showHistory": bool,
                  "permissions": int, "cleanMsgPeriod": int, "startPayload": str}
# 59 CHAT_MEMBERS: chatId z, type Gc, marker xs, count B, query P
MEMBERS = {"chatId": int, "type": str, "marker": int, "count": int, "query": str}
# 55 CHAT_UPDATE: chatId z, options _c (bool flags), changeOwnerId z
CHAT_UPDATE = {"chatId": int, "options": dict, "changeOwnerId": int}
# 221/222/223: chatId Oc {type 0..3, primaryId z}, title P, linkIds I(z)
LINKS = {"chatId": dict, "title": str, "linkIds": list}


def _shape(frame: dict, schema: dict, required: set[str]) -> None:
    assert set(frame) <= set(schema), set(frame) - set(schema)
    assert required <= set(frame)
    for key, value in frame.items():
        assert isinstance(value, schema[key]), (key, value)


def test_frames_match_the_client_schemas():
    _shape(groups.admin_frame(-5, 1, 123), MEMBERS_UPDATE, {"chatId", "operation"})
    assert groups.admin_frame(-5, 1, 123) == {"chatId": -5, "userIds": [1], "type": "ADMIN", "operation": "add",
                                              "permissions": 123}
    _shape(groups.remove_frame(-5, [1], clean=True), MEMBERS_UPDATE, {"chatId", "operation"})
    assert groups.remove_frame(-5, [1], clean=True)["cleanMsgPeriod"] == -1
    _shape(groups.invite_frame(-5, [1], True, "p"), MEMBERS_UPDATE, {"chatId", "operation", "startPayload"})
    _shape(groups.members_frame(-5, kind="ADMIN", query="a", marker=3), MEMBERS, {"chatId", "type"})
    assert groups.members_frame(-5, kind="ADMIN")["type"] in ("MEMBER", "ADMIN", "BLOCKED_MEMBER", "JOIN_REQUEST",
                                                               "COMMENTS_BLACKLIST")
    _shape(groups.transfer_frame(-5, 1), CHAT_UPDATE, {"chatId", "changeOwnerId"})
    frame = groups.settings_frame(-5, {"join_request": True, "disable_forward": False, "comments": True})
    _shape(frame, CHAT_UPDATE, {"chatId", "options"})
    # option names of the client's _c schema
    assert set(frame["options"]) <= {"JOIN_REQUEST", "DISABLE_FORWARD", "COMMENTS", "ALL_CAN_PIN_MESSAGE",
                                     "ONLY_OWNER_CAN_CHANGE_ICON_TITLE", "ONLY_ADMIN_CAN_ADD_MEMBER",
                                     "ONLY_ADMIN_CAN_CALL", "MEMBERS_CAN_SEE_PRIVATE_LINK"}
    for frame in (groups.links_frame(-5, channel=False), groups.link_create_frame(-5, channel=True, title="t"),
                  groups.links_revoke_frame(-5, channel=False, link_ids=[1])):
        _shape(frame, LINKS, {"chatId"})
        assert frame["chatId"]["type"] in (0, 1, 2, 3) and frame["chatId"]["primaryId"] == -5
    # 64 MSG_SEND: message Js {cid, attaches [CONTROL new: userIds, title, chatType CHAT|CHANNEL]}, notify F
    frame = groups.create_frame("t", [1], "channel", True)
    assert set(frame) == {"message", "notify"} and set(frame["message"]) == {"cid", "attaches"}
    assert frame["message"]["attaches"][0]["chatType"] in ("CHAT", "CHANNEL")
    assert set(frame["message"]["attaches"][0]) == {"_type", "event", "chatType", "title", "userIds"}


async def test_members_without_presence_or_contact_do_not_fail_the_page(rw):
    """review 18 R18-1: 59's members carry contact? / presence? (both optional in the client)."""
    gid = await _create(rw, user_ids=(A,))
    rw.env.server.frame_answers[int(Opcode.CHAT_MEMBERS)] = {"members": [
        {"contact": {"id": A, "names": [{"firstName": "Аня"}]}},  # no presence
        {"presence": {}},  # no contact
        {"contact": {"names": "broken"}, "presence": {}},  # not a user
        {"contact": {"id": ME, "names": [{"firstName": "Я"}]}, "presence": {}}]}
    resp = await _get(rw, f"/chats/{gid}/members", role="admins")
    assert resp.status_code == 200, resp.text
    assert [m["user_id"] for m in resp.json()["items"]] == [A, ME]
    assert groups.parse_member(None) is None


async def test_channel_kept_when_its_service_message_does_not_parse(rw):
    """review 18 R18-2: the chat and the message are parsed apart."""
    cid = -71_999_000_001
    rw.env.server.frame_answers[int(Opcode.MSG_SEND)] = {
        "chatId": cid, "chat": {"id": cid, "type": "CHANNEL", "status": "ACTIVE", "owner": ME, "title": "К",
                                "participants": {str(ME): 1}},
        "message": {"id": "x", "attaches": [{"_type": "CONTROL", "event": "new"}]}}  # no time / type
    resp = await _post(rw, "/groups", {"title": "К", "chat_type": "channel"})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["chat_id"], resp.json()["message_id"]) == (cid, None)
    assert (await _chat_row(rw.id, cid)).chat_type == "channel"


async def test_unknown_mask_is_null_not_no_rights(rw):
    """review 18 R18-3: adminParticipants {"id": …} without permissions (live data)."""
    await _foreign(rw)
    g = rw.env.server.groups[FOREIGN]
    g["admins"] = [9999, ME, A]
    g["adminParticipants"] = {str(A): {"id": A}, str(ME): {"id": ME}}
    admins = (await _get(rw, f"/chats/{FOREIGN}/members", role="admins")).json()["items"]
    a = next(m for m in admins if m["user_id"] == A)
    assert (a["role"], a["mask"], a["permissions"]) == ("admin", None, None)
    rights = (await _get(rw, f"/chats/{FOREIGN}/my_rights")).json()
    assert (rights["is_admin"], rights["mask"], rights["permissions"]) == (True, None, None)
    caller = mcp_tools.Caller("account", rw.id, rw.env.alias, "max", "rw", "http://test")
    tool = next(t for t in mcp_tools.visible(caller) if t.name == "get_my_rights")
    assert "null — MAX did not tell the rights" in tool.description


async def test_channel_post_and_edit_by_bits(rw):
    """review 18 R18-4: a channel admin posts with 256 (or 1) and edits with 512 (or 1)."""
    await _channel(rw, owner=9999)
    g = rw.env.server.groups[FOREIGN]
    g["admins"] = [9999, ME]
    for mask, post, edit in ((16, False, False), (256, True, False), (512, False, True), (1, True, True)):
        g["adminParticipants"] = {str(ME): {"permissions": mask}}
        rights = (await _get(rw, f"/chats/{FOREIGN}/my_rights")).json()
        assert (rights["can_post_messages"], rights["can_edit_messages"]) == (post, edit), mask
    g["adminParticipants"] = {str(ME): {"id": ME}}  # unknown mask: as before R5
    rights = (await _get(rw, f"/chats/{FOREIGN}/my_rights")).json()
    assert (rights["can_post_messages"], rights["can_edit_messages"]) == (True, True)


async def test_non_owner_gives_only_its_own_rights(rw):
    """review 18 R18-5: the client's default for a non-owner is its own bits without add_admin."""
    _bot(rw)
    await _foreign(rw)
    g = rw.env.server.groups[FOREIGN]
    g["participants"].update({str(B): 1, str(BOT): 1})
    g["admins"] = [9999, ME]
    g["adminParticipants"] = {str(ME): {"permissions": 2 | 4 | 16 | 32 | 128}}  # no 1, 8, 64
    resp = await _post(rw, f"/groups/{FOREIGN}/admins", {"user_id": A})
    assert (resp.status_code, resp.json()["mask"]) == (200, 251 & (2 | 16 | 32 | 128)), resp.text
    resp = await _post(rw, f"/groups/{FOREIGN}/admins", {"user_id": BOT})
    assert resp.json()["mask"] == 123 & (2 | 16 | 32)
    resp = await _post(rw, f"/groups/{FOREIGN}/admins", {"user_id": B, "permissions": ["delete_message"]})
    assert resp.status_code == 422 and "the account has no delete_message itself" in resp.text
    resp = await _post(rw, f"/groups/{FOREIGN}/admins", {"user_id": B, "permissions": ["add_admin"]})
    assert (resp.status_code, resp.json()["mask"]) == (200, 4)
    sent = len(_admin_frames(rw))
    g["adminParticipants"] = {str(ME): {"permissions": 4}}
    resp = await _post(rw, f"/groups/{FOREIGN}/admins", {"user_id": B})
    assert resp.status_code == 422 and len(_admin_frames(rw)) == sent
    g["adminParticipants"] = {str(ME): {"id": ME}}  # own mask unknown: as is
    assert (await _post(rw, f"/groups/{FOREIGN}/admins", {"user_id": B})).json()["mask"] == 251
