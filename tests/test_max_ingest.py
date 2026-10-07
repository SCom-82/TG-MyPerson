"""test_max_ingest.py — normalization, ingest, read endpoints (MAX plan PR-4, blocks N + I).

  N   normalization of every fixture in tests/fixtures/max (pure functions)
  I   frame → max_raw_events → max_* upsert; idempotency; edit-before-original;
      deletes → deleted_at (text kept); unknown attachment; broken frame stays
      in max_raw_events with normalized=false; unknown sender fetched in one
      batch; chat events; cursors; login snapshot (chats, contacts)
  iso MAX events never reach the TG SSE and vice versa; list_messages under a
      MAX alias never sees tg_messages and under a TG alias never sees max_messages
  API list/get chats, PATCH (ro → 403), messages (+deleted_at), users, resolve,
      search (DB), sync_status (time cursors), contacts
  +   the frame hook on the REAL PyMax client sees a frame even when PyMax's
      own parsing of it fails; raw-events retention
"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest
import pytest_asyncio

import app.max.normalize as normalize
from app.max.config import MaxSettings
from tests.max_env import (  # noqa: F401 — aliases/env are pytest fixtures
    API_KEY,
    PROXY,
    _create_account,
    _pg,
    _qr_login,
    aliases,
    env,
)

FIXTURES = Path(__file__).parent / "fixtures" / "max"
RECEIVED = datetime(2026, 10, 7, 12, 5, tzinfo=timezone.utc)
ME = 100
GROUP = -70000000001
DIALOG = 100500


def fx(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _normalization_cases() -> list[str]:
    return sorted(p.stem for p in FIXTURES.glob("*.json") if p.stem[0] in "ne")


def _q(sql: str, *args) -> list[tuple]:
    conn = _pg()
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []
    finally:
        conn.close()


@pytest.fixture
def clean_max():
    """max_* are shared, global tables: start and end every ingest test empty."""
    tables = "max_media, max_messages, max_sync_state, max_chats, max_users, max_raw_events"
    _q(f"TRUNCATE {tables} RESTART IDENTITY")
    yield
    _q(f"TRUNCATE {tables} RESTART IDENTITY")


# ---------------------------------------------------------------------------
# N — normalization (pure)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", _normalization_cases())
def test_normalize_fixture(name):
    case = fx(name)
    frame = case["frame"]
    msg = normalize.parse_message(frame["payload"])
    row, media = normalize.normalize_message(
        msg,
        me_id=case["me_id"],
        chat_type=case["chat_type"],
        received_at=RECEIVED,
        is_edit=frame["opcode"] == normalize.OP_EDIT,
    )
    expected = dict(case["expect"])
    assert row["tg_date"].isoformat() == expected.pop("tg_date")
    for key, value in expected.items():
        assert row[key] == value, f"{name}: {key}"
    assert row["edit_date"] == (RECEIVED if expected["is_edited"] else None)
    assert media == case["expect_media"]
    assert row["raw_data"]["_pymax"] == "2.4.1"
    assert row["raw_data"]["id"] == expected["message_id"]


def test_unknown_attachment_payload_kept_in_raw_data():
    msg = normalize.parse_message(fx("n13_unknown_attachment")["frame"]["payload"])
    row, _ = normalize.normalize_message(msg, me_id=ME)
    assert row["raw_data"]["attaches"] == [{"_type": "HOLOGRAM", "size": 1}]


def test_removed_status_sets_deleted_at():
    payload = fx("n01_text_private_incoming")["frame"]["payload"]
    payload["message"]["status"] = "REMOVED"
    row, _ = normalize.normalize_message(normalize.parse_message(payload), me_id=ME, received_at=RECEIVED)
    assert row["deleted_at"] == RECEIVED
    assert row["text"] == "Привет"


def test_normalize_chat_and_user():
    chat = normalize.parse_chat(fx("c01_chat")["frame"]["payload"])
    row = normalize.normalize_chat(chat)
    assert {k: row[k] for k in ("id", "chat_type", "title", "username", "description", "members_count", "last_message_id")} == {
        "id": GROUP, "chat_type": "group", "title": "Рабочий чат", "username": "https://max.ru/join/xyz",
        "description": "описание", "members_count": 12, "last_message_id": 3,
    }
    from pymax.types import User

    user = normalize.normalize_user(
        User.model_validate({"id": 200, "names": [{"firstName": "Анна", "lastName": "К"}], "link": "anna", "phone": 79005556677}),
        me_id=ME,
    )
    assert {k: user[k] for k in ("id", "username", "first_name", "last_name", "phone", "is_self")} == {
        "id": 200, "username": "anna", "first_name": "Анна", "last_name": "К", "phone": "+79005556677", "is_self": False,
    }


def test_parse_delete():
    assert normalize.parse_delete(fx("d01_delete")["frame"]["payload"]) == (DIALOG, [1, 2, 999])


# ---------------------------------------------------------------------------
# I — ingest through the session (fake client, real DB)
# ---------------------------------------------------------------------------

async def _authorized(env) -> object:
    env.server.user_id = ME
    await _qr_login(env)
    await env.wait_state("authorized")
    return env.server.clients[-1]


async def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


@pytest.mark.asyncio
async def test_login_snapshot_chats_and_contacts(env, clean_max):
    env.server.chats = [fx("c01_chat")["frame"]["payload"]["chat"]]
    env.server.contacts = [{"id": 200, "names": [{"firstName": "Анна"}], "link": "anna"}]
    await _authorized(env)
    assert _q("SELECT id, chat_type, title FROM max_chats") == [(GROUP, "group", "Рабочий чат")]
    users = dict(_q("SELECT id, first_name FROM max_users"))
    assert users == {200: "Анна", ME: "Сергей"}
    assert _q("SELECT is_self FROM max_users WHERE id = %s", ME) == [(True,)]


@pytest.mark.asyncio
async def test_message_frame_is_journaled_and_stored(env, clean_max):
    client = await _authorized(env)
    await client.push(fx("n03_photo")["frame"])

    rows = _q("SELECT message_id, chat_id, message_type, text FROM max_messages")
    assert rows == [(3, GROUP, "photo", "фото")]
    assert _q("SELECT attach_index, file_type, file_id FROM max_media") == [(0, "photo", "9001")]
    assert _q("SELECT opcode, normalized, error FROM max_raw_events") == [(128, True, None)]
    # chat stub and cursor
    assert _q("SELECT chat_type FROM max_chats WHERE id = %s", GROUP) == [("group",)]
    assert _q("SELECT newest_time_ms, newest_message_id FROM max_sync_state WHERE chat_id = %s", GROUP) == [
        (1791398400000, 3)
    ]
    assert (await env.session()).runtime()["last_event_at"] is not None


@pytest.mark.asyncio
async def test_same_frame_twice_is_one_row(env, clean_max):
    client = await _authorized(env)
    frame = fx("n08_multiple_attachments")["frame"]
    await client.push(frame)
    await client.push(frame)
    assert _q("SELECT count(*) FROM max_messages") == [(1,)]
    assert _q("SELECT count(*) FROM max_media") == [(3,)]
    assert _q("SELECT count(*), bool_and(normalized) FROM max_raw_events") == [(2, True)]


@pytest.mark.asyncio
async def test_edit_arriving_first_is_not_overwritten(env, clean_max):
    client = await _authorized(env)
    await client.push(fx("e01_edit")["frame"])
    await client.push(fx("n01_text_private_incoming")["frame"])
    assert _q("SELECT text, is_edited FROM max_messages WHERE message_id = 1") == [("Привет!", True)]


@pytest.mark.asyncio
async def test_delete_sets_deleted_at_and_keeps_text(env, clean_max):
    client = await _authorized(env)
    await client.push(fx("n01_text_private_incoming")["frame"])
    await client.push(fx("d01_delete")["frame"])
    rows = _q("SELECT text, deleted_at IS NOT NULL FROM max_messages WHERE message_id = 1")
    assert rows == [("Привет", True)]
    # 2 and 999 were never seen: no tombstones, the raw frame says so
    assert _q("SELECT count(*) FROM max_messages") == [(1,)]
    assert _q("SELECT normalized, error FROM max_raw_events WHERE opcode = 142") == [(True, "delete_unknown_message")]


@pytest.mark.asyncio
async def test_unknown_attachment_stored_as_unknown(env, clean_max):
    client = await _authorized(env)
    await client.push(fx("n13_unknown_attachment")["frame"])
    rows = _q("SELECT message_type, raw_data->'attaches' FROM max_messages")
    assert rows == [("unknown", [{"_type": "HOLOGRAM", "size": 1}])]


@pytest.mark.asyncio
async def test_broken_frame_stays_in_raw_events(env, clean_max):
    client = await _authorized(env)
    await client.push(fx("x01_broken_photo")["frame"])
    assert _q("SELECT count(*) FROM max_messages") == [(0,)]
    [(normalized, error, payload)] = _q("SELECT normalized, error, payload FROM max_raw_events")
    assert normalized is False
    assert "ValidationError" in error
    assert payload == fx("x01_broken_photo")["frame"]["payload"]
    assert (await env.session()).state == "authorized"  # nothing propagated into the session


@pytest.mark.asyncio
async def test_unknown_sender_fetched_once(env, clean_max):
    env.server.users = {200: {"id": 200, "names": [{"firstName": "Анна"}], "link": "anna"}}
    client = await _authorized(env)
    await client.push(fx("n03_photo")["frame"])
    await client.push(fx("n04_file")["frame"])  # same sender: no second fetch
    assert await _wait_for(lambda: _q("SELECT first_name FROM max_users WHERE id = 200") == [("Анна",)])
    assert env.server.get_users_calls == [[200]]


@pytest.mark.asyncio
async def test_chat_event_updates_chat(env, clean_max):
    client = await _authorized(env)
    await client.push(fx("n03_photo")["frame"])  # creates a stub chat
    await client.push(fx("c01_chat")["frame"])
    assert _q("SELECT title, members_count FROM max_chats WHERE id = %s", GROUP) == [("Рабочий чат", 12)]


@pytest.mark.asyncio
async def test_non_push_and_other_opcodes_ignored(env, clean_max):
    client = await _authorized(env)
    response = dict(fx("n03_photo")["frame"], cmd=1)  # a response to our own request
    await client.push(response)
    await client.push({"opcode": 129, "cmd": 0, "payload": {"typing": True}})  # NOTIF_TYPING
    assert _q("SELECT count(*) FROM max_raw_events") == [(0,)]


# ---------------------------------------------------------------------------
# Isolation from Telegram
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_max_events_only_in_max_stream(env, clean_max):
    from app.max.stream import max_stream_manager
    from app.services.stream_service import stream_manager

    client = await _authorized(env)
    tg_queue, max_queue = stream_manager.subscribe(), max_stream_manager.subscribe()
    try:
        await client.push(fx("n01_text_private_incoming")["frame"])
        await client.push(fx("e01_edit")["frame"])
        await client.push(fx("d01_delete")["frame"])
        await stream_manager.broadcast({"event": "message", "chat_id": 1, "text": "tg"})

        events = [json.loads(max_queue.get_nowait()) for _ in range(max_queue.qsize())]
        assert [e["event"] for e in events] == ["message", "edit", "delete"]
        assert all(e["platform"] == "max" for e in events)
        assert set(events[0]) == {
            "event", "platform", "message_id", "chat_id", "from_user_id", "sender_chat_id",
            "text", "message_type", "tg_date", "is_outgoing", "is_edited",
        }
        tg_events = [json.loads(tg_queue.get_nowait()) for _ in range(tg_queue.qsize())]
        assert tg_events == [{"event": "message", "chat_id": 1, "text": "tg"}]
    finally:
        stream_manager.unsubscribe(tg_queue)
        max_stream_manager.unsubscribe(max_queue)


@pytest_asyncio.fixture
async def tg_marker(aliases):
    """A tg_message and a TG alias; removed afterwards."""
    tg_alias, _ = aliases("tg", platform="telegram", mode="rw")
    marker = f"tg-marker-{uuid.uuid4().hex[:6]}"
    chat_id = -990_000_000_404
    _q("INSERT INTO tg_chats (id, chat_type, title) VALUES (%s, 'group', 'iso') ON CONFLICT DO NOTHING", chat_id)
    _q(
        "INSERT INTO tg_messages (message_id, chat_id, text, tg_date, message_type, is_outgoing, is_edited) "
        "VALUES (1, %s, %s, now(), 'text', false, false)",
        chat_id, marker,
    )
    yield tg_alias, marker
    _q("DELETE FROM tg_messages WHERE chat_id = %s", chat_id)
    _q("DELETE FROM tg_chats WHERE id = %s", chat_id)


@pytest.mark.asyncio
async def test_list_messages_never_crosses_platforms(env, clean_max, tg_marker):
    tg_alias, marker = tg_marker
    client = await _authorized(env)
    await client.push(fx("n01_text_private_incoming")["frame"])

    max_view = (await env.client.get(f"/api/v1/messages?search={marker}", headers=env.h())).json()
    assert max_view["total"] == 0
    max_all = (await env.client.get("/api/v1/messages", headers=env.h())).json()
    assert [m["text"] for m in max_all["items"]] == ["Привет"]

    tg_view = (await env.client.get(f"/api/v1/messages?search={marker}", headers=env.h(tg_alias))).json()
    assert [m["text"] for m in tg_view["items"]] == [marker]
    tg_max = (await env.client.get("/api/v1/messages?search=Привет", headers=env.h(tg_alias))).json()
    assert all(m["text"] != "Привет" or m["chat_id"] != DIALOG for m in tg_max["items"])
    assert "deleted_at" not in tg_view["items"][0]  # TG schema untouched


# ---------------------------------------------------------------------------
# Read endpoints under the MAX alias
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_endpoints(env, clean_max, aliases):
    env.server.contacts = [{"id": 200, "names": [{"firstName": "Анна"}], "link": "anna", "phone": 79005556677}]
    env.server.chats = [fx("c01_chat")["frame"]["payload"]["chat"]]
    client = await _authorized(env)
    for name in ("n03_photo", "n09_reply", "n01_text_private_incoming", "d01_delete"):
        await client.push(fx(name)["frame"])
    h = env.h()

    chats = (await env.client.get("/api/v1/chats?search=Рабочий", headers=h)).json()
    assert [c["id"] for c in chats["items"]] == [GROUP]
    detail = (await env.client.get(f"/api/v1/chats/{GROUP}", headers=h)).json()
    assert detail["title"] == "Рабочий чат"
    assert (await env.client.get("/api/v1/chats/1", headers=h)).status_code == 404

    msgs = (await env.client.get(f"/api/v1/messages?chat_id={GROUP}", headers=h)).json()
    assert {m["message_id"] for m in msgs["items"]} == {3, 9}  # same timestamp: order not defined
    photo = (await env.client.get(f"/api/v1/messages/{GROUP}/3", headers=h)).json()
    assert photo["media"][0]["file_type"] == "photo"
    assert photo["deleted_at"] is None
    assert list(photo)[-1] == "deleted_at"  # TG fields first, in TG order
    deleted = (await env.client.get(f"/api/v1/messages/{DIALOG}/1", headers=h)).json()
    assert deleted["deleted_at"] is not None and deleted["text"] == "Привет"
    assert (await env.client.get(f"/api/v1/messages/{GROUP}/404", headers=h)).status_code == 404

    users = (await env.client.get("/api/v1/users?search=Анна", headers=h)).json()
    assert [u["id"] for u in users["items"]] == [200]

    found = (await env.client.get("/api/v1/search/global?q=фото", headers=h)).json()
    assert [i["message_id"] for i in found["items"]] == [3]  # the reply's own text is "ответ"
    assert next(i for i in found["items"] if i["message_id"] == 3)["chat"]["title"] == "Рабочий чат"

    sync = (await env.client.get("/api/v1/sync/status", headers=h)).json()["states"]
    state = next(s for s in sync if s["chat_id"] == GROUP)
    assert state["newest_time_ms"] == 1791398400000
    assert {"oldest_time_ms", "last_catchup_at", "is_running"} <= set(state)

    contacts = (await env.client.get("/api/v1/contacts", headers=h)).json()
    assert contacts["items"] == [{
        "id": 200, "username": "anna", "first_name": "Анна", "last_name": None,
        "phone": "+79005556677", "is_bot": False, "is_self": False,
    }]


@pytest.mark.asyncio
async def test_update_chat_settings_ro_403_rw_ok(env, clean_max, aliases):
    client = await _authorized(env)
    await client.push(fx("n03_photo")["frame"])
    ro = await env.client.patch(f"/api/v1/chats/{GROUP}", headers=env.h(), json={"is_monitored": False})
    assert ro.status_code == 403  # update_chat_settings is in the write category (ADR §1.1 p.10)

    rw_alias, _ = aliases("rw", mode="rw")
    rw = await env.client.patch(f"/api/v1/chats/{GROUP}", headers=env.h(rw_alias), json={"is_monitored": False})
    assert rw.status_code == 200
    assert rw.json()["is_monitored"] is False


@pytest.mark.asyncio
async def test_resolve_user(env, clean_max):
    env.server.users = {300: {"id": 300, "names": [{"firstName": "Пётр"}], "link": "petr"}}
    env.server.contacts = [{"id": 200, "names": [{"firstName": "Анна"}], "link": "anna", "phone": 79005556677}]
    await _authorized(env)
    h = env.h()

    by_link = await env.client.post("/api/v1/users/resolve", headers=h, json={"username": "@anna"})
    assert by_link.json() == {"id": 200, "type": "user", "title": "Анна", "username": "anna",
                              "members_count": None, "description": None, "is_joined": None}
    by_phone = await env.client.post("/api/v1/users/resolve", headers=h, json={"username": "+79005556677"})
    assert by_phone.json()["id"] == 200
    by_id_miss = await env.client.post("/api/v1/users/resolve", headers=h, json={"username": "300"})
    assert by_id_miss.json()["title"] == "Пётр"
    assert _q("SELECT first_name FROM max_users WHERE id = 300") == [("Пётр",)]
    unknown_link = await env.client.post("/api/v1/users/resolve", headers=h, json={"username": "@nobody"})
    assert unknown_link.status_code == 404  # no @username lookup over the network in MAX

    bulk = (await env.client.post(
        "/api/v1/users/resolve_by_id", headers=h, json={"user_ids": [200, 300, 404]}
    )).json()
    assert [r["user_id"] for r in bulk["resolved"]] == [200, 300]
    assert [u["user_id"] for u in bulk["unresolved"]] == [404]
    assert bulk["session"] == env.alias


def test_stream_route_is_the_max_one():
    import app.main as main_module

    assert main_module.app.state.route_table.resolve("GET", "/api/v1/_max/stream/messages") == "stream_messages"


# ---------------------------------------------------------------------------
# Frame hook on the real PyMax client; retention
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_frame_hook_sees_frames_pymax_cannot_parse():
    """PyMax 2.4.1 dispatches typed handlers before on_raw and drops the frame when
    its mapping fails; the adapter hook must see the frame regardless."""
    from pymax.protocol import InboundFrame

    import app.max.session as session_module

    seen = []

    async def hook(opcode, cmd, payload):
        seen.append((opcode, cmd, payload))

    client = session_module.build_client(
        transport="web",
        phone="",
        extra_config=session_module.build_extra_config(proxy=PROXY, store=None),
        auth_flow=None,
    )
    client.add_frame_hook(hook)
    await client._ensure_runtime()  # builds connection + App, opens nothing
    broken_chat = InboundFrame(opcode=normalize.OP_CHAT, cmd=0, payload={"chat": {"broken": True}})
    with pytest.raises(RuntimeError):
        await client._app.connection.on_event(broken_chat)  # PyMax's own dispatch fails…
    assert seen == [(normalize.OP_CHAT, 0, {"chat": {"broken": True}})]  # …the hook ran first

    client._reset_runtime()  # reconnect path rebuilds the App: the hook must follow
    await client._app.connection.on_event(InboundFrame(opcode=999, cmd=0, payload={}))
    assert seen[-1][0] == 999


@pytest.mark.asyncio
async def test_raw_events_retention(clean_max):
    from app.max.maintenance import purge_raw_events_once

    old = datetime.now(timezone.utc) - timedelta(days=31)
    _q("INSERT INTO max_raw_events (opcode, payload, received_at) VALUES (128, '{}', %s)", old)
    _q("INSERT INTO max_raw_events (opcode, payload) VALUES (128, '{}')")
    assert await purge_raw_events_once(MaxSettings(raw_events_retention_days=30)) == 1
    assert _q("SELECT count(*) FROM max_raw_events") == [(1,)]
