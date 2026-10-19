"""MCP over Streamable HTTP (ТЗ 2026-10-13 §9, PR-5): T21.

The SDK client (streamablehttp_client) talks to the app in-process (ASGI);
the session manager runs for the test. Data under the non-superuser role.
"""

import contextlib
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from starlette.responses import Response

import app.authz.middleware as mw
from app.authz import keys
from app.config import settings
from app.mcp import files
from app.mcp import server as mcp_server
from app.mcp.stdio_catalog import STDIO_TOOLS
from app.mcp.tools import ACCOUNT_TOOLS, widen_ids
from app.repo import chats, messages, users
from app.repo.rows import ChatRow, MediaRow, MessageRow, UserRow
from app.storage import account_db
from tests.isolation_env import sql

T0 = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
CHAT_A, CHAT_B = -1_009_400_000_800, -1_009_400_000_801


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_mode", "new")
    monkeypatch.setattr(settings, "shadow_legacy_write", False)
    monkeypatch.setattr(settings, "file_link_secret", "test-secret")
    monkeypatch.setattr(files, "UPLOAD_ROOT", tmp_path / "uploads")
    mw._alias_cache.clear()
    mw._mode_cache.clear()
    keys.invalidate()
    yield
    sql("DELETE FROM api_keys WHERE label LIKE 'iso-mcp%%'")
    sql("DELETE FROM audit_logs WHERE alias IN ('iso-a', 'iso-b', 'owner') AND tool <> 'iso_test'")
    keys.invalidate()


@pytest.fixture
def app(app_with_mocked_pool):
    return app_with_mocked_pool


@pytest_asyncio.fixture
async def mcp_keys(accounts):
    _, a = await keys.create_key(scope="account", account_id=accounts.a, label="iso-mcp-a")
    _, b = await keys.create_key(scope="account", account_id=accounts.b, label="iso-mcp-b")
    _, owner = await keys.create_key(scope="owner", account_id=None, label="iso-mcp-owner")
    return SimpleNamespace(a=a, b=b, owner=owner)


@contextlib.asynccontextmanager
async def session(app, key: str):
    def factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers=headers,
                                 timeout=timeout, auth=auth, follow_redirects=True)

    # The session manager's task group must be entered and left in the test's own
    # task (an async fixture tears down in another one), so it runs here.
    async with mcp_server.running():
        async with streamablehttp_client("http://test/mcp", headers={"Authorization": f"Bearer {key}"},
                                         httpx_client_factory=factory) as (read, write, _):
            async with ClientSession(read, write) as s:
                await s.initialize()
                yield s


async def tool(s, name, **args):
    result = await s.call_tool(name, args)
    text = result.content[0].text
    try:
        return json.loads(text)
    except ValueError:
        return text


async def _seed(accounts):
    for acc, chat, text in ((accounts.a, CHAT_A, "A's"), (accounts.b, CHAT_B, "B's")):
        async with account_db(acc) as db:
            pk = await chats.upsert_chat(db, acc, ChatRow(chat, "supergroup", title=text))
            await users.upsert_user(db, acc, "telegram", UserRow(9_400_000_900, first_name="P"), "message")
            await messages.upsert_message(db, acc, pk, MessageRow(
                external_id=1, sent_at=T0, sender_external_id=9_400_000_900, text=text,
                media=[MediaRow("document", file_external_id="f", file_name="doc.pdf", file_size=3,
                                mime_type="application/pdf")],
            ))
            await db.commit()


# ---------------------------------------------------------------------------

async def test_401_without_or_with_a_bad_key(app, accounts):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert (await c.post("/mcp", json=body)).status_code == 401
        assert (await c.post("/mcp", json=body, headers={"authorization": "Bearer nope"})).status_code == 401
        assert (await c.post("/mcp", json=body, headers={"x-api-key": "test-api-key"})).status_code == 401


