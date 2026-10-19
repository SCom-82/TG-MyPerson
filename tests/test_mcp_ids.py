"""MCP ids as strings (plan 2026-10-19, PR-A: Р-1, Р-2, QA R1-1…R1-4).

MAX ids are ~1.2e17 > 2^53: a client that parses JSON numbers as doubles
rounds them and gets a 404. Input schemas take a string or a number, the server
makes it an exact int before authorize() and the REST route; ids beyond 2^53
come back as strings. TG ids are smaller: TG answers do not change.
"""

import asyncio
import json
from types import SimpleNamespace

import jsonschema
import pytest
import pytest_asyncio

import app.main as main_module
from app.authz import keys
from app.config import settings
from app.mcp import files, invoke
from app.mcp import tools as mcp_tools
from app.mcp.stdio_catalog import STDIO_TOOLS
from tests.isolation_env import sql
from tests.max_env import aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_ingest import fx
from tests.test_max_write_limits import OFF, _limits, clock, rw  # noqa: F401 — pytest fixtures
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture
from tests.test_mcp_http import CHAT_A, CHAT_B, _seed, session, tool

BIG = 117466202990583689  # a real MAX message id (brief 19.10): float64 makes it …700
SAVED = 0  # «Избранное»


@pytest.fixture(autouse=True)
def _links(monkeypatch):
    monkeypatch.setattr(settings, "file_link_secret", "test-secret")
    keys.invalidate()
    sql("DELETE FROM audit_logs WHERE alias = 'iso-a'")
    yield
    sql("DELETE FROM api_keys WHERE label LIKE 'mcp-ids%%'")
    sql("DELETE FROM audit_logs WHERE alias = 'iso-a'")
    keys.invalidate()


@pytest.fixture
def rest_calls(monkeypatch):
    """What the tools hand to the in-process REST call."""
    seen = []
    real = invoke.call

    async def spy(platform, method, path, **kw):
        seen.append((method, path, kw.get("query"), kw.get("json_body")))
        return await real(platform, method, path, **kw)

    monkeypatch.setattr(invoke, "call", spy)
    return seen


@pytest_asyncio.fixture
async def max_rw(rw):  # noqa: F811
    await _limits(rw, **OFF)
    _, key = await keys.create_key(scope="account", account_id=rw.id, label="mcp-ids-max")
    return SimpleNamespace(**vars(rw), key=key)


async def _photo(w, chat_id: int, message_id: int) -> None:
    frame = fx("n03_photo")["frame"]
    frame["payload"]["chatId"] = chat_id
    frame["payload"]["message"]["id"] = str(message_id)
    await w.client.push(frame)


async def _audit(alias: str, n: int, status: str = "%") -> list[tuple]:
    for _ in range(100):
        rows = sql("SELECT tool, status, error, chat_id FROM audit_logs WHERE alias = %s AND status LIKE %s"
                   " ORDER BY id", (alias, status), fetch=True)
        if len(rows) >= n:
            return rows
        await asyncio.sleep(0.05)
    return rows


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

def test_widen_ids_rules():
    schema = {"type": "object", "properties": {
        "chat_id": {"type": "integer", "description": "Chat"},
        "message_ids": {"type": "array", "items": {"type": "integer"}},
        "person_id": {"type": "integer"},
        "from_id": {"type": "integer"},
        "limit": {"type": "integer"},
        "sticker_set_id": {"type": "integer"},
    }}
    wide = mcp_tools.widen_ids(schema)["properties"]
    assert wide["chat_id"] == {"type": ["string", "integer"], "pattern": r"^-?\d+$",
                               "description": "Chat. " + mcp_tools.ID_NOTE}
    assert wide["message_ids"]["items"] == {"type": ["string", "integer"], "pattern": r"^-?\d+$"}
    assert wide["sticker_set_id"]["type"] == ["string", "integer"]  # any *_id
    assert wide["person_id"] == {"type": "integer"} and wide["from_id"] == {"type": "integer"}
    assert wide["limit"] == {"type": "integer"}
    assert schema["properties"]["chat_id"] == {"type": "integer", "description": "Chat"}  # a copy
    ended = mcp_tools.widen_ids({"properties": {"user_id": {"type": "integer", "description": "User id."}}})
    assert ended["properties"]["user_id"]["description"] == "User id. " + mcp_tools.ID_NOTE

    full = mcp_tools.widen_ids(schema)
    jsonschema.validate({"chat_id": str(BIG), "message_ids": ["1", 2]}, full)
    jsonschema.validate({"chat_id": -BIG}, full)
    for bad in ("12a", "1.5", "", " 1"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"chat_id": bad}, full)


