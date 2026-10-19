"""Find a Telegram user by phone and keep them as a contact (plan 2026-10-19 §4 PR-D1).

ImportContacts both looks the number up and adds it to the account's address
book; the user goes to users/identities/people like any contact (is_contact,
access_hash), so the phone links the person across platforms (D13).
"""

import random

from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.types import InputPhoneContact

from app.services import tg_store


async def by_phone(client, account_id: int, e164: str, first_name: str | None, last_name: str | None) -> int | None:
    """External user id, None: not registered (or hidden by privacy)."""
    result = await client(ImportContactsRequest([InputPhoneContact(
        client_id=random.getrandbits(63), phone=e164, first_name=first_name or e164, last_name=last_name or "",
    )]))
    if not result.users:
        return None
    user = result.users[0]
    if not getattr(user, "phone", None):
        user.phone = e164.lstrip("+")  # we know it: it links the identity to the person
    await tg_store.store_contacts(account_id, [user], full=False)
    return user.id
