"""MaxSession — one MAX account: PyMax client, auth state machine, supervisor (ADR §2.I).

States: stopped → connecting → (awaiting_qr | awaiting_code | awaiting_password)
        → authorized ⇄ reconnecting → unauthorized | banned | error

Supervisor. PyMax's own BaseClient.start() loop is NOT used: with relogin=False
and a revoked token it neither re-authenticates nor exits — it logs and runs the
next iteration on the same connection, i.e. retries login forever (2.4.1,
pymax/base.py start()). Instead the supervisor calls client.connect() (one
attempt: handshake + login, raises on failure), then waits for the connection to
close, and decides itself:
- revoked token (FAIL_LOGIN_TOKEN / FAIL_LOGOUT_ALL) → unauthorized, session row
  deactivated, no further attempts;
- ban heuristic → banned, no further attempts;
- interactive login (QR / SMS / import) failed → error, no retry: a new login is
  an explicit REST call;
- anything else → retry with backoff 5 s → 10 min, reset after 10 min of uptime.

Hard-wired PyMax parameters (not configurable via env): relogin=False,
telemetry=False, interactive=False (ping does not mark the app foreground),
registration_config=None (an SMS login can never register a number),
store=PgSessionStore, proxy=MAX_PROXY_URL, remote version catalog off, pymax
logger at INFO or above (DEBUG dumps request payloads with the token).
"""

import asyncio
import logging
import tempfile
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import pymax
from pymax import ApiError, Client, ExtraConfig, PasswordAttemptsExceededError, SmsAuthFlow, WebClient
from pymax.versions.catalog import VersionCatalog
from sqlalchemy import update

from app import database
from app.max.auth import (
    PASSWORD_MAX_ATTEMPTS,
    LoginController,
    MaxAuthError,
    MaxQrExpired,
    RestPasswordProvider,
    RestQrAuthFlow,
    RestSmsCodeProvider,
)
from app.max.config import MaxSettings
from app.max.errors import is_banned, is_unauthorized
from app.max.store import (
    PgSessionStore,
    SeededStore,
    SessionFormatError,
    deactivate_session,
    load_stored_transport,
    parse_session_json,
)
from app.models import Account

log = logging.getLogger(__name__)

PYMAX_VERSION = pymax.__version__
REQUEST_TIMEOUT_S = 30.0
BACKOFF_START_S = 5.0
BACKOFF_MAX_S = 600.0
STABLE_RESET_S = 600.0
# How long a REST call waits for the login to reach its next step.
REST_WAIT_S = 30.0

STATES = (
    "stopped", "connecting", "awaiting_qr", "awaiting_code", "awaiting_password",
    "authorized", "reconnecting", "unauthorized", "banned", "error",
)
_FINAL_STATES = ("stopped", "unauthorized", "banned", "error")
_NETWORK_ERRORS = (ConnectionError, EOFError, OSError, TimeoutError)

# Supervisor pause; a module attribute so tests can record backoff without
# patching the global asyncio.sleep that PyMax and httpx also use.
_sleep = asyncio.sleep


def guard_pymax_logger() -> None:
    """pymax logs full request payloads (token included) at DEBUG — never allow it."""
    pymax_logger = logging.getLogger("pymax")
    if pymax_logger.getEffectiveLevel() < logging.INFO:
        pymax_logger.setLevel(logging.INFO)


guard_pymax_logger()


# ---------------------------------------------------------------------------
# Thin PyMax subclasses
# ---------------------------------------------------------------------------

class _AdapterMixin:
    async def _prepare_config(self):  # noqa: ANN202 — pymax ClientConfig
        config = await super()._prepare_config()
        # ExtraConfig has no `interactive`; ClientConfig defaults it to True.
        config.interactive = False
        config.telemetry = False
        config.relogin = False
        config.registration_config = None
        return config

    async def wait_closed(self) -> None:
        await self._connection.wait_closed()


class AdapterWebClient(_AdapterMixin, WebClient):
    pass


class AdapterClient(_AdapterMixin, Client):
    pass


