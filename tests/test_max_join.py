"""MAX join_chat / resolve_chat by a link (plan 2026-10-19 Р4 PR-R4-2, Р-48; QA R4-8).

join/<code> goes through PyMax (CHAT_JOIN 57 / LINK_INFO 89 with "join/…"), a
public https://max.ru/<name> — join_channel with the URL and our own LINK_INFO.
"""

import pytest

import app.authz.middleware as mw
from app.max.api.chats import parse_target
from tests.isolation_env import sql
from tests.max_env import ADMIN_KEY, API_KEY, aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_groups import _chat_row, _post
from tests.test_max_ingest import ME
from tests.test_max_write_limits import _events, _limits, _status, clock, rw  # noqa: F401 — pytest fixtures
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture

GROUP_ID, CHANNEL_ID = -73_000_000_001, -73_000_000_002


def _setup(w) -> None:
    w.env.server.groups[GROUP_ID] = {"id": GROUP_ID, "type": "CHAT", "status": "ACTIVE", "owner": 9999,
                                     "title": "Клуб", "participants": {"9999": 1}, "admins": [9999],
                                     "participantsCount": 1, "description": "оконщики"}
    w.env.server.groups[CHANNEL_ID] = {"id": CHANNEL_ID, "type": "CHANNEL", "status": "ACTIVE", "owner": 9999,
                                       "title": "Новости", "participants": {"9999": 1}, "admins": [9999],
                                       "participantsCount": 1200, "link": "https://max.ru/news"}
    w.env.server.links.update({"join/AbC": GROUP_ID, "https://max.ru/news": CHANNEL_ID})


def _calls(w, *names) -> list[tuple]:
    return [c for c in w.env.server.group_calls if c[0] in names]


@pytest.mark.parametrize(("target", "parsed"), [
    ("https://max.ru/join/AbC", ("join", "join/AbC")),
    ("max.ru/join/AbC", ("join", "join/AbC")),
    ("join/AbC", ("join", "join/AbC")),
    ("https://max.ru/news", ("public", "https://max.ru/news")),
    ("@news", ("public", "https://max.ru/news")),
])
def test_link_forms(target, parsed):
    assert parse_target(target) == parsed


async def test_join_by_invite_and_public_link(rw):
    _setup(rw)
    resp = await _post(rw, "/chats/join", {"target": "https://max.ru/join/AbC"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["id"], body["chat_type"], body["title"], body["already_member"]) == (GROUP_ID, "group", "Клуб", False)
    assert _calls(rw, "join_group") == [("join_group", "join/AbC")]
    row = await _chat_row(rw.id, GROUP_ID)
    assert (row.chat_type, row.my_role) == ("group", "member")

    resp = await _post(rw, "/chats/join", {"target": "@news"})
    assert resp.status_code == 200 and resp.json()["chat_type"] == "channel", resp.text
    assert _calls(rw, "join_channel") == [("join_channel", "https://max.ru/news")]
    # a public link is resolved by our own LINK_INFO frame with the full URL
    assert ("invoke", 89, {"link": "https://max.ru/news"}) in rw.env.server.group_calls
    assert [e[:2] for e in _events(rw.id)] == [("chat_join", "join_chat_endpoint")] * 2
    assert (await _status(rw))["counters"]["chats_joined_last_day"] == 2


async def test_already_member_takes_no_frame_and_no_slot(rw):
    _setup(rw)
    rw.env.server.groups[GROUP_ID]["participants"][str(ME)] = 1
    resp = await _post(rw, "/chats/join", {"target": "join/AbC"})
    assert resp.status_code == 200 and resp.json()["already_member"] is True, resp.text
    assert _calls(rw, "join_group") == [] and _events(rw.id) == []


async def test_bad_link_and_the_daily_limit(rw):
    _setup(rw)
    for target in ("t.me/x", "https://max.ru/a/b", "join/"):
        assert (await _post(rw, "/chats/join", {"target": target})).status_code == 422
    assert rw.env.server.group_calls == [] and _events(rw.id) == []

    def left() -> None:  # the account left again: the next join is a real one
        rw.env.server.groups[GROUP_ID]["participants"].pop(str(ME), None)
        sql("UPDATE chats SET raw = jsonb_set(raw, '{status}', '\"LEFT\"') WHERE account_id = %s", (rw.id,))

    for i in range(5):
        rw.env.server.links[f"join/g{i}"] = GROUP_ID
        left()
        rw.clock.at(i)
        resp = await _post(rw, "/chats/join", {"target": f"join/g{i}"})
        assert resp.status_code == 200 and resp.json()["already_member"] is False, resp.text
    left()
    resp = await _post(rw, "/chats/join", {"target": "join/AbC"})
    assert resp.status_code == 429 and resp.json()["limit"] == "chats_joined_per_day", resp.text
    assert len(_calls(rw, "join_group")) == 5

    await _limits(rw, chats_joined_per_day=0)
    resp = await _post(rw, "/chats/join", {"target": "join/AbC"})
    assert resp.status_code == 429 and "Retry-After" not in resp.headers


async def test_join_error_gives_the_slot_back(rw):
    _setup(rw)
    resp = await _post(rw, "/chats/join", {"target": "join/unknown"})
    assert resp.status_code == 502 and "link.not.found" in resp.text
    assert _events(rw.id) == []


async def test_resolve_without_joining_and_ro_reads_it(rw):
    _setup(rw)
    resp = await _post(rw, "/chats/resolve", {"target": "https://max.ru/join/AbC"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": GROUP_ID, "type": "group", "title": "Клуб", "username": None, "members_count": 1,
                           "description": "оконщики", "is_joined": False}
    assert _calls(rw, "resolve_group_by_link") == [("resolve_group_by_link", "join/AbC")]

    sql("UPDATE accounts SET mode = 'ro' WHERE id = %s", (rw.id,))
    mw._mode_cache.clear()
    rw.env.server.groups[CHANNEL_ID]["participants"][str(ME)] = 1
    resp = await _post(rw, "/chats/resolve", {"target": "@news"})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["type"], resp.json()["username"], resp.json()["is_joined"]) == ("channel", "news", True)
    assert (await _post(rw, "/chats/join", {"target": "@news"})).status_code == 403
    assert (await _post(rw, "/chats/resolve", {"target": "@nobody"})).status_code == 404
    assert _events(rw.id) == [] and await _chat_row(rw.id, CHANNEL_ID) is None  # a resolve stores nothing
