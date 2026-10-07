"""FakePyMaxClient — a MAX server + PyMax client stand-in for adapter tests (no network).

The fake replaces the client factory of app.max.session. It drives the REAL auth
flows we hand it (RestQrAuthFlow / SmsAuthFlow from PyMax with our REST
providers) against a fake `app.api.auth`, and talks to the REAL store we hand it
(PgSessionStore / SeededStore), so login, token rotation and persistence are
exercised end to end. Only the wire protocol is faked.
"""

import asyncio
import time
from types import SimpleNamespace

from pymax import ApiError
from pymax.session.models import SessionInfo
from pymax.types.domain.auth import (
    CheckCodeResponse,
    CheckPasswordResponse,
    CheckQrResponse,
    RequestQrResponse,
    StartAuthResponse,
)
from pymax.types import Chat, User
from pymax.types.domain.profile import Profile
from pymax.types.domain.sync import SyncState

LOGIN_OPCODE = 19


class FakeMaxServer:
    """Server-side state shared by every client a test builds."""

    def __init__(self) -> None:
        self.user_id = 4242
        self.issued = 0
        self.revoked: set[str] = set()
        self.banned = False
        self.rotate_token = False
        self.connect_errors: list[BaseException] = []

        # QR
        self.qr_ttl_ms = 60_000
        self.qr_confirmed = False
        self.qr_requests = 0
        # 2FA
        self.password: str | None = None
        self.password_hint = "pet name"
        self.password_checks: list[str] = []
        # SMS
        self.sms_code = "12345"
        self.code_requests = 0

        # Login snapshot and user directory
        self.chats: list[dict] = []
        self.contacts: list[dict] = []
        self.users: dict[int, dict] = {}
        self.get_users_calls: list[list[int]] = []

        self.factory_calls: list[dict] = []
        self.clients: list["FakePyMaxClient"] = []
        self.authenticate_calls = 0
        self.logged_out = False

    def new_token(self) -> str:
        self.issued += 1
        return f"tok-{self.issued}"

    # PyMax client factory signature (app.max.session.build_client)
    def factory(self, *, transport, phone, extra_config, auth_flow):
        self.factory_calls.append(
            {"transport": transport, "phone": phone, "extra_config": extra_config, "auth_flow": auth_flow}
        )
        client = FakePyMaxClient(self, transport, phone, extra_config, auth_flow)
        self.clients.append(client)
        return client


class _FakeAuthApi:
    def __init__(self, server: FakeMaxServer) -> None:
        self.s = server

    def _login_or_password(self) -> CheckCodeResponse:
        if self.s.password:
            return CheckCodeResponse.model_validate(
                {"passwordChallenge": {"trackId": "pw-track", "hint": self.s.password_hint}}
            )
        return CheckCodeResponse.model_validate({"tokenAttrs": {"LOGIN": {"token": self.s.new_token()}}})

    async def request_qr(self) -> RequestQrResponse:
        self.s.qr_requests += 1
        return RequestQrResponse(
            qr_link=f"https://max.ru/:auth/qr-{self.s.qr_requests}",
            track_id=f"track-{self.s.qr_requests}",
            polling_interval=20,
            ttl=self.s.qr_ttl_ms,
            expires_at=int(time.time() * 1000) + self.s.qr_ttl_ms,
        )

    async def check_qr(self, track_id: str) -> CheckQrResponse:
        return CheckQrResponse.model_validate(
            {"status": {"expiresAt": 0, "loginAvailable": self.s.qr_confirmed}}
        )

    async def confirm_qr(self, track_id: str) -> CheckCodeResponse:
        return self._login_or_password()

    async def check_password(self, track_id: str, password: str) -> CheckPasswordResponse:
        self.s.password_checks.append(password)
        if password != self.s.password:
            # Every other wrong answer as an ApiError — PyMax handles both shapes.
            if len(self.s.password_checks) % 2:
                return CheckPasswordResponse(error="password.invalid")
            raise ApiError(opcode=115, error="password.invalid", message="Invalid password")
        return CheckPasswordResponse.model_validate({"tokenAttrs": {"LOGIN": {"token": self.s.new_token()}}})

    async def request_code(self, phone: str) -> StartAuthResponse:
        self.s.code_requests += 1
        return StartAuthResponse(
            token="sms-track", code_length=5, request_max_duration=60,
            request_count_left=3, alt_action_duration=60,
        )

    async def send_code(self, token: str, code: str) -> CheckCodeResponse:
        if code != self.s.sms_code:
            raise ApiError(opcode=18, error="verify.code.wrong", message="Wrong code")
        return self._login_or_password()


