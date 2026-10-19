"""MCP download_media link → the file itself (QA-R1 2026-10-19, QA-R1-D1).

GET /api/v1/files/{token} streams the route of GET …/media through the
in-process invoke (app/mcp/invoke.py). Its synthetic scope said ASGI 3.0
without spec_version and its receive() answered http.disconnect right after
the body: Starlette's StreamingResponse (ASGI < 2.4) listens for a disconnect,
saw one at once and cancelled the stream → the outer middleware raised
"No response returned" → 500. Here the body is downloaded for real, through
the whole app (ASGI transport) and through uvicorn.
"""

import asyncio
import socket

import httpx
import pytest
import uvicorn

import app.main as main_module
from app.config import settings
from app.mcp import tools as mcp_tools
from tests.max_env import aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_backfill import BLOB, cdn  # noqa: F401 — pytest fixture
from tests.test_max_ingest import _authorized, fx
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture

DIALOG, MESSAGE = 100500, 15  # n15: own photo (0) + forwarded file (1)


async def _link(new_env, cdn, monkeypatch, base_url: str) -> dict:  # noqa: F811
    monkeypatch.setattr(settings, "file_link_secret", "test-secret")
    client = await _authorized(new_env)
    new_env.settings.proxy_url = ""
    new_env.server.file_url = cdn("/file")
    await client.push(fx("n15_own_photo_forward_file")["frame"])
    caller = mcp_tools.Caller("account", new_env.account_id, new_env.alias, "max", "ro", base_url)
    return await mcp_tools.BY_NAME["download_media"].call(caller, {"chat_id": DIALOG, "message_id": MESSAGE,
                                                                   "index": 1})


async def test_link_downloads_the_file_through_the_app(new_env, cdn, monkeypatch):  # noqa: F811
    link = await _link(new_env, cdn, monkeypatch, "http://test")
    resp = await new_env.client.get(link["url"].replace("http://test", ""))  # no key: the token is the credential
    assert resp.status_code == 200, resp.text[:300]
    assert resp.content == BLOB
    assert resp.headers["content-type"] == "application/pdf"
    resumed = await new_env.client.get(link["url"].replace("http://test", ""), headers={"range": "bytes=1000-"})
    assert resumed.status_code == 206 and resumed.content == BLOB[1000:]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_link_downloads_the_file_through_uvicorn(new_env, cdn, monkeypatch):  # noqa: F811
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(main_module.app, host="127.0.0.1", port=port, lifespan="off",
                                           log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.02)
        link = await _link(new_env, cdn, monkeypatch, f"http://127.0.0.1:{port}")
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.get(link["url"])
        assert resp.status_code == 200, resp.text[:300]
        assert resp.content == BLOB
    finally:
        server.should_exit = True
        await task
