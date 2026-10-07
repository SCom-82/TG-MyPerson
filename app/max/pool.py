"""MaxPool — MAX sessions by alias; mirror of TelegramClientPool (ADR §2.C).

Created only when MAX_ENABLED=true (app.main lifespan). start_all() runs as a
background task: a hanging MAX handshake never delays the app startup, and its
failures never touch the Telegram pool or readyz.
"""

import asyncio
import logging

from sqlalchemy import select

from app import database
from app.max.config import MaxSettings
from app.max.session import ClientFactory, MaxSession
from app.models import Account

log = logging.getLogger(__name__)


class MaxAliasNotFound(LookupError):
    pass


class MaxPool:
    def __init__(self, settings: MaxSettings, client_factory: ClientFactory | None = None) -> None:
        self.settings = settings
        self._client_factory = client_factory
        self._sessions: dict[str, MaxSession] = {}
        self._lock = asyncio.Lock()

    def _make(self, account: Account) -> MaxSession:
        return MaxSession(
            account_id=account.id,
            alias=account.alias,
            phone=account.phone,
            settings=self.settings,
            client_factory=self._client_factory,
        )

    async def _load_accounts(self, alias: str | None = None) -> list[Account]:
        stmt = select(Account).where(
            Account.platform == "max",
            Account.is_enabled == True,  # noqa: E712
        )
        if alias is not None:
            stmt = stmt.where(Account.alias == alias)
        async with database.async_session() as db:
            return list((await db.execute(stmt.order_by(Account.id))).scalars().all())

    async def start_all(self) -> None:
        """Resume every enabled MAX account that has a stored session. Never raises."""
        try:
            accounts = await self._load_accounts()
        except Exception as exc:  # noqa: BLE001
            log.error("max pool: failed to load accounts: %s", exc)
            return
        for account in accounts:
            try:
                session = await self.get(account.alias)
                await session.start()
            except Exception as exc:  # noqa: BLE001 — one account must not stop the others
                log.error("max pool: failed to start '%s': %s", account.alias, exc)
        log.info("max pool: started %s", {a: s.state for a, s in self._sessions.items()})

    async def get(self, alias: str) -> MaxSession:
        """Session object for an enabled MAX alias (created lazily, not started)."""
        session = self._sessions.get(alias)
        if session is not None:
            return session
        async with self._lock:
            session = self._sessions.get(alias)
            if session is not None:
                return session
            accounts = await self._load_accounts(alias)
            if not accounts:
                raise MaxAliasNotFound(alias)
            session = self._make(accounts[0])
            self._sessions[alias] = session
            return session

    async def restart(self, alias: str) -> MaxSession:
        await self.stop_alias(alias)
        session = await self.get(alias)
        await session.start()
        return session

    async def stop_alias(self, alias: str) -> None:
        session = self._sessions.pop(alias, None)
        if session is not None:
            await session.stop()

    async def stop_all(self) -> None:
        for alias in list(self._sessions):
            try:
                await self.stop_alias(alias)
            except Exception as exc:  # noqa: BLE001
                log.warning("max pool: error stopping '%s': %s", alias, exc)

    def pool_status(self) -> dict[str, dict]:
        """alias → {runtime, started_at} for GET /accounts."""
        return {
            alias: {"runtime": session.runtime(), "started_at": session.started_at}
            for alias, session in self._sessions.items()
        }


# Singleton, set by app.main lifespan when MAX_ENABLED=true; None otherwise.
max_pool: MaxPool | None = None


def get_max_pool() -> MaxPool | None:
    return max_pool