def build_extra_config(*, proxy: str | None, store: Any) -> ExtraConfig:
    return ExtraConfig(
        proxy=proxy or None,
        store=store,
        persist_session=True,
        relogin=False,
        telemetry=False,
        registration_config=None,
        reconnect=False,  # the supervisor owns reconnects
        request_timeout=REQUEST_TIMEOUT_S,
        password_max_attempts=PASSWORD_MAX_ATTEMPTS,
        log_level="INFO",
    )


def build_client(*, transport: str, phone: str, extra_config: ExtraConfig, auth_flow: Any):
    """Default client factory. Tests substitute a fake with the same signature."""
    work_dir = tempfile.gettempdir()  # unused: the store is ours, PyMax writes no files
    if transport == "web":
        client = AdapterWebClient(work_dir=work_dir, extra_config=extra_config, auth_flow=auth_flow)
    elif transport == "tcp":
        client = AdapterClient(
            phone=phone,
            work_dir=work_dir,
            extra_config=extra_config,
            auth_flow=auth_flow,
            catalog=VersionCatalog(remote=False),
        )
    else:
        raise ValueError(f"unknown transport {transport!r}")
    guard_pymax_logger()  # PyMax's configure_logging runs in the constructor
    return client


ClientFactory = Callable[..., Any]


class MaxSessionUnavailable(Exception):
    """The session cannot serve a request in its current state (→ 503)."""

    def __init__(self, alias: str, state: str) -> None:
        super().__init__(f"Session '{alias}' not available")
        self.alias = alias
        self.state = state


class MaxLoginConflict(Exception):
    """A login cannot start/continue now (→ 409)."""

    def __init__(self, error: str, state: str) -> None:
        super().__init__(error)
        self.error = error
        self.state = state


# ---------------------------------------------------------------------------
# MaxSession
# ---------------------------------------------------------------------------

