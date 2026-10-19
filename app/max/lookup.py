"""Find a MAX user by phone and add them to contacts (plan 2026-10-19 §4 PR-D1).

search_by_phone (opcode CONTACT_INFO_BY_PHONE) → add_contact (CONTACT_UPDATE
ADD) → users/identities/people + the dialog stub me XOR peer with its title:
the chat id of a dialog is known before the first message (pymax
UserService.get_chat_id). PyMax stays behind its client object here (no pymax
import: test_s16_pymax_import_boundary).

store_import (plan Р4 Р-38): the answer of one SYNC for a batch of numbers
(app/max/frames.py), each found user stored the same way (store_found).
"""

import logging
from dataclasses import dataclass

from app.max import normalize
from app.max.store_new import NewMaxStore
from app.utils.phone import mask_phone, normalize_phone

log = logging.getLogger(__name__)


class NotFound(Exception):
    """MAX does not know the number (or the person hides it)."""


@dataclass
class Found:
    user_id: int
    chat_id: int | None
    is_contact: bool
    warning: str | None = None


def _not_found(exc: Exception) -> bool:
    text = f"{getattr(exc, 'error', '')} {exc}".lower().replace("_", ".")
    return "not.found" in text or "not found" in text


async def by_phone(session, e164: str, masked: str) -> Found:
    client = session.client
    try:
        user = await client.search_by_phone(e164)
    except Exception as exc:  # noqa: BLE001 — PyMax raises ApiError
        if _not_found(exc):
            raise NotFound() from exc
        raise
    if user is None or getattr(user, "id", None) is None:
        raise NotFound()

    is_contact, warning = True, None
    try:
        await client.add_contact(user.id)
    except Exception as exc:  # noqa: BLE001 — found anyway: answer, say what failed
        is_contact, warning = False, f"add_contact failed: {type(exc).__name__}: {exc}"
        log.warning("max[%s]: %s found as %s, %s", session.alias, masked, user.id, warning)

    chat_id = await store_found(session, user, e164, is_contact)
    return Found(user.id, chat_id, is_contact, warning)


async def store_found(session, user, e164: str, is_contact: bool) -> int | None:
    """users/identities/people with the number, and the dialog stub me XOR peer; its chat id."""
    row = normalize.normalize_user(user)
    row["phone"] = row["phone"] or e164
    store = NewMaxStore(session.account_id)
    await store.upsert_users([row], "contact", is_contact=is_contact)
    me = getattr(getattr(getattr(session.client, "me", None), "contact", None), "id", None)
    chat_id = me ^ user.id if me is not None else None
    if chat_id is not None:
        await store.ensure_dialog(chat_id)
    return chat_id


def match_phones(numbers: list[str], users: list, phones: dict) -> dict[str, object]:
    """e164 → the user MAX found for it. By the phones map (its values: a user id, or the
    number as MAX knows it); a number missing there — by the user's own phone."""
    by_id = {u.id: u for u in users}
    by_phone = {normalize_phone(str(u.phone)): u for u in users if getattr(u, "phone", None)}
    found: dict[str, object] = {}
    for key, value in phones.items():
        e164 = normalize_phone(str(key))
        user = None
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit() and int(value) in by_id):
            user = by_id.get(int(value))
        if user is None and value is not None:
            user = by_phone.get(normalize_phone(str(value)))
        if e164 in numbers and user is not None:
            found[e164] = user
    for e164 in numbers:
        if e164 not in found and e164 in by_phone:
            found[e164] = by_phone[e164]
    return found


async def store_import(session, contacts: list[tuple[str, str]], users: list,
                       phones: dict) -> tuple[dict[str, tuple[int, int | None]], list[str]]:
    """The parsed SYNC answer stored: ({e164: (user id, chat id)} of the numbers MAX found,
    warnings). A number that does not store is still found — MAX took it (review 17 R17-1)."""
    found = match_phones([e164 for e164, _ in contacts], users, phones)
    out: dict[str, tuple[int, int | None]] = {}
    warnings: list[str] = []
    for e164, user in found.items():
        try:
            chat_id = await store_found(session, user, e164, True)
        except Exception as exc:  # noqa: BLE001
            log.warning("max[%s]: imported %s → %s not stored", session.alias, mask_phone(e164), user.id,
                        exc_info=True)
            warnings.append(f"user {user.id} not stored: {type(exc).__name__}")
            chat_id = None
        out[e164] = (user.id, chat_id)
    return out, warnings