async def test_list_tools_by_key(app, mcp_keys):
    async with session(app, mcp_keys.a) as s:
        rw = {t.name for t in (await s.list_tools()).tools}
    async with session(app, mcp_keys.b) as s:
        ro = {t.name for t in (await s.list_tools()).tools}
    async with session(app, mcp_keys.owner) as s:
        owner = {t.name for t in (await s.list_tools()).tools}

    stdio = {t["name"] for t in STDIO_TOOLS}
    assert len(stdio) == 42
    assert rw == stdio | {"reachable_contacts", "send_to_person", "contact_by_phone", "create_upload"}
    writes = {t.name for t in ACCOUNT_TOOLS if t.is_write}
    assert ro == stdio - writes and "send_message" not in ro and "list_chats" in ro
    assert owner == {"find_person", "get_person", "person_accounts", "person_messages", "person_chats",
                     "update_person", "merge_people", "split_person"}


async def test_tool_schemas_match_the_stdio_wrapper(app, mcp_keys):
    async with session(app, mcp_keys.a) as s:
        listed = {t.name: t for t in (await s.list_tools()).tools}
    file_tools = {"send_photo", "send_file", "send_voice", "send_album", "download_media"}
    for spec in STDIO_TOOLS:
        if spec["name"] in file_tools:
            continue
        assert listed[spec["name"]].description == spec["description"], spec["name"]
        # the wrapper's schema with ids widened to string|integer (plan 2026-10-19 Р-1)
        assert listed[spec["name"]].inputSchema == widen_ids(spec["inputSchema"]), spec["name"]


async def test_reads_are_per_account_and_audited(app, accounts, mcp_keys):
    await _seed(accounts)
    async with session(app, mcp_keys.a) as s:
        chats_a = await tool(s, "list_chats")
        foreign = await tool(s, "get_chat", chat_id=CHAT_B)
        msgs = await tool(s, "list_messages", chat_id=CHAT_A)
    assert [c["id"] for c in chats_a["items"]] == [CHAT_A]
    assert isinstance(foreign, str) and foreign.startswith("HTTP 404")
    assert [m["text"] for m in msgs["items"]] == ["A's"]

    for _ in range(50):
        rows = sql("SELECT tool, status FROM audit_logs WHERE alias = 'iso-a' ORDER BY id", fetch=True)
        if len(rows) >= 3:
            break
        import asyncio
        await asyncio.sleep(0.05)
    assert ("list_chats", "ok") in rows and ("get_chat_detail", "error") in rows


async def test_write_denied_by_policy_is_403_and_audited(app, accounts, mcp_keys):
    sql("INSERT INTO account_tool_policy (account_id, tool_name, effect) VALUES (%s, 'send_message', 'deny')",
        (accounts.a,))
    try:
        async with session(app, mcp_keys.a) as s:
            out = await tool(s, "send_message", chat_id=CHAT_A, text="hi")
        assert isinstance(out, str) and out.startswith("HTTP 403")
    finally:
        sql("DELETE FROM account_tool_policy WHERE account_id = %s", (accounts.a,))


async def test_ro_cannot_call_hidden_write_tool(app, mcp_keys):
    import asyncio

    async with session(app, mcp_keys.b) as s:
        out = await tool(s, "send_message", chat_id=1, text="x")
    async with session(app, mcp_keys.owner) as s:
        await tool(s, "list_chats")
    assert "not available" in out
    # QA D-4: the attempt is in the audit as denied
    for _ in range(50):
        rows = sql("SELECT alias, tool, status, error FROM audit_logs WHERE status = 'denied'"
                   " AND alias IN ('iso-b', 'owner') ORDER BY id", fetch=True)
        if len(rows) >= 2:
            break
        await asyncio.sleep(0.05)
    assert ("iso-b", "send_message", "denied", "tool not available for this key") in rows
    assert ("owner", "list_chats", "denied", "tool not available for this key") in rows


