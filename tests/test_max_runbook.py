"""test_max_runbook.py — the README «Платформа MAX» runbook, step by step (PR-6).

Keeps the documentation honest: every curl step of «Вход: QR + пароль 2FA» is
executed against the app (fake MAX behind it), including the recovery path
error/banned → /auth/logout → new QR, and the final GET /accounts runtime
(API spec §1.3) after events and catch-up.
"""

import asyncio
import time
import uuid

import pytest

import app.max.sync as sync_module
from tests.max_env import ADMIN_KEY, PHONE, _pg, aliases, env  # noqa: F401 — pytest fixtures

GROUP = -70000000001


def _q(sql: str, *args) -> list[tuple]:
    conn = _pg()
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []
    finally:
        conn.close()


async def _wait(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_readme_runbook_qr_password_with_recovery(env, monkeypatch):
    async def no_pause(_s):
        await asyncio.sleep(0)

    monkeypatch.setattr(sync_module, "_sleep", no_pause)
    _q("TRUNCATE max_media, max_messages, max_sync_state, max_chats, max_users, max_raw_events RESTART IDENTITY")
    c, server = env.client, env.server
    server.password = "secret"

    # 0. account via the admin API (as in the README)
    alias = f"mx-runbook-{uuid.uuid4().hex[:6]}"
    created = await c.post("/api/v1/accounts", headers=ADMIN_KEY,
                           json={"alias": alias, "phone": PHONE, "mode": "ro", "platform": "max"})
    assert created.status_code == 201
    try:
        h = {"x-api-key": "test-api-key", "x-session-alias": alias}
        runtime = next(a for a in (await c.get("/api/v1/accounts", headers=ADMIN_KEY)).json()
                       if a["alias"] == alias)["runtime"]
        assert runtime["state"] == "stopped" and runtime["proxy"] is True

        # a stored session that MAX now treats as banned → state banned
        session = await env.pool.get(alias)
        from tests.max_env import _store_session

        await _store_session(session.account_id, token="tok-banned")
        server.banned = True
        await env.pool.restart(alias)
        await env.wait_state("banned", alias=alias)
        server.banned = False

        # 1. new QR is refused while the stored session exists → logout first
        refused = await c.post("/api/v1/auth/qr", headers=h)
        assert refused.status_code == 409
        assert refused.json()["error"] == "stored session exists; POST /auth/logout first"
        assert (await c.post("/api/v1/auth/logout", headers=h)).json() == {"status": "logged_out", "alias": alias}

        # 2. request the QR; the PNG opens with ?api_key= in a browser
        qr = await c.post("/api/v1/auth/qr", headers=h)
        assert qr.status_code == 202 and qr.json()["status"] == "awaiting_qr"
        png = await c.get(f"/api/v1/auth/qr.png?session={alias}&api_key=test-api-key")
        assert png.status_code == 200 and png.headers["content-type"] == "image/png"
        server.qr_confirmed = True  # the phone scanned it

        # 3. wait for the password prompt
        await env.wait_state("awaiting_password", alias=alias)
        status = (await c.get("/api/v1/auth/qr", headers=h)).json()
        assert status["status"] == "awaiting_password" and status["password_hint"] == "pet name"

        # 4. password: wrong, then right
        wrong = await c.post("/api/v1/auth/code", headers=h, json={"code": "", "password": "nope"})
        assert wrong.status_code == 400 and wrong.json() == {"error": "invalid password", "attempts_left": 2}
        ok = await c.post("/api/v1/auth/code", headers=h, json={"code": "", "password": "secret"})
        assert ok.status_code == 200 and ok.json()["status"] == "authorized"

        # 5. check: status + full runtime, after an event and the catch-up pass
        assert (await c.get("/api/v1/auth/status", headers=h)).json()["connected"] is True
        live = await env.pool.get(alias)
        await _wait(lambda: not live.background_running("catchup"))
        await server.clients[-1].push({"opcode": 128, "cmd": 0, "payload": {
            "chatId": GROUP, "message": {"id": "1", "time": int(time.time() * 1000), "type": "USER",
                                         "sender": 200, "text": "hi", "attaches": []}}})
        account = next(a for a in (await c.get("/api/v1/accounts", headers=ADMIN_KEY)).json() if a["alias"] == alias)
        runtime = account["runtime"]
        assert runtime["state"] == "authorized"
        assert runtime["connected"] is True and runtime["authorized"] is True
        assert runtime["transport"] == "web" and runtime["proxy"] is True
        assert runtime["last_event_at"] is not None
        assert runtime["last_catchup_at"] is not None
        assert runtime["catchup_backlog_chats"] == 0
        assert runtime["last_error"] is None
        assert runtime["pymax"] == "2.4.1"
        assert account["is_running"] is True and account["platform_user_id"] == server.user_id
    finally:
        await env.pool.stop_alias(alias)
        _q("DELETE FROM max_raw_events WHERE account_id IN (SELECT id FROM accounts WHERE alias = %s)", alias)
        _q("DELETE FROM accounts WHERE alias = %s", alias)
        for table in ("max_media", "max_messages", "max_sync_state", "max_chats", "max_users"):
            _q(f"DELETE FROM {table}")


@pytest.mark.asyncio
async def test_unauthorized_needs_no_logout(env):
    """README: «При unauthorized сессия уже деактивирована — logout не нужен»."""
    from tests.max_env import _store_session

    await _store_session(env.account_id, token="tok-dead")
    env.server.revoked.add("tok-dead")
    await env.pool.start_all()
    await env.wait_state("unauthorized")
    resp = await env.client.post("/api/v1/auth/qr", headers=env.h())
    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_failed_password_login_needs_no_logout(env):
    """README: «сессия при неудачном входе не сохраняется, logout не нужен»."""
    env.server.password = "secret"
    await env.client.post("/api/v1/auth/qr", headers=env.h())
    env.server.qr_confirmed = True
    await env.wait_state("awaiting_password")
    for pw in ("a", "b", "c"):
        await env.client.post("/api/v1/auth/code", headers=env.h(), json={"code": "", "password": pw})
    await env.wait_state("error")
    env.server.qr_confirmed = False
    again = await env.client.post("/api/v1/auth/qr", headers=env.h())
    assert again.status_code == 202
