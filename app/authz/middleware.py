"""Authorization middleware stack.

Registration order in app/main.py (outermost = first to execute):
  X-API-Key (existing) → resolve_alias → tool_authz → audit_log → route

Middleware are registered as @app.middleware("http") decorators in reverse
order (last registered = outermost), so in main.py we register:
  1. audit_log
  2. tool_authz
  3. resolve_alias
and they execute as: resolve_alias → tool_authz → audit_log → route.
"""

import asyncio
import hashlib
import json
import logging
import secrets
import time
from typing import Any

from fastapi import Request
from starlette.responses import JSONResponse

from app.authz.tool_catalog import READ_ONLY_TOOLS, WRITE_TOOLS, WRITE_TG_TOOLS, tool_is_write

log = logging.getLogger(__name__)

# Paths that bypass alias resolution entirely
_SKIP_PATHS = {
    "/api/v1/healthz",
    "/api/v1/readyz",
    "/docs",
    "/openapi.json",
    "/redoc",
}

# Admin path prefix — authenticated via X-Admin-Key, not X-API-Key + alias
_ADMIN_PREFIX = "/api/v1/accounts"

# Paths that must be excluded from audit_log and tool_authz warnings.
# Health/readiness probes are infra-level — not user-facing tools.
# Docs/schema paths never start with /api/ so they're already excluded
# by the startswith("/api/") guard, but listed here for clarity.
_HEALTH_PATHS = {
    "/api/v1/healthz",
    "/api/v1/readyz",
}

# Simple in-memory alias cache: {alias: (account_id, expiry_time)}
_alias_cache: dict[str, tuple[int, float]] = {}
_CACHE_TTL = 10.0  # seconds — keep short; see single-worker note in README


# ---------------------------------------------------------------------------
# Middleware 1: resolve_alias
# ---------------------------------------------------------------------------

async def admin_auth_middleware(request: Request, call_next):
    """Guard /api/v1/accounts/* with X-Admin-Key.

    - If admin_api_key is empty in settings → 503 (misconfigured).
    - If header missing or wrong → 401.
    - Admin paths bypass resolve_alias / tool_authz entirely.
    - audit_log is still written (account_id=None, alias=None, tool=<endpoint name>).
    """
    from app.config import settings as cfg

    if not request.url.path.startswith(_ADMIN_PREFIX):
        return await call_next(request)

    # Mark as admin path so downstream middleware skips alias/tool checks
    request.state.is_admin_path = True
    request.state.session_alias = None
    request.state.account_id = None
    request.state.tool_is_write = True  # admin ops are "write" for audit purposes

    if not cfg.admin_api_key:
        return JSONResponse(
            status_code=503,
            content={"error": "admin API key not configured on server"},
        )

    provided = request.headers.get("x-admin-key")
    if not provided or not secrets.compare_digest(provided.encode(), cfg.admin_api_key.encode()):
        return JSONResponse(
            status_code=401,
            content={"error": "admin key required"},
        )

    return await call_next(request)


async def resolve_alias_middleware(request: Request, call_next):
    """Resolve X-Session-Alias header (or ?session= query) to an account.

    - Sets request.state.session_alias
    - Sets request.state.account_id
    - Returns 404 if alias not found or disabled
    - Default alias = 'work' (backwards compat)
    """
    # Skip admin paths — already handled by admin_auth_middleware
    if getattr(request.state, "is_admin_path", False):
        return await call_next(request)

    if request.url.path in _SKIP_PATHS:
        request.state.session_alias = "work"
        request.state.account_id = None
        return await call_next(request)

    alias = (
        request.headers.get("x-session-alias")
        or request.query_params.get("session")
        or "work"
    )

    # Check cache
    now = time.monotonic()
    cached = _alias_cache.get(alias)
    if cached and cached[1] > now:
        account_id = cached[0]
        request.state.session_alias = alias
        request.state.account_id = account_id
        return await call_next(request)

    # Cache miss — query DB
    try:
        account_id = await _resolve_alias_from_db(alias)
    except Exception as exc:
        log.error("resolve_alias: DB error for alias '%s': %s", alias, exc)
        account_id = None

    if account_id is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Session alias '{alias}' not registered or disabled"},
        )

    _alias_cache[alias] = (account_id, now + _CACHE_TTL)
    request.state.session_alias = alias
    request.state.account_id = account_id
    return await call_next(request)


