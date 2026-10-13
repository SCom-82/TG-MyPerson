"""Call a REST route's own handler in-process — no network, no middleware.

The MCP tools run exactly the code of the REST endpoints (parameter parsing,
validation, response models, errors), for the platform of the caller's
account: the Telegram routers or the internal MAX ones. Authorization and
audit are done once by the MCP layer (app/authz/policy.py), so the HTTP
middleware stack is not involved.
"""

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from starlette.responses import Response
from starlette.routing import Match

from app.authz.middleware import MAX_INTERNAL_PREFIX

API_PREFIX = "/api/v1"


@dataclass
class Result:
    status: int
    body: Any
    response: Response

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _routes(platform: str) -> list[tuple[str, Any]]:
    if platform == "max":
        from app.max.api.router import MAX_API_ROUTERS

        return [(MAX_INTERNAL_PREFIX, r) for router in MAX_API_ROUTERS for r in router.routes]
    from app.api.router import API_ROUTERS

    return [(API_PREFIX, r) for router in API_ROUTERS for r in router.routes]


def _find(platform: str, method: str, path: str) -> tuple[Any, dict] | None:
    prefix = MAX_INTERNAL_PREFIX if platform == "max" else API_PREFIX
    full = prefix + path
    for route_prefix, route in _routes(platform):
        if not hasattr(route, "get_route_handler"):
            continue
        scope = {"type": "http", "path": full[len(route_prefix):], "method": method, "root_path": ""}
        match, child = route.matches(scope)
        if match == Match.FULL:
            return route, child.get("path_params", {})
    return None


def build_scope(
    *, method: str, path: str, state: dict, query: dict | None = None,
    body: bytes = b"", content_type: str | None = None, headers: dict | None = None,
) -> tuple[dict, Any]:
    from app.main import app

    hdrs = [(k.lower().encode(), str(v).encode()) for k, v in (headers or {}).items()]
    if content_type:
        hdrs.append((b"content-type", content_type.encode()))
    hdrs.append((b"content-length", str(len(body)).encode()))
    query_string = urlencode({k: _q(v) for k, v in (query or {}).items() if v is not None}, doseq=True)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query_string.encode(),
        "headers": hdrs,
        "client": ("mcp", 0),
        "server": ("mcp", 0),
        "app": app,
        "state": dict(state),
        # What the app's outer middleware puts into the scope of every request
        # (HTTPException → JSON, file uploads closed with the request).
        "starlette.exception_handlers": _exception_handlers(app),
    }
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return scope, receive


def _exception_handlers(app) -> tuple[dict, dict]:
    from starlette.exceptions import HTTPException

    handlers = {k: v for k, v in app.exception_handlers.items() if isinstance(k, type)}
    status = {k: v for k, v in app.exception_handlers.items() if isinstance(k, int)}
    if HTTPException not in handlers:
        from fastapi.exception_handlers import http_exception_handler

        handlers[HTTPException] = http_exception_handler
    return handlers, status


def _q(v: Any) -> Any:
    if isinstance(v, bool):
        return "true" if v else "false"
    return v


def _prepare(platform: str, method: str, path: str, *, state: dict, query: dict | None = None,
             json_body: Any = None, multipart: list | None = None, headers: dict | None = None):
    found = _find(platform, method, path)
    if found is None:
        return None
    route, path_params = found
    body, content_type = b"", None
    if json_body is not None:
        body, content_type = json.dumps(json_body, default=str).encode(), "application/json"
    elif multipart is not None:
        body, content_type = encode_multipart(multipart)
    prefix = MAX_INTERNAL_PREFIX if platform == "max" else API_PREFIX
    scope, receive = build_scope(method=method, path=prefix + path, state=state, query=query,
                                 body=body, content_type=content_type, headers=headers)
    scope["path_params"] = path_params
    scope["route"] = route
    return route, scope, receive


async def _handle(route: Any, scope: dict, receive, send) -> None:
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        scope["fastapi_middleware_astack"] = stack
        await route.handle(scope, receive, send)


async def call(
    platform: str, method: str, path: str, *, state: dict,
    query: dict | None = None, json_body: Any = None, multipart: list | None = None,
    headers: dict | None = None,
) -> Result:
    """Run the route for `path` (without /api/v1) of the caller's platform; the whole body."""
    prepared = _prepare(platform, method, path, state=state, query=query, json_body=json_body,
                        multipart=multipart, headers=headers)
    if prepared is None:
        if platform == "max":
            return Result(501, {"error": f"{method} {path} is not supported for platform max"}, Response(status_code=501))
        return Result(404, {"detail": "Not Found"}, Response(status_code=404))
    route, scope, receive = prepared
    status = 500
    chunks: list[bytes] = []
    resp_headers: list = []

    async def send(message):
        nonlocal status, resp_headers
        if message["type"] == "http.response.start":
            status = message["status"]
            resp_headers = message.get("headers", [])
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await _handle(route, scope, receive, send)
    raw = b"".join(chunks)
    response = Response(raw, status_code=status)
    response.raw_headers = resp_headers
    return Result(status, _decode(raw), response)


class Forward(Response):
    """A response that streams the route's own response straight to the client."""

    def __init__(self, route: Any, scope: dict, receive) -> None:
        super().__init__()
        self._route, self._scope, self._receive = route, scope, receive

    async def __call__(self, scope, receive, send) -> None:
        await _handle(self._route, self._scope, self._receive, send)


def forward(platform: str, method: str, path: str, *, state: dict, query: dict | None = None,
            headers: dict | None = None) -> Response:
    prepared = _prepare(platform, method, path, state=state, query=query, headers=headers)
    if prepared is None:
        return Response(status_code=404)
    return Forward(*prepared)


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")


def encode_multipart(parts: list[tuple[str, Any]]) -> tuple[bytes, str]:
    """[(name, value) | (name, (filename, bytes, content_type))] → body, content type."""
    import secrets

    boundary = "mcp" + secrets.token_hex(12)
    out = bytearray()
    for name, value in parts:
        out += f"--{boundary}\r\n".encode()
        if isinstance(value, tuple):
            filename, data, ctype = value
            out += (f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                    f"Content-Type: {ctype or 'application/octet-stream'}\r\n\r\n").encode()
            out += data
        else:
            out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            out += str(_q(value)).encode()
        out += b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"
