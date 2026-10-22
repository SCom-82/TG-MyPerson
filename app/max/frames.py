"""MAX frames PyMax 2.4.1 does not have or spoils (plan 2026-10-19 Р4 Р-49).

The seventh module allowed to import pymax (test_s16_pymax_import_boundary):
raw protocol lives here, so a PyMax upgrade has one place to look at.

- unpin: no PyMax method; CHAT_UPDATE with pinMessageId 0 (Р-33, not checked live);
- poll_attach / poll_view: a poll to send (PyMax's Poll in attaches) and the
  poll of a message as the agent sees it (Р-44, Р-46);
- link_info: LINK_INFO by a public https://max.ru/<name> (PyMax takes join/… only; Р-48);
- sync_contacts / parse_sync: PyMax's import_contacts frame (SYNC), keeping the
  phones map PyMax throws away — which number gave which user (Р-38);
- vote / parse_vote: PyMax's vote_poll frame. Sending and parsing are apart
  (review 17 R17-1): the write slot covers the frame only, an answer we cannot
  parse does not give back a slot of a write that reached MAX.

Release 5 (plan 2026-10-22 Р5 Р-64…Р-72, Р-77): bots (keyboards, /start, info),
who reacted, poll voters, a message link, transcription, search in a chat, post
views, blocking. Frame fields are the MAX web client's (appendix «r5-protocol»);
each frame is a pure *_frame() (canary tests) and a call returning MAX's raw answer.
"""

import logging
import time
from typing import Any

from pymax import ApiError
from pymax.api.messages.payloads import VotePollPayload
from pymax.api.users.payloads import ImportContactsPayload
from pymax.formatting.markdown import Formatter
from pymax.protocol import Opcode
from pymax.types import Chat, User
from pymax.types.domain import Message
from pymax.types.domain.attachments.enums import PollFlags
from pymax.types.domain.attachments.keyboards import InlineKeyboardAttachment
from pymax.types.domain.element import Element, ElementAttributes
from pymax.types.domain.attachments.poll import Poll, PollAnswer, PollAttachment, PollState
from pymax.types.domain.user import ContactInfo

from app.max import groups, html_text

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


async def sync_contacts(client: Any, contacts: list[tuple[str, str]]) -> dict:
    """The SYNC frame for (phone, first name) pairs; MAX's raw answer. Parsed apart
    (parse_sync): the frame is out once this returns (review 17 R17-1)."""
    frame = await client._app.invoke(Opcode.SYNC, sync_frame(contacts))
    return getattr(frame, "payload", None) or {}


def parse_sync(payload: dict) -> tuple[list[User], dict]:
    """(users MAX found, its phones map {phone: user id or MAX's number})."""
    users = [User.model_validate(u) for u in payload.get("contacts") or [] if u]
    phones = payload.get("phones") or {}
    if not isinstance(phones, dict):
        raise ValueError(f"phones is a {type(phones).__name__}, not a map")
    return users, dict(phones)


# -- votes ---------------------------------------------------------------------

async def vote(client: Any, chat_id: int, message_id: int, poll_id: int, answer_ids: list[int]) -> dict:
    """PyMax's vote_poll frame (SEND_VOTE, answersIds) without its parsing: the raw answer (R17-1)."""
    payload = VotePollPayload(chat_id=chat_id, message_id=message_id, poll_id=poll_id,
                              answers_ids=answer_ids).to_payload()
    frame = await client._app.invoke(Opcode.SEND_VOTE, payload)
    return getattr(frame, "payload", None) or {}


def parse_vote(payload: dict) -> PollState:
    return PollState.model_validate(payload["state"])


# -- release 5: bots -------------------------------------------------------------

OP_MSG_LINK = 90  # not in pymax.protocol.Opcode (Р-51)
PRESSABLE = ("CALLBACK", "CHAT", "MESSAGE")  # Р-64: the rest are a URL, PII, geo, a mini app, the clipboard


def _payload(frame: Any) -> dict:
    return getattr(frame, "payload", None) or {}


def keyboard_view(message: Any) -> dict | None:
    """The bot keyboard of a message (Р-64): {callback_id, buttons: [[{text, type, url?, payload?}]]}.

    callbackId is taken where MAX puts it: in the keyboard or next to it in the attachment."""
    for att in getattr(message, "attaches", None) or []:
        if not isinstance(att, InlineKeyboardAttachment):
            continue
        kb = att.keyboard or {}
        callback = kb.get("callbackId") or (att.model_extra or {}).get("callbackId")
        rows = []
        for row in kb.get("buttons") or []:
            rows.append([{k: v for k, v in (("text", b.get("text")), ("type", b.get("type")),
                                             ("url", b.get("url")), ("payload", b.get("payload")))
                          if v is not None}
                         for b in row or [] if isinstance(b, dict)])
        return {"callback_id": str(callback) if callback is not None else None, "buttons": rows}
    return None