async def _resolve_alias_from_db(alias: str) -> int | None:
    from sqlalchemy import select
    from app.database import async_session
    from app.models import Account

    async with async_session() as db:
        result = await db.execute(
            select(Account.id).where(
                Account.alias == alias,
                Account.is_enabled == True,  # noqa: E712
            )
        )
        row = result.scalar_one_or_none()
    return row


def invalidate_alias_cache(alias: str | None = None) -> None:
    """Invalidate cache entry for alias (or all entries if alias is None)."""
    if alias is None:
        _alias_cache.clear()
    else:
        _alias_cache.pop(alias, None)


# ---------------------------------------------------------------------------
# Middleware 2: tool_authz
# ---------------------------------------------------------------------------

def _resolve_route_name(request: Request) -> str | None:
    """Resolve the FastAPI route name for a request BEFORE call_next.

    @app.middleware("http") is called before routing, so scope["route"] is
    not yet set. We manually match the request against the app's router to
    get the route name.
    """
    from starlette.routing import Match

    app = request.app
    routes = getattr(app, "routes", [])
    # Also check subrouters (api_router mounted under /api/v1)
    for route in routes:
        # Check direct routes
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            return getattr(route, "name", None)
        # Check mounted routes (APIRouter)
        if hasattr(route, "routes"):
            for sub_route in route.routes:
                match, _ = sub_route.matches(request.scope)
                if match == Match.FULL:
                    return getattr(sub_route, "name", None)
                # One more level deep (nested mounts)
                if hasattr(sub_route, "routes"):
                    for sub_sub_route in sub_route.routes:
                        match, _ = sub_sub_route.matches(request.scope)
                        if match == Match.FULL:
                            return getattr(sub_sub_route, "name", None)
    return None


async def tool_authz_middleware(request: Request, call_next):
    """Enforce read-only mode: block write tools on ro accounts.

    - Gets tool name by matching route against app.routes before call_next
    - Reads accounts.mode for current account_id
    - Returns 403 if write tool called on ro account
    - Also checks account_tool_policy (deny > allow > mode default)
    - Admin paths are skipped entirely (handled by admin_auth_middleware)
    """
    # Skip admin paths — but still resolve and store route name for audit_log.
    # tool_is_write is already set to True by admin_auth_middleware; leave it.
    if getattr(request.state, "is_admin_path", False):
        request.state.tool_name = _resolve_route_name(request) or "unknown"
        return await call_next(request)
    # Resolve tool name by matching route manually (scope["route"] not yet set)
    tool_name: str | None = _resolve_route_name(request)

    # Store for audit; default sentinel values
    request.state.tool_name = tool_name or "unknown"
    request.state.tool_is_write = False

    if tool_name is None or tool_name not in (READ_ONLY_TOOLS | WRITE_TOOLS):
        # Unknown tool — log warning, pass through (don't break new endpoints).
        # Health/readiness probes are intentionally not in the tool catalog;
        # suppress the warning for them to avoid log pollution.
        if (
            tool_name is not None
            and request.url.path.startswith("/api/")
            and request.url.path not in _HEALTH_PATHS
        ):
            log.warning("tool_authz: unknown tool '%s' at %s", tool_name, request.url.path)
        return await call_next(request)

    try:
        is_write = tool_is_write(tool_name)
    except KeyError:
        # Shouldn't happen since we checked above, but be safe
        return await call_next(request)

    request.state.tool_is_write = is_write

    if not is_write:
        # Read — always allowed regardless of mode
        return await call_next(request)

    account_id = getattr(request.state, "account_id", None)
    alias = getattr(request.state, "session_alias", "work")

    if account_id is None:
        # No account resolved (skip paths) — pass through
        return await call_next(request)

    # Check account_tool_policy first (explicit deny/allow overrides mode)
    policy_effect = await _get_tool_policy(account_id, tool_name)
    if policy_effect == "deny":
        return _forbidden(alias, tool_name, "denied by account policy")

    if policy_effect == "allow":
        return await call_next(request)

    # No explicit policy — fall back to account mode
    mode = await _get_account_mode(account_id)
    if mode == "ro":
        return _forbidden(alias, tool_name, "read-only account")

    return await call_next(request)


def _forbidden(alias: str, tool_name: str, reason: str) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={
            "error": f"tool '{tool_name}' not allowed on read-only account '{alias}'",
            "tool": tool_name,
            "alias": alias,
            "mode": "ro",
            "reason": reason,
        },
    )


