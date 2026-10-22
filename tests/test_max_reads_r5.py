"""MAX release 5, PR R5-2: reads — who reacted, poll voters, message link, transcription, search
in a chat, post stats (plan 2026-10-22 Р5 Р-67…Р-71, Р-77; QA R5-16, R5-17, R5-20…R5-22, R5-24, R5-26).
"""

import pytest
from pymax import ApiError
from pymax.protocol import Opcode

import app.authz.middleware as mw
from app.max import frames
from app.max.api import messages as mx_messages
from app.mcp import tools as mcp_tools
from app.repo import chats
from app.repo.rows import ChatRow
from app.storage import account_db
from tests.isolation_env import sql
from tests.max_env import API_KEY, aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_bots import _frames
from tests.test_max_groups import FOREIGN, _foreign, _get, _post
from tests.test_max_ingest import DIALOG, ME
from tests.test_max_write_limits import _events, clock, rw  # noqa: F401 — pytest fixtures
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture

T = 1_800_000_000_000


def _say(w, chat: int, mid: int, text: str = "", sender: int = 5001, **extra) -> None:
    w.env.server.history.setdefault(chat, []).append(
        {"id": mid, "time": T + mid, "type": "USER", "text": text, "sender": sender, **extra})


async def _dialog(w, chat: int = DIALOG) -> int:
    async with account_db(w.id) as db:
        await chats.upsert_chat(db, w.id, ChatRow(chat, "private", title="Собеседник"))
        await db.commit()
    return chat


# ---------------------------------------------------------------------------
# who reacted (R5-16)
# ---------------------------------------------------------------------------

async def test_reactions_with_users_and_without(rw):
    rw.env.server.reactions[(DIALOG, 5)] = {"totalCount": 2, "counters": [{"reaction": "👍", "count": 2}],
                                            "yourReaction": "👍"}
    rw.env.server.reacted[(DIALOG, 5)] = [{"userId": ME, "reaction": "👍"}, {"userId": 5001, "reaction": "👍"}]
    body = (await _get(rw, f"/messages/{DIALOG}/5/reactions")).json()
    assert (body["total_count"], body["my_reaction"]) == (2, "👍")
    assert body["users"] == [{"user_id": ME, "reaction": "👍"}, {"user_id": 5001, "reaction": "👍"}]
    assert _frames(rw, int(Opcode.MSG_GET_DETAILED_REACTIONS)) == [{"chatId": DIALOG, "messageId": 5, "count": 100}]
    rw.env.server.group_errors = [ApiError(opcode=181, error="not.allowed", message="no details")]
    body = (await _get(rw, f"/messages/{DIALOG}/5/reactions")).json()
    assert (body["total_count"], body["users"]) == (2, None) and "no details" in body["users_error"]


# ---------------------------------------------------------------------------
# poll voters, message link (R5-17)
# ---------------------------------------------------------------------------

async def test_poll_voters(rw):
    resp = await _post(rw, "/messages/poll", {"chat_id": DIALOG, "question": "Когда?", "options": ["Пн", "Вт"]})
    assert resp.status_code == 200, resp.text
    mid = resp.json()["message_id"]
    poll_id = rw.env.server.next_poll_id
    rw.env.server.poll_voters[(poll_id, 1)] = [ME, 5001]
    body = (await _get(rw, f"/messages/{DIALOG}/{mid}/voters", answer_id=1)).json()
    assert (body["poll_id"], body["vote_count"], [v["user_id"] for v in body["voters"]]) == (poll_id, 2, [ME, 5001])
    assert _frames(rw, int(Opcode.VOTERS_LIST_BY_ANSWER)) == [
        {"pollId": poll_id, "chatId": DIALOG, "messageId": mid, "answerId": 1, "count": 100}]
    resp = await _get(rw, f"/messages/{DIALOG}/{mid}/voters", answer_id=9)
    assert resp.status_code == 422 and "not an answer" in resp.text
    _say(rw, DIALOG, 77, attaches=[{"_type": "POLL", "pollId": 1, "version": 1, "title": "?", "settings": 1,
                                    "answers": [{"text": "a", "answerId": 1}],
                                    "state": {"total": 0, "voterPreviewIds": [], "result": []}}])  # 1: anonymous
    resp = await _get(rw, f"/messages/{DIALOG}/77/voters", answer_id=1)
    assert resp.status_code == 422 and "anonymous" in resp.text
    assert len(_frames(rw, int(Opcode.VOTERS_LIST_BY_ANSWER))) == 1  # refusals: no frame


