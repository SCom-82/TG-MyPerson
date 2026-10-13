"""MAX writes and cursors in the per-account tables (STORAGE_MODE=new, ТЗ 2026-10-13 §5.3).

The adapter between MaxIngest/MaxSync and app/repo. It takes the rows
app/max/normalize.py builds for max_* and maps them onto the core rows, so
normalisation stays one piece of code for both storages. Cursors live in
sync_state: newest_ts/oldest_ts (ms ↔ timestamptz here) plus the message id at
the cursor in newest_/oldest_external_id. Catch-up sees only the account's
own chats.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.max import normalize
from app.models import Chat as ChatModel
from app.models import Message as MessageModel
from app.models import SyncState
from app.models import User as UserModel
from app.repo import chats, messages, raw_events, sync_state, users
from app.repo.rows import ChatRow, MediaRow, MessageRow, UserRow
from app.storage import account_db

PLATFORM = "max"


@dataclass
class Cursor:
    """The attributes MaxSync reads from a max_sync_state row."""

    newest_time_ms: int | None
    newest_message_id: int | None
    oldest_time_ms: int | None
    oldest_message_id: int | None


def _ms(dt: datetime | None) -> int | None:
    return normalize.dt_to_ms(dt)


def _ts(ms: int | None) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else None


def chat_row(row: dict) -> ChatRow:
    return ChatRow(
        external_id=row["id"],
        chat_type=row["chat_type"],
        title=row.get("title"),
        username=row.get("username"),
        description=row.get("description"),
        members_count=row.get("members_count"),
        last_message_external_id=row.get("last_message_id"),
        last_message_at=row.get("last_message_at"),
        raw=row.get("raw_data"),
    )


def user_row(row: dict) -> UserRow:
    return UserRow(
        external_id=row["id"],
        username=row.get("username"),
        first_name=row.get("first_name"),
        last_name=row.get("last_name"),
        phone=row.get("phone"),
        is_bot=bool(row.get("is_bot")),
        raw=row.get("raw_data"),
    )


def message_row(row: dict, media: list[dict]) -> MessageRow:
    return MessageRow(
        external_id=row["message_id"],
        sent_at=row["tg_date"],
        sender_external_id=row["from_user_id"],
        sender_chat_external_id=row["sender_chat_id"],
        reply_to_external_id=row["reply_to_message_id"],
        forward_from_chat_external_id=row["forward_from_chat_id"],
        forward_from_message_external_id=row["forward_from_message_id"],
        message_type=row["message_type"],
        text=row["text"],
        text_html=row["text_html"],
        is_edited=row["is_edited"],
        edited_at=row["edit_date"],
        deleted_at=row.get("deleted_at"),
        views=row["views"],
        raw=row["raw_data"],
        is_outgoing_hint=row["is_outgoing"],
        media=[
            MediaRow(
                file_type=m["file_type"],
                attach_index=m["attach_index"],
                file_external_id=m.get("file_id"),
                file_unique_id=m.get("file_unique_id"),
                file_name=m.get("file_name"),
                file_size=m.get("file_size"),
                mime_type=m.get("mime_type"),
            )
            for m in media
        ],
    )


class NewMaxStore:
    def __init__(self, account_id: int) -> None:
        self.account_id = account_id

    # -- chats / users ------------------------------------------------------------

    async def chat_type(self, chat_id: int) -> str | None:
        async with account_db(self.account_id) as db:
            return (await db.execute(
                select(ChatModel.chat_type)
                .where(ChatModel.account_id == self.account_id, ChatModel.external_id == chat_id)
            )).scalar_one_or_none()

    async def upsert_chats(self, rows: list[dict]) -> None:
        async with account_db(self.account_id) as db:
            for row in rows:
                await chats.upsert_chat(db, self.account_id, chat_row(row))
            await db.commit()

    async def upsert_users(self, rows: list[dict], via: str, *, is_contact: bool | None = None) -> None:
        async with account_db(self.account_id) as db:
            for row in rows:
                u = user_row(row)
                u.is_contact = is_contact
                await users.upsert_user(db, self.account_id, PLATFORM, u, via)
            await db.commit()

    # -- messages -----------------------------------------------------------------

    async def store_message(
        self, row: dict, media: list[dict], *, chat_type: str, advance_cursor: bool, time_ms: int | None
    ) -> set[int]:
        """Message + stub chat/sender; returns sender ids that had no users row yet."""
        a = self.account_id
        missing: set[int] = set()
        async with account_db(a) as db:
            chat_pk = await chats.chat_pk(db, a, row["chat_id"])
            if chat_pk is None:
                chat_pk = await chats.upsert_chat(db, a, ChatRow(row["chat_id"], chat_type))
            sender = row["from_user_id"]
            if sender is not None and await users.user_pk(db, a, sender) is None:
                await users.upsert_user(db, a, PLATFORM, UserRow(external_id=sender), "message")
                missing.add(sender)
            await messages.upsert_message(db, a, chat_pk, message_row(row, media))
            if advance_cursor and time_ms is not None:
                await self._advance(db, chat_pk, row["message_id"], time_ms)
            await db.commit()
        return missing

    async def _advance(self, db, chat_pk: int, message_id: int, time_ms: int) -> None:
        state = await sync_state.get_state(db, self.account_id, chat_pk)
        if state is None:
            await sync_state.upsert_state(
                db, self.account_id, chat_pk,
                newest_ts=_ts(time_ms), newest_external_id=message_id,
                oldest_ts=_ts(time_ms), oldest_external_id=message_id, total_synced=0,
            )
        elif state.newest_ts is None or time_ms > _ms(state.newest_ts):
            await sync_state.upsert_state(
                db, self.account_id, chat_pk, newest_ts=_ts(time_ms), newest_external_id=message_id
            )

    async def mark_deleted(self, chat_id: int, ids: list[int], at: datetime) -> set[int]:
        """Soft delete; returns the ids the account has (deleted now or before)."""
        a = self.account_id
        async with account_db(a) as db:
            chat_pk = await chats.chat_pk(db, a, chat_id)
            if chat_pk is None:
                return set()
            await messages.mark_deleted(db, a, chat_pk, ids, at)
            found = set((await db.execute(
                select(MessageModel.external_id).where(
                    MessageModel.account_id == a, MessageModel.chat_id == chat_pk,
                    MessageModel.external_id.in_(ids),
                )
            )).scalars())
            await db.commit()
        return found

    # -- raw journal ----------------------------------------------------------------

    async def journal(self, opcode: int, payload: dict) -> int:
        async with account_db(self.account_id) as db:
            event_id = await raw_events.add_raw_event(db, self.account_id, f"max:{opcode}", payload)
            await db.commit()
        return event_id

    async def finish_journal(self, event_id: int, *, normalized: bool, error: str | None) -> None:
        async with account_db(self.account_id) as db:
            await raw_events.mark_normalized(db, self.account_id, event_id, None if normalized else (error or "not normalized"))
            await db.commit()

    # -- cursors (MaxSync) --------------------------------------------------------

    async def _pk(self, db, chat_id: int) -> int | None:
        return await chats.chat_pk(db, self.account_id, chat_id)

    async def cursor(self, chat_id: int) -> Cursor | None:
        async with account_db(self.account_id) as db:
            pk = await self._pk(db, chat_id)
            state = await sync_state.get_state(db, self.account_id, pk) if pk is not None else None
        if state is None:
            return None
        return Cursor(
            newest_time_ms=_ms(state.newest_ts),
            newest_message_id=state.newest_external_id,
            oldest_time_ms=_ms(state.oldest_ts),
            oldest_message_id=state.oldest_external_id,
        )

    async def move_oldest(self, chat_id: int, message_id: int, time_ms: int, stored_delta: int) -> None:
        async with account_db(self.account_id) as db:
            pk = await self._pk(db, chat_id)
            if pk is None:
                return
            state = await sync_state.get_state(db, self.account_id, pk)
            fields: dict[str, Any] = {"total_synced": ((state.total_synced if state else 0) or 0) + stored_delta}
            if state is None or state.oldest_ts is None or time_ms < _ms(state.oldest_ts):
                fields.update(oldest_ts=_ts(time_ms), oldest_external_id=message_id)
            await sync_state.upsert_state(db, self.account_id, pk, **fields)
            await db.commit()

    async def finish_backfill(self, chat_id: int, fully_synced: bool | None) -> None:
        async with account_db(self.account_id) as db:
            pk = await self._pk(db, chat_id)
            if pk is None or await sync_state.get_state(db, self.account_id, pk) is None:
                return
            fields: dict[str, Any] = {"last_backfill_at": datetime.now(timezone.utc)}
            if fully_synced is not None:
                fields["is_fully_synced"] = fully_synced
            await sync_state.upsert_state(db, self.account_id, pk, **fields)
            await db.commit()

    async def mark_catchup(self, chat_id: int) -> None:
        async with account_db(self.account_id) as db:
            pk = await self._pk(db, chat_id)
            if pk is None or await sync_state.get_state(db, self.account_id, pk) is None:
                return
            await sync_state.upsert_state(db, self.account_id, pk, last_catchup_at=datetime.now(timezone.utc))
            await db.commit()

    async def catchup_rows(self) -> list[tuple[int, int | None, Cursor | None]]:
        """(chat external id, last message ms, cursor) of the account's chats, freshest first."""
        a = self.account_id
        async with account_db(a) as db:
            rows = (await db.execute(
                select(ChatModel.external_id, ChatModel.last_message_at, SyncState)
                .join(SyncState, (SyncState.account_id == ChatModel.account_id)
                      & (SyncState.chat_id == ChatModel.id), isouter=True)
                .where(ChatModel.account_id == a)
                .order_by(ChatModel.last_message_at.desc().nullslast())
            )).all()
        out = []
        for ext, last_at, state in rows:
            cursor = None if state is None else Cursor(
                _ms(state.newest_ts), state.newest_external_id, _ms(state.oldest_ts), state.oldest_external_id
            )
            out.append((ext, _ms(last_at), cursor))
        return out

    async def known_user_ids(self) -> set[int]:
        async with account_db(self.account_id) as db:
            return set((await db.execute(
                select(UserModel.external_id).where(
                    UserModel.account_id == self.account_id, UserModel.first_name.is_not(None)
                )
            )).scalars())
