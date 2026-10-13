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

# Running backfill tasks by (account, chat): two accounts backfill the same
# chat independently (ТЗ 2026-10-13 T10). The account part is the account id,
# or the alias for a session that has none.
_backfill_tasks: dict[tuple[int | str, int], asyncio.Task] = {}


def is_backfill_running(chat_id: int, account: int | str | None = None) -> bool:
    """account None: running for any account (legacy status view)."""
    for (acc, cid), task in list(_backfill_tasks.items()):
        if cid == chat_id and (account is None or acc == account) and not task.done():
            return True
    return False


async def start_backfill(
    chat_id: int,
    limit: int = 1000,
    alias: str = "work",
    direction: str = "backward",
) -> dict:
    """Start a background backfill for a given chat."""
    from app.services import tg_store
    from app.telegram.pool import pool
    tg_session = await pool.get(alias)
    account = getattr(tg_session, "account_id", None)
    key = (account if isinstance(account, int) else alias, chat_id)
    if is_backfill_running(chat_id, key[0]):
        return {"status": "already_running", "chat_id": chat_id}

    client = tg_session.client
    if not client or not await client.is_user_authorized():
        return {"status": "error", "detail": f"Session '{alias}' not authorized"}

    if tg_store.new_mode():
        if not isinstance(account, int):
            return {"status": "error", "detail": f"Session '{alias}' has no account"}
        from app.telegram.pool import confirm_identity

        if not await confirm_identity(tg_session):
            return {"status": "error", "detail": f"Session '{alias}' belongs to another Telegram user"}
        task = asyncio.create_task(_run_backfill_new(account, chat_id, limit, alias, direction))
    else:
        task = asyncio.create_task(_run_backfill(chat_id, limit, alias, direction))
    _backfill_tasks[key] = task
    task.add_done_callback(lambda t: _backfill_tasks.get(key) is t and _backfill_tasks.pop(key, None))
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

            try:
                await upsert_message(session, msg, db_chat)
                count += 1
            except Exception:
                await session.rollback()
                log.warning(
                    "Skipped message %s in chat %d (upsert failed)",
                    getattr(msg, "id", "?"),
                    chat_id,
                    exc_info=True,
                )
                continue

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

            try:
                await upsert_message(session, msg, db_chat)
                count += 1
            except Exception:
                await session.rollback()
                log.warning(
                    "Skipped message %s in chat %d (upsert failed)",
                    getattr(msg, "id", "?"),
                    chat_id,
                    exc_info=True,
                )
                continue

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


# ---------------------------------------------------------------------------
# STORAGE_MODE=new: the account's own tables and cursor (ТЗ 2026-10-13 §5.1)
# ---------------------------------------------------------------------------

async def _shadow_legacy_batch(client, chat_id: int, batch: list) -> None:
    """Same messages into tg_* (shadow write; tg_sync_state is left alone)."""
    from app.services.user_service import upsert_user
    from app.utils.sender import is_user_entity

    async with async_session() as session:
        db_chat = await session.get(TgChat, chat_id)
        if db_chat is None:
            db_chat = await upsert_chat(session, await client.get_entity(chat_id))
        for msg in batch:
            if msg is None or msg.id is None:
                continue
            try:
                if msg.sender and is_user_entity(msg.sender):
                    await upsert_user(session, msg.sender)
                await upsert_message(session, msg, db_chat)
            except Exception:
                await session.rollback()


async def _run_backfill_new(account_id: int, chat_id: int, limit: int, alias: str, direction: str) -> None:
    from app.services import tg_store
    from app.telegram.pool import pool

    client = (await pool.get(alias)).client
    if not client:
        return
    log.info("Backfill started for chat %d account %d (limit=%d, direction=%s)", chat_id, account_id, limit, direction)
    try:
        chat_pk = await tg_store.ensure_chat(account_id, client, chat_id)
        if chat_pk is None:
            return
        state = await tg_store.get_cursor(account_id, chat_pk)
        oldest = state.oldest_external_id if state else None
        newest = state.newest_external_id if state else None
        total = 0
        fully_synced = None

        if direction == "forward":
            kwargs = {"entity": chat_id, "min_id": newest or 0, "reverse": True}
        else:
            kwargs = {"entity": chat_id}
            if oldest:
                kwargs["offset_id"] = oldest

        fetched = 0
        while fetched < limit:
            kwargs["limit"] = min(100, limit - fetched)
            batch = await client.get_messages(**kwargs)
            if not batch:
                if direction != "forward":
                    fully_synced = True
                break
            stored, ids = await tg_store.store_history(account_id, chat_pk, list(batch))
            await tg_store.shadow("backfill", lambda b=list(batch): _shadow_legacy_batch(client, chat_id, b))
            total += stored
            if ids:
                oldest = min([i for i in (oldest, *ids) if i])
                newest = max([i for i in (newest, *ids) if i])
            fetched += len(batch)
            if direction == "forward":
                kwargs["min_id"] = batch[-1].id
            else:
                kwargs["offset_id"] = batch[-1].id
            await asyncio.sleep(1.5)

        fields = {
            "newest_external_id": newest,
            "total_synced": ((state.total_synced if state else 0) or 0) + total,
            "last_backfill_at": datetime.now(timezone.utc),
        }
        if direction != "forward":
            fields["oldest_external_id"] = oldest
            if fully_synced is not None:
                fields["is_fully_synced"] = fully_synced
        await tg_store.save_cursor(account_id, chat_pk, **fields)
        log.info("Backfill done for chat %d account %d: %d messages", chat_id, account_id, total)
    except Exception:
        log.exception("Backfill error for chat %d account %d", chat_id, account_id)
