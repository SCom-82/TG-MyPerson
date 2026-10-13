import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text
from starlette.responses import JSONResponse

from app.api.router import API_ROUTERS, api_router
from app.authz.middleware import (
    MAX_INTERNAL_PREFIX,
    authenticate,
    admin_auth_middleware,
    audit_log_middleware,
    platform_dispatch_middleware,
    resolve_alias_middleware,
    tool_authz_middleware,
)
from app.authz.route_table import RouteTable, verify_route_table
from app.config import settings
from app.max import pool as max_pool_module
from app.max.config import max_settings
from app.max.maintenance import raw_events_loop
from app.max.api import MAX_API_ROUTERS, max_api_router
from app.services.contacts_sync import contacts_sync_loop
from app.services.partition_maintenance import ensure_partitions, partition_loop
from app.telegram.pool import attach_all_handlers, pool

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log = logging.getLogger(__name__)

    # Start all enabled Telegram sessions (supervisor pattern)
    await pool.start_all()

    # Register event handlers for ALL authorized sessions (PR #4: was 'work'-only);
    # every account on its own: one failure does not stop startup.
    await attach_all_handlers(pool)

    # Self-sufficient audit_logs partition maintenance (ADR 2026-06-16):
    # ensure on startup (Layer 1), then a daily background tick (Layer 2).
    # No external cron/sidecar — rotation lives and dies with this process.
    await ensure_partitions()
    partition_task = asyncio.create_task(partition_loop())
    # Address books of TG accounts into users (STORAGE_MODE=new, ТЗ §5.1).
    contacts_task = asyncio.create_task(contacts_sync_loop()) if settings.storage_mode == "new" else None

    # MAX (ADR §2.C/§2.I): only with MAX_ENABLED=true; started in the background
    # so a hanging MAX handshake never delays startup or touches the TG pool.
    max_start_task = max_retention_task = None
    if max_settings.enabled:
        max_pool_module.max_pool = max_pool_module.MaxPool(max_settings)
        max_start_task = asyncio.create_task(max_pool_module.max_pool.start_all())
        max_retention_task = asyncio.create_task(raw_events_loop(max_settings))

    yield

    # Stop the partition loop, then shut down all sessions
    partition_task.cancel()
    for task in (max_start_task, max_retention_task, contacts_task):
        if task is not None:
            task.cancel()
    if max_pool_module.max_pool is not None:
        await max_pool_module.max_pool.stop_all()
        max_pool_module.max_pool = None
    await pool.stop_all()


app = FastAPI(
    title="TG-MyPerson",
    description="Telegram Personal Account Bridge: MTProto → PostgreSQL → REST API",
    version="0.2.0",
    lifespan=lifespan,
    root_path_in_servers=False,
)

PUBLIC_PATHS = {"/api/v1/healthz", "/api/v1/readyz", "/docs", "/openapi.json", "/redoc"}

# ---------------------------------------------------------------------------
# Middleware stack.
# In Starlette, LAST registered = OUTERMOST (first to process request,
# last to process response). We want execution order:
#   X-API-Key → resolve_alias → tool_authz → route → audit_log (response)
#
# But audit_log must wrap everything to catch 403 from tool_authz.
# Solution: register audit_log LAST so it's OUTERMOST:
#   registered: auth_and_https (1st) → resolve_alias (2nd) → tool_authz (3rd) → audit_log (4th=outermost)
#   execution:  audit_log → tool_authz → resolve_alias → auth_and_https → route
# Wait — that breaks resolve_alias needing to run before tool_authz.
#
# Correct approach: register in this order so execution is:
#   audit_log(outer) → X-API-Key → resolve_alias → tool_authz → route
# Registration order (last=outermost):
#   1. tool_authz (innermost)
#   2. platform_dispatch (MAX ADR §2.B: needs the alias' platform, must run
#      before tool_authz so it sees the real tool name of a rewritten path)
#   3. resolve_alias
#   4. admin_auth
#   5. auth_and_https
#   6. audit_log (outermost — sees all responses including 403)
# Execution:
#   audit_log → auth_and_https → admin_auth → resolve_alias → platform_dispatch → tool_authz → route
# ---------------------------------------------------------------------------


@app.middleware("http")
async def tool_authz_mw(request, call_next):
    return await tool_authz_middleware(request, call_next)


@app.middleware("http")
async def platform_dispatch_mw(request, call_next):
    return await platform_dispatch_middleware(request, call_next)


@app.middleware("http")
async def resolve_alias_mw(request, call_next):
    return await resolve_alias_middleware(request, call_next)


@app.middleware("http")
async def admin_auth_mw(request, call_next):
    return await admin_auth_middleware(request, call_next)


@app.middleware("http")
async def auth_and_https_middleware(request, call_next):
    # Trust X-Forwarded-Proto from Traefik
    if request.headers.get("x-forwarded-proto") == "https":
        request.scope["scheme"] = "https"

    # API key check (per-account / owner / shared key) — public and admin paths skip it
    denied = await authenticate(request)
    if denied is not None:
        return denied

    return await call_next(request)


@app.middleware("http")
async def audit_log_mw(request, call_next):
    # Outermost: registered last, wraps all other middleware.
    # Sees ALL responses including 403 from tool_authz and 401 from api_key.
    return await audit_log_middleware(request, call_next)


app.include_router(api_router, prefix=settings.api_prefix)
# Internal MAX router: same paths and route names as the TG one, reachable only
# via platform_dispatch rewrite. Hidden from OpenAPI (duplicate operation names).
app.include_router(max_api_router, prefix=MAX_INTERNAL_PREFIX, include_in_schema=False)

# Tool-name lookup for authz/audit middleware: our routers + their mount prefix.
app.state.route_table = RouteTable(
    [(settings.api_prefix, router.routes) for router in API_ROUTERS]
    + [(MAX_INTERNAL_PREFIX, router.routes) for router in MAX_API_ROUTERS]
)
# Fail closed: without resolvable tool names tool_authz lets every write through.
verify_route_table(app.state.route_table)


@app.get("/api/v1/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/api/v1/readyz")
async def readyz():
    from app.database import engine

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    # Check 'work' session status
    work_session = pool._pool.get("work")
    tg_connected = work_session.is_connected if work_session else False
    tg_authorized = False
    if work_session and work_session.client:
        try:
            tg_authorized = await work_session.client.is_user_authorized()
        except Exception:
            pass

    status = "ready" if (db_ok and tg_authorized) else "not_ready"
    return {
        "status": status,
        "database": db_ok,
        "telegram_connected": tg_connected,
        "telegram_authorized": tg_authorized,
    }
