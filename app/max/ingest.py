"""Event ingest for one MAX session (ADR §2.E, §2.G).

Every server-pushed frame with a journaled opcode goes through on_frame BEFORE
PyMax dispatches it (frame hook of the adapter client, session.py):
  1. the frame is written to max_raw_events (normalized=false);
  2. it is parsed with the same PyMax models PyMax would use and upserted;
  3. the raw row is marked normalized=true — or keeps normalized=false with the
     error if parsing/upsert failed, so nothing is lost when PyMax's models lag
     behind the protocol.
PyMax's own typed handlers are not used: in 2.4.1 the dispatcher runs typed
handlers first and on_raw last, and a frame whose typed mapping raises never
reaches on_raw at all.

Every step is guarded: an exception here never propagates into PyMax or the
supervisor.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app import database
from app.max import normalize, repo
from app.max.store_new import NewMaxStore
from app.max.stream import max_stream_manager
from app.services.stream_service import stream_manager
from app.services.tg_store import new_mode, shadow
from app.utils.raw import jsonable

if TYPE_CHECKING:
    from app.max.session import MaxSession

log = logging.getLogger(__name__)

_ERROR_MAX_LEN = 2000
FILL_ASKS_PER_SESSION = 2


class MaxIngest:
    def __init__(self, session: "MaxSession") -> None:
        self.session = session
        self._client: Any = None
        self._known_users: set[int] = set()
        self._locks: dict[int | None, asyncio.Lock] = {}
        self._fills = 0
        # get_users asks per user in this session: the first sight of a stub plus one
        # retry if it is still without a name (get_users failed, Р-11), then not again.
        self._fill_asks: dict[int, int] = {}

    def install(self) -> None:
        self.session.client_hooks.append(self.attach)
        self.session.authorized_hooks.append(self.on_authorized)

    def attach(self, client: Any) -> None:
        self._client = client
        client.add_frame_hook(self.on_frame)

    @property
    def me_id(self) -> int | None:
        me = getattr(self._client, "me", None)
        contact = getattr(me, "contact", None)
        return getattr(contact, "id", None)

    @property
    def new(self) -> NewMaxStore | None:
        """STORAGE_MODE=new: the account's tables are primary, max_* a shadow."""
        return NewMaxStore(self.session.account_id) if new_mode() else None

    async def _broadcast(self, data: dict) -> None:
        if new_mode():
            await stream_manager.broadcast(data, self.session.account_id)
        else:
            await max_stream_manager.broadcast(data)

    # -- frames --------------------------------------------------------------

    async def on_frame(self, opcode: int, cmd: int, payload: dict | None) -> None:
        if cmd != normalize.SERVER_PUSH or opcode not in normalize.JOURNALED_OPCODES:
            return
        if new_mode() and not getattr(self.session, "identity_ok", False):
            # Not yet confirmed (or mismatched) profile: nothing goes into the
            # account's tables; the gap fill after authorization catches up.
            log.warning("max[%s]: frame opcode=%s before identity check — dropped", self.session.alias, opcode)
            return
        payload = payload or {}
        # PyMax runs every inbound frame in its own task (connection.py), so frames
        # of one chat would race: a delete processed before the insert of its
        # message was lost (QA D-1). The chat lock is taken before the first await,
        # and tasks start in arrival order, so asyncio.Lock's FIFO keeps the
        # server's order per chat; different chats still run concurrently.
        async with self._chat_lock(normalize.frame_chat_id(opcode, payload)):
            await self._process_frame(opcode, payload)

    def _chat_lock(self, chat_id: int | None) -> asyncio.Lock:
        lock = self._locks.get(chat_id)
        if lock is None:
            lock = self._locks[chat_id] = asyncio.Lock()
        return lock

    async def _process_frame(self, opcode: int, payload: dict) -> None:
        received_at = datetime.now(timezone.utc)
        self.session.last_event_at = received_at
        new = self.new
        # The journal is jsonb: binary fields as {"_bytes": n} (D16 B); parsing gets the frame as is.
        journaled = jsonable(payload)
        new_raw_id = None
        if new is not None:
            try:
                new_raw_id = await new.journal(opcode, journaled)
            except Exception:  # noqa: BLE001
                log.exception("max[%s]: failed to journal frame opcode=%s", self.session.alias, opcode)
        raw_id = None
        if new is None or self._shadow_on():
            try:
                async with database.async_session() as db:
                    raw_id = await repo.insert_raw_event(db, self.session.account_id, opcode, journaled)
                    await db.commit()
            except Exception:  # noqa: BLE001
                log.log(logging.WARNING if new else logging.ERROR,
                        "max[%s]: failed to journal frame opcode=%s", self.session.alias, opcode,
                        exc_info=True)

        error: str | None = None
        normalized = False
        try:
            if opcode in (normalize.OP_MESSAGE, normalize.OP_EDIT):
                error = await self._on_message(payload, received_at, is_edit=opcode == normalize.OP_EDIT)
            elif opcode == normalize.OP_DELETE:
                chat_id, ids = normalize.parse_delete(payload)
                error = await self._on_delete(chat_id, ids, received_at)
            elif opcode == normalize.OP_CHAT:
                await self._on_chat(payload)
            normalized = True
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"[:_ERROR_MAX_LEN]
            log.warning("max[%s]: frame opcode=%s not normalized: %s", self.session.alias, opcode, error)

        if new_raw_id is not None:
            try:
                await new.finish_journal(new_raw_id, normalized=normalized, error=error)
            except Exception:  # noqa: BLE001
                log.exception("max[%s]: failed to update raw event %s", self.session.alias, new_raw_id)
        if raw_id is not None:
            try:
                async with database.async_session() as db:
                    await repo.finish_raw_event(db, raw_id, normalized=normalized, error=error)
                    await db.commit()
            except Exception:  # noqa: BLE001
                log.exception("max[%s]: failed to update raw event %s", self.session.alias, raw_id)

    async def _on_message(self, payload: dict, received_at: datetime, *, is_edit: bool) -> str | None:
        msg = normalize.parse_message(payload)
        if str(getattr(msg.status, "value", msg.status)) == "REMOVED" and msg.chat_id is not None:
            return await self._on_delete(msg.chat_id, [msg.id], received_at)
        event = "edit" if is_edit or str(getattr(msg.status, "value", msg.status)) == "EDITED" else "message"
        row = await self.store_message(msg, received_at=received_at, is_edit=event == "edit")
        await self._broadcast(self._event_payload(event, row))
        return None

    async def store_message(self, msg: Any, *, received_at: datetime | None = None, is_edit: bool = False,
                            chat_id: int | None = None, advance_cursor: bool | None = None) -> dict:
        """Upsert one PyMax message (live event, history page). Returns the row.

        newest_time_ms means "everything up to here, without gaps" (ADR §2.J).
        History paths pass advance_cursor=True. A live event (None) moves it only
        for a chat already caught up in the current connection; otherwise it
        stores the message but leaves max_sync_state alone (and does not create
        it, so a never-seen chat still gets its catch-up seed).
        """
        cid = msg.chat_id if msg.chat_id is not None else chat_id
        if advance_cursor is None:
            advance_cursor = cid in self.session.caught_up
        new = self.new
        if new is None:
            return await self._store_legacy(msg, cid, received_at, is_edit, advance_cursor)

        chat_type = await new.chat_type(cid) if cid is not None else None
        row, media = normalize.normalize_message(
            msg, me_id=self.me_id, chat_id=cid, chat_type=chat_type, received_at=received_at, is_edit=is_edit,
        )
        missing = await new.store_message(
            row, media,
            chat_type=chat_type or normalize.guess_chat_type(msg, row["chat_id"]),
            advance_cursor=advance_cursor, time_ms=msg.time,
        )
        await shadow("max message", lambda: self._store_legacy(
            msg, cid, received_at, is_edit, advance_cursor, fill=False
        ))
        self._fill_once(missing)
        return row

    def _fill_once(self, user_ids: set[int]) -> None:
        todo = {u for u in user_ids if self._fill_asks.get(u, 0) < FILL_ASKS_PER_SESSION}
        for u in todo:
            self._fill_asks[u] = self._fill_asks.get(u, 0) + 1
        if todo:
            self._spawn(self.fill_users(todo))

    async def store_sent(self, msg: Any, chat_id: int, *, is_edit: bool = False) -> dict:
        """A message we just sent or edited (PR-7): stored at once, not on the echo.

        Under the chat lock of the frames, so the echo of the same message is
        an upsert after it, not a race.
        """
        async with self._chat_lock(chat_id):
            return await self.store_message(msg, received_at=datetime.now(timezone.utc), is_edit=is_edit,
                                            chat_id=chat_id)

    async def apply_delete(self, chat_id: int, ids: list[int]) -> None:
        """Messages we just deleted (PR-7): soft delete as for a delete frame."""
        async with self._chat_lock(chat_id):
            await self._on_delete(chat_id, ids, datetime.now(timezone.utc))

    async def _store_legacy(self, msg: Any, cid: int | None, received_at: datetime | None, is_edit: bool,
                            advance_cursor: bool, *, fill: bool = True) -> dict:
        async with database.async_session() as db:
            chat = await repo.get_chat(db, cid) if cid is not None else None
            row, media = normalize.normalize_message(
                msg,
                me_id=self.me_id,
                chat_id=cid,
                chat_type=chat.chat_type if chat is not None else None,
                received_at=received_at,
                is_edit=is_edit,
            )
            if chat is None:
                await repo.ensure_chat(db, row["chat_id"], normalize.guess_chat_type(msg, row["chat_id"]))
            missing = await repo.ensure_users(db, {row["from_user_id"]} - self._known_users)
            await repo.upsert_message(db, row, media)
            await repo.bump_chat_last_message(db, row["chat_id"], row["message_id"], row["tg_date"])
            if advance_cursor:
                await repo.advance_cursor(db, row["chat_id"], row["message_id"], msg.time)
            await db.commit()
        if row["from_user_id"] is not None:
            self._known_users.add(row["from_user_id"])
        if missing and fill:
            self._spawn(self.fill_users(missing))
        return row

    async def _on_delete(self, chat_id: int, ids: list[int], received_at: datetime) -> str | None:
        new = self.new
        if new is not None:
            found = await new.mark_deleted(chat_id, ids, received_at)
            await shadow("max delete", lambda: self._delete_legacy(chat_id, ids, received_at))
        else:
            found = await self._delete_legacy(chat_id, ids, received_at)
        await self._broadcast(
            {"event": "delete", "platform": "max", "chat_id": chat_id, "message_ids": ids}
        )
        unknown = set(ids) - found
        # No tombstones for messages we never had (ADR §2.G).
        return "delete_unknown_message" if unknown else None

    async def _delete_legacy(self, chat_id: int, ids: list[int], received_at: datetime) -> set[int]:
        async with database.async_session() as db:
            found = await repo.mark_deleted(db, chat_id, ids, received_at)
            await db.commit()
        return found

    async def _on_chat(self, payload: dict) -> None:
        chat = normalize.parse_chat(payload)
        new = self.new
        if new is not None:
            await new.upsert_chats([normalize.normalize_chat(chat)])
            await shadow("max chat", lambda: self._chat_legacy(chat))
        else:
            await self._chat_legacy(chat)

    async def _chat_legacy(self, chat: Any) -> None:
        async with database.async_session() as db:
            await repo.upsert_chat(db, normalize.normalize_chat(chat))
            await db.commit()

    # -- login snapshot / users ---------------------------------------------

    async def on_authorized(self, client: Any) -> None:
        """Chats and contacts from the login response; then the names of unknown dialog peers.

        new storage: peers of private chats without a name are fetched in the
        background (get_users) — their names become the dialog titles (Р-4).
        """
        self._fill_asks = {}
        me_id = self.me_id
        chats_ = list(getattr(client, "chats", None) or [])
        contacts = [c for c in (getattr(client, "contacts", None) or []) if c is not None]
        me = getattr(getattr(client, "me", None), "contact", None)
        users = contacts + ([me] if me is not None else [])
        new = self.new
        if new is not None:
            await new.upsert_chats([normalize.normalize_chat(c) for c in chats_])
            await new.upsert_users([normalize.normalize_user(u) for u in contacts], "contact", is_contact=True)
            if me is not None:
                await new.upsert_users([normalize.normalize_user(me)], "dialog")
            await shadow("max login", lambda: self._authorized_legacy(chats_, users, me_id))
            self._fill_once(await new.dialog_peers_missing())
        else:
            await self._authorized_legacy(chats_, users, me_id)
        self._known_users.update(u.id for u in users)

    async def _authorized_legacy(self, chats_: list, users: list, me_id: int | None) -> None:
        async with database.async_session() as db:
            for chat in chats_:
                await repo.upsert_chat(db, normalize.normalize_chat(chat))
            await repo.upsert_users(db, [normalize.normalize_user(u, me_id=me_id) for u in users])
            await db.commit()

    async def fill_users(self, user_ids: set[int]) -> None:
        """Fetch unknown senders in one batch and fill their stub rows (best effort)."""
        client = self._client
        if client is None or not user_ids:
            return
        try:
            users = [u for u in await client.get_users(sorted(user_ids)) if u is not None]
            new = self.new
            if new is not None:
                await new.upsert_users([normalize.normalize_user(u) for u in users], "message")
                await shadow("max users", lambda: self._users_legacy(users))
            else:
                await self._users_legacy(users)
        except Exception:  # noqa: BLE001
            log.warning("max[%s]: failed to fetch users %s", self.session.alias, sorted(user_ids), exc_info=True)

    async def _users_legacy(self, users: list) -> None:
        async with database.async_session() as db:
            await repo.upsert_users(db, [normalize.normalize_user(u, me_id=self.me_id) for u in users])
            await db.commit()

    def _shadow_on(self) -> bool:
        from app.config import settings

        return settings.shadow_legacy_write

    def _spawn(self, coro) -> None:  # noqa: ANN001
        """Tied to the session: stop() cancels it, a stopped session writes nothing."""
        self._fills += 1
        self.session.spawn_background(f"fill-users-{self._fills}", coro)

    @staticmethod
    def _event_payload(event: str, row: dict) -> dict:
        """Same keys as the TG stream payload + platform (API spec §3, stream_messages)."""
        return {
            "event": event,
            "platform": "max",
            "message_id": row["message_id"],
            "chat_id": row["chat_id"],
            "from_user_id": row["from_user_id"],
            "sender_chat_id": row["sender_chat_id"],
            "text": row["text"],
            "message_type": row["message_type"],
            "tg_date": row["tg_date"].isoformat() if row["tg_date"] else None,
            "is_outgoing": row["is_outgoing"],
            "is_edited": row["is_edited"],
        }
