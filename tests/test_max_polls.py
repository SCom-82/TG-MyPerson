"""MAX polls, votes, pins and reactions (plan 2026-10-19 Р4 PR-R4-1, Р-33, Р-44…Р-47; QA R4-5…R4-7).

Frames on PyMax's own MessageService (the stub App of test_max_media_send),
routes on the fake MAX with the write limits.
"""

from types import SimpleNamespace

from pymax import ApiError
from pymax.protocol import Opcode

import app.authz.middleware as mw
from app.max import frames
from app.max.normalize import parse_stored
from tests.isolation_env import sql
from tests.max_env import ADMIN_KEY, API_KEY, aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_groups import _create, _post
from tests.test_max_ingest import DIALOG
from tests.test_max_media_send import stub  # noqa: F401 — pytest fixture
from tests.test_max_write_limits import OFF, _events, _limits, clock, rw  # noqa: F401 — pytest fixtures
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture

STATE = {"total": 1, "voterPreviewIds": [],
         "result": [{"answerId": 1, "voteCount": 1, "votes": [], "rate": 100, "options": 0},
                    {"answerId": 2, "voteCount": 0, "votes": [], "rate": 0, "options": 0}]}


def _poll_message(mid: int, settings: int = 0, with_ids: bool = True) -> dict:
    answers = [{"text": "да", **({"answerId": 1} if with_ids else {})},
               {"text": "нет", **({"answerId": 2} if with_ids else {})}]
    return {"id": mid, "time": 1_791_000_000_000, "type": "USER", "text": "", "sender": 777,
            "attaches": [{"_type": "POLL", "pollId": 55, "version": 1, "title": "R4?", "settings": settings,
                          "answers": answers, "state": STATE}]}


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------

async def test_poll_vote_pin_unpin_reactions_frames(stub):  # noqa: F811
    poll = frames.poll_attach("R4?", ["да", "нет"], anonymous=True, multiple=True)
    await stub.api.messages.send_message(5, None, attachments=[poll])
    [send] = stub.sent(Opcode.MSG_SEND)
    assert send["message"]["attaches"] == [{"_type": "POLL", "title": "R4?", "settings": 3,
                                            "answers": [{"text": "да"}, {"text": "нет"}]}]
    assert int(frames.poll_attach("q", ["a", "b"], anonymous=False, multiple=False).settings) == 0

    stub.answer = {"state": STATE}
    state = await stub.api.messages.vote_poll(5, 9001, 55, [1])
    assert stub.sent(Opcode.SEND_VOTE) == [{"chatId": 5, "messageId": 9001, "pollId": 55, "answersIds": [1]}]
    assert frames.poll_state(state) == {"total": 1, "answers": [{"answer_id": 1, "vote_count": 1},
                                                                {"answer_id": 2, "vote_count": 0}]}

    stub.answer = {"chat": {"id": 5}}
    await stub.api.messages.pin_message(5, 9001, notify_pin=False)
    await frames.unpin(SimpleNamespace(_app=stub), 5)
    assert stub.sent(Opcode.CHAT_UPDATE) == [{"chatId": 5, "notifyPin": False, "pinMessageId": 9001},
                                             {"chatId": 5, "pinMessageId": 0, "notifyPin": False}]

    stub.answer = {"messagesReactions": {"9001": {"totalCount": 2, "counters": [{"count": 2, "reaction": "👍"}],
                                                  "yourReaction": "👍"}}}
    found = await stub.api.messages.get_reactions(5, [9001])
    assert stub.sent(Opcode.MSG_GET_REACTIONS) == [{"chatId": 5, "messageIds": [9001]}]
    assert (found["9001"].total_count, found["9001"].your_reaction) == (2, "👍")


def test_poll_view_takes_answer_ids_from_the_state_when_answers_lack_them():
    assert frames.find_poll(SimpleNamespace(attaches=[])) is None
    poll = frames.find_poll(parse_stored({**_poll_message(1, settings=2 | 8, with_ids=False), "chatId": 5}))
    view = frames.poll_view(poll)
    assert [(a["answer_id"], a["text"], a["vote_count"]) for a in view["answers"]] == [(1, "да", 1), (2, "нет", 0)]
    assert (view["multiselect"], view["closed"], view["anonymous"], view["total"]) == (True, True, False, 1)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

async def _poll(w, chat: int = DIALOG, **extra):
    return await w.http.post("/api/v1/messages/poll", headers=w.env.h(),
                             json={"chat_id": chat, "question": "R4?", "options": ["да", "нет"], **extra})


