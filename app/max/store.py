"""PgSessionStore — PyMax StoreProtocol on top of account_sessions (ADR §2.I).

The session lives in account_sessions.session_plaintext as JSON v1:

    {"v":1,"transport":"web","token":"…","device_id":"…","mt_instance_id":"…",
     "phone":"+7…","user_agent":{…},"sync":{…},"pymax":"2.4.1"}

No files in the container: PyMax's SQLite store is never used. Every write
(save_session, update_token, delete_session) commits in its own transaction, so
token rotation and sync markers survive a redeploy.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

import pymax
from pymax.session import StoreProtocol
from pymax.session.models import SessionInfo
from sqlalchemy import select

from app import database
from app.models import AccountSession

log = logging.getLogger(__name__)

SESSION_FORMAT_VERSION = 1
TRANSPORTS = ("web", "tcp")


class SessionFormatError(ValueError):
    """session_plaintext is not a MAX session JSON v1 (or of another transport)."""


def session_to_json(info: SessionInfo, transport: str) -> str:
    data: dict[str, Any] = {"v": SESSION_FORMAT_VERSION, "transport": transport}
    data.update(info.model_dump(mode="json"))
    data["pymax"] = pymax.__version__
    return json.dumps(data, ensure_ascii=False)


def parse_session_json(raw: str, expected_transport: str | None = None) -> tuple[str, SessionInfo]:
    """(transport, SessionInfo) from JSON v1. Raises SessionFormatError."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SessionFormatError("session is not valid JSON") from exc
    if not isinstance(data, dict) or data.get("v") != SESSION_FORMAT_VERSION:
        raise SessionFormatError("unsupported session format version")
    transport = data.get("transport")
    if transport not in TRANSPORTS:
        raise SessionFormatError(f"unknown transport {transport!r}")
    if expected_transport is not None and transport != expected_transport:
        raise SessionFormatError(f"session transport {transport!r} != {expected_transport!r}")
    if not data.get("token") or not data.get("device_id"):
        raise SessionFormatError("session must contain token and device_id")
    fields = {k: v for k, v in data.items() if k not in ("v", "transport", "pymax")}
    try:
        return transport, SessionInfo.model_validate(fields)
    except ValueError as exc:
        raise SessionFormatError(f"invalid session fields: {exc}") from exc


async def load_stored_transport(account_id: int) -> str | None:
    """Transport of the active stored session, or None (no session / not MAX JSON)."""
    raw = await _load_raw(account_id)
    if raw is None:
        return None
    try:
        transport, _ = parse_session_json(raw)
    except SessionFormatError:
        log.warning("max store: account %s has an unreadable session, ignoring it", account_id)
        return None
    return transport


async def _load_raw(account_id: int) -> str | None:
    async with database.async_session() as db:
        row = (
            await db.execute(
                select(AccountSession.session_plaintext).where(
                    AccountSession.account_id == account_id,
                    AccountSession.is_active == True,  # noqa: E712
                )
            )
        ).scalar_one_or_none()
    return row


class PgSessionStore(StoreProtocol):
    """One active MAX session per account, fixed transport (ADR §2.H)."""

    def __init__(self, account_id: int, transport: str) -> None:
        if transport not in TRANSPORTS:
            raise ValueError(f"unknown transport {transport!r}")
        self.account_id = account_id
        self.transport = transport

    async def save_session(self, session_info: SessionInfo) -> None:
        payload = session_to_json(session_info, self.transport)
        now = datetime.now(timezone.utc)
        async with database.async_session() as db:
            row = (
                await db.execute(
                    select(AccountSession).where(
                        AccountSession.account_id == self.account_id,
                        AccountSession.is_active == True,  # noqa: E712
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                db.add(
                    AccountSession(
                        account_id=self.account_id,
                        session_plaintext=payload,
                        authorized_at=now,
                        last_connected_at=now,
                        is_active=True,
                    )
                )
            else:
                if row.session_plaintext:
                    try:
                        _, old = parse_session_json(row.session_plaintext)
                    except SessionFormatError:
                        old = None
                    if old is None or old.token != session_info.token:
                        row.authorized_at = now
                row.session_plaintext = payload
                row.last_connected_at = now
            await db.commit()

    async def update_token(self, old_token: str, new_token: str, /) -> None:
        current = await self.load_session()
        if current is None or current.token != old_token:
            log.warning("max store: update_token for a token that is not the active one, ignored")
            return
        await self.save_session(current.model_copy(update={"token": new_token}))

    async def load_session(self) -> SessionInfo | None:
        raw = await _load_raw(self.account_id)
        if raw is None:
            return None
        try:
            _, info = parse_session_json(raw, expected_transport=self.transport)
        except SessionFormatError as exc:
            log.warning("max store: account %s session ignored: %s", self.account_id, exc)
            return None
        return info

    async def load_session_by_device_id(self, device_id: str) -> SessionInfo | None:
        info = await self.load_session()
        return info if info is not None and info.device_id == device_id else None

    async def load_session_by_phone(self, phone: str) -> SessionInfo | None:
        info = await self.load_session()
        return info if info is not None and info.phone == phone else None

    async def delete_session(self, token: str, /) -> None:
        """Deactivate (not delete) the active row if it holds this token."""
        info = await self.load_session()
        if info is None or info.token != token:
            return
        await deactivate_session(self.account_id)

    async def close(self) -> None:
        return None


async def deactivate_session(account_id: int) -> None:
    """is_active=false, session_plaintext=NULL — same as TG _clear_session_in_db."""
    async with database.async_session() as db:
        row = (
            await db.execute(
                select(AccountSession).where(
                    AccountSession.account_id == account_id,
                    AccountSession.is_active == True,  # noqa: E712
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            row.session_plaintext = None
            row.is_active = False
            await db.commit()


class SeededStore(StoreProtocol):
    """In-memory store pre-filled with an imported session (POST /auth/session).

    The import is tried against MAX first; only a session that logged in is
    written to account_sessions (API spec §2.5). After promote() every write
    goes through to the target store, so token rotation on the already
    connected client is persisted too.
    """

    def __init__(self, info: SessionInfo) -> None:
        self.session: SessionInfo | None = info
        self.target: PgSessionStore | None = None

    @property
    def promoted(self) -> bool:
        return self.target is not None

    async def promote(self, target: PgSessionStore) -> PgSessionStore:
        if self.session is not None:
            await target.save_session(self.session)
        self.target = target
        return target

    async def save_session(self, session_info: SessionInfo) -> None:
        self.session = session_info
        if self.target is not None:
            await self.target.save_session(session_info)

    async def update_token(self, old_token: str, new_token: str, /) -> None:
        if self.session is not None and self.session.token == old_token:
            self.session = self.session.model_copy(update={"token": new_token})
        if self.target is not None:
            await self.target.update_token(old_token, new_token)

    async def load_session(self) -> SessionInfo | None:
        return self.session

    async def load_session_by_device_id(self, device_id: str) -> SessionInfo | None:
        return self.session if self.session and self.session.device_id == device_id else None

    async def load_session_by_phone(self, phone: str) -> SessionInfo | None:
        return self.session if self.session and self.session.phone == phone else None

    async def delete_session(self, token: str, /) -> None:
        if self.session is not None and self.session.token == token:
            self.session = None
        if self.target is not None:
            await self.target.delete_session(token)

    async def close(self) -> None:
        return None