@pytest.mark.parametrize("platform", ["telegram", "max"])
def test_every_id_input_takes_a_string(platform):
    caller = mcp_tools.Caller("account", 1, "x", platform, "rw", "http://test")
    widened = 0
    for t in mcp_tools.visible(caller):
        for name, prop in t.schema["properties"].items():
            declared = prop if prop.get("type") != "array" else prop["items"]
            if declared.get("type") == "string":
                continue  # upload_id, q, ...: strings anyway
            if name in mcp_tools.SMALL_IDS:
                assert declared["type"] == "integer", (t.name, name)
            elif name in mcp_tools.ID_KEYS or name.endswith("_id"):
                assert declared["type"] == ["string", "integer"], (t.name, name)
                assert mcp_tools.ID_NOTE in prop["description"], (t.name, name)
                widened += 1
    assert widened > 20
    assert mcp_tools.BY_NAME["send_to_person"].schema["properties"]["person_id"] == {"type": "integer"}
    assert mcp_tools.BY_NAME["person_messages"].schema["properties"]["chat_id"]["type"] == ["string", "integer"]
    assert mcp_tools.BY_NAME["merge_people"].schema["properties"]["to_id"] == {"type": "integer"}


async def test_list_tools_shows_widened_ids(max_rw):
    async with session(main_module.app, max_rw.key) as s:
        listed = {t.name: t.inputSchema for t in (await s.list_tools()).tools}
    assert listed["get_message"]["properties"]["message_id"]["type"] == ["string", "integer"]
    assert listed["forward_message"]["properties"]["message_ids"]["items"]["type"] == ["string", "integer"]
    stdio = {t["name"]: t for t in STDIO_TOOLS}
    assert listed["list_chats"] == mcp_tools.widen_ids(stdio["list_chats"]["inputSchema"])


# ---------------------------------------------------------------------------
# MAX: exact ids in, strings out (R1-1, R1-4)
# ---------------------------------------------------------------------------

async def test_max_big_ids_as_strings_reach_rest_exactly(max_rw, rest_calls):
    await _photo(max_rw, SAVED, BIG)
    async with session(main_module.app, max_rw.key) as s:
        as_string = await tool(s, "get_message", chat_id=str(SAVED), message_id=str(BIG))
        as_number = await tool(s, "get_message", chat_id=SAVED, message_id=BIG)
        raw = (await s.call_tool("get_message", {"chat_id": "0", "message_id": str(BIG)})).content[0].text
        link = await tool(s, "download_media", chat_id="0", message_id=str(BIG))
    assert as_string == as_number
    assert as_string["message_id"] == str(BIG) and as_string["chat_id"] == 0
    assert f'"message_id": "{BIG}"' in raw
    assert [c[1] for c in rest_calls] == [f"/messages/0/{BIG}"] * 4
    token = link["url"].rsplit("/", 1)[1]
    assert files.verify(token, "file")["m"] == BIG


async def test_max_writes_with_string_ids(max_rw):
    await _photo(max_rw, SAVED, BIG)
    async with session(main_module.app, max_rw.key) as s:
        fwd = await tool(s, "forward_message", from_chat_id="0", to_chat_id="0", message_ids=[str(BIG), str(BIG + 1)])
        deleted = await tool(s, "delete_message", chat_id="0", message_id=str(BIG))
    writes = max_rw.env.server.writes
    assert [w[2] for w in writes if w[0] == "forward_message"] == [BIG, BIG + 1], writes
    assert len(fwd["message_ids"]) == 2, fwd
    assert deleted["for_all"] is True and ("delete_message", 0, [BIG], False) in writes


