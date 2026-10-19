"""MAX frames PyMax 2.4.1 does not have or spoils (plan 2026-10-19 Р4 Р-49).

The seventh module allowed to import pymax (test_s16_pymax_import_boundary):
raw protocol lives here, so a PyMax upgrade has one place to look at.

- unpin: no PyMax method; CHAT_UPDATE with pinMessageId 0 (Р-33, not checked live);
- poll_attach / poll_view: a poll to send (PyMax's Poll in attaches) and the
  poll of a message as the agent sees it (Р-44, Р-46);
- link_info: LINK_INFO by a public https://max.ru/<name> (PyMax takes join/… only; Р-48);
- sync_contacts: PyMax's import_contacts frame (SYNC), keeping the phones map
  PyMax throws away — which number gave which user (Р-38).
"""

import logging
from typing import Any

from pymax.api.users.payloads import ImportContactsPayload
from pymax.protocol import Opcode
from pymax.types import Chat, User
from pymax.types.domain.attachments.enums import PollFlags
from pymax.types.domain.attachments.poll import Poll, PollAnswer, PollAttachment
from pymax.types.domain.user import ContactInfo

from app.max import groups

log = logging.getLogger(__name__)


# -- pin -----------------------------------------------------------------------

def unpin_frame(chat_id: int) -> dict:
    """PyMax's PinMessagePayload with no message: MAX has one field pinMessageId (Р-33)."""
    return {"chatId": chat_id, "pinMessageId": 0, "notifyPin": False}


async def unpin(client: Any, chat_id: int) -> None:
    await client._app.invoke(Opcode.CHAT_UPDATE, unpin_frame(chat_id))
    groups._forget(client, chat_id)


def pinned_id(chat: Any) -> int | None:
    pinned = getattr(chat, "pinned_message", None)
    return int(pinned.id) if pinned is not None and getattr(pinned, "id", None) is not None else None


# -- polls ---------------------------------------------------------------------

def poll_attach(question: str, options: list[str], *, anonymous: bool, multiple: bool) -> Poll:
    flags = PollFlags(0)
    if anonymous:
        flags |= PollFlags.FLAG_SETTINGS_ANONYMOUS
    if multiple:
        flags |= PollFlags.FLAG_SETTINGS_MULTISELECT
    return Poll(title=question, answers=[PollAnswer(text=o) for o in options], settings=flags)


def find_poll(message: Any) -> PollAttachment | None:
    """The poll among a message's own attachments."""
    for att in getattr(message, "attaches", None) or []:
        if isinstance(att, PollAttachment):
            return att
    return None


def answer_ids(poll: PollAttachment) -> list[int | None]:
    """answer_id per answer; MAX may leave them out of answers — then from state.result by position."""
    ids = [a.answer_id for a in poll.answers]
    result = poll.state.result or []
    if any(i is None for i in ids) and len(result) == len(ids):
        ids = [i if i is not None else r.answer_id for i, r in zip(ids, result)]
    return ids


def poll_state(poll_or_state: Any) -> dict:
    state = getattr(poll_or_state, "state", poll_or_state)
    return {"total": state.total,
            "answers": [{"answer_id": r.answer_id, "vote_count": r.vote_count} for r in state.result or []]}


def poll_view(poll: PollAttachment) -> dict:
    """The poll for the agent (Р-44): ids to vote with, counts as of the stored copy."""
    votes = {r.answer_id: r.vote_count for r in poll.state.result or []}
    settings = int(poll.settings or 0)
    return {
        "poll_id": poll.poll_id,
        "title": poll.title,
        "answers": [{"answer_id": i, "text": a.text, "vote_count": votes.get(i, 0)}
                    for i, a in zip(answer_ids(poll), poll.answers)],
        "total": poll.state.total,
        "anonymous": bool(settings & PollFlags.FLAG_SETTINGS_ANONYMOUS),
        "multiselect": bool(settings & PollFlags.FLAG_SETTINGS_MULTISELECT),
        "revote": bool(settings & PollFlags.FLAG_SETTINGS_REVOTE),
        "closed": bool(settings & PollFlags.FLAG_SETTINGS_CLOSED),
        "quiz": bool(settings & PollFlags.FLAG_SETTINGS_QUIZ),
    }


# -- links and contacts (R4-2) ------------------------------------------------

async def link_info(client: Any, url: str) -> Chat | None:
    """What a public link points to, without joining (webmaxsocket resolveLink; not checked live)."""
    frame = await client._app.invoke(Opcode.LINK_INFO, {"link": url})
    data = (getattr(frame, "payload", None) or {}).get("chat")
    return Chat.model_validate(data) if data else None


def sync_frame(contacts: list[tuple[str, str]]) -> dict:
    """PyMax's ImportContactsPayload: {contactList: {phone: {firstName}}} (no last name in PyMax)."""
    return ImportContactsPayload.from_contacts(
        [ContactInfo(phone=phone, first_name=first) for phone, first in contacts]).to_payload()


async def sync_contacts(client: Any, contacts: list[tuple[str, str]]) -> tuple[list[User], dict]:
    """(users MAX found, its phones map {phone: user id}) for (phone, first name) pairs."""
    frame = await client._app.invoke(Opcode.SYNC, sync_frame(contacts))
    payload = getattr(frame, "payload", None) or {}
    users = [User.model_validate(u) for u in payload.get("contacts") or [] if u]
    return users, dict(payload.get("phones") or {})