async def test_message_link(rw):
    body = (await _get(rw, f"/messages/{DIALOG}/5/link")).json()
    assert body == {"chat_id": DIALOG, "message_id": 5, "link": f"https://max.ru/c/{DIALOG}/5"}
    assert _frames(rw, 90) == [{"chatId": DIALOG, "messageId": 5}]
    rw.env.server.frame_answers[90] = {}
    assert (await _get(rw, f"/messages/{DIALOG}/5/link")).status_code == 502


# ---------------------------------------------------------------------------
# transcription (R5-17)
# ---------------------------------------------------------------------------

@pytest.fixture
def fast(monkeypatch):
    slept: list[float] = []
    now = [0.0]

    async def sleep(sec):
        slept.append(sec)
        now[0] += sec

    monkeypatch.setattr(mx_messages, "_sleep", sleep)
    monkeypatch.setattr(mx_messages, "_clock", lambda: now[0])
    mx_messages._transcripts.clear()
    yield slept, now
    mx_messages._transcripts.clear()


async def test_transcribe_polls_until_ready_and_caches(rw, fast):
    slept, now = fast
    _say(rw, DIALOG, 60, attaches=[{"_type": "AUDIO", "audioId": 6000, "duration": 3, "url": "https://a"}])
    rw.env.server.transcripts[6000] = [{"transcriptionStatus": 0}, {"transcriptionStatus": 0},
                                       {"transcriptionStatus": 1, "transcription": "Привет"}]
    body = (await _post(rw, f"/messages/{DIALOG}/60/transcribe")).json()
    assert (body["status"], body["text"], body["media_id"], body["index"]) == ("done", "Привет", 6000, 0)
    assert slept == [2.0, 2.0]
    assert _frames(rw, int(Opcode.TRANSCRIBE_MEDIA))[-1] == {"mediaId": 6000, "messageId": 60, "chatId": DIALOG}
    calls = len(_frames(rw, int(Opcode.TRANSCRIBE_MEDIA)))
    now[0] += 600
    again = (await _post(rw, f"/messages/{DIALOG}/60/transcribe")).json()
    assert again["cached"] is True and again["text"] == "Привет"
    assert len(_frames(rw, int(Opcode.TRANSCRIBE_MEDIA))) == calls  # ready: from the cache for an hour


async def test_transcribe_gives_up_after_20_s_and_refuses_text(rw, fast):
    slept, now = fast
    _say(rw, DIALOG, 61, attaches=[{"_type": "VIDEO", "videoId": 6100, "height": 1, "width": 1, "thumbnail": "",
                                    "token": "t", "videoType": 1}])
    rw.env.server.transcripts[6100] = [{"transcriptionStatus": 0}]
    body = (await _post(rw, f"/messages/{DIALOG}/61/transcribe", {"index": 0})).json()
    assert (body["status"], body["text"]) == ("processing", None) and sum(slept) == 20.0
    calls = len(_frames(rw, int(Opcode.TRANSCRIBE_MEDIA)))
    now[0] += 5  # inside the 10 s gap: no MAX call
    assert (await _post(rw, f"/messages/{DIALOG}/61/transcribe")).json()["cached"] is True
    assert len(_frames(rw, int(Opcode.TRANSCRIBE_MEDIA))) == calls
    now[0] += 10
    rw.env.server.transcripts[6100] = [{"transcriptionStatus": 3}]
    assert (await _post(rw, f"/messages/{DIALOG}/61/transcribe")).json()["status"] == "failed"
    _say(rw, DIALOG, 62, "просто текст")
    resp = await _post(rw, f"/messages/{DIALOG}/62/transcribe")
    assert resp.status_code == 422 and "no voice" in resp.text


# ---------------------------------------------------------------------------
# search in a chat (R5-17)
# ---------------------------------------------------------------------------