async def test_bad_ids_are_refused_before_rest_and_audited(max_rw, rest_calls):
    async with session(main_module.app, max_rw.key) as s:
        results = [await s.call_tool("get_message", {"chat_id": bad, "message_id": "1"})
                   for bad in ("12a", "1.5", "")]
        listed = await s.call_tool("forward_message", {"from_chat_id": "0", "to_chat_id": "0",
                                                       "message_ids": ["1", "x"]})
        flag = await s.call_tool("get_message", {"chat_id": True, "message_id": 1})
    for r in (*results, listed, flag):
        assert r.isError and "must be an integer id" in r.content[0].text, r.content[0].text
    assert rest_calls == [] and max_rw.env.server.writes == []
    rows = await _audit(max_rw.env.alias, 5, "error")
    assert [(t, st) for t, st, *_ in rows] == [("get_single_message", "error")] * 3 + [
        ("forward_message", "error"), ("get_single_message", "error")]
    assert rows[0][2] == "Error: chat_id must be an integer id"


async def test_missing_required_argument_is_refused_and_audited(max_rw, rest_calls):
    async with session(main_module.app, max_rw.key) as s:
        r = await s.call_tool("get_message", {"chat_id": "0"})
    assert r.isError and r.content[0].text.startswith("Input validation error:") and "message_id" in r.content[0].text
    assert rest_calls == []
    [(name, status, error, _)] = await _audit(max_rw.env.alias, 1, "error")
    assert (name, status) == ("get_single_message", "error") and error.startswith("Input validation error")


def test_safe_ints():
    assert mcp_tools.safe_ints({"a": [BIG, -BIG, 2**53 - 1, -(2**53 - 1), True, 1.5, None],
                                "b": {"c": 2**53}, "t": (BIG,)}) == {
        "a": [str(BIG), str(-BIG), 2**53 - 1, -(2**53 - 1), True, 1.5, None],
        "b": {"c": str(2**53)}, "t": [str(BIG)]}


# ---------------------------------------------------------------------------
# TG: same answers, string ids work too (R1-2, R1-4)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def tg_keys(accounts):
    _, a = await keys.create_key(scope="account", account_id=accounts.a, label="mcp-ids-a")
    return SimpleNamespace(a=a)


async def test_tg_answers_do_not_change(app_with_mocked_pool, accounts, tg_keys, monkeypatch):
    await _seed(accounts)
    monkeypatch.setattr(settings, "storage_mode", "new")
    monkeypatch.setattr(settings, "shadow_legacy_write", False)
    async with session(app_with_mocked_pool, tg_keys.a) as s:
        text = (await s.call_tool("list_messages", {"chat_id": CHAT_A})).content[0].text
        by_string = (await s.call_tool("list_messages", {"chat_id": str(CHAT_A)})).content[0].text
        one = await tool(s, "get_message", chat_id=str(CHAT_A), message_id="1")
    body = await invoke.call("telegram", "GET", "/messages", query={"chat_id": CHAT_A},
                             state={"account_id": accounts.a, "session_alias": "iso-a",
                                    "platform": "telegram", "scope": "account"})
    # the text before PR-A: json.dumps of the REST body, nothing turned into strings
    assert text == json.dumps(body.body, ensure_ascii=False, indent=2, default=str) == by_string
    assert isinstance(json.loads(text)["items"][0]["chat_id"], int)
    assert one["chat_id"] == CHAT_A and one["message_id"] == 1


async def test_write_chat_ids_guard_sees_an_int(app_with_mocked_pool, accounts, tg_keys, monkeypatch):
    monkeypatch.setattr(settings, "storage_mode", "new")
    sql("UPDATE accounts SET write_chat_ids = %s WHERE id = %s", ([CHAT_A], accounts.a))
    try:
        async with session(app_with_mocked_pool, tg_keys.a) as s:
            denied = await tool(s, "send_message", chat_id=str(CHAT_B), text="x")
            allowed = await tool(s, "send_message", chat_id=str(CHAT_A), text="x")
    finally:
        sql("UPDATE accounts SET write_chat_ids = NULL WHERE id = %s", (accounts.a,))
    assert denied.startswith("HTTP 403") and "write_chat_ids" in denied
    assert "write_chat_ids" not in allowed  # passed authorize(); the mocked pool has no client
    rows = await _audit("iso-a", 2)
    assert (rows[0][1], rows[0][3]) == ("denied", CHAT_B)
