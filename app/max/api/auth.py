"""MAX auth endpoints (API spec §2). Same paths and route names as the TG ones.

Reached via platform_dispatch rewrite only (prefix /api/v1/_max).
"""

import io

import qrcode
import qrcode.image.pure
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.max.api.common import session_or_error as _session
from app.max.session import MaxLoginConflict, MaxSession, MaxSessionUnavailable
from app.schemas import AuthMeResponse, AuthStatusResponse, LoginCodeRequest, LoginRequest, SessionImportRequest

router = APIRouter(prefix="/auth", tags=["max-auth"])


def _unavailable(exc: MaxSessionUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc), "state": exc.state})


def _conflict(exc: MaxLoginConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": exc.error, "state": exc.state})


@router.get("/status", response_model=AuthStatusResponse, name="auth_status")
async def auth_status(request: Request):
    session = await _session(request)
    if isinstance(session, JSONResponse):
        return session
    return AuthStatusResponse(**await session.auth_status())


@router.get("/me", response_model=AuthMeResponse, name="auth_me")
async def auth_me(request: Request):
    session = await _session(request)
    if isinstance(session, JSONResponse):
        return session
    try:
        return AuthMeResponse(**await session.me())
    except MaxSessionUnavailable as exc:
        return _unavailable(exc)


@router.post("/qr", name="auth_qr_start")
async def auth_qr_start(request: Request):
    session = await _session(request)
    if isinstance(session, JSONResponse):
        return session
    try:
        snapshot = await session.start_qr_login()
    except MaxLoginConflict as exc:
        return _conflict(exc)
    if snapshot["status"] == "error":
        return JSONResponse(status_code=502, content={**snapshot, "alias": session.alias})
    return JSONResponse(
        status_code=202,
        content={
            "status": snapshot["status"],
            "alias": session.alias,
            "qr_link": snapshot["qr_link"],
            "expires_at": snapshot["expires_at"],
            "qr_png_url": f"/api/v1/auth/qr.png?session={session.alias}",
            "password_hint": snapshot["password_hint"],
        },
    )


@router.get("/qr", name="auth_qr_status")
async def auth_qr_status(request: Request):
    session = await _session(request)
    if isinstance(session, JSONResponse):
        return session
    return session.qr_snapshot()


@router.get("/qr.png", name="auth_qr_status")
async def auth_qr_png(request: Request):
    session = await _session(request)
    if isinstance(session, JSONResponse):
        return session
    snapshot = session.qr_snapshot()
    if snapshot["status"] != "awaiting_qr" or not snapshot["qr_link"]:
        return JSONResponse(status_code=404, content={"error": "no active QR", "status": snapshot["status"]})
    buf = io.BytesIO()
    qrcode.make(snapshot["qr_link"], image_factory=qrcode.image.pure.PyPNGImage).save(buf)
    return Response(content=buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/login", name="auth_login")
async def auth_login(req: LoginRequest, request: Request):
    # As for TG, the phone comes from accounts.phone; the body field is accepted and ignored.
    session = await _session(request)
    if isinstance(session, JSONResponse):
        return session
    try:
        return await session.start_sms_login()
    except MaxLoginConflict as exc:
        return _conflict(exc)


@router.post("/code", name="auth_code")
async def auth_code(req: LoginCodeRequest, request: Request):
    session = await _session(request)
    if isinstance(session, JSONResponse):
        return session
    try:
        status, body = await session.submit_code(req.code, req.password)
    except MaxLoginConflict as exc:
        return _conflict(exc)
    return JSONResponse(status_code=status, content=body)


@router.post("/session", name="auth_session")
async def auth_session(req: SessionImportRequest, request: Request):
    session = await _session(request)
    if isinstance(session, JSONResponse):
        return session
    return await session.import_session(req.session_string)


@router.post("/logout", name="auth_logout")
async def auth_logout(request: Request):
    session = await _session(request)
    if isinstance(session, JSONResponse):
        return session
    return await session.logout()