async def _vote(w, chat: int, mid: int, **body):
    return await w.http.post(f"/api/v1/messages/{chat}/{mid}/vote", headers=w.env.h(), json=body)


async def test_send_poll_and_the_poll_in_the_message(rw):
    await _limits(rw, **OFF)
    resp = await _poll(rw, is_anonymous=False, allows_multiple=True)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answers"] == [{"answer_id": 1, "text": "да"}, {"answer_id": 2, "text": "нет"}]
    [[sent]] = rw.env.server.sent_attachments
    assert (sent.title, int(sent.settings)) == ("R4?", 2)

    [item] = (await rw.http.get(f"/api/v1/messages?chat_id={DIALOG}", headers=rw.env.h())).json()["items"]
    assert item["message_type"] == "poll"
    assert item["poll"]["poll_id"] == body["poll_id"] and item["poll"]["multiselect"] is True
    assert [a["answer_id"] for a in item["poll"]["answers"]] == [1, 2]
    resp = await rw.http.post("/api/v1/messages", headers=rw.env.h(), json={"chat_id": DIALOG, "text": "без опроса"})
    plain = (await rw.http.get(f"/api/v1/messages/{DIALOG}/{resp.json()['message_id']}", headers=rw.env.h())).json()
    assert plain["poll"] is None

    for extra, parameter in (({"quiz_correct_option": 0}, "quiz_correct_option"),
                             ({"schedule_date": "2026-10-20T10:00:00Z"}, "schedule_date")):
        resp = await _poll(rw, **extra)
        assert resp.status_code == 501 and resp.json()["parameter"] == parameter
    assert [e[0] for e in _events(rw.id)] == ["send", "send"]


async def test_vote_checks_before_the_slot_then_votes(rw):
    await _limits(rw, **OFF)
    mid = (await _poll(rw)).json()["message_id"]
    for body, status in (({"answer_ids": [9]}, 422), ({"answer_ids": [1, 2]}, 422),
                         ({"answer_ids": [1], "poll_id": 1}, 422)):
        resp = await _vote(rw, DIALOG, mid, **body)
        assert resp.status_code == status, (body, resp.text)
    plain = (await rw.http.post("/api/v1/messages", headers=rw.env.h(),
                                json={"chat_id": DIALOG, "text": "x"})).json()["message_id"]
    assert (await _vote(rw, DIALOG, plain, answer_ids=[1])).status_code == 404
    rw.env.server.history[DIALOG].append(_poll_message(77, settings=8))  # closed, only in MAX
    resp = await _vote(rw, DIALOG, 77, answer_ids=[1])
    assert resp.status_code == 409 and "get_message" in rw.client.calls  # no stored raw → read from MAX
    assert [w for w in rw.env.server.writes if w[0] == "vote_poll"] == []
    assert [e[0] for e in _events(rw.id)] == ["send", "send"]

    resp = await _vote(rw, DIALOG, mid, answer_ids=[2])
    assert resp.status_code == 200, resp.text
    poll_id = rw.env.server.votes[0][2]
    assert resp.json() == {"chat_id": DIALOG, "message_id": mid, "poll_id": poll_id, "answer_ids": [2],
                           "state": {"total": 1, "answers": [{"answer_id": 1, "vote_count": 0},
                                                             {"answer_id": 2, "vote_count": 1}]}}
    assert [e[:2] for e in _events(rw.id)][-1] == ("action", "vote_poll")

    rw.env.server.write_errors = [ApiError(opcode=304, error="poll.vote.denied", message="already voted")]
    resp = await _vote(rw, DIALOG, mid, answer_ids=[1])
    assert resp.status_code == 502 and "already voted" in resp.text
    assert len([e for e in _events(rw.id) if e[0] == "action"]) == 1  # given back


