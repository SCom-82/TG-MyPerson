"""Helpers shared by MAX endpoints."""

import logging
from collections.abc import Awaitable
from typing import Any, TypeVar

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.max.pool import MaxAliasNotFound, get_max_pool
from app.max.session import MaxSession

log = logging.getLogger(__name__)
T = TypeVar("T")


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


async def call_max(call: Awaitable[T]) -> T:
    """A PyMax write; its failure → 502 with the error (the write slot is given back)."""
    try:
        return await call
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — PyMax raises ApiError, ValueError, timeouts, ...
        log.warning("max write failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")


def param_not_supported(request: Request, tool: str, parameter: str) -> JSONResponse:
    """501 for a parameter MAX has no counterpart for (API spec §4)."""
    return JSONResponse(status_code=501, content={
        "error": f"{parameter} is not supported for platform max",
        "tool": tool,
        "parameter": parameter,
        "platform": "max",
        "alias": alias_of(request),
    })


async def store_sent(session: MaxSession, msg: Any, chat_id: int, *, is_edit: bool = False) -> None:
    """Store what we sent at once; a failure is logged — the message is out, the echo stores it."""
    if msg is None or session.ingest is None:
        return
    try:
        await session.ingest.store_sent(msg, chat_id, is_edit=is_edit)
    except Exception:  # noqa: BLE001
        log.warning("max[%s]: sent message %s in chat %s not stored", session.alias,
                    getattr(msg, "id", None), chat_id, exc_info=True)
