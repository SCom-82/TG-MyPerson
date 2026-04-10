import logging

from fastapi import APIRouter, HTTPException

from app.config import settings
from app.schemas import (
    AuthStatusResponse,
    AuthMeResponse,
    LoginRequest,
    LoginCodeRequest,
    SessionImportRequest,
)
from app.telegram.client import tg_bridge

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/status", response_model=AuthStatusResponse)
async def auth_status():
    return await tg_bridge.get_auth_status()


@router.get("/me", response_model=AuthMeResponse)
async def auth_me():
    """Detailed info about the authorized user."""
    client = tg_bridge.client
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=503, detail="Telegram client not authorized")
    me = await client.get_me()
    return AuthMeResponse(
        user_id=me.id,
        username=getattr(me, "username", None),
        first_name=getattr(me, "first_name", None),
        last_name=getattr(me, "last_name", None),
        phone=getattr(me, "phone", None),
        is_premium=bool(getattr(me, "premium", False)),
        is_verified=bool(getattr(me, "verified", False)),
        is_bot=bool(getattr(me, "bot", False)),
        dc_id=getattr(me, "dc_id", None),
        lang_code=getattr(me, "lang_code", None),
    )


@router.post("/login")
async def auth_login(req: LoginRequest):
    return await tg_bridge.send_code(req.phone_number)


@router.post("/code")
async def auth_code(req: LoginCodeRequest):
    result = await tg_bridge.sign_in(req.code, req.password)

    # Persist session string to DB if authorized
    if result.get("status") == "authorized" and result.get("session_string"):
        await _save_session(result["session_string"])

    return result


@router.post("/session")
async def auth_session(req: SessionImportRequest):
    result = await tg_bridge.import_session(req.session_string)

    if result.get("status") == "authorized":
        session_str = tg_bridge.get_session_string()
        if session_str:
            await _save_session(session_str)

    return result


@router.post("/logout")
async def auth_logout():
    return await tg_bridge.logout()


async def _save_session(session_string: str) -> None:
    """Persist session string to tg_session table."""
    from sqlalchemy import select
    from app.database import async_session
    from app.models import TgSession
    from datetime import datetime, timezone

    try:
        async with async_session() as session:
            stmt = select(TgSession).where(TgSession.session_name == "default")
            result = await session.execute(stmt)
            db_session = result.scalar_one_or_none()

            if db_session is None:
                db_session = TgSession(
                    session_name="default",
                    session_string=session_string,
                    phone_number=settings.tg_phone_number,
                    is_active=True,
                    last_connected_at=datetime.now(timezone.utc),
                )
                session.add(db_session)
            else:
                db_session.session_string = session_string
                db_session.is_active = True
                db_session.last_connected_at = datetime.now(timezone.utc)

            await session.commit()
            log.info("Session string saved to database")
    except Exception:
        log.exception("Failed to save session string to database")