async def test_owner_tools(app, accounts, mcp_keys):
    await _seed(accounts)
    async with session(app, mcp_keys.owner) as s:
        found = await tool(s, "find_person", q="9400000900")
        person = found["items"][0]["id"]
        per_account = await tool(s, "person_accounts", person_id=person)
        msgs = await tool(s, "person_messages", person_id=person)
        not_mine = await tool(s, "list_chats")
    assert {a["alias"] for a in per_account["accounts"] if a["knows"]} >= {"iso-a", "iso-b"}
    assert {m["text"] for m in msgs["items"]} == {"A's", "B's"}
    assert "not available" in not_mine


# ---------------------------------------------------------------------------
# Files (§9.2)
# ---------------------------------------------------------------------------

async def test_download_media_returns_a_signed_link(app, accounts, mcp_keys, monkeypatch):
    await _seed(accounts)
    async with session(app, mcp_keys.a) as s:
        link = await tool(s, "download_media", chat_id=CHAT_A, message_id=1, save_to="/home/x/doc.pdf")
    assert link["file_name"] == "doc.pdf" and link["mime_type"] == "application/pdf"
    assert link["url"].startswith("http://test/api/v1/files/") and "curl" in link
    assert "not used" in link["note"]

    seen = {}

    def fake_forward(platform, method, path, *, state, **kw):
        seen.update(platform=platform, path=path, account=state["account_id"], headers=kw.get("headers"))
        return Response(b"PDF", media_type="application/pdf")

    monkeypatch.setattr("app.mcp.invoke.forward", fake_forward)
    path = link["url"].replace("http://test", "")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(path, headers={"range": "bytes=1-"})  # no API key: the token is the credential
        assert r.status_code == 200 and r.content == b"PDF"
        assert seen == {"platform": "telegram", "path": f"/messages/{CHAT_A}/1/media", "account": accounts.a,
                        "headers": {"range": "bytes=1-"}}
        bad = path[:-3] + ("000" if not path.endswith("000") else "111")
        assert (await c.get(bad)).status_code == 403