async def test_search_chat_messages_stores_what_it_finds(rw):
    await _dialog(rw)
    _say(rw, DIALOG, 70, "Счёт на оплату окон")
    _say(rw, DIALOG, 71, "Привет")
    body = (await _get(rw, f"/messages/{DIALOG}/search", query="счёт")).json()
    assert [i["message_id"] for i in body["items"]] == [70] and body["total"] == 1
    assert _frames(rw, int(Opcode.MSG_SEARCH)) == [{"chatId": DIALOG, "query": "счёт", "count": 20}]
    assert (await _get(rw, f"/messages/{DIALOG}/70")).json()["text"] == "Счёт на оплату окон"
    assert _events(rw.id) == []  # a read
    assert (await _get(rw, f"/messages/{DIALOG}/search", query="x", limit=31)).status_code == 422
    assert (await _get(rw, "/messages/424242/search", query="x")).status_code == 404  # unknown chat
    rw.env.server.frame_answers[int(Opcode.MSG_SEARCH)] = {"result": [{"x": 1}, {"id": 72, "time": T, "type": "USER",
                                                                                 "text": "сам", "sender": 1}]}
    assert [i["message_id"] for i in (await _get(rw, f"/messages/{DIALOG}/search", query="a")).json()["items"]] == [72]


# ---------------------------------------------------------------------------
# post stats (R5-26)
# ---------------------------------------------------------------------------

async def test_post_stats(rw):
    await _foreign(rw, kind="channel")
    rw.env.server.post_stats[(FOREIGN, 10)] = {"views": 120, "forwards": 3}
    rw.env.server.post_stats[(FOREIGN, 11)] = {"views": 7}
    body = (await rw.http.get(f"/api/v1/messages/{FOREIGN}/stats", headers=rw.env.h(),
                              params=[("message_ids", 10), ("message_ids", 11), ("message_ids", 12)])).json()
    assert body == {"chat_id": FOREIGN, "missing": [12], "items": [
        {"message_id": 10, "views": 120, "forwards": 3}, {"message_id": 11, "views": 7, "forwards": None}]}
    assert _frames(rw, int(Opcode.MSG_GET_STAT)) == [{"chatId": FOREIGN, "messageIds": [10, 11, 12], "readOnly": True}]
    many = [("message_ids", i) for i in range(1, 102)]
    resp = await rw.http.get(f"/api/v1/messages/{FOREIGN}/stats", headers=rw.env.h(), params=many)
    assert resp.status_code == 422
    resp = await rw.http.get(f"/api/v1/messages/{FOREIGN}/stats", headers=rw.env.h(), params=many[:100])
    assert resp.status_code == 200 and len(resp.json()["missing"]) == 98
    assert (await _get(rw, f"/messages/{FOREIGN}/stats")).status_code == 422  # 0 ids
    await _dialog(rw)
    resp = await _get(rw, f"/messages/{DIALOG}/stats", message_ids=1)
    assert resp.status_code == 422 and "private" in resp.text
    rw.env.server.group_errors = [ApiError(opcode=74, error="stat.denied", message="denied")]
    assert (await _get(rw, f"/messages/{FOREIGN}/stats", message_ids=10)).status_code == 502
    assert len(_frames(rw, int(Opcode.MSG_GET_STAT))) == 3 and _events(rw.id) == []


# ---------------------------------------------------------------------------
# ro, TG, route names, MCP (R5-20…R5-22)
# ---------------------------------------------------------------------------

READS = [("GET", f"/messages/{DIALOG}/5/link", None, "get_message_link"),
         ("GET", f"/messages/{DIALOG}/5/voters?answer_id=1", None, "get_poll_voters"),
         ("POST", f"/messages/{DIALOG}/5/transcribe", {}, "transcribe_voice"),
         ("GET", f"/messages/{DIALOG}/search?query=a", None, "search_chat_messages"),
         ("GET", f"/messages/{FOREIGN}/stats?message_ids=1", None, "get_post_stats"),
         ("GET", "/bots/info?bot_id=1", None, "get_bot_info")]