def callback_frame(callback_id: str, button_type: str, payload: str | None) -> dict:
    """MSG_SEND_CALLBACK 118 {callbackId, type, payload?, timestamp}: a button pressed."""
    frame = {"callbackId": callback_id, "type": button_type, "timestamp": int(time.time() * 1000)}
    if payload is not None:
        frame["payload"] = payload
    return frame


async def press(client: Any, callback_id: str, button_type: str, payload: str | None) -> dict:
    return _payload(await client._app.invoke(Opcode.MSG_SEND_CALLBACK, callback_frame(callback_id, button_type,
                                                                                       payload)))


def bot_start_frame(*, chat_id: int | None, bot_id: int, start_payload: str | None) -> dict:
    """MSG_SEND 64 with a CONTROL botStarted (Р-65): to the dialog by chatId, else by userId."""
    attach = {"_type": "CONTROL", "event": "botStarted"}
    if start_payload is not None:
        attach["startPayload"] = start_payload
    frame: dict = {"message": {"cid": int(time.time() * 1000), "attaches": [attach]}}
    if chat_id is not None:
        frame["chatId"] = chat_id
    else:
        frame["userId"] = bot_id
    return frame


async def start_bot(client: Any, chat_id: int, bot_id: int, start_payload: str | None) -> dict:
    """/start to the bot. MAX refusing the dialog id (me ^ bot, not checked live) → the same
    frame by userId: nothing was sent by the refused one, so it is one send."""
    try:
        return _payload(await client._app.invoke(Opcode.MSG_SEND, bot_start_frame(
            chat_id=chat_id, bot_id=bot_id, start_payload=start_payload)))
    except ApiError as exc:
        if not _about_the_chat(exc):
            raise  # flood, a blocked or stopped bot ...: no second frame (review 19 R19-3)
        log.info("max: botStarted by chatId %s refused (%s), by userId", chat_id, exc)
    return _payload(await client._app.invoke(Opcode.MSG_SEND, bot_start_frame(
        chat_id=None, bot_id=bot_id, start_payload=start_payload)))


# Errors that say the dialog id was not taken (the reason for the userId frame); narrowed by
# the live check's codes (plan §5 p.10).
CHAT_ERRORS = ("not.found", "chat", "dialog")


def _about_the_chat(exc: ApiError) -> bool:
    code = str(getattr(exc, "error", "") or "").lower()
    return any(part in code for part in CHAT_ERRORS)


def parse_sent(payload: dict, chat_id: int | None = None) -> Message | None:
    """The message of a MSG_SEND / 118 answer (None if it has none)."""
    msg = payload.get("message")
    if not msg:
        return None
    return Message.model_validate({**msg, "chatId": payload.get("chatId", chat_id)})


async def bot_info(client: Any, bot_id: int) -> dict:
    """BOT_INFO 145 {botId} → {commands [{botId, name, description}], contact, startMessage}."""
    return _payload(await client._app.invoke(Opcode.BOT_INFO, {"botId": bot_id}))


async def chat_bot_commands(client: Any, chat_id: int) -> dict:
    """CHAT_BOT_COMMANDS 144 {chatId} → {commands, contacts {id: contact}}."""
    return _payload(await client._app.invoke(Opcode.CHAT_BOT_COMMANDS, {"chatId": chat_id}))


def parse_user(data: Any) -> User | None:
    return User.model_validate(data) if isinstance(data, dict) and data.get("id") is not None else None


# -- release 5: reads -------------------------------------------------------------

def detailed_reactions_frame(chat_id: int, message_id: int, *, count: int = 100, marker: Any = None) -> dict:
    frame = {"chatId": chat_id, "messageId": message_id, "count": count}
    if marker is not None:
        frame["marker"] = marker
    return frame


async def detailed_reactions(client: Any, chat_id: int, message_id: int, *, count: int = 100) -> dict:
    """MSG_GET_DETAILED_REACTIONS 181 → {reactions [{userId, reaction}], marker} (Р-67)."""
    return _payload(await client._app.invoke(Opcode.MSG_GET_DETAILED_REACTIONS,
                                             detailed_reactions_frame(chat_id, message_id, count=count)))


def voters_frame(chat_id: int, message_id: int, poll_id: int, answer_id: int, *, count: int,
                 marker: Any = None) -> dict:
    frame = {"pollId": poll_id, "chatId": chat_id, "messageId": message_id, "answerId": answer_id, "count": count}
    if marker is not None:
        frame["marker"] = marker
    return frame


