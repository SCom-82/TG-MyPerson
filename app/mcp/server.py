"""MCP server: Streamable HTTP at /mcp, the key decides who calls (ТЗ 2026-10-13 §9.1).

- ASGI wrapper on the mount: Authorization: Bearer <key> → api_keys (account
  or owner key); no/invalid key → 401 before MCP. The caller travels to the
  handlers on the request (request.state.mcp_caller).
- list_tools depends on the caller: owner → contacts tools; ro account →
  no messenger writes; rw → everything + reachable_contacts, send_to_person,
  create_upload.
- call_tool → ids to int (strings accepted: MAX ids exceed 2^53) → schema
  check → authorize() → the REST route in-process → audit(): the same
  decision and the same audit row as the REST API. Integers beyond 2^53 in
  the result are strings.
- stateless + JSON responses: any worker, no session affinity.
"""

import contextlib
import hashlib
import json
import logging
from typing import Any

import jsonschema
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.authz import keys
from app.authz.policy import AuthContext, audit, authorize
from app.database import async_session
from app.mcp.tools import BY_NAME, Caller, ToolError, coerce_ids, safe_ints, visible
from app.models import Account

log = logging.getLogger(__name__)

server: Server = Server("tg-myperson")
session_manager: StreamableHTTPSessionManager | None = None


def _caller() -> Caller:
    request = server.request_context.request
    caller = getattr(getattr(request, "state", None), "mcp_caller", None)
    if caller is None:
        raise ToolError("no caller")  # cannot happen behind the ASGI wrapper
    return caller


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(name=t.name, description=t.description, inputSchema=t.schema)
        for t in visible(_caller())
    ]


def _digest(args: dict) -> str:
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# Input validation is ours, not the SDK's (validate_input=False): an id that is
# not digits must reach the audit like any refused call (plan 2026-10-19 R1-3),
# and ids given as strings are made ints before authorize() and the REST call.
@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict) -> list[types.TextContent] | types.CallToolResult:
    caller = _caller()
    args = arguments or {}
    tool = BY_NAME.get(name)
    ctx = AuthContext(account_id=caller.account_id, alias=caller.alias, platform=caller.platform,
                      scope=caller.scope)
    if tool is None or tool not in visible(caller):
        # Reaching for another level (ro → write, account → contacts, owner → data)
        # is visible in the audit like a denied REST call (QA D-4).
        audit(ctx, tool.route if tool else name, status="denied", is_write=bool(tool and tool.is_write),
              error="tool not available for this key")
        return _text(f"Error: tool '{name}' is not available for this key")
    try:
        args = coerce_ids(args, tool.schema)
        jsonschema.validate(instance=args, schema=tool.schema)
    except (ToolError, jsonschema.ValidationError) as exc:
        error = str(exc) if isinstance(exc, ToolError) else f"Input validation error: {exc.message}"
        audit(ctx, tool.route, status="error", is_write=tool.is_write, error=error[:500])
        return types.CallToolResult(content=_text(error), isError=True)
    chat = args.get("chat_id", args.get("to_chat_id"))
    decision = await authorize(ctx, tool.route, chat_external_id=chat, is_write=tool.is_write)
    common = {"is_write": tool.is_write, "chat_id": chat if isinstance(chat, int) else None,
              "target_user_id": args.get("user_id"),
              "params_digest": _digest(args) if tool.is_write else None}
    if not decision.allowed:
        audit(ctx, tool.route, status="denied", error=f"HTTP {decision.status}", **common)
        body = json.loads(decision.response(ctx, tool.route).body)
        return _text(f"HTTP {decision.status}: {json.dumps(body, ensure_ascii=False)}")
    try:
        result = await tool.call(caller, args)
    except ToolError as exc:
        audit(ctx, tool.route, status="error", error=str(exc)[:500], **common)
        return _text(str(exc))
    except Exception as exc:  # noqa: BLE001 — the agent gets the error, the server stays up
        log.exception("mcp tool %s failed", name)
        audit(ctx, tool.route, status="error", error=f"{type(exc).__name__}: {exc}"[:500], **common)
        return _text(f"Error: {exc}")
    audit(ctx, tool.route, status="ok", **common)
    return _text(json.dumps(safe_ints(result), ensure_ascii=False, indent=2, default=str))


def _text(text: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=text)]


# ---------------------------------------------------------------------------
# ASGI: authentication in front of the session manager
# ---------------------------------------------------------------------------

def _base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


async def _caller_for(info: keys.KeyInfo, request: Request) -> Caller | None:
    if info.scope == "owner":
        return Caller("owner", None, "owner", "telegram", "ro", _base_url(request))
    async with async_session() as db:
        row = (await db.execute(
            select(Account.alias, Account.platform, Account.mode)
            .where(Account.id == info.account_id, Account.is_enabled)
        )).one_or_none()
    if row is None:
        return None
    return Caller("account", info.account_id, row.alias, row.platform, row.mode, _base_url(request))


async def mcp_asgi(scope, receive, send) -> None:
    if scope["type"] != "http":
        return
    request = Request(scope, receive)
    auth = request.headers.get("authorization", "")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    info = await keys.lookup(key)
    caller = await _caller_for(info, request) if info is not None else None
    if caller is None:
        await JSONResponse({"detail": "Authorization: Bearer <account or owner key> required"},
                           status_code=401)(scope, receive, send)
        return
    if session_manager is None:
        await JSONResponse({"detail": "MCP is not running"}, status_code=503)(scope, receive, send)
        return
    scope.setdefault("state", {})["mcp_caller"] = caller
    await session_manager.handle_request(scope, receive, send)


class McpEndpoint:
    """ASGI app object (Starlette Route treats a plain function as a request handler)."""

    async def __call__(self, scope, receive, send) -> None:
        await mcp_asgi(scope, receive, send)


@contextlib.asynccontextmanager
async def running():
    """The session manager runs once per instance: a fresh one per lifespan."""
    global session_manager
    session_manager = StreamableHTTPSessionManager(app=server, stateless=True, json_response=True)
    async with session_manager.run():
        try:
            yield session_manager
        finally:
            session_manager = None