class FakePyMaxClient:
    def __init__(self, server: FakeMaxServer, transport, phone, extra_config, auth_flow) -> None:
        self.server = server
        self.transport = transport
        self.extra_config = extra_config
        self.auth_flow = auth_flow
        self.app = SimpleNamespace(
            api=SimpleNamespace(auth=_FakeAuthApi(server)),
            config=SimpleNamespace(phone=phone, password_max_attempts=extra_config.password_max_attempts),
        )
        self.me = None
        self.chats: list = []
        self.contacts: list = []
        self.is_connected = False
        self._frame_hooks: list = []
        self.closed = False
        self.calls: list[str] = []  # every network-ish method called on the client
        self._closed = asyncio.Event()

    async def connect(self) -> None:
        self.calls.append("connect")
        if self.server.connect_errors:
            raise self.server.connect_errors.pop(0)
        store = self.extra_config.store
        session = await store.load_session()
        if session is None:
            self.server.authenticate_calls += 1
            result = await self.auth_flow.authenticate(self.app)
            if not result.token:
                raise RuntimeError("Authentication failed: no token received")
            session = SessionInfo(token=result.token, device_id="dev-1", phone=self.app.config.phone or "",
                                  mt_instance_id="mt-1")
            await store.save_session(session)

        # login
        if session.token in self.server.revoked:
            raise ApiError(opcode=LOGIN_OPCODE, error="FAIL_LOGIN_TOKEN", message="token revoked")
        if self.server.banned:
            raise ApiError(opcode=LOGIN_OPCODE, error="account.blocked", message="Account is blocked")
        if self.server.rotate_token:
            new = self.server.new_token()
            await store.update_token(session.token, new)
            session = session.model_copy(update={"token": new})
        # login also refreshes sync markers (pymax AuthService._update_session)
        await store.save_session(session.model_copy(update={"sync": SyncState(chats_sync=777)}))

        self.me = Profile.model_validate(
            {"contact": {"id": self.server.user_id, "names": [{"firstName": "Сергей", "lastName": "С"}],
                         "link": "sergey", "phone": 79001112233}}
        )
        self.chats = [Chat.model_validate(c) for c in self.server.chats]
        self.contacts = [User.model_validate(u) for u in self.server.contacts]
        self.is_connected = True

    # -- adapter surface (app.max.session._AdapterMixin) --------------------

    def add_frame_hook(self, hook) -> None:
        self._frame_hooks.append(hook)

    async def push(self, frame: dict) -> None:
        """Deliver a server frame the way the adapter's frame hook sees it."""
        for hook in self._frame_hooks:
            await hook(frame["opcode"], frame.get("cmd", 0), frame.get("payload"))

    async def get_users(self, user_ids: list[int]) -> list:
        self.calls.append("get_users")
        self.server.get_users_calls.append(list(user_ids))
        return [User.model_validate(self.server.users[i]) for i in user_ids if i in self.server.users]

    async def wait_closed(self) -> None:
        await self._closed.wait()

    def drop(self) -> None:
        """Simulate a network drop."""
        self.is_connected = False
        self._closed.set()

    async def close(self) -> None:
        self.calls.append("close")
        self.closed = True
        self.is_connected = False
        self._closed.set()

    async def logout(self) -> bool:
        self.calls.append("logout")
        self.server.logged_out = True
        return True

    # Anything that would change what the MAX side sees ("read", presence, typing)
    async def read_message(self, *a, **kw):  # pragma: no cover - must never be called
        self.calls.append("read_message")

    def set_presence(self, *a, **kw):  # pragma: no cover - must never be called
        self.calls.append("set_presence")
