import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text
from starlette.responses import JSONResponse

from app.api.router import api_router
from app.authz.middleware import (
    admin_auth_middleware,
    audit_log_middleware,
    resolve_alias_middleware,
    tool_authz_middleware,
)
from app.config import settings
from app.telegram.pool import pool
from app.telegram.handlers import register_handlers

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start all enabled Telegram sessions (supervisor pattern)
    await pool.start_all()

    # Register event handlers for the 'work' session if authorized
    work_session = pool._pool.get("work")
    if work_session and work_session.client and await work_session.client.is_user_authorized():
        register_handlers(work_session.client)

    yield

    # Shutdown all sessions
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
#   2. resolve_alias
#   3. auth_and_https
#   4. audit_log (outermost — sees all responses including 403)
# ---------------------------------------------------------------------------


@app.middleware("http")
async def tool_authz_mw(request, call_next):
    return await tool_authz_middleware(request, call_next)


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

    # API key check — skip for public paths and admin paths (admin uses X-Admin-Key)
    if settings.api_key and request.url.path not in PUBLIC_PATHS:
        if not request.url.path.startswith("/api/v1/accounts"):
            api_key = request.headers.get("x-api-key") or request.query_params.get("api_key")
            if not api_key or not secrets.compare_digest(api_key.encode(), settings.api_key.encode()):
                return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})

    return await call_next(request)


@app.middleware("http")
async def audit_log_mw(request, call_next):
    # Outermost: registered last, wraps all other middleware.
    # Sees ALL responses including 403 from tool_authz and 401 from api_key.
    return await audit_log_middleware(request, call_next)


app.include_router(api_router, prefix=settings.api_prefix)


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
