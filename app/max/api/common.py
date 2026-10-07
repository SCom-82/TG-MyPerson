"""Helpers shared by MAX endpoints."""

from fastapi import Request
from fastapi.responses import JSONResponse

from app.max.pool import MaxAliasNotFound, get_max_pool
from app.max.session import MaxSession


def alias_of(request: Request) -> str:
    return getattr(request.state, "session_alias", "")


async def session_or_error(request: Request) -> MaxSession | JSONResponse:
    alias = alias_of(request)
    pool = get_max_pool()
    if pool is None:
        return JSONResponse(
            status_code=503,
            content={"detail": f"Session '{alias}' not available", "state": "stopped", "reason": "MAX is disabled"},
        )
    try:
        return await pool.get(alias)
    except MaxAliasNotFound:
        return JSONResponse(status_code=404, content={"error": f"Session alias '{alias}' not registered or disabled"})


async def authorized_or_error(request: Request) -> MaxSession | JSONResponse:
    """Session whose client is connected and authorized, else 503 (API spec §5)."""
    session = await session_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    if session.client is None:
        return JSONResponse(
            status_code=503,
            content={"detail": f"Session '{session.alias}' not available", "state": session.state},
        )
    return session
