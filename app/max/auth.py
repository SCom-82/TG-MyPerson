"""REST-driven auth for PyMax (ADR §2.H, API spec §2.3–2.4).

PyMax authenticates *inside* client.connect(), on the open connection, by asking
providers for the SMS code / 2FA password and by handing the QR link to a
handler. The default providers read the console. Here each provider parks on an
asyncio.Future that a REST call (POST /auth/code) resolves; LoginController is
the shared state the REST layer reads (GET /auth/qr) and writes.

Limits (architect review 07.10):
- 2FA password: at most PASSWORD_MAX_ATTEMPTS checks and one PASSWORD_TIMEOUT_S
  deadline from the first prompt. PyMax 2.4.1 QrAuthFlow re-asks a wrong
  password forever (password_max_attempts is applied only in SmsAuthFlow), so
  the provider enforces it and raises MaxPasswordAttemptsExceeded.
- SMS code: CODE_TIMEOUT_S.
- QR: lives until the server's expires_at; PyMax raises on expiry.
Every limit ends the login: the supervisor closes the connection, state=error.
"""

import asyncio
import time
from collections.abc import Callable
from datetime import datetime, timezone

from pymax import QrAuthFlow

CODE_TIMEOUT_S = 600.0
PASSWORD_TIMEOUT_S = 600.0
PASSWORD_MAX_ATTEMPTS = 3

_QR_EXPIRED_MESSAGE = "QR authentication expired"  # pymax/auth/qr.py, 2.4.1


class MaxAuthError(Exception):
    """Interactive login failed for good — never retried automatically.

    Deliberately not a TimeoutError/ConnectionError subclass: PyMax and the
    supervisor treat those as network errors and would reconnect.
    """


class MaxAuthTimeout(MaxAuthError):
    pass


class MaxPasswordAttemptsExceeded(MaxAuthError):
    pass


class MaxQrExpired(MaxAuthError):
    pass


class LoginController:
    """State of one interactive login (QR or SMS) shared with the REST layer."""

    def __init__(self, method: str, on_change: Callable[[], None] = lambda: None) -> None:
        if method not in ("qr", "sms"):
            raise ValueError(method)
        self.method = method
        self.step: str | None = None  # awaiting_qr | awaiting_code | awaiting_password
        self.qr_link: str | None = None
        self.qr_expires_at: datetime | None = None
        self.password_hint: str | None = None
        self.password_attempts = 0  # passwords handed to PyMax so far
        self.last_error: str | None = None
        self._password_deadline: float | None = None
        self._code: asyncio.Future[str] | None = None
        self._password: asyncio.Future[str] | None = None
        self._on_change = on_change

    # -- state -------------------------------------------------------------

    def _set(self, step: str | None) -> None:
        self.step = step
        self._on_change()

    def on_qr(self, link: str, expires_at_ms: int | None) -> None:
        self.qr_link = link
        if expires_at_ms is not None:
            self.qr_expires_at = datetime.fromtimestamp(expires_at_ms / 1000, tz=timezone.utc)
        self._set("awaiting_qr")

    def qr_alive(self) -> bool:
        return (
            self.step == "awaiting_qr"
            and self.qr_expires_at is not None
            and self.qr_expires_at > datetime.now(timezone.utc)
        )

    @property
    def password_pending(self) -> bool:
        """PyMax is waiting for a password right now."""
        return self._password is not None and not self._password.done()

    @property
    def password_attempts_left(self) -> int:
        return max(PASSWORD_MAX_ATTEMPTS - self.password_attempts, 0)

    # -- provider side (called by PyMax inside connect) --------------------

    async def wait_code(self) -> str:
        self._code = asyncio.get_running_loop().create_future()
        self._set("awaiting_code")
        try:
            return await asyncio.wait_for(self._code, CODE_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise MaxAuthTimeout("SMS code was not provided in time") from exc
        finally:
            self._code = None

    async def wait_password(self, hint: str | None) -> str:
        if self.password_attempts >= PASSWORD_MAX_ATTEMPTS:
            self.last_error = "password attempts exceeded"
            raise MaxPasswordAttemptsExceeded(self.last_error)
        if self._password_deadline is None:
            self._password_deadline = time.monotonic() + PASSWORD_TIMEOUT_S
        elif self.password_attempts > 0:
            # PyMax asks again only after the previous password was rejected.
            self.last_error = "invalid password"
        remaining = self._password_deadline - time.monotonic()
        if remaining <= 0:
            raise MaxAuthTimeout("2FA password was not provided in time")

        self.password_hint = hint
        self._password = asyncio.get_running_loop().create_future()
        self._set("awaiting_password")
        try:
            password = await asyncio.wait_for(self._password, remaining)
        except asyncio.TimeoutError as exc:
            raise MaxAuthTimeout("2FA password was not provided in time") from exc
        finally:
            self._password = None
        self.password_attempts += 1
        return password

    # -- REST side ---------------------------------------------------------

    def submit_code(self, code: str) -> bool:
        if self._code is None or self._code.done():
            return False
        self._code.set_result(code)
        return True

    def submit_password(self, password: str) -> bool:
        if self._password is None or self._password.done():
            return False
        self._password.set_result(password)
        return True

    def cancel(self) -> None:
        for fut in (self._code, self._password):
            if fut is not None and not fut.done():
                fut.cancel()


class RestSmsCodeProvider:
    """pymax SmsCodeProvider: waits for POST /auth/code."""

    def __init__(self, controller: LoginController) -> None:
        self.controller = controller

    async def get_code(self, phone: str) -> str:
        return await self.controller.wait_code()


class RestPasswordProvider:
    """pymax PasswordProvider: waits for POST /auth/code {password}; owns the limits."""

    def __init__(self, controller: LoginController) -> None:
        self.controller = controller

    async def get_password(self, hint: str | None = None) -> str:
        return await self.controller.wait_password(hint)


class RestQrHandler:
    """pymax QrHandler: publishes the QR link for GET /auth/qr(.png)."""

    def __init__(self, controller: LoginController) -> None:
        self.controller = controller

    async def show_qr(self, qr_url: str) -> None:
        # expires_at is not passed to handlers; RestQrAuthFlow._poll_qr adds it.
        self.controller.qr_link = qr_url


class RestQrAuthFlow(QrAuthFlow):
    """QrAuthFlow that also exposes expires_at and maps expiry to MaxQrExpired."""

    def __init__(self, controller: LoginController) -> None:
        super().__init__(RestQrHandler(controller), RestPasswordProvider(controller))
        self.controller = controller

    async def _poll_qr(self, app, qr_info) -> bool:  # noqa: ANN001 — pymax internals
        self.controller.on_qr(qr_info.qr_link, qr_info.expires_at)
        return await super()._poll_qr(app, qr_info)

    async def authenticate(self, app):  # noqa: ANN001, ANN201
        try:
            return await super().authenticate(app)
        except RuntimeError as exc:
            if str(exc) == _QR_EXPIRED_MESSAGE:
                raise MaxQrExpired("QR expired before it was confirmed") from exc
            raise