async def voters(client: Any, chat_id: int, message_id: int, poll_id: int, answer_id: int, *, count: int,
                 marker: Any = None) -> dict:
    """VOTERS_LIST_BY_ANSWER 305 → {voters [{userId, timestamp}], voteCount, marker} (Р-68)."""
    return _payload(await client._app.invoke(Opcode.VOTERS_LIST_BY_ANSWER, voters_frame(
        chat_id, message_id, poll_id, answer_id, count=count, marker=marker)))


def message_link_frame(chat_id: int, message_id: int) -> dict:
    return {"chatId": chat_id, "messageId": message_id}


async def message_link(client: Any, chat_id: int, message_id: int) -> dict:
    """90 {chatId, messageId} → {link} (Р-69)."""
    return _payload(await client._app.invoke(OP_MSG_LINK, message_link_frame(chat_id, message_id)))


def media_id(message: Any, index: int | None) -> tuple[int, int] | None:
    """(attach index, id) of the voice (audioId) or video note (videoId) to transcribe; index — the attach_index."""
    for i, att in enumerate(getattr(message, "attaches", None) or []):
        if index is not None and i != index:
            continue
        found = getattr(att, "audio_id", None) or getattr(att, "video_id", None)
        if found is not None:
            return i, int(found)
    return None


def transcribe_frame(chat_id: int, message_id: int, media: int) -> dict:
    return {"mediaId": media, "messageId": message_id, "chatId": chat_id}


async def transcribe(client: Any, chat_id: int, message_id: int, media: int) -> dict:
    """TRANSCRIBE_MEDIA 202 → {transcription?, transcriptionStatus} (Р-70)."""
    return _payload(await client._app.invoke(Opcode.TRANSCRIBE_MEDIA, transcribe_frame(chat_id, message_id, media)))


def search_frame(chat_id: int, query: str, count: int, marker: int | None = None) -> dict:
    frame = {"chatId": chat_id, "query": query, "count": count}
    if marker is not None:
        frame["marker"] = marker
    return frame


async def search_chat(client: Any, chat_id: int, query: str, count: int, marker: int | None = None) -> dict:
    """MSG_SEARCH 73 → {result, marker, total} (Р-71)."""
    return _payload(await client._app.invoke(Opcode.MSG_SEARCH, search_frame(chat_id, query, count, marker)))


def found_messages(payload: dict, chat_id: int) -> list[Message]:
    """The messages of a MSG_SEARCH answer. The item's schema is not in the appendix: a message
    itself or {message: …}; an item that is neither is skipped (logged)."""
    out = []
    for item in payload.get("result") or []:
        data = item.get("message") if isinstance(item, dict) and isinstance(item.get("message"), dict) else item
        if not isinstance(data, dict) or data.get("id") is None:
            log.info("max: a search result without a message: %r", item)
            continue
        out.append(Message.model_validate({**data, "chatId": chat_id}))
    return out


def post_stats_frame(chat_id: int, message_ids: list[int]) -> dict:
    """MSG_GET_STAT 74, readOnly always (Р-77): without it MAX counts a view, as the client does
    when a post is on screen."""
    return {"chatId": chat_id, "messageIds": list(message_ids), "readOnly": True}


async def post_stats(client: Any, chat_id: int, message_ids: list[int]) -> dict:
    """→ {stats: {<message id>: {views?, forwards?}}}."""
    return _payload(await client._app.invoke(Opcode.MSG_GET_STAT, post_stats_frame(chat_id, message_ids)))


# -- release 5: blocking ------------------------------------------------------------

def block_frame(user_id: int, *, block: bool) -> dict:
    """CONTACT_UPDATE 34 {contactId, action BLOCK / UNBLOCK} (Р-72)."""
    return {"contactId": user_id, "action": "BLOCK" if block else "UNBLOCK"}


async def set_blocked(client: Any, user_id: int, *, block: bool) -> dict:
    return _payload(await client._app.invoke(Opcode.CONTACT_UPDATE, block_frame(user_id, block=block)))


BLOCKED_PAGE = 200


def blocked_frame(start: int = 0, count: int = BLOCKED_PAGE) -> dict:
    return {"status": "BLOCKED", "from": start, "count": count}


async def blocked_ids(client: Any, *, pages: int = 5) -> set[int]:
    """CONTACT_LIST 36 status BLOCKED: ids of the blocked contacts, up to pages × 200."""
    out: set[int] = set()
    for page in range(pages):
        contacts = _payload(await client._app.invoke(Opcode.CONTACT_LIST, blocked_frame(page * BLOCKED_PAGE))
                            ).get("contacts") or []
        out |= {int(c["id"]) for c in contacts if isinstance(c, dict) and c.get("id") is not None}
        if len(contacts) < BLOCKED_PAGE:
            break
    return out


# -- release 5: scheduled (delayed) messages, Р-73 ---------------------------------