class MaxSession:
    def __init__(
        self,
        *,
        account_id: int,
        alias: str,
        phone: str,
        settings: MaxSettings,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.account_id = account_id
        self.alias = alias
        self.phone = phone
        self.settings = settings
        self._client_factory = client_factory or build_client

        self.state = "stopped"
        self.transport: str | None = None
        self.last_error: str | None = None
        self.started_at: datetime | None = None
        self.last_event_at: datetime | None = None
        self.last_catchup_at: datetime | None = None
        self.catchup_backlog_chats = 0
        self.profile: Any = None
        self.login: LoginController | None = None
        self.qr_expired = False

        # PR-4: ingest registers event handlers on every freshly built client.
        self.client_hooks: list[Callable[[Any], None]] = []

        self._client: Any = None
        self._task: asyncio.Task | None = None
        self._changed = asyncio.Event()

    # -- state helpers -----------------------------------------------------

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    def _set_state(self, state: str, error: str | None = None) -> None:
        assert state in STATES, state
        self.state = state
        if error is not None or state == "authorized":
            self.last_error = error
        self._notify()

    def _on_login_step(self) -> None:
        if self.login is not None and self.login.step:
            self.state = self.login.step
        self._notify()

    async def wait_until(self, predicate: Callable[[], bool], timeout: float = REST_WAIT_S) -> bool:
        deadline = time.monotonic() + timeout
        while not predicate():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            changed = self._changed
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except asyncio.TimeoutError:
                return predicate()
        return True

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def connected(self) -> bool:
        return self.state == "authorized" and self._client is not None and bool(
            getattr(self._client, "is_connected", False)
        )

    @property
    def client(self) -> Any:
        """Authorized PyMax client or None (PR-4+ read/write go through it)."""
        return self._client if self.state == "authorized" else None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Resume from the stored session; without one stay 'stopped' (needs a login)."""
        if self.running:
            return
        transport = await load_stored_transport(self.account_id)
        if transport is None:
            self.transport = None
            self._set_state("stopped")
            return
        self._spawn(transport, store=PgSessionStore(self.account_id, transport), auth_flow=None, one_shot=False)

    async def stop(self) -> None:
        if self.login is not None:
            self.login.cancel()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._client = None
        if self.state not in ("unauthorized", "banned"):
            self._set_state("stopped")

    def _spawn(self, transport: str, *, store: Any, auth_flow: Any, one_shot: bool) -> None:
        self.transport = transport
        self._task = asyncio.create_task(
            self._supervise(transport, store=store, auth_flow=auth_flow, one_shot=one_shot),
            name=f"max-supervisor-{self.alias}",
        )

    def _proxy_ok(self) -> bool:
        return bool(self.settings.proxy_url) or not self.settings.require_proxy

    async def _supervise(self, transport: str, *, store: Any, auth_flow: Any, one_shot: bool) -> None:
        if not self._proxy_ok():
            # Fail-closed: no connection is opened, the factory is not called.
            self._set_state("error", "proxy required: MAX_PROXY_URL is empty and MAX_REQUIRE_PROXY=true")
            return

        backoff = BACKOFF_START_S
        was_authorized = False
        while True:
            client = None
            began = time.monotonic()
            self._set_state("reconnecting" if was_authorized else "connecting")
            try:
                client = self._client_factory(
                    transport=transport,
                    phone=self.phone,
                    extra_config=build_extra_config(proxy=self.settings.proxy_url, store=store),
                    auth_flow=auth_flow or _NoInteractiveLogin(),
                )
                for hook in self.client_hooks:
                    hook(client)
                await client.connect()
                if not client.is_connected:
                    raise ConnectionError("client did not start")

                if isinstance(store, SeededStore):
                    # Import succeeded: persist it; reconnects use the DB store directly.
                    store = await store.promote(PgSessionStore(self.account_id, transport))
                self._client = client
                was_authorized = True
                one_shot = False  # from here on it is a normal running session
                auth_flow = None
                await self._on_authorized(client)
                await client.wait_closed()
                raise ConnectionError("connection closed")
            except asyncio.CancelledError:
                raise
            except MaxQrExpired as exc:
                self.qr_expired = True
                self._set_state("error", str(exc))
                return
            except (MaxAuthError, PasswordAttemptsExceededError) as exc:
                self._set_state("error", str(exc) or type(exc).__name__)
                return
            except ApiError as exc:
                if is_unauthorized(exc):
                    log.warning("max[%s]: session token revoked (%s); not re-logging in", self.alias, exc.error)
                    if not isinstance(store, SeededStore):
                        await deactivate_session(self.account_id)
                    self._set_state("unauthorized", f"session revoked: {exc.error}")
                    return
                if is_banned(exc):
                    log.error("max[%s]: account looks restricted: %s", self.alias, exc)
                    self._set_state("banned", str(exc))
                    return
                error: BaseException = exc
            except Exception as exc:  # noqa: BLE001 — supervisor must survive anything
                error = exc
            finally:
                self._client = None
                if client is not None:
                    try:
                        await client.close()
                    except Exception:  # noqa: BLE001
                        log.debug("max[%s]: close failed", self.alias, exc_info=True)

            if one_shot:
                self._set_state("error", str(error) or type(error).__name__)
                return

            if time.monotonic() - began >= STABLE_RESET_S:
                backoff = BACKOFF_START_S
            network = isinstance(error, _NETWORK_ERRORS)
            self._set_state("reconnecting" if (network and was_authorized) else "error", str(error) or type(error).__name__)
            log.warning("max[%s]: %s; retry in %.0fs", self.alias, error, backoff)
            await _sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    async def _on_authorized(self, client: Any) -> None:
        self.profile = getattr(client, "me", None)
        self.started_at = datetime.now(timezone.utc)
        # Record the profile id before announcing "authorized", so whoever waits
        # for the state sees a consistent account row.
        user_id = self.user_id
        if user_id is not None:
            try:
                async with database.async_session() as db:
                    await db.execute(
                        update(Account).where(Account.id == self.account_id).values(platform_user_id=user_id)
                    )
                    await db.commit()
            except Exception:  # noqa: BLE001
                log.warning("max[%s]: failed to store platform_user_id", self.alias, exc_info=True)
        self.login = None
        self.qr_expired = False
        self._set_state("authorized")

    # -- interactive login -------------------------------------------------

    async def _ensure_can_login(self, method: str) -> None:
        if self.state == "authorized":
            raise MaxLoginConflict("already authorized", self.state)
        if self.login is not None and self.running:
            if self.login.method != method:
                raise MaxLoginConflict("login already in progress", self.state)
            return
        if await load_stored_transport(self.account_id) is not None:
            raise MaxLoginConflict("stored session exists; POST /auth/logout first", self.state)

    async def start_qr_login(self) -> dict:
        await self._ensure_can_login("qr")
        if self.login is not None and self.running:
            # Same QR login still going: return its current QR / password step.
            await self.wait_until(lambda: self.state in ("awaiting_qr", "awaiting_password", "authorized") or not self.running)
            return self.qr_snapshot()

        await self.stop()
        self.login = LoginController("qr", on_change=self._on_login_step)
        self.qr_expired = False
        self._spawn(
            "web",
            store=PgSessionStore(self.account_id, "web"),
            auth_flow=RestQrAuthFlow(self.login),
            one_shot=True,
        )
        await self.wait_until(lambda: self.state in ("awaiting_qr", "authorized") or not self.running)
        return self.qr_snapshot()

    def qr_snapshot(self) -> dict:
        login = self.login
        if self.state == "authorized":
            status = "authorized"
        elif self.qr_expired:
            status = "expired"
        elif login is not None and login.method == "qr" and self.running and login.step:
            status = login.step
        else:
            status = "error"
        error = self.last_error if status in ("error", "expired") else (login.last_error if login else None)
        if status == "error" and error is None:
            error = "no QR login in progress"
        return {
            "status": status,
            "qr_link": login.qr_link if login and login.method == "qr" and status == "awaiting_qr" else None,
            "expires_at": (
                login.qr_expires_at.isoformat().replace("+00:00", "Z")
                if login and login.qr_expires_at and status == "awaiting_qr"
                else None
            ),
            "error": error,
            "password_hint": login.password_hint if login and status == "awaiting_password" else None,
        }

    async def start_sms_login(self) -> dict:
        await self._ensure_can_login("sms")
        if self.login is not None and self.running:
            return {"status": "code_sent", "phone": self.phone, "alias": self.alias}

        await self.stop()
        self.login = LoginController("sms", on_change=self._on_login_step)
        flow = SmsAuthFlow(RestSmsCodeProvider(self.login), RestPasswordProvider(self.login))
        self._spawn("tcp", store=PgSessionStore(self.account_id, "tcp"), auth_flow=flow, one_shot=True)
        await self.wait_until(lambda: self.state == "awaiting_code" or not self.running)
        if self.state != "awaiting_code":
            raise MaxLoginConflict(self.last_error or "failed to request SMS code", self.state)
        return {"status": "code_sent", "phone": self.phone, "alias": self.alias}

    async def submit_code(self, code: str | None, password: str | None) -> tuple[int, dict]:
        """POST /auth/code. Returns (http_status, body)."""
        login = self.login
        if login is None or not self.running or login.step not in ("awaiting_code", "awaiting_password"):
            raise MaxLoginConflict("no login in progress", self.state)

        if login.step == "awaiting_code":
            if not code:
                return 400, {"error": "code required"}
            login.submit_code(code)
            await self.wait_until(lambda: self.state in ("authorized", "awaiting_password") or not self.running)
            if self.state == "awaiting_password" and password:
                return await self._submit_password(login, password)
            return self._login_result(login, wrong="invalid code")

        if not password:
            return 400, {"error": "password required", "attempts_left": login.password_attempts_left}
        return await self._submit_password(login, password)

    async def _submit_password(self, login: LoginController, password: str) -> tuple[int, dict]:
        before = login.password_attempts
        login.submit_password(password)
        await self.wait_until(
            lambda: self.state == "authorized"
            or not self.running
            or (self.state == "awaiting_password" and login.password_attempts > before and login.password_pending)
        )
        return self._login_result(login, wrong="invalid password")

    def _login_result(self, login: LoginController, *, wrong: str) -> tuple[int, dict]:
        if self.state == "authorized":
            return 200, {
                "status": "authorized",
                "alias": self.alias,
                "user_id": self.user_id,
                "username": self.username,
            }
        if self.state == "awaiting_password" and self.running:
            if login.password_attempts == 0:
                return 200, {"status": "2fa_required", "alias": self.alias, "hint": login.password_hint}
            return 400, {
                "error": login.last_error or wrong,
                "attempts_left": login.password_attempts_left,
            }
        # The login ended (PyMax rejected the code, attempts/time ran out).
        error = wrong if wrong == "invalid code" else (self.last_error or wrong)
        return 400, {"error": error, "detail": self.last_error, "state": self.state}

    # -- session import / logout ------------------------------------------

    async def import_session(self, raw: str) -> dict:
        """POST /auth/session: log in with an exported session; persist only on success."""
        try:
            transport, info = parse_session_json(raw)
        except SessionFormatError as exc:
            return {"status": "error", "detail": f"Session is invalid: {exc}"}

        await self.stop()
        self.login = None
        self._spawn(transport, store=SeededStore(info), auth_flow=None, one_shot=True)
        await self.wait_until(lambda: self.state == "authorized" or not self.running)
        if self.state == "authorized":
            return {"status": "authorized", "alias": self.alias, "user_id": self.user_id, "username": self.username}

        await self.stop()
        await self.start()  # back to whatever was stored before (nothing was overwritten)
        return {"status": "error", "detail": "Session is invalid or expired"}

    async def logout(self) -> dict:
        client = self.client
        if client is not None:
            try:
                await client.logout()
            except Exception:  # noqa: BLE001 — local cleanup must happen anyway
                log.warning("max[%s]: remote logout failed", self.alias, exc_info=True)
        await self.stop()
        await deactivate_session(self.account_id)
        self.profile = None
        self.transport = None
        self._set_state("stopped")
        return {"status": "logged_out", "alias": self.alias}

    # -- read-only views ---------------------------------------------------

    @property
    def user_id(self) -> int | None:
        contact = getattr(self.profile, "contact", None)
        return getattr(contact, "id", None)

    @property
    def username(self) -> str | None:
        contact = getattr(self.profile, "contact", None)
        link = getattr(contact, "link", None)
        return str(link) if link is not None else None

    async def auth_status(self) -> dict:
        return {
            "connected": self.connected,
            "phone_number": self.phone,
            "user_id": self.user_id,
            "username": self.username,
        }

    async def me(self) -> dict:
        if self.state != "authorized" or self.profile is None:
            raise MaxSessionUnavailable(self.alias, self.state)
        contact = self.profile.contact
        names = getattr(contact, "names", None) or []
        first = names[0] if names else None
        phone = getattr(contact, "phone", None)
        return {
            "user_id": contact.id,
            "username": self.username,
            "first_name": getattr(first, "first_name", None) or getattr(first, "name", None),
            "last_name": getattr(first, "last_name", None),
            "phone": f"+{phone}" if phone else None,
            "is_premium": False,
            "is_verified": False,
            "is_bot": False,
            "dc_id": None,
            "lang_code": None,
        }

    def runtime(self) -> dict:
        def iso(dt: datetime | None) -> str | None:
            return dt.isoformat().replace("+00:00", "Z") if dt else None

        return {
            "state": self.state,
            "connected": self.connected,
            "authorized": self.state == "authorized",
            "transport": self.transport,
            "proxy": bool(self.settings.proxy_url),
            "last_event_at": iso(self.last_event_at),
            "last_catchup_at": iso(self.last_catchup_at),
            "catchup_backlog_chats": self.catchup_backlog_chats,
            "last_error": self.last_error,
            "pymax": PYMAX_VERSION,
        }


class _NoInteractiveLogin:
    """Auth flow for token sessions: PyMax must never fall back to an interactive login."""

    async def authenticate(self, app: Any) -> Any:
        raise MaxAuthError("stored session missing; interactive login required")
