"""Find a MAX user by phone and add them to contacts (plan 2026-10-19 §4 PR-D1).

search_by_phone (opcode CONTACT_INFO_BY_PHONE) → add_contact (CONTACT_UPDATE
ADD) → users/identities/people + the dialog stub me XOR peer with its title:
the chat id of a dialog is known before the first message (pymax
UserService.get_chat_id). PyMax stays behind its client object here (no pymax
import: test_s16_pymax_import_boundary).
"""

import logging
from dataclasses import dataclass

from app.max import normalize
from app.max.store_new import NewMaxStore

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

    row = normalize.normalize_user(user)
    row["phone"] = row["phone"] or e164
    store = NewMaxStore(session.account_id)
    await store.upsert_users([row], "contact", is_contact=is_contact)
    me = getattr(getattr(getattr(client, "me", None), "contact", None), "id", None)
    chat_id = me ^ user.id if me is not None else None
    if chat_id is not None:
        await store.ensure_dialog(chat_id)
    return Found(user.id, chat_id, is_contact, warning)
