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
from pymax.types.domain.member import Member
from pymax.types.domain import Message
from pymax.types.domain.attachments.file import FileRequest
from pymax.types.domain.attachments.video import VideoRequest
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

        # History (chat_id → message dicts with "time"), chat list paging, media
        self.history: dict[int, list[dict]] = {}
        self.history_calls: list[dict] = []
        self.chat_page = 2
        self.fetch_chats_calls: list[int | None] = []
        self.fetch_chats_gate: asyncio.Event | None = None  # set → fetch_chats waits for it
        self.fetch_chats_errors: list[BaseException] = []  # raised one per call, in order
        self.login_chats: list[dict] | None = None  # login snapshot if it differs from fetch_chats
        self.history_errors: dict[int, list[BaseException]] = {}  # chat_id → raised one per call
        self.file_url = ""
        self.video_url = ""
        self.video_not_ready = False

        # Writes (PR-7): every write call in order, errors raised one per write call
        self.writes: list[tuple] = []
        self.write_errors: list[BaseException] = []
        self.next_message_id = 900_000

        # Lookup by phone (plan 2026-10-19 D1): e164 → user dict; errors one per call
        self.phone_book: dict[str, dict] = {}
        self.lookup_calls: list[str] = []
        self.lookup_errors: list[BaseException] = []
        self.add_contact_errors: list[BaseException] = []
        self.added_contacts: list[int] = []

        # Groups (plan 2026-10-19 Р3): chat_id → Chat dict; every group call (name, args)
        self.groups: dict[int, dict] = {}
        self.group_calls: list[tuple] = []
        self.group_errors: list[BaseException] = []
        self.members_page = 2
        self.join_queue: dict[int, list[int]] = {}
        self.next_group_id = -71_000_000_000

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
        self._app = SimpleNamespace(invoke=self._admin_frame)  # raw frames (remove_group_admin)
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
        login_chats = self.server.login_chats if self.server.login_chats is not None else self.server.chats
        self.chats = [Chat.model_validate(c) for c in login_chats]
        self.contacts = [User.model_validate(u) for u in self.server.contacts]
        self.is_connected = True

    # -- adapter surface (app.max.session._AdapterMixin) --------------------

    def add_frame_hook(self, hook) -> None:
        self._frame_hooks.append(hook)

    async def push(self, frame: dict) -> None:
        """Deliver a server frame the way the adapter's frame hook sees it."""
        for hook in self._frame_hooks:
            await hook(frame["opcode"], frame.get("cmd", 0), frame.get("payload"))

    async def fetch_history(self, chat_id: int, forward: int = 0, backward: int = 40, backward_time: int = 0,
                            forward_time: int = 0, from_time: int | None = None, interactive: bool = False,
                            **_: object) -> list:
        self.calls.append("fetch_history")
        self.server.history_calls.append(
            {"chat_id": chat_id, "forward": forward, "backward": backward, "from_time": from_time,
             "interactive": interactive}
        )
        if self.server.history_errors.get(chat_id):
            raise self.server.history_errors[chat_id].pop(0)
        msgs = sorted(self.server.history.get(chat_id, []), key=lambda m: m["time"])
        point = from_time if from_time is not None else int(time.time() * 1000)
        back = [m for m in msgs if m["time"] <= point][-backward:] if backward else []
        fwd = [m for m in msgs if m["time"] >= point][:forward] if forward else []
        picked = {m["id"]: m for m in back + fwd}
        return [Message.model_validate(m) for m in sorted(picked.values(), key=lambda m: m["time"])]

    async def fetch_chats(self, marker: int | None = None) -> list:
        self.calls.append("fetch_chats")
        self.server.fetch_chats_calls.append(marker)
        if self.server.fetch_chats_gate is not None:
            await self.server.fetch_chats_gate.wait()
        if self.server.fetch_chats_errors:
            raise self.server.fetch_chats_errors.pop(0)
        chats = sorted(self.server.chats, key=lambda c: c.get("lastEventTime", 0), reverse=True)
        if marker is not None:
            chats = [c for c in chats if c.get("lastEventTime", 0) < marker]
        return [Chat.model_validate(c) for c in chats[: self.server.chat_page]]

    async def get_message(self, chat_id: int, message_id: int):
        self.calls.append("get_message")
        for m in self.server.history.get(chat_id, []):
            if int(m["id"]) == int(message_id):
                return Message.model_validate({**m, "chatId": chat_id})
        return None

    async def get_file_by_id(self, chat_id: int, message_id: int, file_id: int):
        self.calls.append("get_file_by_id")
        return FileRequest(url=self.server.file_url, unsafe=False)

    async def get_video_by_id(self, chat_id: int, message_id: int, video_id: int):
        self.calls.append("get_video_by_id")
        if self.server.video_not_ready:
            raise ApiError(opcode=83, error="video.not.ready", message="Video is not ready")
        return VideoRequest(url=self.server.video_url)

    async def get_users(self, user_ids: list[int]) -> list:
        self.calls.append("get_users")
        self.server.get_users_calls.append(list(user_ids))
        return [User.model_validate(self.server.users[i]) for i in user_ids if i in self.server.users]

    # -- groups (plan 2026-10-19 Р3) -------------------------------------------

    def _group_call(self, name: str, *args):
        self.calls.append(name)
        self.server.group_calls.append((name, *args))
        if self.server.group_errors:
            raise self.server.group_errors.pop(0)

    def _chat(self, chat_id: int) -> Chat:
        return Chat.model_validate(self.server.groups[chat_id])

    def _group(self, chat_id: int) -> dict:
        if chat_id not in self.server.groups:
            raise ApiError(opcode=48, error="chat.not.found", message="Chat not found")
        return self.server.groups[chat_id]

    async def create_group(self, name: str, participant_ids=None, notify: bool = True):
        self._group_call("create_group", name, list(participant_ids or []), notify)
        self.server.next_group_id -= 1
        cid, now = self.server.next_group_id, int(time.time() * 1000)
        members = [self.server.user_id, *(participant_ids or [])]
        self.server.groups[cid] = {"id": cid, "type": "CHAT", "status": "ACTIVE", "owner": self.server.user_id,
                                   "title": name, "participants": {str(u): now for u in members},
                                   "participantsCount": len(members), "admins": [], "lastEventTime": now}
        msg = self._new_message(cid, None, attaches=[{"_type": "CONTROL", "event": "new", "title": name}])
        return self._chat(cid), msg

    async def invite_users_to_group(self, chat_id: int, user_ids: list[int], show_history: bool = True):
        self._group_call("invite_users_to_group", chat_id, list(user_ids), show_history)
        g = self._group(chat_id)
        for u in user_ids:
            g["participants"][str(u)] = int(time.time() * 1000)
        g["participantsCount"] = len(g["participants"])
        return self._chat(chat_id)

    async def remove_users_from_group(self, chat_id: int, user_ids: list[int], clean_msg_period: int) -> bool:
        self._group_call("remove_users_from_group", chat_id, list(user_ids), clean_msg_period)
        g = self._group(chat_id)
        for u in user_ids:
            g["participants"].pop(str(u), None)
        g["participantsCount"] = len(g["participants"])
        return True

    async def add_admin(self, chat_id: int, user_id: int, permissions: list) -> None:
        mask = 0
        for p in permissions:
            mask |= int(p)
        self._group_call("add_admin", chat_id, user_id, mask)
        g = self._group(chat_id)
        g["admins"] = sorted({*g.get("admins", []), user_id})
        g.setdefault("adminParticipants", {})[str(user_id)] = {"permissions": mask}

    async def _admin_frame(self, opcode, payload: dict):
        self._group_call("invoke", int(opcode), payload)
        g = self._group(payload["chatId"])
        if payload.get("type") == "ADMIN" and payload.get("operation") == "remove":
            g["admins"] = [a for a in g.get("admins", []) if a not in payload["userIds"]]
        return None

    async def change_group_profile(self, chat_id: int, name=None, description=None, photo=None) -> None:
        self._group_call("change_group_profile", chat_id, name, description)
        g = self._group(chat_id)
        if name is not None:
            g["title"] = name
        if description is not None:
            g["description"] = description

    async def change_group_settings(self, chat_id: int, **options) -> None:
        self._group_call("change_group_settings", chat_id, options)
        self._group(chat_id).setdefault("options", {}).update({k.upper(): v for k, v in options.items()})

    async def rework_invite_link(self, chat_id: int):
        self._group_call("rework_invite_link", chat_id)
        g = self._group(chat_id)
        g["link"] = f"https://max.ru/join/new-{len(self.server.group_calls)}"
        return self._chat(chat_id)

    async def get_chat(self, chat_id: int):
        self.calls.append("get_chat")
        self._group(chat_id)
        return self._chat(chat_id)

    async def get_chat_members(self, chat_id: int, marker: int | None = None, count: int = 50):
        self._group_call("get_chat_members", chat_id, marker, count)
        ids = sorted(int(u) for u in self._group(chat_id)["participants"])
        start = marker or 0
        page = ids[start:start + min(count, self.server.members_page)]
        nxt = start + len(page) if start + len(page) < len(ids) else 0
        return [Member.model_validate({"contact": self._user_of(u), "presence": {}}) for u in page], nxt

    def _user_of(self, uid: int) -> dict:
        return self.server.users.get(uid) or {"id": uid, "names": [{"firstName": f"U{uid}"}]}

    async def get_join_requests(self, chat_id: int, count: int = 100):
        self._group_call("get_join_requests", chat_id, count)
        return [Member.model_validate({"contact": self._user_of(u), "presence": {}})
                for u in self.server.join_queue.get(chat_id, [])[:count]]

    async def confirm_join_requests(self, chat_id: int, user_ids: list[int], show_history: bool = True):
        self._group_call("confirm_join_requests", chat_id, list(user_ids), show_history)
        g = self._group(chat_id)
        for u in user_ids:
            g["participants"][str(u)] = 1
        self.server.join_queue[chat_id] = [u for u in self.server.join_queue.get(chat_id, []) if u not in user_ids]
        return self._chat(chat_id)

    async def decline_join_requests(self, chat_id: int, user_ids: list[int]):
        self._group_call("decline_join_requests", chat_id, list(user_ids))
        self.server.join_queue[chat_id] = [u for u in self.server.join_queue.get(chat_id, []) if u not in user_ids]
        return None

    async def leave_group(self, chat_id: int) -> None:
        self._group_call("leave_group", chat_id)

    async def leave_channel(self, chat_id: int) -> None:
        self._group_call("leave_channel", chat_id)

    async def delete_chat(self, chat_id: int, last_event_time=None, for_all: bool = True) -> None:
        self._group_call("delete_chat", chat_id, for_all)
        self.server.groups.pop(chat_id, None)

    async def search_by_phone(self, phone: str):
        self.calls.append("search_by_phone")
        self.server.lookup_calls.append(phone)
        if self.server.lookup_errors:
            raise self.server.lookup_errors.pop(0)
        if phone not in self.server.phone_book:
            raise ApiError(opcode=46, error="contact.not.found", message="Contact not found")
        return User.model_validate(self.server.phone_book[phone])

    async def add_contact(self, contact_id: int):
        self.calls.append("add_contact")
        if self.server.add_contact_errors:
            raise self.server.add_contact_errors.pop(0)
        self.server.added_contacts.append(contact_id)
        return None

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

    # -- writes (PR-7) ---------------------------------------------------------

    def _write(self, name: str, *args) -> None:
        self.calls.append(name)
        if self.server.write_errors:
            raise self.server.write_errors.pop(0)
        self.server.writes.append((name, *args))

    def _new_message(self, chat_id: int, text: str | None, **extra) -> Message:
        self.server.next_message_id += 1
        m = {"id": self.server.next_message_id, "time": int(time.time() * 1000), "type": "USER",
             "text": text or "", "attaches": [], "sender": self.server.user_id, **extra}
        self.server.history.setdefault(chat_id, []).append(m)
        return Message.model_validate({**m, "chatId": chat_id})

    async def send_message(self, chat_id: int, text: str | None = None, reply_to: int | None = None,
                           attachments=None, *, notify: bool = True, send_at=None) -> Message:
        kinds = [type(a).__name__ for a in attachments or []]
        names = [getattr(a, "name", None) or getattr(a, "file_name", None) for a in attachments or []]
        self._write("send_message", chat_id, text, reply_to, kinds, names)
        return self._new_message(chat_id, text)

    async def forward_message(self, chat_id: int, message_id, source_chat_id: int | None = None, *,
                              notify: bool = True) -> Message:
        self._write("forward_message", chat_id, message_id, source_chat_id)
        return self._new_message(chat_id, f"fwd {message_id}")

    async def edit_message(self, chat_id: int, message_id: int, text: str | None = None, attachments=None):
        self._write("edit_message", chat_id, message_id, text)
        for m in self.server.history.get(chat_id, []):
            if int(m["id"]) == int(message_id):
                m.update(text=text, status="EDITED")
                return Message.model_validate({**m, "chatId": chat_id})
        raise ApiError(opcode=67, error="message.not.found", message="Message not found")

    async def delete_message(self, chat_id: int, message_ids: list[int], for_me: bool) -> bool:
        self._write("delete_message", chat_id, list(message_ids), for_me)
        return True

    async def add_reaction(self, chat_id: int, message_id: int, reaction: str):
        self._write("add_reaction", chat_id, message_id, reaction)
        return None

    async def remove_reaction(self, chat_id: int, message_id: int):
        self._write("remove_reaction", chat_id, message_id)
        return None

    # Anything that would change what the MAX side sees ("read", presence, typing):
    # recorded, so the read paths can assert they never call it.
    async def read_message(self, message_id, chat_id: int):
        if not isinstance(message_id, int):  # the live MAX (ticket 4b4ffe3c): then it drops the link
            raise ApiError(opcode=50, error="proto.payload", message="Expected number at 38")
        self._write("read_message", message_id, chat_id)

    def set_presence(self, *a, **kw):  # pragma: no cover - must never be called
        self.calls.append("set_presence")