async def test_pin_unpin_checked_by_the_fresh_chat(rw):
    await _limits(rw, **OFF)
    gid = await _create(rw, user_ids=())
    mid = (await rw.http.post("/api/v1/messages", headers=rw.env.h(), json={"chat_id": gid, "text": "важно"})).json()[
        "message_id"]
    resp = await _post(rw, f"/messages/{gid}/{mid}/pin?notify=false")
    assert resp.status_code == 200 and resp.json() == {"status": "pinned", "chat_id": gid, "message_id": mid}
    assert [c for c in rw.env.server.group_calls if c[0] == "pin_message"] == [("pin_message", gid, mid, False)]

    resp = await _post(rw, f"/messages/{gid}/{mid}/unpin")
    assert resp.status_code == 200 and resp.json()["status"] == "unpinned", resp.text
    unpin = [c for c in rw.env.server.group_calls if c[0] == "invoke"]
    assert unpin == [("invoke", 55, {"chatId": gid, "pinMessageId": 0, "notifyPin": False})]

    await _post(rw, f"/messages/{gid}/{mid}/pin")
    rw.env.server.unpin_ignored = True
    resp = await _post(rw, f"/messages/{gid}/{mid}/unpin")
    assert resp.status_code == 502 and "still pinned" in resp.text
    other = (await rw.http.post("/api/v1/messages", headers=rw.env.h(), json={"chat_id": gid, "text": "ещё"})).json()[
        "message_id"]
    rw.env.server.pinned[gid] = mid

    async def pins_nothing(chat_id, message_id, notify_pin):
        rw.env.server.group_calls.append(("pin_message", chat_id, message_id))
        return True

    rw.client.pin_message = pins_nothing
    resp = await _post(rw, f"/messages/{gid}/{other}/pin")
    assert resp.status_code == 502 and f"has message {mid} pinned" in resp.text
    # every call reached MAX: the 502s keep their slots
    assert [e[1] for e in _events(rw.id) if e[0] == "action"] == [
        "pin_message", "unpin_message", "pin_message", "unpin_message", "pin_message"]


async def test_reactions_read_and_ro_sees_them(rw):
    rw.env.server.reactions[(DIALOG, 5)] = {"totalCount": 3, "yourReaction": None,
                                            "counters": [{"count": 2, "reaction": "👍"}, {"count": 1, "reaction": "🔥"}]}
    resp = await rw.http.get(f"/api/v1/messages/{DIALOG}/5/reactions", headers=rw.env.h())
    assert resp.json() == {"chat_id": DIALOG, "message_id": 5, "total_count": 3, "my_reaction": None,
                           "counters": [{"reaction": "👍", "count": 2}, {"reaction": "🔥", "count": 1}],
                           "users": [], "next_marker": None}  # who reacted: Р5 Р-67
    rw.env.server.reactions = None
    sql("UPDATE accounts SET mode = 'ro' WHERE id = %s", (rw.id,))
    mw._mode_cache.clear()
    resp = await rw.http.get(f"/api/v1/messages/{DIALOG}/6/reactions", headers=rw.env.h())
    assert resp.status_code == 200 and resp.json()["total_count"] == 0 and resp.json()["counters"] == []
    for path, body in ((f"/messages/{DIALOG}/6/vote", {"answer_ids": [1]}), (f"/messages/{DIALOG}/6/pin", {}),
                       (f"/messages/{DIALOG}/6/unpin", {})):
        assert (await _post(rw, path, body)).status_code == 403
    assert _events(rw.id) == [] and rw.env.server.writes == []


async def test_vote_frame_is_pymax_and_an_unparsed_answer_keeps_the_slot(rw, stub):  # noqa: F811
    """review 17 R17-1: our SEND_VOTE is PyMax's frame; the vote reached MAX, its slot stays."""
    stub.answer = {"state": STATE}
    await stub.api.messages.vote_poll(5, 9001, 55, [1, 2])
    await frames.vote(SimpleNamespace(_app=stub), 5, 9001, 55, [1, 2])
    pymax_frame, ours = stub.sent(Opcode.SEND_VOTE)
    assert ours == pymax_frame
    assert frames.poll_state(frames.parse_vote({"state": STATE}))["total"] == 1

    await _limits(rw, **OFF)
    mid = (await _poll(rw)).json()["message_id"]
    rw.env.server.vote_answer = {"state": {"result": "x"}}
    resp = await _vote(rw, DIALOG, mid, answer_ids=[1])
    assert resp.status_code == 502 and "MAX took the vote" in resp.text, resp.text
    assert rw.env.server.votes and [e[1] for e in _events(rw.id) if e[0] == "action"] == ["vote_poll"]