def delayed_attrs(when_ms: int, *, notify: bool = True) -> dict:
    """message.delayedAttributes of 64 / 67 (qs: timeToFire ms, notifySender?)."""
    return {"timeToFire": when_ms, "notifySender": notify}


def schedule_frame(chat_id: int, text: str, when_ms: int, reply_to: int | None = None,
                   elements: list | None = None) -> dict:
    """MSG_SEND 64 of a delayed text, PyMax's shape plus delayedAttributes; text and elements as
    prepare_text made them (Р-78). Ours, not PyMax's send_message(send_at=): PyMax's Message
    requires delayedAttributes.notifyOpponents, the client's schema does not — an answer without
    it would fail after the frame went out."""
    message: dict = {"text": text, "cid": int(time.time() * 1000), "elements": dump_elements(elements),
                     "attaches": [], "delayedAttributes": delayed_attrs(when_ms)}
    if reply_to:
        message["link"] = {"type": "REPLY", "messageId": reply_to}
    return {"chatId": chat_id, "message": message, "notify": True}


async def schedule(client: Any, chat_id: int, text: str, when_ms: int, reply_to: int | None = None,
                   elements: list | None = None) -> dict:
    """→ MAX's raw answer {chatId, message}; its id is read apart (R17-1)."""
    return _payload(await client._app.invoke(Opcode.MSG_SEND, schedule_frame(chat_id, text, when_ms, reply_to,
                                                                             elements)))


def list_delayed_frame(chat_id: int) -> dict:
    """CHAT_HISTORY 49 of the chat's delayed messages, as the client reads them."""
    return {"chatId": chat_id, "from": 1, "forward": 100, "getMessages": True, "itemType": "DELAYED"}


async def list_delayed(client: Any, chat_id: int) -> list[dict]:
    """The chat's delayed messages as MAX sends them (raw dicts: text and elements kept as they are)."""
    payload = _payload(await client._app.invoke(Opcode.CHAT_HISTORY, list_delayed_frame(chat_id)))
    return [m for m in payload.get("messages") or [] if isinstance(m, dict)]


def cancel_delayed_frame(chat_id: int, message_ids: list[int]) -> dict:
    """MSG_DELETE 66 of delayed messages (forMe false: nobody gets them)."""
    return {"chatId": chat_id, "messageIds": list(message_ids), "forMe": False, "itemType": "DELAYED"}


async def cancel_delayed(client: Any, chat_id: int, message_ids: list[int]) -> dict:
    return _payload(await client._app.invoke(Opcode.MSG_DELETE, cancel_delayed_frame(chat_id, message_ids)))


def fire_now_frame(chat_id: int, message: dict, now_ms: int) -> dict:
    """MSG_EDIT 67 with delayedAttributes timeToFire now — the client's "send now". Text and
    elements go back as MAX keeps them, not re-parsed."""
    frame = {"chatId": chat_id, "messageId": int(message["id"]), "delayedAttributes": {"timeToFire": now_ms}}
    if message.get("text"):
        frame["text"] = message["text"]
    if message.get("elements"):
        frame["elements"] = message["elements"]
    return frame


async def fire_now(client: Any, chat_id: int, message: dict) -> dict:
    return _payload(await client._app.invoke(Opcode.MSG_EDIT, fire_now_frame(chat_id, message,
                                                                             int(time.time() * 1000))))


# -- release 5: text as is, markup by parse_mode (Р-78) ------------------------------

PARSE_MODES = ("markdown", "html")


class TextMarkupError(ValueError):
    """parse_mode text that cannot be turned into MAX elements (→ 422 before the slot)."""


def prepare_text(text: str | None, parse_mode: str | None) -> tuple[str | None, list[Element]]:
    """(text, elements) to send. No parse_mode — the text byte for byte, no elements (PyMax 2.4.1
    ran every text through its markdown and ate "_" in links and file names); "markdown" —
    PyMax's Formatter as before; "html" — TG's tag subset (app/max/html_text.py)."""
    if not text:
        return text or None, []
    if parse_mode is None:
        return text, []
    mode = parse_mode.lower()
    if mode == "markdown":
        clean, elements = Formatter.format_markdown(text)
        return clean, list(elements)
    if mode == "html":
        try:
            plain, items = html_text.html_to_elements(text)
        except html_text.HtmlTextError as exc:
            raise TextMarkupError(f"parse_mode html: {exc}") from exc
        return plain, [Element(type=i["type"], from_=i["from"], length=i["length"],
                               attributes=ElementAttributes(url=i["url"]) if "url" in i else None) for i in items]
    raise TextMarkupError(f"parse_mode {parse_mode!r}: markdown, html or none")


def dump_elements(elements: list | None) -> list[dict]:
    """Elements as the frame carries them ({type, from, length, attributes?})."""
    return [e.model_dump(by_alias=True, exclude_none=True) if isinstance(e, Element) else e for e in elements or []]
