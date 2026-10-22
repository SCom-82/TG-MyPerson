"""MAX bots (plan 2026-10-22 Р5 Р-65, Р-66): /api/v1/bots/..., MAX-only routes.

start_bot sends the bot's /start (64 CONTROL botStarted) to the dialog with it — a
send of the write limits, a new dialog the first time. get_bot_info reads a bot's
commands, profile and start message (145), or the bots of a chat (144).
"""

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.authz.write_limits import SEND, write_slot
from app.max import frames, groups, normalize
from app.max.api import groups as mx_groups
from app.max.api.common import call_max, call_max_read, store_sent

log = logging.getLogger(__name__)

router = APIRouter(prefix="/bots", tags=["max-bots"])
UNCERTAIN = "/start may have been sent, check list_messages of the dialog with the bot"


class StartBotRequest(BaseModel):
    bot_id: int
    start_payload: str | None = Field(default=None, min_length=1, max_length=512)


@router.post("/start", name="start_bot")
async def start_bot(req: StartBotRequest, request: Request):
    """/start of a bot, with max.ru/<bot>?start=<payload>'s payload. The target is checked to be a
    bot before the slot (CONTACT_INFO options BOT): not a bot → 422. The dialog id is me ^ bot
    (MAX's rule for dialogs; not checked live for a bot), by userId if MAX refuses it."""
    session = await mx_groups.connected(request)
    me = mx_groups.me_of(session)
    if not await call_max_read(groups.is_bot(session.client, req.bot_id)):
        raise HTTPException(status_code=422, detail=f"user {req.bot_id} is not a bot")
    if me is None:
        raise HTTPException(status_code=503, detail="the account's own id is not known yet")
    request.state.audit_target_user_id = req.bot_id
    dialog = me ^ req.bot_id
    async with write_slot(request, SEND, "start_bot", dialog):
        answer = await call_max(frames.start_bot(session.client, dialog, req.bot_id, req.start_payload), UNCERTAIN)
    out = {"status": "started", "bot_id": req.bot_id, "chat_id": answer.get("chatId", dialog), "message_id": None}
    try:
        sent = frames.parse_sent(answer, dialog)
    except Exception:  # noqa: BLE001 — /start is out; the echo stores it
        log.warning("max[%s]: the answer of /start does not parse", session.alias, exc_info=True)
        sent = None
    if sent is not None:
        await store_sent(session, sent, sent.chat_id)
        out.update(chat_id=sent.chat_id, message_id=sent.id)
    if isinstance(answer.get("chat"), dict):
        await mx_groups.store_chat(session, normalize.parse_chat(answer))
    return out


def _profile(user) -> dict | None:
    if user is None:
        return None
    row = normalize.normalize_user(user)
    return {"user_id": row["id"], "first_name": row["first_name"], "last_name": row["last_name"],
            "username": row["username"], "description": user.description, "is_bot": row["is_bot"]}


def _commands(items, bot_id: int | None = None) -> list[dict]:
    return [{"name": c.get("name"), "description": c.get("description")} for c in items or []
            if isinstance(c, dict) and (bot_id is None or int(c.get("botId") or 0) in (0, bot_id))]


@router.get("/info", name="get_bot_info")
async def get_bot_info(request: Request, bot_id: int | None = Query(None), chat_id: int | None = Query(None)):
    """bot_id → its commands, profile and start message (145); chat_id → the bots of the chat with
    their commands (144). One of the two."""
    if (bot_id is None) == (chat_id is None):
        raise HTTPException(status_code=422, detail="pass bot_id or chat_id (one of them)")
    if chat_id is not None:
        await mx_groups.known_chat(request, chat_id, kinds=("private", "group", "channel"))
    session = await mx_groups.connected(request)
    if bot_id is not None:
        payload = await call_max_read(frames.bot_info(session.client, bot_id))
        start = payload.get("startMessage") or {}
        text = start.get("text") if isinstance(start, dict) else None
        return {"bot_id": bot_id, "profile": _profile(frames.parse_user(payload.get("contact"))),
                "commands": _commands(payload.get("commands")),
                "start_message": text.get("text") if isinstance(text, dict) else text}
    payload = await call_max_read(frames.chat_bot_commands(session.client, chat_id))
    contacts = payload.get("contacts") or {}
    commands = [c for c in payload.get("commands") or [] if isinstance(c, dict)]
    ids = list(dict.fromkeys([int(k) for k in contacts] + [int(c["botId"]) for c in commands if c.get("botId")]))
    bots = []
    for bid in ids:
        user = frames.parse_user(contacts.get(str(bid)) or contacts.get(bid))
        bots.append({"bot_id": bid, "profile": _profile(user),
                     "commands": [{"name": c.get("name"), "description": c.get("description")}
                                  for c in commands if int(c.get("botId") or 0) == bid]})
    return {"chat_id": chat_id, "bots": bots}
