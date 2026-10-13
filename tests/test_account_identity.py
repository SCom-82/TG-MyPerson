"""accounts.external_user_id must match the logged-in user (review 13.10, R2-1).

TG: attach_handlers (STORAGE_MODE=new) fills an empty id from get_me, refuses
handlers for another user or a user another account has. REST login of another
user → 409, nothing saved. MAX (new): another profile id → state error, no frames.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.main  # noqa: F401 — import the API modules with the real pool before fixtures mock it

from app.config import settings
from app.repo import chats, messages
from app.repo.rows import ChatRow, MessageRow
from app.storage import account_db
from tests.isolation_env import SELF_A, SELF_B, create_account, drop_account, sql


@pytest.fixture
def new_mode(monkeypatch):
    monkeypatch.setattr(settings, "storage_mode", "new")
    monkeypatch.setattr(settings, "shadow_legacy_write", False)


def _tg_session(account_id, me_id):
    client = MagicMock()
    client.get_me = AsyncMock(return_value=SimpleNamespace(id=me_id))
    return SimpleNamespace(account_id=account_id, client=client, me_id=None, last_error=None)


async def _attach(session):
    from app.telegram import pool as pool_module

    with patch("app.telegram.handlers.register_handlers") as reg, \
            patch("app.services.tg_store.preload", AsyncMock(return_value=0)):
        await pool_module.attach_handlers("x", session)
    return reg


async def test_tg_empty_id_is_filled_from_get_me(accounts, new_mode):
    fresh = create_account("iso-fresh", external_user_id=None)
    try:
        session = _tg_session(fresh, 9_400_000_777)
        reg = await _attach(session)
        reg.assert_called_once()
        assert sql("SELECT external_user_id FROM accounts WHERE id = %s", (fresh,), fetch=True) == [(9_400_000_777,)]
    finally:
        drop_account(fresh)


async def test_tg_other_user_gets_no_handlers(accounts, new_mode):
    session = _tg_session(accounts.a, SELF_B)  # account A, but the session is B's user
    reg = await _attach(session)
    reg.assert_not_called()
    assert "identity mismatch" in session.last_error
    assert sql("SELECT external_user_id FROM accounts WHERE id = %s", (accounts.a,), fetch=True) == [(SELF_A,)]


async def test_tg_user_of_another_account_gets_no_handlers(accounts, new_mode):
    fresh = create_account("iso-fresh2", external_user_id=None)
    try:
        session = _tg_session(fresh, SELF_A)  # A's user under a new account
        reg = await _attach(session)
        reg.assert_not_called()
        assert "already belongs" in session.last_error
    finally:
        drop_account(fresh)


async def test_rest_login_of_another_user_is_409(accounts, test_client, monkeypatch):
    stopped = []

    class _Session:
        async def sign_in(self, code, password=None):
            return {"status": "authorized", "user_id": SELF_B, "alias": "iso-a"}

        def get_session_string(self):
            return "STRING"

    async def get(alias):
        return _Session()

    async def stop(alias):
        stopped.append(alias)

    monkeypatch.setattr("app.api.auth.pool", SimpleNamespace(get=get, stop=stop))
    r = await test_client.post("/api/v1/auth/code", headers={"x-api-key": "test-api-key", "x-session-alias": "iso-a"},
                               json={"code": "12345"})
    assert r.status_code == 409 and str(SELF_B) in r.json()["detail"]
    assert stopped == ["iso-a"]
    assert sql("SELECT count(*) FROM account_sessions WHERE account_id = %s", (accounts.a,), fetch=True) == [(0,)]


async def test_rest_login_of_the_same_user_is_saved(accounts, test_client, monkeypatch):
    class _Session:
        async def sign_in(self, code, password=None):
            return {"status": "authorized", "user_id": SELF_A, "alias": "iso-a"}

        def get_session_string(self):
            return "STRING"

    async def get(alias):
        return _Session()

    monkeypatch.setattr("app.api.auth.pool", SimpleNamespace(get=get, stop=AsyncMock()))
    r = await test_client.post("/api/v1/auth/code", headers={"x-api-key": "test-api-key", "x-session-alias": "iso-a"},
                               json={"code": "12345"})
    assert r.status_code == 200
    assert sql("SELECT count(*) FROM account_sessions WHERE account_id = %s", (accounts.a,), fetch=True) == [(1,)]
    sql("DELETE FROM account_sessions WHERE account_id = %s", (accounts.a,))


async def test_older_edit_does_not_overwrite_newer(accounts):
    t0 = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    async with account_db(accounts.a) as db:
        chat = await chats.upsert_chat(db, accounts.a, ChatRow(-1_009_400_000_700, "supergroup"))
        await messages.upsert_message(db, accounts.a, chat, MessageRow(
            external_id=1, sent_at=t0, text="second edit", is_edited=True, edited_at=t0 + timedelta(minutes=2)))
        await messages.upsert_message(db, accounts.a, chat, MessageRow(
            external_id=1, sent_at=t0, text="first edit", is_edited=True, edited_at=t0 + timedelta(minutes=1)))
        await db.commit()
        [msg], _ = await messages.get_messages(db, accounts.a)
    assert msg.text == "second edit"


# ---------------------------------------------------------------------------
# R3-2: a mismatched session is not usable at all; R3-3: startup survives
# ---------------------------------------------------------------------------

def _pool_session(account_id, me_id, alias="iso-a"):
    from app.telegram.session import TelegramSession

    s = TelegramSession(alias=alias, phone="+7", api_id=1, api_hash="x", account_id=account_id)
    client = MagicMock()
    client.is_user_authorized = AsyncMock(return_value=True)
    client.get_me = AsyncMock(return_value=SimpleNamespace(id=me_id))
    s._client = client
    return s


async def test_r3_2_mismatched_session_gives_409_everywhere(accounts, new_mode, monkeypatch):
    from fastapi import HTTPException

    from app.telegram import pool as pool_module

    bad = _pool_session(accounts.a, SELF_B)
    monkeypatch.setattr(pool_module.pool, "_pool", {"iso-a": bad})

    async def get(alias):
        return bad

    monkeypatch.setattr(pool_module.pool, "get", get)
    request = SimpleNamespace(state=SimpleNamespace(session_alias="iso-a"))
    with pytest.raises(HTTPException) as exc:
        await pool_module.require_authorized_client(request)
    assert exc.value.status_code == 409 and bad.identity_ok is False

    from app.services import backfill_service, contacts_sync

    result = await backfill_service.start_backfill(-1, alias="iso-a")
    assert result["status"] == "error" and "another Telegram user" in result["detail"]
    bad._client.__call__ = AsyncMock()  # GetContactsRequest must not be sent
    assert await contacts_sync.contacts_sync_once() == {}


async def test_r3_2_matching_session_is_confirmed_once(accounts, new_mode, monkeypatch):
    from app.telegram import pool as pool_module

    good = _pool_session(accounts.a, SELF_A)

    async def get(alias):
        return good

    monkeypatch.setattr(pool_module.pool, "get", get)
    request = SimpleNamespace(state=SimpleNamespace(session_alias="iso-a"))
    assert await pool_module.require_authorized_client(request) is good._client
    assert await pool_module.require_authorized_client(request) is good._client
    assert good.identity_ok is True and good._client.get_me.await_count == 1


async def test_r3_2_legacy_mode_is_unchanged(accounts, monkeypatch):
    from app.telegram import pool as pool_module

    bad = _pool_session(accounts.a, SELF_B)

    async def get(alias):
        return bad

    monkeypatch.setattr(pool_module.pool, "get", get)
    request = SimpleNamespace(state=SimpleNamespace(session_alias="iso-a"))
    assert await pool_module.require_authorized_client(request) is bad._client
    bad._client.get_me.assert_not_awaited()


async def test_r3_3_one_failing_account_does_not_stop_startup(accounts, new_mode, monkeypatch):
    from app.telegram import pool as pool_module

    broken = _pool_session(accounts.a, SELF_A, alias="iso-a")
    fine = _pool_session(accounts.b, SELF_B, alias="iso-b")
    attached = []

    async def attach(alias, session):
        if alias == "iso-a":
            raise RuntimeError("boom")
        attached.append(alias)

    monkeypatch.setattr(pool_module, "attach_handlers", attach)
    fake_pool = SimpleNamespace(_pool={"iso-a": broken, "iso-b": fine})
    await pool_module.attach_all_handlers(fake_pool)
    assert attached == ["iso-b"] and "boom" in broken.last_error