async def test_reads_route_names_tg_and_ro(rw, aliases):  # noqa: F811
    import app.main as main_module

    table = main_module.app.state.route_table
    for method, path, _, tool in READS:
        assert table.resolve(method, "/api/v1/_max" + path.split("?")[0]) == tool, path
    tg_alias, _ = aliases("tgr5r", platform="telegram", mode="rw")
    for method, path, body, tool in READS:
        resp = await rw.http.request(method, "/api/v1" + path, json=body, headers={**API_KEY, "x-session-alias": tg_alias})
        assert resp.status_code == 501 and resp.json()["tool"] == tool, (tool, resp.text)
    await _foreign(rw, kind="channel")
    await _dialog(rw)
    sql("UPDATE accounts SET mode = 'ro' WHERE id = %s", (rw.id,))
    mw._mode_cache.clear()
    rw.env.server.post_stats[(FOREIGN, 1)] = {"views": 1}
    for method, path in (("GET", f"/messages/{FOREIGN}/stats?message_ids=1"), ("GET", f"/messages/{DIALOG}/5/link"),
                         ("GET", f"/messages/{DIALOG}/search?query=a")):
        resp = await rw.http.request(method, "/api/v1" + path, headers=rw.env.h())
        assert resp.status_code == 200, (path, resp.text)


async def test_mcp_tool_counts_and_calls(rw):
    names = lambda platform, mode: {t.name for t in mcp_tools.visible(  # noqa: E731
        mcp_tools.Caller("account", rw.id, rw.env.alias, platform, mode, "http://test"))}
    max_rw, max_ro = names("max", "rw"), names("max", "ro")
    assert (len(max_rw), len(max_ro), len(names("telegram", "rw")), len(names("telegram", "ro"))) == (77, 31, 46, 21)
    reads = {"get_bot_info", "get_message_link", "get_poll_voters", "transcribe_voice", "search_chat_messages",
             "get_post_stats"}
    assert reads | {"press_button", "start_bot", "block_user", "unblock_user"} <= max_rw
    assert reads <= max_ro and not {"press_button", "start_bot", "block_user"} & max_ro
    assert "reading does not count a view" in mcp_tools.BY_NAME["get_post_stats"].description

    await _foreign(rw, kind="channel")
    rw.env.server.post_stats[(FOREIGN, 10)] = {"views": 5, "forwards": 1}
    caller = mcp_tools.Caller("account", rw.id, rw.env.alias, "max", "ro", "http://test")
    tool = mcp_tools.BY_NAME["get_post_stats"]
    out = await tool.call(caller, mcp_tools.coerce_ids({"chat_id": str(FOREIGN), "message_ids": ["10", 11]},
                                                       tool.schema))
    assert out == {"chat_id": FOREIGN, "items": [{"message_id": 10, "views": 5, "forwards": 1}], "missing": [11]}


# ---------------------------------------------------------------------------
# Canaries against the appendix (R5-24)
# ---------------------------------------------------------------------------

def test_read_frames_match_the_client_schemas():
    # 181: chatId z, messageId z, reaction P?, marker xs?, count B?
    assert set(frames.detailed_reactions_frame(1, 2, marker=3)) == {"chatId", "messageId", "count", "marker"}
    # 305: pollId xs, chatId z, messageId z, answerId bs, marker V?, count B?
    assert set(frames.voters_frame(1, 2, 3, 4, count=5)) == {"pollId", "chatId", "messageId", "answerId", "count"}
    # 90: chatId z, messageId z
    assert frames.message_link_frame(1, 2) == {"chatId": 1, "messageId": 2}
    # 202: mediaId xs, messageId z, chatId z
    assert frames.transcribe_frame(1, 2, 3) == {"mediaId": 3, "messageId": 2, "chatId": 1}
    # 73: chatId z, query P, count B, marker z?
    assert frames.search_frame(1, "q", 20) == {"chatId": 1, "query": "q", "count": 20}
    assert frames.search_frame(1, "q", 20, 9)["marker"] == 9
    # 74: chatId z, messageIds I(z), readOnly F? — always true (Р-77)
    assert frames.post_stats_frame(1, [2, 3]) == {"chatId": 1, "messageIds": [2, 3], "readOnly": True}