async def test_unpin_only_the_pinned_message(rw):
    """QA-R4 D4: our unpin frame unpins whatever is pinned — so only message_id itself."""
    await _limits(rw, **OFF)
    gid = await _create(rw, user_ids=())
    send = lambda t: rw.http.post("/api/v1/messages", headers=rw.env.h(), json={"chat_id": gid, "text": t})  # noqa: E731
    x, y = (await send("x")).json()["message_id"], (await send("y")).json()["message_id"]
    resp = await _post(rw, f"/messages/{gid}/{y}/unpin")
    assert resp.status_code == 404 and "no message is pinned" in resp.text
    await _post(rw, f"/messages/{gid}/{x}/pin")
    resp = await _post(rw, f"/messages/{gid}/{y}/unpin")
    assert resp.status_code == 409 and f"pinned: {x}" in resp.text
    assert rw.env.server.pinned[gid] == x
    assert [c for c in rw.env.server.group_calls if c[0] == "invoke"] == []
    assert [e[1] for e in _events(rw.id) if e[0] == "action"] == ["pin_message"]
    assert (await _post(rw, f"/messages/{gid}/{x}/unpin")).json()["status"] == "unpinned"


# ---------------------------------------------------------------------------
# Р4.1 (live check 19.10)
# ---------------------------------------------------------------------------

async def test_poll_is_not_anonymous_on_max_unless_asked_and_then_501(rw):
    """MAX refused ANONYMOUS (settings 1) with "Invalid poll settings" and dropped the socket."""
    await _limits(rw, **OFF)
    resp = await rw.http.post("/api/v1/messages/poll", headers=rw.env.h(),
                              json={"chat_id": DIALOG, "question": "q", "options": ["a", "b"]})
    assert resp.status_code == 200, resp.text
    [[sent]] = rw.env.server.sent_attachments
    assert int(sent.settings) == 0
    resp = await _poll(rw, is_anonymous=True)
    assert resp.status_code == 501 and resp.json()["parameter"] == "is_anonymous", resp.text
    assert len(rw.env.server.sent_attachments) == 1 and [e[0] for e in _events(rw.id)] == ["send"]


async def test_get_message_refreshes_the_poll_counts(rw):
    """A vote elsewhere (another account, the app) does not come as a frame: get_message re-reads."""
    await _limits(rw, **OFF)
    mid = (await _poll(rw)).json()["message_id"]
    stored = (await rw.http.get(f"/api/v1/messages/{DIALOG}/{mid}", headers=rw.env.h())).json()["poll"]
    await rw.client.vote_poll(DIALOG, mid, stored["poll_id"], [2])  # someone votes in MAX
    poll = (await rw.http.get(f"/api/v1/messages/{DIALOG}/{mid}", headers=rw.env.h())).json()["poll"]
    assert poll["total"] == 1 and [a["vote_count"] for a in poll["answers"]] == [0, 1]
    [listed] = (await rw.http.get(f"/api/v1/messages?chat_id={DIALOG}", headers=rw.env.h())).json()["items"]
    assert listed["poll"]["total"] == 1  # the stored copy is updated too

    async def down(chat_id, message_id):
        raise TimeoutError("no answer")

    rw.client.get_message = down
    poll = (await rw.http.get(f"/api/v1/messages/{DIALOG}/{mid}", headers=rw.env.h())).json()["poll"]
    assert poll["total"] == 1  # the stored copy, not an error


async def test_vote_updates_the_stored_poll(rw):
    await _limits(rw, **OFF)
    mid = (await _poll(rw)).json()["message_id"]
    assert (await _vote(rw, DIALOG, mid, answer_ids=[1])).status_code == 200
    [listed] = (await rw.http.get(f"/api/v1/messages?chat_id={DIALOG}", headers=rw.env.h())).json()["items"]
    assert listed["poll"]["total"] == 1 and listed["poll"]["answers"][0]["vote_count"] == 1


async def test_get_chat_shows_the_pinned_message(rw):
    await _limits(rw, **OFF)
    gid = await _create(rw, user_ids=())
    mid = (await rw.http.post("/api/v1/messages", headers=rw.env.h(), json={"chat_id": gid, "text": "важно"})).json()[
        "message_id"]
    chat = (await rw.http.get(f"/api/v1/chats/{gid}", headers=rw.env.h())).json()
    assert chat["pinned_message"] is None
    await _post(rw, f"/messages/{gid}/{mid}/pin")
    chat = (await rw.http.get(f"/api/v1/chats/{gid}", headers=rw.env.h())).json()
    assert chat["pinned_message"]["message_id"] == mid and chat["pinned_message"]["text"] == "важно"
    await _post(rw, f"/messages/{gid}/{mid}/unpin")
    assert (await rw.http.get(f"/api/v1/chats/{gid}", headers=rw.env.h())).json()["pinned_message"] is None
