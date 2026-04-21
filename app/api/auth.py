import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from app.database import async_session
from app.models import Account, AccountSession
from app.schemas import (
    AuthStatusResponse,
    AuthMeResponse,
    LoginRequest,
    LoginCodeRequest,
    SessionImportRequest,
)
from app.telegram.pool import pool

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

_DEFAULT_ALIAS = "work"


@router.get("/status", response_model=AuthStatusResponse)
async def auth_status(request: Request, session: str = Query(_DEFAULT_ALIAS)):
    alias = request.query_params.get("session", session)
    tg_session = await pool.get(alias)
    return await tg_session.get_auth_status()


@router.get("/me", response_model=AuthMeResponse)
async def auth_me(request: Request, session: str = Query(_DEFAULT_ALIAS)):
    alias = getattr(request.state, "session_alias", session)
    tg_session = await pool.get(alias)
    client = tg_session.client
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=503, detail=f"Session '{alias}' not authorized")
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
async def auth_login(req: LoginRequest, request: Request, session: str = Query(_DEFAULT_ALIAS)):
    """Send SMS login code to the phone associated with `session` alias."""
    alias = getattr(request.state, "session_alias", session)
    tg_session = await pool.get(alias)

    # If already authorized — 409
    if tg_session.client and await tg_session.client.is_user_authorized():
        raise HTTPException(status_code=409, detail=f"Session '{alias}' is already authorized")

    return await tg_session.send_code()


@router.post("/code")
async def auth_code(req: LoginCodeRequest, request: Request, session: str = Query(_DEFAULT_ALIAS)):
    """Complete sign-in with code (+optional 2FA) for `session` alias."""
    alias = getattr(request.state, "session_alias", session)
    tg_session = await pool.get(alias)
    result = await tg_session.sign_in(req.code, req.password)

    # On success: persist session string to DB and update account
    if result.get("status") == "authorized":
        session_string = tg_session.get_session_string()
        user_id = result.get("user_id")
        if session_string:
            await _save_session_to_db(alias, session_string, user_id)

    return result


@router.post("/session")
async def auth_session(req: SessionImportRequest, request: Request, session: str = Query(_DEFAULT_ALIAS)):
    """Import a StringSession for `session` alias and restart it in pool."""
    alias = getattr(request.state, "session_alias", session)
    tg_session = await pool.get(alias)
    result = await tg_session.import_session(req.session_string)

    if result.get("status") == "authorized":
        session_string = tg_session.get_session_string()
        user_id = result.get("user_id")
        if session_string:
            await _save_session_to_db(alias, session_string, user_id)
        # Restart in pool with new session string
        await pool.restart(alias)

    return result


@router.post("/logout")
async def auth_logout(request: Request, session: str = Query(_DEFAULT_ALIAS)):
    """Log out `session` alias and clear session from DB."""
    alias = getattr(request.state, "session_alias", session)
    tg_session = await pool.get(alias)
    result = await tg_session.logout()

    # Clear session from DB
    await _clear_session_in_db(alias)

    # Remove from pool
    await pool.stop(alias)

    return result


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _save_session_to_db(alias: str, session_string: str, user_id: int | None) -> None:
    """Persist session string to account_sessions and update accounts.tg_user_id."""
    now = datetime.now(timezone.utc)
    try:
        async with async_session() as db:
            # Get account
            acc_result = await db.execute(select(Account).where(Account.alias == alias))
            account = acc_result.scalar_one_or_none()
            if account is None:
                log.error("_save_session_to_db: account '%s' not found", alias)
                return

            # Update tg_user_id if provided
            if user_id and not account.tg_user_id:
                account.tg_user_id = user_id

            # Upsert active account_session
            sess_result = await db.execute(
                select(AccountSession).where(
                    AccountSession.account_id == account.id,
                    AccountSession.is_active == True,  # noqa: E712
                )
            )
            account_session = sess_result.scalar_one_or_none()

            if account_session is None:
                account_session = AccountSession(
                    account_id=account.id,
                    session_plaintext=session_string,
                    authorized_at=now,
                    last_connected_at=now,
                    is_active=True,
                )
                db.add(account_session)
            else:
                account_session.session_plaintext = session_string
                account_session.authorized_at = now
                account_session.last_connected_at = now

            await db.commit()
            log.info("Session saved to DB for alias '%s'", alias)
    except Exception:
        log.exception("Failed to save session to DB for alias '%s'", alias)


async def _clear_session_in_db(alias: str) -> None:
    """Set session_plaintext = NULL and is_active = false for alias."""
    try:
        async with async_session() as db:
            acc_result = await db.execute(select(Account).where(Account.alias == alias))
            account = acc_result.scalar_one_or_none()
            if account is None:
                return

            sess_result = await db.execute(
                select(AccountSession).where(
                    AccountSession.account_id == account.id,
                    AccountSession.is_active == True,  # noqa: E712
                )
            )
            account_session = sess_result.scalar_one_or_none()
            if account_session:
                account_session.session_plaintext = None
                account_session.is_active = False
                await db.commit()
                log.info("Session cleared in DB for alias '%s'", alias)
    except Exception:
        log.exception("Failed to clear session in DB for alias '%s'", alias)
