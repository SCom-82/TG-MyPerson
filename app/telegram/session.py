"""TelegramSession — per-alias Telethon client instance.

Each account (alias) gets its own instance initialized from the
account_sessions table (multi-account architecture, Phase 1+).
"""

import asyncio
import logging

from telethon import TelegramClient
from telethon.sessions import StringSession

log = logging.getLogger(__name__)


class TelegramSession:
    """One Telethon client for one account alias. Not a singleton."""

    def __init__(
        self,
        alias: str,
        phone: str,
        api_id: int,
        api_hash: str,
        initial_session_string: str | None = None,
    ) -> None:
        self.alias = alias
        self.phone = phone
        self._api_id = api_id
        self._api_hash = api_hash
        self._initial_session_string = initial_session_string

        self._client: TelegramClient | None = None
        self._phone_code_hash: str | None = None
        self._lock = asyncio.Lock()

        # Supervisor state (set by pool)
        self.last_error: str | None = None
        self.last_started_at: float | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def client(self) -> TelegramClient | None:
        return self._client

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected()

    @property
    def is_running(self) -> bool:
        """True when client is connected (does NOT check authorization)."""
        return self.is_connected

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect the client. Does NOT log in — use send_code/sign_in."""
        if not self._api_id or not self._api_hash:
            log.warning(
                "[%s] TG_API_ID / TG_API_HASH not set, skipping init", self.alias
            )
            return

        session = StringSession(self._initial_session_string or "")
        self._client = TelegramClient(
            session,
            self._api_id,
            self._api_hash,
            system_version="4.16.30-vxCUSTOM",
        )
        await self._client.connect()

        if await self._client.is_user_authorized():
            me = await self._client.get_me()
            log.info(
                "[%s] Authorized as %s (id=%s)",
                self.alias,
                me.username or me.first_name,
                me.id,
            )
        else:
            log.info(
                "[%s] Connected but not authorized — use /auth/login?session=%s",
                self.alias,
                self.alias,
            )

        import time
        self.last_started_at = time.time()

    async def stop(self) -> None:
        if self._client:
            await self._client.disconnect()
            log.info("[%s] Disconnected", self.alias)

    # ------------------------------------------------------------------
    # Auth flow
    # ------------------------------------------------------------------

    async def send_code(self) -> dict:
        """Send SMS code to self.phone."""
        async with self._lock:
            if not self._client:
                raise RuntimeError(f"[{self.alias}] Client not initialized")
            result = await self._client.send_code_request(self.phone)
            self._phone_code_hash = result.phone_code_hash
            return {"status": "code_sent", "phone": self.phone, "alias": self.alias}

    async def sign_in(self, code: str, password: str | None = None) -> dict:
        """Complete sign-in with code and optional 2FA password."""
        async with self._lock:
            if not self._client:
                raise RuntimeError(f"[{self.alias}] Client not initialized")

            try:
                await self._client.sign_in(
                    phone=self.phone,
                    code=code,
                    phone_code_hash=self._phone_code_hash,
                )
            except Exception as e:
                if "Two-steps verification" in str(e) or "SessionPasswordNeeded" in type(e).__name__:
                    if not password:
                        return {"status": "2fa_required", "alias": self.alias}
                    await self._client.sign_in(password=password)
                else:
                    raise

            me = await self._client.get_me()
            session_string = self._client.session.save()
            log.info("[%s] Signed in as %s (id=%s)", self.alias, me.username or me.first_name, me.id)

            return {
                "status": "authorized",
                "alias": self.alias,
                "user_id": me.id,
                "username": me.username,
                "session_string": session_string,
            }

    async def import_session(self, session_string: str) -> dict:
        """Replace current session with an imported string session."""
        async with self._lock:
            if self._client:
                await self._client.disconnect()

            session = StringSession(session_string)
            self._client = TelegramClient(
                session,
                self._api_id,
                self._api_hash,
                system_version="4.16.30-vxCUSTOM",
            )
            await self._client.connect()

            if not await self._client.is_user_authorized():
                return {"status": "error", "detail": "Session string is invalid or expired", "alias": self.alias}

            me = await self._client.get_me()
            self._initial_session_string = session_string
            log.info("[%s] Session imported, authorized as %s (id=%s)", self.alias, me.username or me.first_name, me.id)
            return {
                "status": "authorized",
                "alias": self.alias,
                "user_id": me.id,
                "username": me.username,
            }

    async def logout(self) -> dict:
        """Log out and disconnect."""
        if self._client and await self._client.is_user_authorized():
            await self._client.log_out()
            log.info("[%s] Logged out", self.alias)
        return {"status": "logged_out", "alias": self.alias}

    async def get_auth_status(self) -> dict:
        """Return current authorization status."""
        if not self._client or not self._client.is_connected():
            return {
                "alias": self.alias,
                "connected": False,
                "phone_number": None,
                "user_id": None,
                "username": None,
            }

        if not await self._client.is_user_authorized():
            return {
                "alias": self.alias,
                "connected": True,
                "phone_number": None,
                "user_id": None,
                "username": None,
            }

        me = await self._client.get_me()
        return {
            "alias": self.alias,
            "connected": True,
            "phone_number": me.phone,
            "user_id": me.id,
            "username": me.username,
        }

    def get_session_string(self) -> str | None:
        if self._client and self._client.session:
            return self._client.session.save()
        return None
