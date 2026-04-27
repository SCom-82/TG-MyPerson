"""TelegramClientPool — manages a pool of per-alias TelegramSession instances.

Lifecycle:
  - start_all(): called in app lifespan startup; starts all enabled accounts.
  - stop_all(): called in app lifespan shutdown.
  - get(alias): lazy-start a session; returns a running TelegramSession.
  - restart(alias): stop + remove + re-get.

Supervisor pattern: errors in one session are caught and logged; they do not
prevent other sessions from starting or running.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models import Account, AccountSession
from app.telegram.session import TelegramSession

if TYPE_CHECKING:
    from telethon import TelegramClient

log = logging.getLogger(__name__)


class TelegramClientPool:
    def __init__(self) -> None:
        self._pool: dict[str, TelegramSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _alias_lock(self, alias: str) -> asyncio.Lock:
        if alias not in self._locks:
            self._locks[alias] = asyncio.Lock()
        return self._locks[alias]

    async def _load_session(self, alias: str) -> TelegramSession:
        """Load account + session from DB and create TelegramSession."""
        async with async_session() as db:
            result = await db.execute(
                select(Account).where(Account.alias == alias, Account.is_enabled == True)  # noqa: E712
            )
            account = result.scalar_one_or_none()
            if account is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Session alias '{alias}' not registered or disabled",
                )

            sess_result = await db.execute(
                select(AccountSession).where(
                    AccountSession.account_id == account.id,
                    AccountSession.is_active == True,  # noqa: E712
                )
            )
            account_session = sess_result.scalar_one_or_none()
            session_string = account_session.session_plaintext if account_session else None

        tg_session = TelegramSession(
            alias=alias,
            phone=account.phone,
            api_id=settings.tg_api_id,
            api_hash=settings.tg_api_hash,
            initial_session_string=session_string,
        )
        return tg_session

    async def _start_one(self, alias: str) -> TelegramSession | None:
        """Start a single session with error isolation."""
        try:
            session = await self._load_session(alias)
            await session.start()
            self._pool[alias] = session
            log.info("Pool: session '%s' started", alias)
            return session
        except HTTPException:
            raise
        except Exception as exc:
            log.error("Pool: failed to start session '%s': %s", alias, exc)
            # Store a failed stub so we can report status
            stub = TelegramSession(
                alias=alias,
                phone="",
                api_id=settings.tg_api_id,
                api_hash=settings.tg_api_hash,
            )
            stub.last_error = str(exc)
            stub.last_started_at = time.time()
            self._pool[alias] = stub
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get(self, alias: str) -> TelegramSession:
        """Return a running TelegramSession for alias (lazy start)."""
        # Fast path: already in pool and connected
        if alias in self._pool and self._pool[alias].is_running:
            return self._pool[alias]

        # Slow path: acquire alias lock to prevent double-start
        async with self._alias_lock(alias):
            # Re-check inside lock
            if alias in self._pool and self._pool[alias].is_running:
                return self._pool[alias]

            session = await _load_and_start(alias, self)
            return session

    async def start_all(self) -> None:
        """Start all enabled accounts. Errors per session are isolated."""
        async with async_session() as db:
            result = await db.execute(
                select(Account).where(Account.is_enabled == True)  # noqa: E712
            )
            accounts = result.scalars().all()

        if not accounts:
            log.info("Pool: no enabled accounts found, skipping start_all")
            return

        log.info("Pool: starting %d account(s)", len(accounts))

        async def _safe_start(account: Account) -> None:
            try:
                await self._start_one(account.alias)
            except HTTPException:
                pass  # Already handled in _start_one

        await asyncio.gather(*[_safe_start(a) for a in accounts])
        log.info("Pool: start_all done. Running: %s", list(self._pool.keys()))

    async def stop_all(self) -> None:
        """Stop all sessions in pool."""
        for alias, session in list(self._pool.items()):
            try:
                await session.stop()
            except Exception as exc:
                log.warning("Pool: error stopping session '%s': %s", alias, exc)
        self._pool.clear()
        log.info("Pool: all sessions stopped")

    async def stop(self, alias: str) -> None:
        """Stop a specific session."""
        if alias in self._pool:
            try:
                await self._pool[alias].stop()
            except Exception as exc:
                log.warning("Pool: error stopping session '%s': %s", alias, exc)
            del self._pool[alias]

    async def stop_alias(self, alias: str) -> None:
        """Stop session for alias and remove from pool (Phase 3: called on account disable).

        Thread-safe via alias lock. No-op if alias not in pool.
        """
        async with self._alias_lock(alias):
            if alias in self._pool:
                try:
                    await self._pool[alias].stop()
                except Exception as exc:
                    log.warning("Pool: error stopping session '%s' via stop_alias: %s", alias, exc)
                del self._pool[alias]
                log.info("Pool: session '%s' stopped and removed via stop_alias", alias)

    async def reload_account(self, alias: str) -> TelegramSession:
        """Stop alias session, reload fresh account data from DB, lazy-start again.

        Used when session_string or critical account params change.
        After restart, re-registers event handlers for the 'work' alias.
        """
        async with self._alias_lock(alias):
            # Stop and remove existing session
            if alias in self._pool:
                try:
                    await self._pool[alias].stop()
                except Exception as exc:
                    log.warning("Pool: error stopping '%s' during reload: %s", alias, exc)
                del self._pool[alias]

        # get() will lazy-start from fresh DB data (re-reads account + session)
        session = await self.get(alias)
        await _maybe_register_handlers(alias, session)
        return session

    async def restart(self, alias: str) -> TelegramSession:
        """Stop, remove and re-start a session.

        After restart, re-registers event handlers for the 'work' alias.
        """
        await self.stop(alias)
        session = await self.get(alias)
        await _maybe_register_handlers(alias, session)
        return session

    def pool_status(self) -> list[dict]:
        """Return status of all sessions in pool.

        Includes last_started_at (ISO timestamp string or None) and
        last_error for Phase 3 /accounts list endpoint.
        """
        import time as _time
        from datetime import datetime, timezone

        result = []
        for alias, session in self._pool.items():
            last_started_at_raw = session.last_started_at
            last_started_at_iso: str | None = None
            if last_started_at_raw is not None:
                try:
                    last_started_at_iso = datetime.fromtimestamp(
                        last_started_at_raw, tz=timezone.utc
                    ).isoformat()
                except Exception:
                    pass

            result.append({
                "alias": alias,
                "is_running": session.is_running,
                "last_error": session.last_error,
                "last_started_at": last_started_at_raw,  # float or None (raw)
                "last_started_at_iso": last_started_at_iso,  # ISO string for API responses
            })
        return result


async def _load_and_start(alias: str, pool: TelegramClientPool) -> TelegramSession:
    """Helper to load account from DB and start it (used inside lock)."""
    # Check DB first to give 404 on unknown alias
    async with async_session() as db:
        result = await db.execute(
            select(Account).where(Account.alias == alias, Account.is_enabled == True)  # noqa: E712
        )
        account = result.scalar_one_or_none()

    if account is None:
        raise HTTPException(
            status_code=404,
            detail=f"Session alias '{alias}' not registered or disabled",
        )

    session = await pool._start_one(alias)
    if session is None:
        # Start failed — return the stub so callers can detect via is_running
        return pool._pool[alias]
    return session


async def require_authorized_client(request: Request) -> "TelegramClient":
    """Dependency / helper: return TelegramClient for current request's session_alias.

    Raises 503 if session is not authorized.
    """
    alias = getattr(request.state, "session_alias", "work")
    session = await pool.get(alias)
    if not session.client or not await session.client.is_user_authorized():
        raise HTTPException(
            status_code=503,
            detail=f"Session '{alias}' not authorized — use /auth/login?session={alias}",
        )
    return session.client


async def _maybe_register_handlers(alias: str, session: TelegramSession) -> None:
    """Re-register event handlers after restart/reload for the 'work' alias.

    Only 'work' is handled here because that is the only alias that has
    real-time event handlers (message capture, stream broadcasting).
    TODO: generalise via a callback registry if more aliases need handlers.
    """
    if alias != "work":
        return
    if not session.client:
        return
    try:
        if await session.client.is_user_authorized():
            from app.telegram.handlers import register_handlers
            register_handlers(session.client)
            log.info("Pool: re-registered handlers for alias 'work' after restart/reload")
    except Exception as exc:
        log.warning("Pool: failed to re-register handlers for 'work': %s", exc)


# Singleton pool instance
pool = TelegramClientPool()
