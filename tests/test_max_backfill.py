"""test_max_backfill.py — backfill, gap catch-up, chat sync, media (MAX plan PR-5, block B + media).

  B  backward with days stops by time; repeats continue from oldest_time_ms with no
     duplicates; end of history → is_fully_synced; forward from newest_time_ms
     takes exactly what is missing; forward without a cursor → 409; already_running
  C  catch-up after (re)connect: "ours T0, chat T0+30 min" → exactly the gap,
     cursor moved; a new chat gets a seed of MAX_CATCHUP_SEED; MAX_CATCHUP_MAX_CHATS
     per pass, the rest in the next pass; runtime last_catchup_at / backlog
  S  sync_chats pages fetch_chats by the time marker
  W  watchdog: no read_message / set_presence, every history call interactive=False
  M  download_media: stream from a real local HTTP server, Range → 206, beyond size
     → 416, CDN ignoring Range, photo URL from the attachment, video.not.ready → 502,
     ?index=, fallback to get_message, not authorized → 503; proxy wiring
"""

import asyncio
import time
import urllib.parse

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from pymax import Message

import app.max.sync as sync_module
from tests.max_env import _pg, _qr_login, aliases, env  # noqa: F401 — pytest fixtures

GROUP = -70000000001
OTHER_CHAT = -70000000002
THIRD_CHAT = -70000000003
SENDER = 200
MIN = 60_000
DAY = 24 * 60 * MIN


def _q(sql: str, *args) -> list[tuple]:
    conn = _pg()
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    async def no_pause(_seconds):
        await asyncio.sleep(0)

    monkeypatch.setattr(sync_module, "_sleep", no_pause)
    sync_module._backfill_tasks.clear()
    tables = "max_media, max_messages, max_sync_state, max_chats, max_users, max_raw_events"
    _q(f"TRUNCATE {tables} RESTART IDENTITY")
    yield
    _q(f"TRUNCATE {tables} RESTART IDENTITY")


def now_ms() -> int:
    return int(time.time() * 1000)


def msg(mid: int, t: int, text: str | None = None, attaches=None) -> dict:
    return {"id": str(mid), "time": t, "type": "USER", "sender": SENDER, "text": text or f"m{mid}",
            "attaches": attaches or []}


def chat(cid: int, last: dict | None, title: str = "чат") -> dict:
    data = {"id": cid, "type": "CHAT", "status": "ACTIVE", "owner": SENDER, "title": title,
            "lastEventTime": last["time"] if last else 0}
    if last:
        data["lastMessage"] = last
    return data


async def _login(env, *, chats=(), history=None):
    env.server.chats = list(chats)
    env.server.history = history or {}
    await _qr_login(env)
    await env.wait_state("authorized")
    session = await env.session()
    # let the automatic catch-up pass of this login finish
    await _wait(lambda: not session.background_running("catchup"))
    return session


async def _wait(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.02)


async def _backfill(env, **body) -> dict:
    resp = await env.client.post("/api/v1/sync/backfill", headers=env.h(), json=body)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    await _wait(lambda: not sync_module.is_backfill_running(body["chat_id"]))
    return result


def stored_ids(chat_id: int) -> list[int]:
    return [r[0] for r in _q("SELECT message_id FROM max_messages WHERE chat_id = %s ORDER BY message_id", chat_id)]