# Cache: {account_id: (mode, expiry)}
_mode_cache: dict[int, tuple[str, float]] = {}


async def _get_account_mode(account_id: int) -> str:
    now = time.monotonic()
    cached = _mode_cache.get(account_id)
    if cached and cached[1] > now:
        return cached[0]

    from sqlalchemy import select
    from app.database import async_session
    from app.models import Account

    async with async_session() as db:
        result = await db.execute(select(Account.mode).where(Account.id == account_id))
        mode = result.scalar_one_or_none() or "rw"

    _mode_cache[account_id] = (mode, now + _CACHE_TTL)
    return mode


async def _get_tool_policy(account_id: int, tool_name: str) -> str | None:
    """Return 'allow', 'deny', or None (no policy)."""
    from sqlalchemy import select
    from app.database import async_session
    from app.models import AccountToolPolicy

    async with async_session() as db:
        result = await db.execute(
            select(AccountToolPolicy.effect).where(
                AccountToolPolicy.account_id == account_id,
                AccountToolPolicy.tool_name == tool_name,
            )
        )
        return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Middleware 3: audit_log
# ---------------------------------------------------------------------------

async def audit_log_middleware(request: Request, call_next):
    """Write an audit log entry for every API call.

    Uses asyncio.create_task to write asynchronously — does not block response.
    """
    response = await call_next(request)

    # Skip non-API paths (docs, redoc, openapi.json, etc.)
    if not request.url.path.startswith("/api/"):
        return response

    # Skip health/readiness probes — infra-level, not user actions
    if request.url.path in _HEALTH_PATHS:
        return response

    tool_name = getattr(request.state, "tool_name", "unknown")
    alias = getattr(request.state, "session_alias", None)
    account_id = getattr(request.state, "account_id", None)
    is_write = getattr(request.state, "tool_is_write", False)

    # Determine status
    status_code = response.status_code
    if status_code == 403:
        status = "denied"
    elif 200 <= status_code < 300:
        status = "ok"
    else:
        status = "error"

    # Extract chat_id from path params if present
    chat_id = _extract_int_param(request, "chat_id")
    target_user_id = _extract_int_param(request, "user_id")

    # params_digest — only for write operations, from body
    params_digest: str | None = None
    if is_write and status != "denied":
        try:
            body = await _try_read_body(request)
            if body:
                params_digest = _digest(body)
        except Exception:
            pass

    # Error text — first 500 chars from response body on non-ok
    error_text: str | None = None
    if status != "ok":
        try:
            # We can't read streaming response body here; store status code hint
            error_text = f"HTTP {status_code}"
        except Exception:
            pass

    asyncio.create_task(
        _write_audit_log(
            alias=alias,
            account_id=account_id,
            tool=tool_name,
            is_write=is_write,
            chat_id=chat_id,
            target_user_id=target_user_id,
            params_digest=params_digest,
            status=status,
            error=error_text,
        )
    )

    return response


def _extract_int_param(request: Request, param: str) -> int | None:
    """Try to get int param from path params or query string."""
    val = request.path_params.get(param) or request.query_params.get(param)
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


async def _try_read_body(request: Request) -> bytes | None:
    """Attempt to read body without consuming it (may not work on streaming)."""
    # FastAPI caches body after first read via receive()
    try:
        body = await request.body()
        return body if body else None
    except Exception:
        return None


def _digest(body: bytes) -> str:
    """SHA256 hex digest of body, first 16 chars."""
    try:
        canonical = json.dumps(json.loads(body), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]
    except Exception:
        return hashlib.sha256(body).hexdigest()[:16]


async def _write_audit_log(
    *,
    alias: str | None,
    account_id: int | None,
    tool: str,
    is_write: bool,
    chat_id: int | None,
    target_user_id: int | None,
    params_digest: str | None,
    status: str,
    error: str | None,
) -> None:
    """Write audit log entry to DB. Called as background task."""
    from datetime import datetime, timezone
    from app.database import async_session
    from app.models import AuditLog

    try:
        async with async_session() as db:
            entry = AuditLog(
                ts=datetime.now(timezone.utc),
                account_id=account_id,
                alias=alias,
                tool=tool,
                is_write=is_write,
                chat_id=chat_id,
                target_user_id=target_user_id,
                params_digest=params_digest,
                status=status,
                error=error,
            )
            db.add(entry)
            await db.commit()
    except Exception as exc:
        log.warning("audit_log: failed to write log entry: %s", exc)
