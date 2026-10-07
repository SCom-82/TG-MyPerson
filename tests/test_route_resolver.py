"""test_route_resolver.py — tool-name resolution independent of FastAPI internals.

FastAPI >= 0.137 wraps included routers in a private _IncludedRouter; the old
walk over app.routes found no names, so tool_authz let every write through.
The resolver now uses a flat table built from our routers (app/authz/route_table.py).

Run on both sides of the 0.137 boundary (pyproject pins <0.137; the second run
uses a venv with the production versions installed explicitly):
    pytest tests/test_route_resolver.py tests/test_authz_middleware.py
    <venv with fastapi==0.141.1 starlette==1.6.0>/bin/python -m pytest <same files>

Guards:
  - every route served under /api/v1 resolves to its own name (cross-checked
    against the app's OpenAPI operationIds, i.e. what FastAPI actually serves);
  - every resolved name is in the tool catalog, except the admin API;
  - every APIRouter defined in app/api/* is listed in API_ROUTERS.
"""

import importlib
import pkgutil
import re

import pytest
from fastapi import APIRouter
from starlette.requests import Request

import app.api
from app.api.router import API_ROUTERS
from app.authz.route_table import RouteTable
from app.authz.tool_catalog import ALL_TOOLS

_ADMIN_PREFIX = "/api/v1/accounts"
_NON_TOOL_PATHS = {"/api/v1/healthz", "/api/v1/readyz"}  # infra probes, not tools


@pytest.fixture
def fastapi_app():
    import app.main as main_module

    return main_module.app


def _sample_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


@pytest.mark.parametrize(
    "method,path,expected",
    [
        ("GET", "/api/v1/chats", "list_chats"),
        ("POST", "/api/v1/chats/join", "join_chat_endpoint"),
        ("GET", "/api/v1/chats/-1001234", "get_chat_detail"),
        ("PATCH", "/api/v1/chats/-1001234", "update_chat_settings"),
        ("GET", "/api/v1/messages", "list_messages"),
        ("POST", "/api/v1/messages", "send_message"),
        ("GET", "/api/v1/messages/scheduled", "list_scheduled"),
        ("GET", "/api/v1/messages/-100/42", "get_single_message"),
        ("GET", "/api/v1/messages/-100/42/media", "download_media"),
        ("DELETE", "/api/v1/messages/-100/42", "delete_message"),
        ("POST", "/api/v1/users/resolve_by_id", "resolve_by_id"),
        ("POST", "/api/v1/users/7/block", "block_user"),
        ("GET", "/api/v1/auth/status", "auth_status"),
        ("POST", "/api/v1/auth/login", "auth_login"),
        ("GET", "/api/v1/sync/status", "sync_status"),
        ("POST", "/api/v1/sync/backfill", "trigger_backfill"),
        ("GET", "/api/v1/stream/messages", "stream_messages"),
        ("GET", "/api/v1/accounts", "admin_list_accounts"),
        ("PATCH", "/api/v1/accounts/3", "admin_patch_account"),
        ("HEAD", "/api/v1/chats", "list_chats"),
    ],
)
def test_resolves_known_tools(fastapi_app, method, path, expected):
    assert fastapi_app.state.route_table.resolve(method, path) == expected


@pytest.mark.parametrize(
    "method,path",
    [
        ("PUT", "/api/v1/chats"),            # path known, method not
        ("GET", "/api/v1/no-such-endpoint"),
        ("GET", "/api/v1/chats/1/2/3/4"),
        ("GET", "/api/v1/healthz"),           # app-level probe, not a tool
        ("GET", "/docs"),
    ],
)
def test_unknown_resolves_to_none(fastapi_app, method, path):
    assert fastapi_app.state.route_table.resolve(method, path) is None


def test_middleware_uses_table(fastapi_app):
    """_resolve_route_name goes through fastapi_app.state.route_table (no app.routes walk)."""
    from app.authz.middleware import _resolve_route_name

    scope = {"type": "http", "method": "POST", "path": "/api/v1/messages", "headers": [], "app": fastapi_app}
    assert _resolve_route_name(Request(scope)) == "send_message"


def test_missing_table_fails_loud(fastapi_app, monkeypatch, caplog):
    from app.authz.middleware import _resolve_route_name

    monkeypatch.delattr(fastapi_app.state, "route_table")
    scope = {"type": "http", "method": "POST", "path": "/api/v1/messages", "headers": [], "app": fastapi_app}
    assert _resolve_route_name(Request(scope)) is None
    assert "route_table is not set" in caplog.text


