import asyncio
import logging

from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import settings

log = logging.getLogger(__name__)


class TelegramBridge:
    """Manages the Telethon client lifecycle and authentication."""

    def __init__(self):
        self._client: TelegramClient | None = None
        self._phone_code_hash: str | None = None
        self._lock = asyncio.Lock()

    @property
    def client(self) -> TelegramClient | None:
        return self._client

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected()

    async def start(self) -> None:
        """Initialize and connect the client (does NOT log in)."""
        if not settings.tg_api_id or not settings.tg_api_hash:
            log.warning("TG_API_ID / TG_API_HASH not set, skipping Telegram client init")
            return

        session = StringSession(settings.tg_session_string or "")
        self._client = TelegramClient(
            session,
            settings.tg_api_id,
            settings.tg_api_hash,
            system_version="4.16.30-vxCUSTOM",
        )
        await self._client.connect()

        if await self._client.is_user_authorized():
            me = await self._client.get_me()
            log.info("Telegram authorized as %s (id=%s)", me.username or me.first_name, me.id)
        else:
            log.info("Telegram client connected but not authorized — use /auth/login")

    async def stop(self) -> None:
        if self._client:
            await self._client.disconnect()
            log.info("Telegram client disconnected")

    async def send_code(self, phone: str) -> dict:
        """Send login code to phone. Returns status info."""
        async with self._lock:
            if not self._client:
                raise RuntimeError("Telegram client not initialized")
            result = await self._client.send_code_request(phone)
            self._phone_code_hash = result.phone_code_hash
            return {"status": "code_sent", "phone": phone}

    async def sign_in(self, code: str, password: str | None = None) -> dict:
        """Complete sign-in with code and optional 2FA password."""
        async with self._lock:
            if not self._client:
                raise RuntimeError("Telegram client not initialized")

            try:
                await self._client.sign_in(
                    phone=settings.tg_phone_number,
                    code=code,
                    phone_code_hash=self._phone_code_hash,
                )
            except Exception as e:
                if "Two-steps verification" in str(e) or "SessionPasswordNeeded" in type(e).__name__:
                    if not password:
                        return {"status": "2fa_required"}
                    await self._client.sign_in(password=password)
                else:
                    raise

            me = await self._client.get_me()
            session_string = self._client.session.save()
            log.info("Signed in as %s (id=%s)", me.username or me.first_name, me.id)

            return {
                "status": "authorized",
                "user_id": me.id,
                "username": me.username,
                "session_string": session_string,
            }

    async def import_session(self, session_string: str) -> dict:
        """Replace current session with an imported one."""
        async with self._lock:
            if self._client:
                await self._client.disconnect()

            session = StringSession(session_string)
            self._client = TelegramClient(
                session,
                settings.tg_api_id,
                settings.tg_api_hash,
                system_version="4.16.30-vxCUSTOM",
            )
            await self._client.connect()

            if not await self._client.is_user_authorized():
                return {"status": "error", "detail": "Session string is invalid or expired"}

            me = await self._client.get_me()
            log.info("Session imported, authorized as %s (id=%s)", me.username or me.first_name, me.id)
            return {
                "status": "authorized",
                "user_id": me.id,
                "username": me.username,
            }

    async def logout(self) -> dict:
        """Log out and disconnect."""
        if self._client and await self._client.is_user_authorized():
            await self._client.log_out()
            log.info("Logged out from Telegram")
        return {"status": "logged_out"}

    async def get_auth_status(self) -> dict:
        """Return current authorization status."""
        if not self._client or not self._client.is_connected():
            return {"connected": False, "phone_number": None, "user_id": None, "username": None}

        if not await self._client.is_user_authorized():
            return {"connected": True, "phone_number": None, "user_id": None, "username": None}

        me = await self._client.get_me()
        return {
            "connected": True,
            "phone_number": me.phone,
            "user_id": me.id,
            "username": me.username,
        }

    def get_session_string(self) -> str | None:
        """Return current session string for persistence."""
        if self._client and self._client.session:
            return self._client.session.save()
        return None


# Singleton
tg_bridge = TelegramBridge()
