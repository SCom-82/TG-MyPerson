import logging

from telethon import events, utils

from app.database import async_session
from app.services import tg_store
from app.services.message_service import upsert_message_from_event
from app.services.stream_service import stream_manager

log = logging.getLogger(__name__)


def register_handlers(client, account_id: int | None = None) -> None:
    """Register Telethon event handlers for real-time message capture.

    STORAGE_MODE=legacy: tg_* only, as before (account_id is not needed).
    STORAGE_MODE=new: the handlers are bound to account_id — rows go to the
    account's tables, the legacy write runs as a shadow, SSE goes to the
    account's subscribers, deletions are recorded (§5.4).
    """
    if tg_store.new_mode() and account_id is None:
        raise ValueError("register_handlers: account_id is required in STORAGE_MODE=new")

    async def _legacy(event, is_edit: bool):
        async with async_session() as session:
            return await upsert_message_from_event(session, event, is_edit=is_edit)

    async def _store(event, is_edit: bool):
        if not tg_store.new_mode():
            msg = await _legacy(event, is_edit)
            if msg:
                await stream_manager.broadcast(msg)
            return
        msg = await tg_store.store_event(account_id, event)
        await tg_store.shadow("message", lambda: _legacy(event, is_edit))
        if msg:
            await stream_manager.broadcast(msg, account_id)

    @client.on(events.NewMessage)
    async def on_new_message(event):
        try:
            await _store(event, is_edit=False)
        except Exception:
            log.exception("Error handling new message (chat=%s, msg=%s)", event.chat_id, event.id)

    @client.on(events.MessageEdited)
    async def on_message_edited(event):
        try:
            await _store(event, is_edit=True)
        except Exception:
            log.exception("Error handling edited message (chat=%s, msg=%s)", event.chat_id, event.id)

    @client.on(events.MessageDeleted)
    async def on_message_deleted(event):
        log.debug("Message(s) deleted: %s", event.deleted_ids)
        if not tg_store.new_mode():
            return
        try:
            marked = await tg_store.store_deleted(account_id, event.chat_id, list(event.deleted_ids))
            await stream_manager.broadcast(
                {"event": "delete", "chat_id": event.chat_id, "message_ids": list(event.deleted_ids)},
                account_id,
            )
            log.debug("Marked %d message(s) deleted for account %s", marked, account_id)
        except Exception:
            log.exception("Error handling deleted messages (chat=%s)", event.chat_id)

    log.info("Telegram event handlers registered (account=%s)", account_id)