def test_first_match_wins_in_router_order():
    async def _ep():  # pragma: no cover - never called
        pass

    router = APIRouter(prefix="/x")
    router.add_api_route("/fixed", _ep, methods=["GET"], name="fixed")
    router.add_api_route("/{item}", _ep, methods=["GET"], name="param")
    table = RouteTable([("/api/v1", router.routes)])
    assert table.resolve("GET", "/api/v1/x/fixed") == "fixed"
    assert table.resolve("GET", "/api/v1/x/other") == "param"


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def test_every_served_api_route_resolves_to_its_name(fastapi_app):
    """Cross-check against OpenAPI: what FastAPI really serves must be in the table.

    FastAPI's default operationId is "<route name>_<path>_<method>", so the
    resolved name must be its prefix.
    """
    checked = 0
    for path, operations in fastapi_app.openapi()["paths"].items():
        if not path.startswith("/api/v1/") or path in _NON_TOOL_PATHS:
            continue
        for method, op in operations.items():
            name = fastapi_app.state.route_table.resolve(method.upper(), _sample_path(path))
            assert name is not None, f"{method.upper()} {path} is served but not in the route table"
            assert op["operationId"].startswith(name), (method, path, name, op["operationId"])
            checked += 1
    assert checked >= 50  # sanity: the whole API, not an empty schema


def test_every_route_name_is_a_cataloged_tool(fastapi_app):
    """A route outside the catalog passes authz with a warning — catch it in CI instead."""
    for entry in fastapi_app.state.route_table.entries:
        if entry.path.startswith(_ADMIN_PREFIX):
            continue  # admin API: X-Admin-Key, no tool authz
        if entry.name == "max_unsupported":
            continue  # internal MAX catch-all; the real tool comes from forced_tool_name
        assert entry.name in ALL_TOOLS, (
            f"route {sorted(entry.methods)} {entry.path} ('{entry.name}') is not in "
            "app/authz/tool_catalog.py"
        )


def test_every_api_router_is_registered():
    """A router defined in app/api/* but missing from API_ROUTERS would be invisible to authz."""
    defined = []
    for info in pkgutil.iter_modules(app.api.__path__):
        module = importlib.import_module(f"app.api.{info.name}")
        candidate = getattr(module, "router", None)
        if isinstance(candidate, APIRouter):
            defined.append(info.name)
            assert any(candidate is r for r in API_ROUTERS), f"app/api/{info.name}.py router not in API_ROUTERS"
    assert len(defined) == len(API_ROUTERS)


# ---------------------------------------------------------------------------
# Startup check: the service refuses to start with a broken table
# ---------------------------------------------------------------------------

def test_verify_route_table_accepts_real_table(fastapi_app):
    from app.authz.route_table import verify_route_table

    verify_route_table(fastapi_app.state.route_table)  # must not raise


def test_verify_route_table_rejects_empty_table():
    from app.authz.route_table import verify_route_table

    with pytest.raises(RuntimeError, match="refusing to start"):
        verify_route_table(RouteTable([]))


def test_app_import_fails_when_key_routes_do_not_resolve(monkeypatch):
    """Simulates a FastAPI change that hides route paths: app.main must not import."""
    import app.authz.route_table as rt
    import app.main as main_module

    monkeypatch.setattr(rt.RouteTable, "resolve", lambda self, method, path: None)
    with pytest.raises(RuntimeError, match="refusing to start"):
        importlib.reload(main_module)
    monkeypatch.undo()
    importlib.reload(main_module)  # restore a healthy module for later tests


def test_every_max_api_router_is_registered():
    """Same guard for the internal MAX router: every APIRouter in app/max/api/*
    except the aggregate max_api_router must be a leaf listed in MAX_API_ROUTERS."""
    import app.max.api
    from app.max.api.router import MAX_API_ROUTERS, max_api_router

    found = 0
    for info in pkgutil.iter_modules(app.max.api.__path__):
        module = importlib.import_module(f"app.max.api.{info.name}")
        for attr, candidate in vars(module).items():
            if not isinstance(candidate, APIRouter) or candidate is max_api_router:
                continue
            found += 1
            assert any(candidate is r for r in MAX_API_ROUTERS), (
                f"app/max/api/{info.name}.py: {attr} is not in MAX_API_ROUTERS"
            )
    assert found >= len(MAX_API_ROUTERS)