async def test_expired_link_is_403(app, accounts, monkeypatch):
    monkeypatch.setattr(files, "LINK_TTL", -1)
    link = files.file_link("http://test", accounts.a, CHAT_A, 1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(link["url"].replace("http://test", ""))
    assert r.status_code == 403 and "expired" in r.text


async def test_upload_then_send_file(app, accounts, mcp_keys, monkeypatch):
    sent = {}

    class Client:
        async def send_file(self, entity, file, **kw):
            sent.update(entity=entity, name=file.name, data=file.read(), caption=kw.get("caption"))
            return SimpleNamespace(id=77)

    async def authorized(request):
        assert request.state.account_id == accounts.a
        return Client()

    monkeypatch.setattr("app.api.messages.require_authorized_client", authorized)
    async with session(app, mcp_keys.a) as s:
        up = await tool(s, "create_upload")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.put(up["upload_url"].replace("http://test", "") + "?name=report.pdf", content=b"%PDF-1")
            assert r.status_code == 200 and r.json()["size"] == 6
        result = await tool(s, "send_file", chat_id=CHAT_A, upload_id=up["upload_id"], caption="see")
        local = await tool(s, "send_file", chat_id=CHAT_A, file_path="/tmp/x.pdf")
        plain_http = await tool(s, "send_file", chat_id=CHAT_A, file_url="http://example.com/x.pdf")
        private = await tool(s, "send_file", chat_id=CHAT_A, file_url="https://127.0.0.1/x.pdf")
    assert result["message_id"] == 77
    assert sent == {"entity": CHAT_A, "name": "report.pdf", "data": b"%PDF-1", "caption": "see"}
    assert "create_upload" in local
    assert "https" in plain_http
    assert "non-public" in private


async def test_upload_of_another_account_is_not_found(app, accounts, mcp_keys):
    async with session(app, mcp_keys.a) as s:
        up = await tool(s, "create_upload")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        await c.put(up["upload_url"].replace("http://test", ""), content=b"x")
    with pytest.raises(files.LinkError, match="not found"):
        files.read_upload(accounts.b, up["upload_id"])


def test_link_tokens_are_kind_bound_and_need_a_secret(monkeypatch):
    token, _ = files.sign({"k": "upload", "a": 1, "u": "abc"})
    with pytest.raises(files.LinkError, match="kind"):
        files.verify(token, "file")
    monkeypatch.setattr(settings, "file_link_secret", "")
    with pytest.raises(files.LinkError, match="FILE_LINK_SECRET"):
        files.file_link("http://x", 1, 2, 3)


def test_expired_uploads_are_purged(tmp_path):
    files.UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    d = files.UPLOAD_ROOT / "5"
    d.mkdir()
    (d / "abc").write_bytes(b"x")
    (d / "abc.json").write_text(json.dumps({"expires_at": time.time() - 1, "file_name": "f"}))
    assert files.purge_expired() == 1 and not (d / "abc").exists()


@pytest.mark.parametrize("platform", ["telegram", "max"])
def test_every_tool_reaches_its_rest_route(platform):
    """Each of the 42 tools (+ contacts tools) resolves to a real route with the
    same name authz and audit use; MAX: what MAX has, the rest is 501 like REST."""
    import re

    from app.mcp import invoke
    from app.mcp.tools import BY_NAME

    missing = []
    for name, tool in BY_NAME.items():
        ref = getattr(tool.call, "rest_route", None)
        if ref is None:
            continue  # person_accounts / create_upload: composed, covered above
        method, template = ref
        path = re.sub(r"\{[a-z_]+\}", "1", template)
        found = invoke._find(platform, method, path)
        if found is None:
            if platform == "max":
                continue  # 501 for MAX, as the REST API answers
            missing.append(name)
            continue
        route_name = found[0].name
        expected = {"download_media": "get_single_message", "person_accounts": "get_person"}.get(name, tool.route)
        assert route_name == expected, (name, route_name)
    assert not missing, missing


# ---------------------------------------------------------------------------
# R5-1: file_url connects only to the addresses that passed the check
# ---------------------------------------------------------------------------

async def test_file_url_dns_rebinding_is_not_possible(monkeypatch):
    import socket as _socket

    answers = [[(_socket.AF_INET, "93.184.216.34")], [(_socket.AF_INET, "127.0.0.1")]]
    calls = []

    async def resolve(host, port):
        calls.append(host)
        return answers[min(len(calls) - 1, 1)]  # 1st: public, any later lookup: loopback

    captured = {}

    class Stop(Exception):
        pass

    class FakeConnector:
        def __init__(self, *, resolver, use_dns_cache):
            captured["resolver"] = resolver
            captured["dns_cache"] = use_dns_cache
            raise Stop()

    monkeypatch.setattr(files, "_resolve", resolve)
    monkeypatch.setattr("aiohttp.TCPConnector", FakeConnector)
    with pytest.raises(Stop):
        await files.fetch_url("https://rebind.example/x.pdf")

    resolver = captured["resolver"]
    got = await resolver.resolve("rebind.example", 443)
    got_again = await resolver.resolve("rebind.example", 443)
    assert [g["host"] for g in got + got_again] == ["93.184.216.34", "93.184.216.34"]
    assert calls == ["rebind.example"]  # resolved exactly once, the connection never re-resolves
    assert captured["dns_cache"] is False
    with pytest.raises(OSError):
        await resolver.resolve("other.example", 443)


async def test_file_url_any_non_public_address_refuses(monkeypatch):
    import socket as _socket

    async def resolve(host, port):
        return [(_socket.AF_INET, "93.184.216.34"), (_socket.AF_INET, "10.0.0.5")]

    monkeypatch.setattr(files, "_resolve", resolve)
    with pytest.raises(files.LinkError, match="non-public"):
        await files.fetch_url("https://mixed.example/x.pdf")
