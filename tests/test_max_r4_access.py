"""Release 4 access: ro, Telegram, route names, MCP (plan 2026-10-19 Р4 §3 PR-R4-2 tests 8-10; QA R4-2,
R4-13, R4-14).
"""

import asyncio

import app.authz.middleware as mw
from app.authz import tool_catalog
from app.mcp import tools as mcp_tools
from tests.isolation_env import sql
from tests.max_env import ADMIN_KEY, API_KEY, aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_groups import FOREIGN, _foreign, _get
from tests.test_max_ingest import DIALOG
from tests.test_max_write_limits import _events, clock, rw  # noqa: F401 — pytest fixtures
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture

FILE = {"file": ("v.ogg", b"OggS", "audio/ogg")}
# (method, path, json body, multipart files, tool): the 14 new writes of a MAX account
WRITES = [
    ("POST", f"/messages/{DIALOG}/5/pin", {}, None, "pin_message"),
    ("POST", f"/messages/{DIALOG}/5/unpin", {}, None, "unpin_message"),
    ("POST", "/chats/join", {"target": "@news"}, None, "join_chat_endpoint"),
    ("POST", "/messages/send-voice", None, FILE, "send_voice"),
    ("POST", "/messages/album", None, [("files", ("a.png", b"1", "image/png"))] * 2, "send_album"),
    ("POST", "/messages/poll", {"chat_id": DIALOG, "question": "q", "options": ["a", "b"]}, None, "send_poll"),
    ("POST", "/messages/send-video-note", None, FILE, "send_video_note"),
    ("POST", f"/messages/{DIALOG}/5/vote", {"answer_ids": [1]}, None, "vote_poll"),
    ("POST", "/contacts/import", {"contacts": [{"phone": "+79001110001", "first_name": "x"}]}, None,
     "import_contacts"),
    ("POST", "/contacts/6001/remove", {}, None, "remove_contact"),
    ("POST", f"/chats/{DIALOG}/delete", {"confirm_user_id": 1}, None, "delete_dialog"),
    ("POST", "/folders", {"title": "x"}, None, "create_folder"),
    ("PATCH", "/folders/f-1", {"title": "y"}, None, "update_folder"),
    ("POST", "/folders/f-1/delete", {"confirm_title": "y"}, None, "delete_folder"),
]
MAX_ONLY = [w for w in WRITES if w[4] in tool_catalog.MAX_ONLY_TOOLS] + [
    ("GET", f"/messages/{DIALOG}/5/reactions", None, None, "get_reactions"),
    ("GET", "/folders", None, None, "list_folders"),
]


async def _call(w, method, path, body, files, headers=None):
    h = headers or w.env.h()
    if files is not None:
        return await w.http.request(method, "/api/v1" + path, headers=h, data={"chat_id": str(DIALOG)}, files=files)
    return await w.http.request(method, "/api/v1" + path, headers=h, json=body)


def test_catalog_counts():
    assert len(WRITES) == 14 and {w[4] for w in WRITES} <= tool_catalog.WRITE_MESSENGER_TOOLS
    assert len(MAX_ONLY) == 10 and {w[4] for w in MAX_ONLY} == {*tool_catalog.MAX_R4_WRITE_TOOLS,
                                                               *tool_catalog.MAX_R4_READ_TOOLS}


async def test_ro_cannot_write_and_nothing_reaches_max(rw):
    await _foreign(rw)
    sql("UPDATE accounts SET mode = 'ro' WHERE id = %s", (rw.id,))
    mw._mode_cache.clear()
    for method, path, body, files, tool in WRITES:
        resp = await _call(rw, method, path, body, files)
        assert resp.status_code == 403, (tool, resp.text)
    server = rw.env.server
    assert (server.writes, server.group_calls, server.sync_calls, server.sent_attachments) == ([], [], [], [])
    assert _events(rw.id) == []
    assert (await _get(rw, "/folders")).status_code == 200
    for _ in range(100):
        denied = sql("SELECT tool FROM audit_logs WHERE alias = %s AND status = 'denied'", (rw.env.alias,),
                     fetch=True)
        if len(denied) >= len(WRITES):
            break
        await asyncio.sleep(0.05)
    assert sorted(t for (t,) in denied) == sorted(w[4] for w in WRITES)


