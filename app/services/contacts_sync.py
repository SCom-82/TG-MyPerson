"""Daily address-book sync of every TG account (ТЗ 2026-10-13 §5.1), STORAGE_MODE=new only.

GetContactsRequest per authorized session → users(via='contact', is_contact=true);
accounts' users that left the address book lose is_contact. In-process loop
like partition_loop; never raises.
"""

import asyncio
import logging

from app.config import settings
from app.services import tg_store

log = logging.getLogger(__name__)


async def contacts_sync_once() -> dict[str, int]:
    from telethon.tl.functions.contacts import GetContactsRequest

    from app.telegram.pool import pool

    synced: dict[str, int] = {}
    for alias, session in list(pool._pool.items()):
        account_id = getattr(session, "account_id", None)
        client = session.client
        if account_id is None or client is None:
            continue
        try:
            if not await client.is_user_authorized():
                continue
            from app.telegram.pool import confirm_identity

            if not await confirm_identity(session):
                log.warning("contacts_sync: alias '%s' skipped: %s", alias, session.identity_error)
                continue
            result = await client(GetContactsRequest(hash=0))
            synced[alias] = await tg_store.store_contacts(
                account_id, list(getattr(result, "users", []) or []), full=True
            )
        except Exception as exc:  # noqa: BLE001 — one account must not stop the others
            log.warning("contacts_sync: alias '%s' failed: %s", alias, exc)
    if synced:
        log.info("contacts_sync: %s", synced)
    return synced


async def contacts_sync_loop() -> None:
    while True:
        if tg_store.new_mode():
            await contacts_sync_once()
        await asyncio.sleep(settings.contacts_sync_hours * 3600)
