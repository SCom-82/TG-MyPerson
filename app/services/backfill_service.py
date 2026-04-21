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


async def start_backfill(chat_id: int, limit: int = 1000, alias: str = "work") -> dict:
    """Start a background backfill for a given chat."""
    if is_backfill_running(chat_id):
        return {"status": "already_running", "chat_id": chat_id}

    from app.telegram.pool import pool
    tg_session = await pool.get(alias)
    client = tg_session.client
    if not client or not await client.is_user_authorized():
        return {"status": "error", "detail": f"Session '{alias}' not authorized"}

    task = asyncio.create_task(_run_backfill(chat_id, limit, alias))
    _backfill_tasks[chat_id] = task
    return {"status": "started", "chat_id": chat_id, "limit": limit}


async def _run_backfill(chat_id: int, limit: int, alias: str = "work") -> None:
    """Background task: fetch history from Telegram and save to DB."""
    from app.telegram.pool import pool
    tg_session = await pool.get(alias)
    client = tg_session.client
    if not client:
        return

    log.info("Backfill started for chat %d (limit=%d)", chat_id, limit)
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

            # Fetch messages older than oldest known
            min_id = 0
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

                    # Upsert sender if available
                    if msg.sender:
                        from app.services.user_service import upsert_user
                        try:
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

    except Exception:
        log.exception("Backfill error for chat %d", chat_id)
    finally:
        _backfill_tasks.pop(chat_id, None)
        log.info("Backfill finished for chat %d: %d messages saved", chat_id, count)


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