async def test_telegram_gets_501_for_max_only_routes(rw, aliases):  # noqa: F811
    tg_alias, _ = aliases("tgr4", platform="telegram", mode="rw")
    h = {**API_KEY, "x-session-alias": tg_alias}
    for method, path, body, files, tool in MAX_ONLY:
        resp = await _call(rw, method, path, body, files, headers=h)
        assert resp.status_code == 501, (tool, resp.text)
        assert (resp.json()["platform"], resp.json()["tool"]) == ("telegram", tool)


async def test_route_names_on_max(rw):
    import app.main as main_module

    table = main_module.app.state.route_table
    for method, path, _, _, tool in [*WRITES, *MAX_ONLY,
                                     ("POST", "/chats/resolve", None, None, "resolve_chat_endpoint")]:
        assert table.resolve(method, "/api/v1/_max" + path) == tool, path


async def test_mcp_tools_and_calls(rw):
    names = lambda mode: {t.name for t in mcp_tools.visible(  # noqa: E731
        mcp_tools.Caller("account", rw.id, rw.env.alias, "max", mode, "http://test"))}
    rw_tools, ro_tools = names("rw"), names("ro")
    r4_mcp = {"send_video_note", "vote_poll", "get_reactions", "import_contacts", "remove_contact", "delete_dialog",
              "list_folders", "create_folder", "update_folder", "delete_folder"}
    shared = {"pin_message", "unpin_message", "join_chat", "resolve_chat", "send_voice", "send_album", "send_poll"}
    assert r4_mcp | shared <= rw_tools and (len(rw_tools), len(ro_tools)) == (77, 31)
    assert ro_tools & (r4_mcp | shared) == {"resolve_chat", "get_reactions", "list_folders"}

    await _foreign(rw)
    caller = mcp_tools.Caller("account", rw.id, rw.env.alias, "max", "rw", "http://test")
    tool = mcp_tools.BY_NAME["create_folder"]
    args = mcp_tools.coerce_ids({"title": "MCP", "chat_ids": [str(FOREIGN)]}, tool.schema)
    created = await tool.call(caller, args)
    assert created["chat_ids"] == [FOREIGN]
    renamed = await mcp_tools.BY_NAME["update_folder"].call(caller, {"folder_id": created["folder_id"],
                                                                     "title": "MCP 2"})
    assert (renamed["title"], renamed["chat_ids"]) == ("MCP 2", [FOREIGN])
    listed = await mcp_tools.BY_NAME["list_folders"].call(caller, {})
    assert [f["title"] for f in listed["items"]] == ["MCP 2"]


def test_send_poll_schema_says_max_takes_no_anonymous_poll():
    """review 17.2 R17.2-1: a MAX key sees is_anonymous default false with the reason; TG — the stdio schema."""
    from app.mcp.stdio_catalog import STDIO_TOOLS

    def schema(platform: str) -> dict:
        caller = mcp_tools.Caller("account", 1, "x", platform, "rw", "http://test")
        return next(t for t in mcp_tools.visible(caller) if t.name == "send_poll").schema

    max_prop = schema("max")["properties"]["is_anonymous"]
    assert max_prop["default"] is False and "MAX: false only" in max_prop["description"]
    stdio = next(t for t in STDIO_TOOLS if t["name"] == "send_poll")["inputSchema"]
    assert schema("telegram") == mcp_tools.widen_ids(stdio) and schema("telegram")["properties"]["is_anonymous"][
        "default"] is True
    assert schema("max")["properties"]["chat_id"] == schema("telegram")["properties"]["chat_id"]