def cursor(chat_id: int) -> tuple:
    rows = _q("SELECT oldest_time_ms, newest_time_ms, is_fully_synced, total_messages_synced "
              "FROM max_sync_state WHERE chat_id = %s", chat_id)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# B — backfill
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backward_days_stops_by_time(env):
    t = now_ms()
    history = {GROUP: [msg(i, t - i * DAY // 2) for i in range(1, 121)]}  # one message per 12 h, 60 days
    await _login(env, history=history)
    result = await _backfill(env, chat_id=GROUP, limit=1000, direction="backward", days=30)
    assert result == {"status": "started", "chat_id": GROUP, "limit": 1000, "direction": "backward", "days": 30}
    ids = stored_ids(GROUP)
    assert ids == list(range(1, 60))  # 59 × 12 h < 30 days; message 60 is exactly 30 days old
    oldest, _, fully, total = cursor(GROUP)
    assert oldest == history[GROUP][58]["time"]
    assert fully is False
    assert total == 59


@pytest.mark.asyncio
async def test_backward_repeats_continue_without_duplicates(env):
    t = now_ms()
    history = {GROUP: [msg(i, t - i * MIN) for i in range(1, 251)]}
    await _login(env, history=history)
    await _backfill(env, chat_id=GROUP, limit=120)
    assert stored_ids(GROUP) == list(range(1, 121))
    await _backfill(env, chat_id=GROUP, limit=120)
    assert stored_ids(GROUP) == list(range(1, 241))
    assert _q("SELECT count(*) FROM max_messages") == [(240,)]
    pages = [c for c in env.server.history_calls if c["backward"]]
    assert all(c["backward"] <= 100 for c in pages)  # pages of 100
    await _backfill(env, chat_id=GROUP, limit=1000)
    assert stored_ids(GROUP) == list(range(1, 251))
    assert cursor(GROUP)[2] is True  # reached the beginning


@pytest.mark.asyncio
async def test_forward_takes_exactly_the_gap(env):
    t = now_ms() - 60 * MIN
    history = {GROUP: [msg(i, t + i * MIN) for i in range(1, 11)]}
    await _login(env, history=history)
    await _backfill(env, chat_id=GROUP, limit=10)
    newest_before = cursor(GROUP)[1]
    assert newest_before == history[GROUP][-1]["time"]

    history[GROUP] += [msg(i, t + i * MIN) for i in range(11, 16)]
    await _backfill(env, chat_id=GROUP, limit=1000, direction="forward")
    assert stored_ids(GROUP) == list(range(1, 16))
    assert cursor(GROUP)[1] == history[GROUP][-1]["time"]
    forward_calls = [c for c in env.server.history_calls if c["forward"]]
    assert forward_calls[0]["from_time"] == newest_before


@pytest.mark.asyncio
async def test_forward_without_cursor_409(env):
    await _login(env)
    resp = await env.client.post("/api/v1/sync/backfill", headers=env.h(),
                                 json={"chat_id": GROUP, "direction": "forward"})
    assert resp.status_code == 409
    assert "newest_time_ms" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_already_running_and_sync_status(env, monkeypatch):
    t = now_ms()
    await _login(env, history={GROUP: [msg(i, t - i * MIN) for i in range(1, 151)]})
    gate = asyncio.Event()

    async def hold(_seconds):
        await gate.wait()

    monkeypatch.setattr(sync_module, "_sleep", hold)
    first = await env.client.post("/api/v1/sync/backfill", headers=env.h(), json={"chat_id": GROUP, "limit": 150})
    assert first.json()["status"] == "started"
    await _wait(lambda: cursor(GROUP) is not None)
    second = await env.client.post("/api/v1/sync/backfill", headers=env.h(), json={"chat_id": GROUP})
    assert second.json() == {"status": "already_running", "chat_id": GROUP}
    states = (await env.client.get("/api/v1/sync/status", headers=env.h())).json()["states"]
    assert next(s for s in states if s["chat_id"] == GROUP)["is_running"] is True
    gate.set()
    await _wait(lambda: not sync_module.is_backfill_running(GROUP))


@pytest.mark.asyncio
async def test_backfill_needs_authorized_session(env):
    resp = await env.client.post("/api/v1/sync/backfill", headers=env.h(), json={"chat_id": GROUP})
    assert resp.status_code == 503
    assert resp.json()["state"] == "stopped"


@pytest.mark.asyncio
async def test_one_bad_message_does_not_stop_backfill(env, monkeypatch):
    t = now_ms()
    await _login(env, history={GROUP: [msg(i, t - i * MIN) for i in range(1, 6)]})
    session = await env.session()
    original = session.ingest.store_message

    async def flaky(m, **kw):
        if m.id == 3:
            raise RuntimeError("boom")
        return await original(m, **kw)

    monkeypatch.setattr(session.ingest, "store_message", flaky)
    await _backfill(env, chat_id=GROUP, limit=10)
    assert stored_ids(GROUP) == [1, 2, 4, 5]


# ---------------------------------------------------------------------------
# C — gap catch-up on (re)connect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_catchup_fills_exactly_the_gap(env):
    t0 = now_ms() - 60 * MIN
    history = {GROUP: [msg(i, t0 + (i - 10) * MIN) for i in range(1, 41)]}  # 10 is at T0, 40 at T0+30 min
    session = await _login(env, history=history)
    # what we had before the downtime: everything up to T0
    _q("TRUNCATE max_media, max_messages, max_sync_state RESTART IDENTITY")
    for m in history[GROUP][:10]:
        await session.ingest.store_message(Message.model_validate(m), chat_id=GROUP, advance_cursor=True)
    assert cursor(GROUP)[1] == t0

    env.server.chats = [chat(GROUP, history[GROUP][-1])]
    env.server.history_calls.clear()
    await env.pool.restart(env.alias)  # reconnect → catch-up
    await env.wait_state("authorized")
    session = await env.session()
    await _wait(lambda: session.last_catchup_at is not None and not session.background_running("catchup"))

    assert stored_ids(GROUP) == list(range(1, 41))
    assert cursor(GROUP)[1] == t0 + 30 * MIN
    assert all(c["from_time"] >= t0 for c in env.server.history_calls)  # only the gap was asked
    runtime = session.runtime()
    assert runtime["last_catchup_at"] is not None
    assert runtime["catchup_backlog_chats"] == 0
    assert _q("SELECT last_catchup_at IS NOT NULL FROM max_sync_state WHERE chat_id = %s", GROUP) == [(True,)]


@pytest.mark.asyncio
async def test_catchup_seeds_a_new_chat(env):
    env.settings.catchup_seed = 5
    t = now_ms()
    history = {GROUP: [msg(i, t - (30 - i) * MIN) for i in range(1, 31)]}
    await _login(env, chats=[chat(GROUP, history[GROUP][-1])], history=history)
    assert stored_ids(GROUP) == [26, 27, 28, 29, 30]  # the last 5, not the full history
    seed = [c for c in env.server.history_calls if c["chat_id"] == GROUP]
    assert seed == [{"chat_id": GROUP, "forward": 0, "backward": 5, "from_time": None, "interactive": False}]


@pytest.mark.asyncio
async def test_catchup_respects_max_chats_per_pass(env, monkeypatch):
    env.settings.catchup_max_chats = 2
    env.settings.catchup_seed = 3
    t = now_ms()
    history = {cid: [msg(i, t - (10 - i) * MIN - k) for i in range(1, 11)]
               for k, cid in enumerate((GROUP, OTHER_CHAT, THIRD_CHAT))}
    chats = [chat(cid, history[cid][-1]) for cid in history]

    gate = asyncio.Event()
    ticks = []

    async def tick(seconds):
        if seconds == sync_module.CATCHUP_TICK_S:
            ticks.append(seconds)
            await gate.wait()

    monkeypatch.setattr(sync_module, "_sleep", tick)
    env.server.chats = chats
    env.server.history = history
    await _qr_login(env)
    await env.wait_state("authorized")
    session = await env.session()
    await _wait(lambda: ticks)  # first pass done, waiting for the next tick
    assert session.runtime()["catchup_backlog_chats"] == 1
    assert sum(1 for cid in history if stored_ids(cid)) == 2

    gate.set()
    await _wait(lambda: not session.background_running("catchup"))
    assert all(len(stored_ids(cid)) == 3 for cid in history)
    assert session.runtime()["catchup_backlog_chats"] == 0
    assert ticks == [sync_module.CATCHUP_TICK_S]


@pytest.mark.asyncio
async def test_catchup_stops_with_the_session(env, monkeypatch):
    gate = asyncio.Event()

    async def hold(_seconds):
        await gate.wait()

    monkeypatch.setattr(sync_module, "_sleep", hold)
    t = now_ms()
    history = {cid: [msg(1, t)] for cid in (GROUP, OTHER_CHAT)}
    env.server.chats = [chat(cid, history[cid][-1]) for cid in history]
    env.server.history = history
    await _qr_login(env)
    await env.wait_state("authorized")
    session = await env.session()
    await _wait(lambda: session.background_running("catchup"))
    await env.client.post("/api/v1/auth/logout", headers=env.h())
    assert not session.background_running("catchup")


# ---------------------------------------------------------------------------
# B-12 … B-14 — live events vs the cursor (ADR §2.J, fixed 07.10)
# ---------------------------------------------------------------------------

def _live(chat_id: int, m: dict) -> dict:
    return {"opcode": 128, "cmd": 0, "payload": {"chatId": chat_id, "message": m}}


@pytest.mark.asyncio
async def test_b12_live_event_before_catchup_loses_nothing(env):
    """Ours T0; 7 messages in the chat during the downtime; right after the reconnect
    a live 8th arrives, then the catch-up runs → all 7 + the 8th, cursor = the 8th."""
    t0 = now_ms() - 60 * MIN
    before = [msg(i, t0 - (3 - i) * MIN) for i in range(1, 4)]          # 3 at T0
    gap = [msg(i, t0 + (i - 3) * MIN) for i in range(4, 11)]            # 7 during the downtime
    live = msg(11, t0 + 30 * MIN)
    session = await _login(env, history={GROUP: before})
    for m in before:
        await session.ingest.store_message(Message.model_validate(m), chat_id=GROUP, advance_cursor=True)
    assert cursor(GROUP)[1] == t0

    env.server.history = {GROUP: before + gap + [live]}
    env.server.chats = [chat(GROUP, live)]
    env.server.fetch_chats_gate = asyncio.Event()  # hold the catch-up at its first step
    await env.pool.restart(env.alias)
    await env.wait_state("authorized")
    session = await env.session()
    await env.server.clients[-1].push(_live(GROUP, live))
    assert stored_ids(GROUP) == [1, 2, 3, 11]
    assert cursor(GROUP)[1] == t0  # the live 8th did NOT jump the cursor over the gap

    env.server.fetch_chats_gate.set()
    await _wait(lambda: session.last_catchup_at is not None and not session.background_running("catchup"))
    assert stored_ids(GROUP) == list(range(1, 12))
    assert cursor(GROUP)[1] == live["time"]


@pytest.mark.asyncio
async def test_b13_new_chat_live_first_then_seed(env):
    """First live message in a chat we never saw: no max_sync_state until the
    catch-up, which then does the seed."""
    env.settings.catchup_seed = 5
    t = now_ms()
    history = {GROUP: [msg(i, t - (30 - i) * MIN) for i in range(1, 31)]}
    env.server.history = history
    env.server.chats = [chat(GROUP, history[GROUP][-1])]
    env.server.fetch_chats_gate = asyncio.Event()
    await _qr_login(env)
    await env.wait_state("authorized")
    session = await env.session()
    await env.server.clients[-1].push(_live(GROUP, history[GROUP][-1]))
    assert stored_ids(GROUP) == [30]
    assert cursor(GROUP) is None  # no max_sync_state yet

    env.server.fetch_chats_gate.set()
    await _wait(lambda: session.last_catchup_at is not None and not session.background_running("catchup"))
    assert stored_ids(GROUP) == [26, 27, 28, 29, 30]  # the seed ran
    assert cursor(GROUP)[1] == history[GROUP][-1]["time"]


@pytest.mark.asyncio
async def test_b14_live_event_after_catchup_moves_cursor(env):
    t = now_ms() - 10 * MIN
    history = {GROUP: [msg(i, t + i * MIN) for i in range(1, 4)]}
    session = await _login(env, chats=[chat(GROUP, history[GROUP][-1])], history=history)
    assert GROUP in session.caught_up
    newer = msg(4, t + 5 * MIN)
    await env.server.clients[-1].push(_live(GROUP, newer))
    assert cursor(GROUP)[1] == newer["time"]


@pytest.mark.asyncio
async def test_catchup_covers_unmonitored_chats(env):
    """MAX keeps every chat: is_monitored does not gate the gap fill."""
    env.settings.catchup_seed = 3
    t = now_ms()
    history = {GROUP: [msg(i, t - (5 - i) * MIN) for i in range(1, 6)]}
    env.server.fetch_chats_gate = asyncio.Event()
    env.server.chats = [chat(GROUP, history[GROUP][-1])]
    env.server.history = history
    await _qr_login(env)
    await env.wait_state("authorized")
    session = await env.session()
    # the chat row exists (login snapshot) and is switched off before the gap fill
    _q("UPDATE max_chats SET is_monitored = false WHERE id = %s", GROUP)
    env.server.fetch_chats_gate.set()
    await _wait(lambda: session.last_catchup_at is not None and not session.background_running("catchup"))
    assert stored_ids(GROUP) == [3, 4, 5]


@pytest.mark.asyncio
async def test_backlog_chats_stay_out_of_caught_up(env, monkeypatch):
    env.settings.catchup_max_chats = 1
    env.settings.catchup_seed = 2
    t = now_ms()
    history = {cid: [msg(i, t - (5 - i) * MIN - k) for i in range(1, 6)]
               for k, cid in enumerate((GROUP, OTHER_CHAT))}
    gate = asyncio.Event()

    async def tick(seconds):
        if seconds == sync_module.CATCHUP_TICK_S:
            await gate.wait()

    monkeypatch.setattr(sync_module, "_sleep", tick)
    env.server.chats = [chat(cid, history[cid][-1]) for cid in history]
    env.server.history = history
    await _qr_login(env)
    await env.wait_state("authorized")
    session = await env.session()
    await _wait(lambda: session.runtime()["catchup_backlog_chats"] == 1)
    assert GROUP in session.caught_up and OTHER_CHAT not in session.caught_up
    gate.set()
    await _wait(lambda: not session.background_running("catchup"))
    assert {GROUP, OTHER_CHAT} <= session.caught_up


@pytest.mark.asyncio
async def test_b15_failed_chat_sync_does_not_mark_stale_chats_caught_up(env, monkeypatch):
    """sync_chats fails in the catch-up pass; the chat has a tail but its
    last_message_at in the DB is stale → not caught up, a live event does not move
    the cursor, the next pass (tick) fills the tail."""
    t0 = now_ms() - 60 * MIN
    before = [msg(i, t0 - (3 - i) * MIN) for i in range(1, 4)]   # ours up to T0
    tail = [msg(i, t0 + (i - 3) * MIN) for i in range(4, 8)]     # 4 we missed
    live = msg(8, t0 + 30 * MIN)
    session = await _login(env, chats=[chat(GROUP, before[-1])], history={GROUP: before})
    assert GROUP in session.caught_up  # caught up as of this login: last_message_at == T0

    gate = asyncio.Event()
    ticks = []

    async def tick(seconds):
        if seconds == sync_module.CATCHUP_TICK_S:
            ticks.append(seconds)
            await gate.wait()

    monkeypatch.setattr(sync_module, "_sleep", tick)
    env.server.history = {GROUP: before + tail + [live]}
    env.server.chats = [chat(GROUP, tail[-1])]
    env.server.login_chats = [chat(GROUP, before[-1])]  # the login snapshot is stale too
    env.server.fetch_chats_errors = [RuntimeError("chat list unavailable")]
    await env.pool.restart(env.alias)  # reconnect: the first pass cannot refresh the chat list
    await env.wait_state("authorized")
    session = await env.session()
    await _wait(lambda: ticks)  # pass 1 done, waiting for the tick

    assert GROUP not in session.caught_up
    assert session.runtime()["catchup_backlog_chats"] >= 1
    await env.server.clients[-1].push({"opcode": 128, "cmd": 0, "payload": {"chatId": GROUP, "message": live}})
    assert cursor(GROUP)[1] == t0  # the live event did not jump over the tail

    gate.set()  # pass 2: chat list fresh → the tail is seen and filled
    await _wait(lambda: not session.background_running("catchup"))
    assert stored_ids(GROUP) == list(range(1, 9))
    assert cursor(GROUP)[1] == live["time"]
    assert GROUP in session.caught_up
    assert session.runtime()["catchup_backlog_chats"] == 0


@pytest.mark.asyncio
async def test_failed_chat_catchup_retried_on_tick(env, monkeypatch):
    """The gap fill of one chat fails: it is not caught up, the pass reports
    backlog ≥ 1, the next tick retries it and fills the gap; other chats are done."""
    t0 = now_ms() - 60 * MIN
    before = {cid: [msg(i, t0 - (3 - i) * MIN - k) for i in range(1, 4)]
              for k, cid in enumerate((GROUP, OTHER_CHAT))}
    session = await _login(env, chats=[chat(cid, before[cid][-1]) for cid in before], history=before)
    for cid in before:
        for m in before[cid]:
            await session.ingest.store_message(Message.model_validate(m), chat_id=cid, advance_cursor=True)

    tails = {cid: [msg(i, t0 + (i - 3) * MIN - k) for i in range(4, 7)] for k, cid in enumerate(before)}
    gate = asyncio.Event()
    ticks = []

    async def tick(seconds):
        if seconds == sync_module.CATCHUP_TICK_S:
            ticks.append(seconds)
            await gate.wait()

    monkeypatch.setattr(sync_module, "_sleep", tick)
    env.server.history = {cid: before[cid] + tails[cid] for cid in before}
    env.server.chats = [chat(cid, tails[cid][-1]) for cid in before]
    env.server.history_errors = {GROUP: [RuntimeError("history unavailable")]}
    await env.pool.restart(env.alias)
    await env.wait_state("authorized")
    session = await env.session()
    await _wait(lambda: ticks)  # pass 1 done

    assert GROUP not in session.caught_up and OTHER_CHAT in session.caught_up
    assert session.runtime()["catchup_backlog_chats"] == 1
    assert stored_ids(GROUP) == [1, 2, 3] and stored_ids(OTHER_CHAT) == list(range(1, 7))

    gate.set()  # pass 2 retries the failed chat
    await _wait(lambda: not session.background_running("catchup"))
    assert stored_ids(GROUP) == list(range(1, 7))
    assert cursor(GROUP)[1] == tails[GROUP][-1]["time"]
    assert GROUP in session.caught_up
    assert session.runtime()["catchup_backlog_chats"] == 0
    assert ticks == [sync_module.CATCHUP_TICK_S]


# ---------------------------------------------------------------------------
# S — chat list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_chats_pages_by_marker(env):
    t = now_ms()
    await _login(env)
    env.server.chats = [chat(-70000000100 - i, msg(1, t - i * MIN), title=f"чат {i}") for i in range(5)]
    env.server.fetch_chats_calls.clear()
    resp = await env.client.post("/api/v1/sync/chats", headers=env.h())
    assert resp.json() == {"status": "ok", "chats_synced": 5}
    markers = env.server.fetch_chats_calls
    assert markers[0] is None and all(b < a for a, b in zip(markers[1:], markers[2:]))
    assert _q("SELECT count(*) FROM max_chats WHERE title LIKE 'чат %%'") == [(5,)]


# ---------------------------------------------------------------------------
# W — reading never marks read
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_watchdog_nothing_marked_read(env):
    t = now_ms()
    history = {GROUP: [msg(i, t - (50 - i) * MIN) for i in range(1, 51)]}
    session = await _login(env, chats=[chat(GROUP, history[GROUP][-1])], history=history)
    await _backfill(env, chat_id=GROUP, limit=100)
    await env.server.clients[-1].push({"opcode": 128, "cmd": 0, "payload": {"chatId": GROUP, "message": msg(99, t)}})
    await env.pool.restart(env.alias)
    await env.wait_state("authorized")
    session = await env.session()
    await _wait(lambda: not session.background_running("catchup"))
    for client in env.server.clients:
        assert not {"read_message", "set_presence"} & set(client.calls), client.calls
    assert env.server.history_calls and all(c["interactive"] is False for c in env.server.history_calls)


# ---------------------------------------------------------------------------
# M — media
# ---------------------------------------------------------------------------

BLOB = bytes(range(256)) * 2500  # 640 000 bytes > 2 chunks of 256 KiB


@pytest_asyncio.fixture
async def cdn():
    async def ranged(request):
        rng = request.headers.get("Range", "")
        if rng.startswith("bytes="):
            start = int(rng[6:-1])
            if start >= len(BLOB):
                return web.Response(status=416, headers={"Content-Range": f"bytes */{len(BLOB)}"})
            return web.Response(status=206, body=BLOB[start:], content_type="application/pdf",
                                headers={"Content-Range": f"bytes {start}-{len(BLOB) - 1}/{len(BLOB)}"})
        return web.Response(body=BLOB, content_type="application/pdf")

    async def no_range(request):
        return web.Response(body=BLOB, content_type="video/mp4")

    async def gone(request):
        return web.Response(status=404)

    app = web.Application()
    app.router.add_get("/file", ranged)
    app.router.add_get("/plain", no_range)
    app.router.add_get("/gone", gone)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    yield lambda path: str(server.make_url(path))
    await server.close()


FILE_ATT = {"_type": "FILE", "fileId": 9002, "name": "Договор.pdf", "size": len(BLOB), "token": "f"}


async def _media_env(env, cdn, attaches, *, store=True):
    t = now_ms()
    m = msg(5, t, attaches=attaches)
    session = await _login(env, history={GROUP: [m]})
    env.settings.proxy_url = ""  # the test CDN is local; proxy wiring is tested separately
    if store:
        await env.server.clients[-1].push({"opcode": 128, "cmd": 0, "payload": {"chatId": GROUP, "message": m}})
    env.server.file_url = cdn("/file")
    return session


@pytest.mark.asyncio
async def test_download_file_streams_with_headers(env, cdn):
    await _media_env(env, cdn, [FILE_ATT])
    resp = await env.client.get(f"/api/v1/messages/{GROUP}/5/media", headers=env.h())
    assert resp.status_code == 200
    assert resp.content == BLOB
    assert resp.headers["content-length"] == str(len(BLOB))
    assert resp.headers["x-expected-size"] == str(len(BLOB))
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-type"] == "application/pdf"
    assert urllib.parse.quote("Договор.pdf", safe="") in resp.headers["content-disposition"]


@pytest.mark.asyncio
async def test_download_range_resume(env, cdn):
    await _media_env(env, cdn, [FILE_ATT])
    resp = await env.client.get(f"/api/v1/messages/{GROUP}/5/media", headers={**env.h(), "Range": "bytes=300000-"})
    assert resp.status_code == 206
    assert resp.content == BLOB[300000:]
    assert resp.headers["content-range"] == f"bytes 300000-{len(BLOB) - 1}/{len(BLOB)}"
    assert resp.headers["content-length"] == str(len(BLOB) - 300000)


@pytest.mark.asyncio
async def test_download_range_beyond_size_416(env, cdn):
    await _media_env(env, cdn, [FILE_ATT])
    resp = await env.client.get(f"/api/v1/messages/{GROUP}/5/media",
                                headers={**env.h(), "Range": f"bytes={len(BLOB)}-"})
    assert resp.status_code == 416
    assert resp.headers["content-range"] == f"bytes */{len(BLOB)}"


@pytest.mark.asyncio
async def test_download_cdn_ignoring_range(env, cdn):
    await _media_env(env, cdn, [{"_type": "VIDEO", "height": 1, "width": 1, "videoId": 7, "thumbnail": "t",
                                 "token": "v", "videoType": 0}])
    env.server.video_url = cdn("/plain")
    resp = await env.client.get(f"/api/v1/messages/{GROUP}/5/media", headers={**env.h(), "Range": "bytes=1000-"})
    assert resp.status_code == 206
    assert resp.content == BLOB[1000:]
    assert resp.headers["content-range"] == f"bytes 1000-{len(BLOB) - 1}/{len(BLOB)}"


@pytest.mark.asyncio
async def test_download_photo_by_index(env, cdn):
    photo = {"_type": "PHOTO", "baseUrl": cdn("/file"), "height": 1, "width": 1, "photoId": 1, "photoToken": "p"}
    await _media_env(env, cdn, [FILE_ATT, photo])
    resp = await env.client.get(f"/api/v1/messages/{GROUP}/5/media?index=1", headers=env.h())
    assert resp.status_code == 200
    assert 'filename="photo_5.jpg"' in resp.headers["content-disposition"]
    assert (await env.client.get(f"/api/v1/messages/{GROUP}/5/media?index=2", headers=env.h())).status_code == 404


@pytest.mark.asyncio
async def test_download_video_not_ready_502(env, cdn):
    await _media_env(env, cdn, [{"_type": "VIDEO", "height": 1, "width": 1, "videoId": 7, "thumbnail": "t",
                                 "token": "v", "videoType": 0}])
    env.server.video_not_ready = True
    resp = await env.client.get(f"/api/v1/messages/{GROUP}/5/media", headers=env.h())
    assert resp.status_code == 502
    assert "video.not.ready" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_download_cdn_error_502(env, cdn):
    await _media_env(env, cdn, [FILE_ATT])
    env.server.file_url = cdn("/gone")
    assert (await env.client.get(f"/api/v1/messages/{GROUP}/5/media", headers=env.h())).status_code == 502


@pytest.mark.asyncio
async def test_download_not_in_db_falls_back_to_get_message(env, cdn):
    await _media_env(env, cdn, [FILE_ATT], store=False)
    resp = await env.client.get(f"/api/v1/messages/{GROUP}/5/media", headers=env.h())
    assert resp.status_code == 200
    assert "get_message" in env.server.clients[-1].calls
    assert (await env.client.get(f"/api/v1/messages/{GROUP}/404/media", headers=env.h())).status_code == 404


@pytest.mark.asyncio
async def test_download_text_message_404(env, cdn):
    await _media_env(env, cdn, [])
    resp = await env.client.get(f"/api/v1/messages/{GROUP}/5/media", headers=env.h())
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Message has no media"}


@pytest.mark.asyncio
async def test_download_needs_authorized_session(env):
    resp = await env.client.get(f"/api/v1/messages/{GROUP}/5/media", headers=env.h())
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_media_goes_through_the_max_proxy():
    from aiohttp_socks import ProxyConnector

    from app.max.media import http_session

    session, kwargs = http_session("socks5://svc:pw@max-egress:1080")
    try:
        assert isinstance(session.connector, ProxyConnector)
        assert kwargs == {}
    finally:
        await session.close()
    session, kwargs = http_session("http://proxy.local:3128")
    try:
        assert kwargs == {"proxy": "http://proxy.local:3128"}
    finally:
        await session.close()
