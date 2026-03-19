import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text
from starlette.responses import JSONResponse

from app.api.router import api_router
from app.config import settings
from app.telegram.client import tg_bridge
from app.telegram.handlers import register_handlers

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start Telegram client
    await tg_bridge.start()

    # Register event handlers if authorized
    if tg_bridge.client and await tg_bridge.client.is_user_authorized():
        register_handlers(tg_bridge.client)

    yield

    # Shutdown
    await tg_bridge.stop()


app = FastAPI(
    title="TG-MyPerson",
    description="Telegram Personal Account Bridge: MTProto → PostgreSQL → REST API",
    version="0.1.0",
    lifespan=lifespan,
    root_path_in_servers=False,
)

PUBLIC_PATHS = {"/api/v1/healthz", "/api/v1/readyz", "/docs", "/openapi.json", "/redoc"}


@app.middleware("http")
async def auth_and_https_middleware(request, call_next):
    # Trust X-Forwarded-Proto from Traefik
    if request.headers.get("x-forwarded-proto") == "https":
        request.scope["scheme"] = "https"

    # API key check
    if settings.api_key and request.url.path not in PUBLIC_PATHS:
        api_key = request.headers.get("x-api-key") or request.query_params.get("api_key")
        if api_key != settings.api_key:
            return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})

    return await call_next(request)


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

    tg_connected = tg_bridge.is_connected
    tg_authorized = False
    if tg_bridge.client:
        try:
            tg_authorized = await tg_bridge.client.is_user_authorized()
        except Exception:
            pass

    status = "ready" if (db_ok and tg_authorized) else "not_ready"
    return {
        "status": status,
        "database": db_ok,
        "telegram_connected": tg_connected,
        "telegram_authorized": tg_authorized,
    }
