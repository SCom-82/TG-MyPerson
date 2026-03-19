import logging

from telethon import events, utils

from app.database import async_session
from app.services.message_service import upsert_message_from_event
from app.services.stream_service import stream_manager

log = logging.getLogger(__name__)


def register_handlers(client) -> None:
    """Register Telethon event handlers for real-time message capture."""

    @client.on(events.NewMessage)
    async def on_new_message(event):
        try:
            async with async_session() as session:
                msg = await upsert_message_from_event(session, event)
                if msg:
                    await stream_manager.broadcast(msg)
        except Exception:
            log.exception("Error handling new message (chat=%s, msg=%s)", event.chat_id, event.id)

    @client.on(events.MessageEdited)
    async def on_message_edited(event):
        try:
            async with async_session() as session:
                msg = await upsert_message_from_event(session, event, is_edit=True)
                if msg:
                    await stream_manager.broadcast(msg)
        except Exception:
            log.exception("Error handling edited message (chat=%s, msg=%s)", event.chat_id, event.id)

    @client.on(events.MessageDeleted)
    async def on_message_deleted(event):
        log.debug("Message(s) deleted: %s", event.deleted_ids)

    log.info("Telegram event handlers registered")
