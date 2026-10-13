"""Reads, keys and contacts over the per-account tables (ТЗ 2026-10-13 §6, PR-3): T14–T18.

STORAGE_MODE=new, the real middleware stack, data under the non-superuser app
role (tests/isolation_env.py).
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.authz import keys
from app.authz.policy import AuthContext, authorize
from app.config import settings
from app.repo import chats, messages, snapshots, users
from app.repo.rows import ChatRow, MessageRow, SnapshotMemberRow, UserRow
from app.storage import account_db
from tests.isolation_env import SELF_A, sql

T0 = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
SHARED = -1_009_400_000_500
ONLY_B = -1_009_400_000_501
P_DIRECT, P_USERNAME, P_PHONE, P_NONE = 9_400_000_601, 9_400_000_602, 9_400_000_603, 9_400_000_604
LEGACY = {"x-api-key": "test-api-key"}


@pytest.fixture(autouse=True)
def _new_mode(monkeypatch):
    import app.authz.middleware as mw

    monkeypatch.setattr(settings, "storage_mode", "new")
    monkeypatch.setattr(settings, "shadow_legacy_write", False)
    monkeypatch.setattr(settings, "require_account", False)
    mw._alias_cache.clear()
    mw._mode_cache.clear()
    keys.invalidate()
    yield
    sql("DELETE FROM api_keys WHERE label LIKE 'iso-test%%'")
    keys.invalidate()


async def _seed(account_id: int, chat: int, text: str, *, sender: int = P_DIRECT, access_hash=None) -> int:
    async with account_db(account_id) as db:
        pk = await chats.upsert_chat(db, account_id, ChatRow(chat, "supergroup", title=f"chat {chat}"))
        await users.upsert_user(db, account_id, "telegram",
                                UserRow(sender, first_name="Peer", access_hash=access_hash), "message")
        await messages.upsert_message(db, account_id, pk, MessageRow(
            external_id=1, sent_at=T0, sender_external_id=sender, text=text,
        ))
        snap = await snapshots.create_snapshot(db, account_id, pk, [SnapshotMemberRow(external_user_id=sender)])
        await db.commit()
    return snap


def h(alias: str | None = None, key: str | None = None) -> dict:
    headers = {"authorization": f"Bearer {key}"} if key else dict(LEGACY)
    if alias:
        headers["x-session-alias"] = alias
    return headers


# ---------------------------------------------------------------------------
# T14: another account's ids → 404
# ---------------------------------------------------------------------------

async def test_t14_foreign_ids_are_404(accounts, test_client):
    await _seed(accounts.a, SHARED, "A's text")
    snap_b = await _seed(accounts.b, ONLY_B, "B's text")
    await _seed(accounts.b, SHARED, "B in the shared chat")

    r = await test_client.get("/api/v1/chats", headers=h("iso-a"))
    assert r.status_code == 200 and [c["id"] for c in r.json()["items"]] == [SHARED]

    for path in (
        f"/api/v1/chats/{ONLY_B}",
        f"/api/v1/messages?chat_id={ONLY_B}",
        f"/api/v1/messages/{ONLY_B}/1",
        f"/api/v1/snapshots/{snap_b}/members",
    ):
        assert (await test_client.get(path, headers=h("iso-a"))).status_code == 404, path

    r = await test_client.get(f"/api/v1/messages?chat_id={SHARED}", headers=h("iso-a"))
    assert [m["text"] for m in r.json()["items"]] == ["A's text"]
    r = await test_client.get(f"/api/v1/messages/{SHARED}/1", headers=h("iso-b"))
    assert r.json()["text"] == "B in the shared chat" and "deleted_at" not in r.json()


async def test_patch_chat_returns_fresh_row(accounts, test_client):
    """Ticket «PATCH /chats/{id}: протухший updated_at» — closed by the mapper."""
    await _seed(accounts.a, SHARED, "x")
    r = await test_client.patch(f"/api/v1/chats/{SHARED}", headers=h("iso-a"), json={"is_monitored": False})
    assert r.status_code == 200 and r.json()["is_monitored"] is False and r.json()["updated_at"]


# ---------------------------------------------------------------------------
# T15: keys and aliases
# ---------------------------------------------------------------------------

async def test_t15_account_key_alias_and_scopes(accounts, test_client):
    await _seed(accounts.a, SHARED, "A")
    _, key_a = await keys.create_key(scope="account", account_id=accounts.a, label="iso-test-a")
    _, owner = await keys.create_key(scope="owner", account_id=None, label="iso-test-owner")

    r = await test_client.get("/api/v1/chats", headers=h(key=key_a))  # no alias: the key's account
    assert r.status_code == 200 and [c["id"] for c in r.json()["items"]] == [SHARED]
    assert (await test_client.get("/api/v1/chats", headers=h("iso-a", key_a))).status_code == 200
    assert (await test_client.get("/api/v1/chats", headers=h("iso-b", key_a))).status_code == 403
    assert (await test_client.get("/api/v1/chats", headers=h(key=owner))).status_code == 403
    assert (await test_client.get("/api/v1/people", headers=h(key=key_a))).status_code == 403
    assert (await test_client.get("/api/v1/people", headers=LEGACY)).status_code == 403
    assert (await test_client.get("/api/v1/people", headers=h(key=owner))).status_code == 200
    assert (await test_client.get("/api/v1/chats", headers=h(key="tgmp_wrong"))).status_code == 401


async def test_t15_admin_key_endpoints(accounts, test_client):
    admin = {"x-admin-key": "test-admin-key"}
    r = await test_client.post(f"/api/v1/accounts/{accounts.a}/keys", headers=admin, json={"label": "iso-test-x"})
    assert r.status_code == 201
    created = r.json()
    assert created["key"].startswith("tgmp_")

    listed = (await test_client.get(f"/api/v1/accounts/{accounts.a}/keys", headers=admin)).json()
    assert [k["id"] for k in listed] == [created["id"]] and "key" not in listed[0]
    assert (await test_client.get("/api/v1/chats", headers=h(key=created["key"]))).status_code == 200

    assert (await test_client.delete(f"/api/v1/keys/{created['id']}", headers=admin)).status_code == 200
    assert (await test_client.get("/api/v1/chats", headers=h(key=created["key"]))).status_code == 401

    r = await test_client.post("/api/v1/owner-keys", headers=admin, json={"label": "iso-test-owner2"})
    assert r.status_code == 201 and r.json()["scope"] == "owner"
    assert (await test_client.post("/api/v1/owner-keys", json={})).status_code == 401  # admin key required


# ---------------------------------------------------------------------------
# T16: REQUIRE_ACCOUNT (D6)
# ---------------------------------------------------------------------------

async def test_t16_require_account(accounts, test_client, monkeypatch):
    monkeypatch.setattr(settings, "require_account", True)
    r = await test_client.get("/api/v1/chats", headers=LEGACY)
    assert r.status_code == 400
    assert r.json() == {"detail": "account required: X-Session-Alias or per-account key"}
    assert (await test_client.get("/api/v1/chats", headers=h("iso-a"))).status_code == 200


async def test_implicit_work_is_audited(accounts, test_client):
    import asyncio

    sql("DELETE FROM audit_logs WHERE error = 'implicit_work' AND tool = 'list_chat_snapshots'")
    await test_client.get("/api/v1/snapshots/chat/1", headers=LEGACY)
    for _ in range(50):
        rows = sql("SELECT alias FROM audit_logs WHERE error = 'implicit_work' AND tool = 'list_chat_snapshots'",
                   fetch=True)
        if rows:
            break
        await asyncio.sleep(0.05)
    assert rows == [("work",)]
    sql("DELETE FROM audit_logs WHERE error = 'implicit_work' AND tool = 'list_chat_snapshots'")


# ---------------------------------------------------------------------------
# T17: /reachable
# ---------------------------------------------------------------------------

async def _contacts(accounts):
    async with account_db(accounts.a) as db:
        # direct: A knows the hash; username/phone: identities A never met (seen by B).
        await users.upsert_user(db, accounts.a, "telegram",
                                UserRow(P_DIRECT, first_name="Direct", access_hash=7), "message")
        await db.commit()
    async with account_db(accounts.b) as db:
        await users.upsert_user(db, accounts.b, "telegram", UserRow(P_USERNAME, username="uname"), "message")
        await users.upsert_user(db, accounts.b, "telegram", UserRow(P_PHONE, phone="79005550000"), "message")
        await users.upsert_user(db, accounts.b, "telegram", UserRow(P_NONE, first_name="Nobody"), "message")
        chat = await chats.upsert_chat(db, accounts.b, ChatRow(ONLY_B, "group"))
        await messages.upsert_message(db, accounts.b, chat, MessageRow(
            external_id=5, sent_at=T0, sender_external_id=P_USERNAME, text="secret of B",
        ))
        await db.commit()
    async with account_db(accounts.m) as db:
        await users.upsert_user(db, accounts.m, "max", UserRow(9_400_000_699, first_name="Max user"), "message")
        await db.commit()


async def test_t17_reachable(accounts, test_client):
    await _contacts(accounts)
    r = await test_client.get("/api/v1/reachable", headers=h("iso-a"))
    assert r.status_code == 200
    methods = {i["external_user_id"]: i["method"] for i in r.json()["items"]}
    assert methods == {P_DIRECT: "direct", P_USERNAME: "username", P_PHONE: "phone"}
    assert "secret of B" not in r.text and "chat" not in r.text
    phone = next(i for i in r.json()["items"] if i["method"] == "phone")
    assert "address book" in phone["note"]

    assert (await test_client.get("/api/v1/reachable", headers=h("iso-b"))).status_code == 403  # ro


async def test_send_to_person_direct(accounts, test_client, monkeypatch):
    from telethon.tl.types import InputPeerUser

    await _contacts(accounts)
    person = sql("SELECT person_id FROM identities WHERE external_user_id = %s", (P_DIRECT,), fetch=True)[0][0]
    client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(chat_id=P_DIRECT, id=99)))

    async def authorized(request):
        return client

    monkeypatch.setattr("app.telegram.pool.require_authorized_client", authorized)
    r = await test_client.post("/api/v1/messages/to-person", headers=h("iso-a"),
                               json={"person_id": person, "text": "hi"})
    assert r.status_code == 200 and r.json()["method"] == "direct"
    client.send_message.assert_awaited_once_with(InputPeerUser(P_DIRECT, 7), "hi")
    # ro account: the tool is a messenger write → 403 before the endpoint
    r = await test_client.post("/api/v1/messages/to-person", headers=h("iso-b"),
                               json={"person_id": person, "text": "hi"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# T18: /people with the owner key
# ---------------------------------------------------------------------------

async def test_t18_person_messages_across_accounts(accounts, test_client):
    await _seed(accounts.a, SHARED, "said to A", sender=P_DIRECT)
    await _seed(accounts.b, ONLY_B, "said to B", sender=P_DIRECT)
    _, owner = await keys.create_key(scope="owner", account_id=None, label="iso-test-owner")
    person = sql("SELECT person_id FROM identities WHERE external_user_id = %s", (P_DIRECT,), fetch=True)[0][0]

    r = await test_client.get(f"/api/v1/people/{person}/messages", headers=h(key=owner))
    assert r.status_code == 200
    got = {(i["account"], i["chat_id"], i["text"]) for i in r.json()["items"]}
    assert got == {("iso-a", SHARED, "said to A"), ("iso-b", ONLY_B, "said to B")}

    detail = (await test_client.get(f"/api/v1/people/{person}", headers=h(key=owner))).json()
    known = {a["alias"]: a for a in detail["accounts"] if a["alias"] in ("iso-a", "iso-b")}
    assert known["iso-a"]["knows"] and known["iso-a"]["messages"] == 1
    assert known["iso-b"]["reachable"] is None  # ro: reachability is not computed

    r = await test_client.get("/api/v1/people", params={"q": str(P_DIRECT)}, headers=h(key=owner))
    assert [p["id"] for p in r.json()["items"]] == [person]
    chats_ = (await test_client.get(f"/api/v1/people/{person}/chats", headers=h(key=owner))).json()["items"]
    assert {c["account"] for c in chats_} == {"iso-a", "iso-b"}

    r = await test_client.patch(f"/api/v1/people/{person}", headers=h(key=owner), json={"tags": ["vip"]})
    assert r.json()["tags"] == ["vip"]


# ---------------------------------------------------------------------------
# authorize(): write limits
# ---------------------------------------------------------------------------

async def test_authorize_write_limits(accounts):
    ctx = AuthContext(account_id=accounts.a, alias="iso-a")
    sql("UPDATE accounts SET write_chat_ids = '{1}', write_rate_per_hour = NULL WHERE id = %s", (accounts.a,))
    assert (await authorize(ctx, "send_message", chat_external_id=1, is_write=True)).allowed
    denied = await authorize(ctx, "send_message", chat_external_id=2, is_write=True)
    assert (denied.allowed, denied.status) == (False, 403)

    sql("UPDATE accounts SET write_chat_ids = NULL, write_rate_per_hour = 0 WHERE id = %s", (accounts.a,))
    limited = await authorize(ctx, "send_message", is_write=True)
    assert (limited.allowed, limited.status) == (False, 429)
    assert (await authorize(ctx, "sync_chats", is_write=False)).allowed  # reads / DB writes unaffected

    max_ctx = AuthContext(account_id=accounts.m, alias="iso-max", platform="max")
    sql("UPDATE accounts SET mode = 'rw' WHERE id = %s", (accounts.m,))
    import app.authz.middleware as mw

    mw._mode_cache.clear()
    no_list = await authorize(max_ctx, "send_message", chat_external_id=5, is_write=True)
    assert (no_list.allowed, no_list.status) == (False, 403)
    sql("UPDATE accounts SET write_chat_ids = NULL, write_rate_per_hour = NULL WHERE id = %s", (accounts.a,))
