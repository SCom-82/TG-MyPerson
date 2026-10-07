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
from app.max.stream import max_stream_manager

if TYPE_CHECKING:
    from app.max.session import MaxSession

log = logging.getLogger(__name__)

_ERROR_MAX_LEN = 2000


class MaxIngest:
    def __init__(self, session: "MaxSession") -> None:
        self.session = session
        self._client: Any = None
        self._known_users: set[int] = set()
        self._locks: dict[int | None, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()

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

    # -- frames --------------------------------------------------------------

    async def on_frame(self, opcode: int, cmd: int, payload: dict | None) -> None:
        if cmd != normalize.SERVER_PUSH or opcode not in normalize.JOURNALED_OPCODES:
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
        try:
            async with database.async_session() as db:
                raw_id = await repo.insert_raw_event(db, self.session.account_id, opcode, payload)
                await db.commit()
        except Exception:  # noqa: BLE001
            log.exception("max[%s]: failed to journal frame opcode=%s", self.session.alias, opcode)
            raw_id = None

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
        await max_stream_manager.broadcast(self._event_payload(event, row))
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
        if missing:
            self._spawn(self.fill_users(missing))
        return row

    async def _on_delete(self, chat_id: int, ids: list[int], received_at: datetime) -> str | None:
        async with database.async_session() as db:
            found = await repo.mark_deleted(db, chat_id, ids, received_at)
            await db.commit()
        await max_stream_manager.broadcast(
            {"event": "delete", "platform": "max", "chat_id": chat_id, "message_ids": ids}
        )
        unknown = set(ids) - found
        # No tombstones for messages we never had (ADR §2.G).
        return "delete_unknown_message" if unknown else None

    async def _on_chat(self, payload: dict) -> None:
        chat = normalize.parse_chat(payload)
        async with database.async_session() as db:
            await repo.upsert_chat(db, normalize.normalize_chat(chat))
            await db.commit()

    # -- login snapshot / users ---------------------------------------------

    async def on_authorized(self, client: Any) -> None:
        """Chats and contacts from the login response (no extra network)."""
        me_id = self.me_id
        async with database.async_session() as db:
            for chat in getattr(client, "chats", None) or []:
                await repo.upsert_chat(db, normalize.normalize_chat(chat))
            contacts = [c for c in (getattr(client, "contacts", None) or []) if c is not None]
            me = getattr(getattr(client, "me", None), "contact", None)
            users = contacts + ([me] if me is not None else [])
            await repo.upsert_users(db, [normalize.normalize_user(u, me_id=me_id) for u in users])
            await db.commit()
        self._known_users.update(u.id for u in users)

    async def fill_users(self, user_ids: set[int]) -> None:
        """Fetch unknown senders in one batch and fill their stub rows (best effort)."""
        client = self._client
        if client is None or not user_ids:
            return
        try:
            users = await client.get_users(sorted(user_ids))
            async with database.async_session() as db:
                await repo.upsert_users(
                    db, [normalize.normalize_user(u, me_id=self.me_id) for u in users if u is not None]
                )
                await db.commit()
        except Exception:  # noqa: BLE001
            log.warning("max[%s]: failed to fetch users %s", self.session.alias, sorted(user_ids), exc_info=True)

    def _spawn(self, coro) -> None:  # noqa: ANN001
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
