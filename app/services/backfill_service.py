import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import TgChat, TgSyncState
from app.services.chat_service import upsert_chat
from app.services.message_service import upsert_message
log = logging.getLogger(__name__)

# Track running backfill tasks
_backfill_tasks: dict[int, asyncio.Task] = {}


def is_backfill_running(chat_id: int) -> bool:
    task = _backfill_tasks.get(chat_id)
    return task is not None and not task.done()


async def start_backfill(
    chat_id: int,
    limit: int = 1000,
    alias: str = "work",
    direction: str = "backward",
) -> dict:
    """Start a background backfill for a given chat."""
    if is_backfill_running(chat_id):
        return {"status": "already_running", "chat_id": chat_id}

    from app.telegram.pool import pool
    tg_session = await pool.get(alias)
    client = tg_session.client
    if not client or not await client.is_user_authorized():
        return {"status": "error", "detail": f"Session '{alias}' not authorized"}

    task = asyncio.create_task(_run_backfill(chat_id, limit, alias, direction))
    _backfill_tasks[chat_id] = task
    return {"status": "started", "chat_id": chat_id, "limit": limit, "direction": direction}


async def _run_backfill(chat_id: int, limit: int, alias: str = "work", direction: str = "backward") -> None:
    """Background task: fetch history from Telegram and save to DB."""
    from app.telegram.pool import pool
    tg_session = await pool.get(alias)
    client = tg_session.client
    if not client:
        return

    log.info("Backfill started for chat %d (limit=%d, direction=%s)", chat_id, limit, direction)
    count = 0

    try:
        async with async_session() as session:
            # Get or create sync state
            stmt = select(TgSyncState).where(TgSyncState.chat_id == chat_id)
            result = await session.execute(stmt)
            sync_state = result.scalar_one_or_none()

            if sync_state is None:
                sync_state = TgSyncState(chat_id=chat_id)
                session.add(sync_state)
                await session.flush()

            db_chat = await session.get(TgChat, chat_id)
            if db_chat is None:
                # Try to get entity and create chat
                try:
                    entity = await client.get_entity(chat_id)
                    db_chat = await upsert_chat(session, entity)
                except Exception:
                    log.error("Cannot find chat %d", chat_id)
                    return

            if direction == "forward":
                await _run_forward_fill(
                    client, session, sync_state, db_chat, chat_id, limit, count
                )
            else:
                await _run_backward_fill(
                    client, session, sync_state, db_chat, chat_id, limit, count
                )

    except Exception:
        log.exception("Backfill error for chat %d", chat_id)
    finally:
        _backfill_tasks.pop(chat_id, None)
        log.info("Backfill finished for chat %d", chat_id)


async def _run_backward_fill(
    client, session, sync_state, db_chat, chat_id: int, limit: int, count: int
) -> None:
    """Fetch messages older than oldest known (original behaviour)."""
    max_id = sync_state.oldest_message_id or 0

    kwargs = {"limit": min(limit, 100), "entity": chat_id}
    if max_id:
        kwargs["offset_id"] = max_id

    fetched_total = 0
    oldest_id = max_id

    while fetched_total < limit:
        batch_size = min(100, limit - fetched_total)
        kwargs["limit"] = batch_size

        messages = await client.get_messages(**kwargs)
        if not messages:
            sync_state.is_fully_synced = True
            break

        for msg in messages:
            if msg is None or msg.id is None:
                continue

            # Upsert sender if available — only for User entities.
            # Channel/Chat objects (e.g. broadcast senders) have no
            # first_name and must not be passed to upsert_user.
            if msg.sender:
                from app.services.user_service import upsert_user
                from app.utils.sender import is_user_entity
                try:
                    if is_user_entity(msg.sender):
                        await upsert_user(session, msg.sender)
                except Exception:
                    pass

            await upsert_message(session, msg, db_chat)
            count += 1

            if oldest_id == 0 or msg.id < oldest_id:
                oldest_id = msg.id

        fetched_total += len(messages)
        kwargs["offset_id"] = messages[-1].id

        # Rate limiting: 1.5 sec between batches
        await asyncio.sleep(1.5)

    # Update sync state
    sync_state.oldest_message_id = oldest_id or sync_state.oldest_message_id
    if sync_state.newest_message_id is None or (db_chat.last_message_id and db_chat.last_message_id > (sync_state.newest_message_id or 0)):
        sync_state.newest_message_id = db_chat.last_message_id
    sync_state.total_messages_synced = (sync_state.total_messages_synced or 0) + count
    sync_state.last_backfill_at = datetime.now(timezone.utc)
    await session.commit()
    log.info("Backward fill done for chat %d: %d messages saved", chat_id, count)


async def _run_forward_fill(
    client, session, sync_state, db_chat, chat_id: int, limit: int, count: int
) -> None:
    """Fetch messages newer than newest_message_id (forward-fill for incremental sync).

    Edge case: newest_message_id IS NULL → min_id=0, equivalent to full backfill
    from the beginning. Accepted per ADR-1: rare case, backfill already ran in
    our scenario.
    """
    min_id = sync_state.newest_message_id or 0

    kwargs = {
        "entity": chat_id,
        "min_id": min_id,
        "reverse": True,
    }

    fetched_total = 0
    newest_id_seen = min_id

    while fetched_total < limit:
        batch_size = min(100, limit - fetched_total)
        kwargs["limit"] = batch_size

        messages = await client.get_messages(**kwargs)
        if not messages:
            break

        for msg in messages:
            if msg is None or msg.id is None:
                continue

            if msg.sender:
                from app.services.user_service import upsert_user
                from app.utils.sender import is_user_entity
                try:
                    if is_user_entity(msg.sender):
                        await upsert_user(session, msg.sender)
                except Exception:
                    pass

            await upsert_message(session, msg, db_chat)
            count += 1

            if msg.id > newest_id_seen:
                newest_id_seen = msg.id

        fetched_total += len(messages)
        # Advance min_id to last seen so next batch continues from there
        kwargs["min_id"] = messages[-1].id

        # Rate limiting: 1.5 sec between batches
        await asyncio.sleep(1.5)

    # Update sync state — do NOT touch oldest_message_id or is_fully_synced
    sync_state.newest_message_id = max(sync_state.newest_message_id or 0, newest_id_seen) or None
    sync_state.total_messages_synced = (sync_state.total_messages_synced or 0) + count
    sync_state.last_backfill_at = datetime.now(timezone.utc)
    await session.commit()
    log.info("Forward fill done for chat %d: %d messages saved, newest_id=%s", chat_id, count, newest_id_seen or None)


async def get_sync_states(session: AsyncSession) -> list[dict]:
    """Return sync status for all chats with backfill state."""
    stmt = select(TgSyncState, TgChat.title).join(TgChat, TgSyncState.chat_id == TgChat.id, isouter=True)
    result = await session.execute(stmt)
    rows = result.all()

    states = []
    for sync_state, chat_title in rows:
        states.append({
            "chat_id": sync_state.chat_id,
            "chat_title": chat_title,
            "oldest_message_id": sync_state.oldest_message_id,
            "newest_message_id": sync_state.newest_message_id,
            "is_fully_synced": sync_state.is_fully_synced,
            "total_messages_synced": sync_state.total_messages_synced,
            "last_backfill_at": sync_state.last_backfill_at,
            "is_running": is_backfill_running(sync_state.chat_id),
        })

    return states
