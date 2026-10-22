"""MAX messages (API spec §3, §4).

Reads: list_messages, get_single_message, download_media.
Writes (PR-7): send_message, send_file, forward_message, edit_message,
delete_message, react_to_message — any chat, within the account's write
limits (app/authz/write_limits.py). What we sent is stored at once, not on
the echo. Parameters MAX has no counterpart for (schedule_date, scheduled
edits, a quiz) → 501.

Release 4 (plan 2026-10-19 Р4 PR-R4-1): voice, video and video notes, albums
(one MAX message with several attachments), polls and votes, pin / unpin,
reactions of a message. A file is read, classified and its duration taken
BEFORE the write slot: a bad file costs no limit (Р-35).

Release 5 (plan 2026-10-22 Р5 Р-64, Р-67…Р-71, Р-77): bot buttons (press_button),
who reacted, poll voters, a message link, transcription of a voice, MAX's search in
a chat, views and forwards of posts. Reads are reads for ro accounts too.
"""

import asyncio
import logging
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.authz import write_limits
from app.authz.write_limits import ACTION, SEND, WriteLimited, write_slot
from app.database import async_session, get_db
from app.api import data_reads
from pydantic import BaseModel, Field

from app.max import frames, groups, media, normalize, repo
from app.max.config import max_settings
from app.max.api import groups as mx_groups
from app.services.tg_store import new_mode
from app.max.api.common import (
    authorized_or_error,
    call_max,
    call_max_read,
    param_not_supported,
    prepared_text,
    send_text,
    store_sent,
)
from app.max.schemas import MaxMessageResponse
from app.schemas import (
    EditMessageRequest,
    ForwardMessageRequest,
    PaginatedResponse,
    ReactRequest,
    SendMessageRequest,
    SendPollRequest,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/messages", tags=["max-messages"])


@router.get("", response_model=PaginatedResponse, name="list_messages")
async def list_messages(
    chat_id: int | None = Query(None),
    from_user_id: int | None = Query(None),
    search: str | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    message_type: str | None = Query(None),
    is_outgoing: bool | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    if new_mode():
        return await data_reads.list_messages(
            request, chat_id=chat_id, from_user_id=from_user_id, search=search, date_from=date_from,
            date_to=date_to, message_type=message_type, is_outgoing=is_outgoing, limit=limit, offset=offset,
            cls=MaxMessageResponse,
        )
    # Deleted messages are listed too (with deleted_at), like TG keeps them (ADR §2.G).
    messages, total = await repo.get_messages(
        db,
        chat_id=chat_id,
        from_user_id=from_user_id,
        search=search,
        date_from=date_from,
        date_to=date_to,
        message_type=message_type,
        is_outgoing=is_outgoing,
        limit=limit,
        offset=offset,
    )
    return PaginatedResponse(
        items=[MaxMessageResponse.model_validate(m) for m in messages], total=total, limit=limit, offset=offset
    )


# ---------------------------------------------------------------------------
# Search in a chat, post stats (Р-71, Р-77): before /{chat_id}/{message_id}, which
# would take "search" / "stats" for a message id (route order decides, authz too)
# ---------------------------------------------------------------------------

SEARCH_MAX = 30
STATS_MAX = 100


@router.get("/{chat_id}/search", name="search_chat_messages")
async def search_chat_messages(chat_id: int, request: Request, query: str = Query(min_length=1, max_length=200),
                               limit: int = Query(20, ge=1, le=SEARCH_MAX), marker: int | None = Query(None)):
    """MAX's own search in one chat (73). What it finds is stored as read from MAX, so
    get_message / list_messages see it; search_global keeps searching our base."""
    await mx_groups.known_chat(request, chat_id, kinds=("private", "group", "channel"))
    session = await mx_groups.connected(request)
    payload = await call_max_read(frames.search_chat(session.client, chat_id, query, limit, marker))
    try:
        found = frames.found_messages(payload, chat_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"MAX's search answer was not understood: "
                                                    f"{type(exc).__name__}: {exc}")
    for msg in found:
        await store_sent(session, msg, chat_id)
    return {"chat_id": chat_id, "query": query, "total": payload.get("total"), "next_marker": payload.get("marker"),
            "items": [{"message_id": m.id, "from_user_id": m.sender, "date": normalize.ms_to_dt(m.time),
                       "text": m.text or None} for m in found]}


@router.get("/{chat_id}/stats", name="get_post_stats")
async def get_post_stats(chat_id: int, request: Request, message_ids: list[int] = Query(...)):
    """Views and forwards of posts (74, readOnly: true — reading does not count a view, Р-77).
    A post MAX did not answer for is in missing, no zero is made up. Not stored: counts change."""
    ids = list(dict.fromkeys(message_ids))
    if not 1 <= len(ids) <= STATS_MAX:
        raise HTTPException(status_code=422, detail=f"message_ids: 1…{STATS_MAX}")
    await mx_groups.known_chat(request, chat_id, kinds=("group", "channel"))
    session = await mx_groups.connected(request)
    stats = (await call_max_read(frames.post_stats(session.client, chat_id, ids))).get("stats") or {}
    items, missing = [], []
    for mid in ids:
        entry = stats.get(str(mid)) or stats.get(mid)
        if not isinstance(entry, dict):
            missing.append(mid)
            continue
        items.append({"message_id": mid, "views": entry.get("views"), "forwards": entry.get("forwards")})
    return {"chat_id": chat_id, "items": items, "missing": missing}


POLL_REFRESH_SEC = 10


@router.get("/{chat_id}/{message_id}", response_model=MaxMessageResponse, name="get_single_message")
async def get_single_message(chat_id: int, message_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """A message with an open poll is re-read from MAX first (Р4.1): the stored copy holds the
    counts as of its arrival, votes do not come as frames. A failed re-read answers the stored copy."""
    if new_mode():
        found = await data_reads.get_message(request, chat_id, message_id, MaxMessageResponse)
        if found.poll is not None and not found.poll.closed and await _refresh_poll(request, chat_id, message_id):
            found = await data_reads.get_message(request, chat_id, message_id, MaxMessageResponse)
        return found
    msg = await repo.get_message(db, chat_id, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return MaxMessageResponse.model_validate(msg)


async def _refresh_poll(request: Request, chat_id: int, message_id: int, session=None) -> bool:
    """The message fresh from MAX (a read: ro too), stored over the old copy; False on any failure."""
    if session is None:
        session = await authorized_or_error(request)
        if isinstance(session, JSONResponse):
            return False
    try:
        fresh = await asyncio.wait_for(session.client.get_message(chat_id, message_id), POLL_REFRESH_SEC)
    except Exception as exc:  # noqa: BLE001
        log.info("max[%s]: poll %s/%s not re-read: %s: %s", session.alias, chat_id, message_id,
                 type(exc).__name__, exc)
        return False
    if fresh is None or frames.find_poll(fresh) is None:
        return False
    await store_sent(session, fresh, chat_id)
    return True


def _content_disposition(name: str) -> str:
    """Same as the TG endpoint: ASCII fallback + RFC 5987 name (Cyrillic file names)."""
    try:
        name.encode("ascii")
        ascii_name = name
    except UnicodeEncodeError:
        ascii_name = "download"
    encoded = urllib.parse.quote(name, safe="")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded}'


def _range_not_satisfiable(size: str) -> JSONResponse:
    return JSONResponse(
        status_code=416, content={"detail": "Range Not Satisfiable"}, headers={"Content-Range": f"bytes */{size}"}
    )


@router.get("/{chat_id}/{message_id}/media", name="download_media")
async def download_media(
    chat_id: int,
    message_id: int,
    request: Request,
    index: int = Query(0, ge=0, description="Attachment number in the message (MAX: several per message)"),
    db: AsyncSession = Depends(get_db),
):
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    client = session.client

    # 1. The attachment: from the stored raw message, else from MAX.
    if new_mode():
        raw = await data_reads.stored_raw(request, chat_id, message_id)
    else:
        stored = await repo.get_message(db, chat_id, message_id)
        raw = dict(stored.raw_data) if stored is not None and stored.raw_data else None
    if raw is None:
        try:
            fetched = await client.get_message(chat_id, message_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")
        if fetched is None:
            raise HTTPException(status_code=404, detail="Message not found")
        raw = media.message_from_client(fetched)
    raw.pop("_pymax", None)
    try:
        ref = media.attachment_ref(raw, index)  # own attachments, then a forwarded message's (Р-6)
    except media.MediaNotFound:
        raise HTTPException(status_code=404, detail="Message has no media")

    # 2. Its URL.
    try:
        resolved = await media.resolve(client, chat_id, message_id, ref.attachment, None, origin=ref.origin)
    except media.MediaNotFound:
        raise HTTPException(status_code=404, detail="No downloadable media")
    except media.MediaUpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # 3. Range (prefix form bytes=N- only, for resume — as TG).
    offset = 0
    match = re.match(r"bytes=(\d+)-$", request.headers.get("range", "").strip())
    if match:
        offset = int(match.group(1))
        if resolved.size is not None and offset >= resolved.size:
            return _range_not_satisfiable(str(resolved.size))

    # 4. Upstream through the MAX proxy; fail before any byte is sent.
    try:
        upstream = await media.open_stream(resolved.url, proxy_url=session.settings.proxy_url, offset=offset,
                                           user_agent=media.client_user_agent(client))
    except media.RangeNotSatisfiable as exc:
        return _range_not_satisfiable(exc.size)
    except media.MediaUpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    total = upstream.total if upstream.total is not None else resolved.size
    headers = {"Accept-Ranges": "bytes", "Content-Disposition": _content_disposition(resolved.filename)}
    status_code = 206 if offset else 200
    if total is not None:
        headers["X-Expected-Size"] = str(total)
        headers["Content-Length"] = str(total - offset)
        if offset:
            headers["Content-Range"] = f"bytes {offset}-{total - 1}/{total}"
    return StreamingResponse(upstream.chunks(), status_code=status_code, media_type=upstream.content_type,
                             headers=headers)


# ---------------------------------------------------------------------------
# Writes (phase 2, API spec §4)
# ---------------------------------------------------------------------------

SCHEDULE_MIN = timedelta(seconds=60)
SCHEDULE_MAX = timedelta(days=365)


def _schedule_at(value: datetime) -> datetime:
    """schedule_date in UTC, now + 60 s … now + 365 days (Р-73), else 422 before the slot."""
    when = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if not now + SCHEDULE_MIN <= when <= now + SCHEDULE_MAX:
        raise HTTPException(status_code=422, detail="schedule_date: from now + 60 s to now + 365 days")
    return when


@router.post("", name="send_message")
async def send_message(req: SendMessageRequest, request: Request):
    """The text goes as is; parse_mode markdown / html makes markup (Р5 Р-78). schedule_date (Р-73):
    MAX sends the text at that time; it is not stored as sent until MAX sends it."""
    if req.schedule_date is not None and not max_settings.scheduled_enabled:
        return param_not_supported(request, "send_message", "schedule_date")
    when = _schedule_at(req.schedule_date) if req.schedule_date is not None else None
    text, elements = prepared_text(req.text, req.parse_mode)
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    if when is None:
        msg = await send_text(request, session, "send_message", req.chat_id, req.text,
                              reply_to=req.reply_to_message_id, parse_mode=req.parse_mode)
        return {"status": "sent", "message_id": msg.id, "chat_id": req.chat_id, "schedule_date": None}
    async with write_slot(request, SEND, "send_message", req.chat_id):
        answer = await call_max(frames.schedule(session.client, req.chat_id, text,
                                                int(when.timestamp() * 1000), req.reply_to_message_id, elements),
                                "the message may have been scheduled, check list_scheduled before retrying")
    message = answer.get("message") if isinstance(answer.get("message"), dict) else {}
    return {"status": "scheduled", "message_id": message.get("id"), "chat_id": req.chat_id,
            "schedule_date": when.isoformat()}


# ---------------------------------------------------------------------------
# Scheduled messages (Р-73): the TG paths and names; MAX_SCHEDULED_ENABLED=false → 501
# ---------------------------------------------------------------------------

def _scheduled_off(request: Request, tool: str) -> JSONResponse | None:
    if max_settings.scheduled_enabled:
        return None
    return JSONResponse(status_code=501, content={
        "error": f"{tool} is switched off for platform max (MAX_SCHEDULED_ENABLED=false)", "tool": tool,
        "platform": "max", "alias": getattr(request.state, "session_alias", "")})


def _scheduled_item(chat_id: int, m: dict) -> dict:
    attrs = m.get("delayedAttributes") if isinstance(m.get("delayedAttributes"), dict) else {}
    attaches = [a for a in m.get("attaches") or [] if isinstance(a, dict)]
    return {"message_id": int(m["id"]), "chat_id": chat_id, "text": m.get("text") or None,
            "date": normalize.ms_to_dt(attrs.get("timeToFire") or m.get("time")),
            "has_media": bool(attaches), "media_type": attaches[0].get("_type") if attaches else None}


@router.get("/scheduled", name="list_scheduled")
async def list_scheduled(request: Request, chat_id: int = Query(...)):
    off = _scheduled_off(request, "list_scheduled")
    if off is not None:
        return off
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    found = await call_max_read(frames.list_delayed(session.client, chat_id))
    items = [_scheduled_item(chat_id, m) for m in found if m.get("id") is not None]
    return {"items": items, "total": len(items), "chat_id": chat_id}


@router.delete("/scheduled/{chat_id}/{message_id}", name="cancel_scheduled")
async def cancel_scheduled(chat_id: int, message_id: int, request: Request):
    off = _scheduled_off(request, "cancel_scheduled")
    if off is not None:
        return off
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    async with write_slot(request, ACTION, "cancel_scheduled", chat_id):
        await call_max(frames.cancel_delayed(session.client, chat_id, [message_id]),
                       "it may have been cancelled, check list_scheduled")
    return {"status": "cancelled", "chat_id": chat_id, "message_id": message_id}


@router.post("/scheduled/{chat_id}/{message_id}/send-now", name="send_scheduled_now")
async def send_scheduled_now(chat_id: int, message_id: int, request: Request):
    """The client's "send now": 67 with timeToFire now; text and elements as MAX keeps them. An
    action slot (review 19 R19-1): not a second send, but the account's write stop holds it. With
    media → 422 (R19-2): 67 without the attachments would edit them away; send it from the app."""
    off = _scheduled_off(request, "send_scheduled_now")
    if off is not None:
        return off
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    found = await call_max_read(frames.list_delayed(session.client, chat_id))
    message = next((m for m in found if m.get("id") is not None and int(m["id"]) == message_id), None)
    if message is None:
        raise HTTPException(status_code=404, detail=f"no scheduled message {message_id} in chat {chat_id}")
    if message.get("attaches"):
        raise HTTPException(status_code=422, detail="a scheduled message with media is sent now from the MAX app")
    async with write_slot(request, ACTION, "send_scheduled_now", chat_id):
        await call_max(frames.fire_now(session.client, chat_id, message),
                       "it may have been sent, check list_messages")
    return {"status": "sent", "chat_id": chat_id, "message_id": message_id}


def _attachment(data: bytes, name: str, **kind) -> object:
    """media.upload_attachment, an unreadable duration → 422 before the slot (Р-35)."""
    try:
        return media.upload_attachment(data, name, **kind)
    except media.DurationUnknown as exc:
        raise HTTPException(status_code=422, detail=str(exc))


async def _send_attachments(request: Request, session, tool: str, chat_id: int, text: str | None,
                            reply_to: int | None, attachments: list, parse_mode: str | None = None) -> object:
    """One MSG_SEND with the attachments, one send of the limits; stored at once. The caption as is,
    markup by parse_mode (Р5 Р-78). A failed upload (UploadError) is a 502 and gives the slot back:
    nothing was sent."""
    text, elements = prepared_text(text, parse_mode)
    async with write_slot(request, SEND, tool, chat_id):
        msg = await call_max(session.client.send_message(
            chat_id, text or None, reply_to=reply_to, attachments=attachments, elements=elements,
        ))
    await store_sent(session, msg, chat_id)
    return msg


def _sent(msg, chat_id: int) -> dict:
    return {"status": "sent", "message_id": msg.id, "chat_id": chat_id, "schedule_date": None}


@router.post("/send-file", name="send_file")
async def send_file(
    request: Request,
    chat_id: int = Form(...),
    file: UploadFile = File(...),
    caption: str | None = Form(None),
    reply_to_message_id: int | None = Form(None),
    parse_mode: Literal["markdown", "html"] | None = Form(None),
    schedule_date: datetime | None = Form(None),
    voice_note: bool = Form(False),
    force_document: bool = Form(False),
):
    """Images (by extension) go as a photo and .mp4/.mov/.m4v as a video unless
    force_document, the rest as a file; voice_note=true → a voice message (Р-36).
    A video upload can take up to a minute (MAX processes it before the send)."""
    if schedule_date is not None:
        return param_not_supported(request, "send_file", "schedule_date")
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    name = file.filename or ("voice.ogg" if voice_note else "file.bin")
    attachment = _attachment(await file.read(), name, force_document=force_document, voice=voice_note)
    msg = await _send_attachments(request, session, "send_file", chat_id, caption, reply_to_message_id,
                                  [attachment], parse_mode)
    return _sent(msg, chat_id)


@router.post("/send-voice", name="send_voice")
async def send_voice(
    request: Request,
    chat_id: int = Form(...),
    file: UploadFile = File(...),
    caption: str | None = Form(None),
    reply_to_message_id: int | None = Form(None),
    parse_mode: Literal["markdown", "html"] | None = Form(None),
    schedule_date: datetime | None = Form(None),
):
    """send_file with voice_note=true (the TG form): .ogg/.opus, .m4a, .mp3."""
    if schedule_date is not None:
        return param_not_supported(request, "send_voice", "schedule_date")
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    attachment = _attachment(await file.read(), file.filename or "voice.ogg", voice=True)
    msg = await _send_attachments(request, session, "send_voice", chat_id, caption, reply_to_message_id,
                                  [attachment], parse_mode)
    return _sent(msg, chat_id)


@router.post("/send-video-note", name="send_video_note")
async def send_video_note(
    request: Request,
    chat_id: int = Form(...),
    file: UploadFile = File(...),
    reply_to_message_id: int | None = Form(None),
    duration_ms: int | None = Form(None, ge=1, le=600_000),
):
    """A round video message (MAX-only): .mp4, square, up to 60 s; duration_ms wins over the file's."""
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    attachment = _attachment(await file.read(), file.filename or "note.mp4", video_note=True,
                             duration_ms=duration_ms)
    msg = await _send_attachments(request, session, "send_video_note", chat_id, None, reply_to_message_id,
                                  [attachment])
    return _sent(msg, chat_id)


@router.post("/album", name="send_album")
async def send_album(
    request: Request,
    chat_id: int = Form(...),
    files: list[UploadFile] = File(...),
    caption: str | None = Form(None),
    reply_to_message_id: int | None = Form(None),
    parse_mode: Literal["markdown", "html"] | None = Form(None),
    schedule_date: datetime | None = Form(None),
):
    """2-10 files in ONE MAX message (Р-37): photos, videos, files by send_file's rules;
    one send of the limits. Each video can take up to a minute to upload."""
    if schedule_date is not None:
        return param_not_supported(request, "send_album", "schedule_date")
    if len(files) < 2:
        raise HTTPException(status_code=400, detail="Album requires at least 2 files")
    if len(files) > 10:
        raise HTTPException(status_code=400, detail="Album supports max 10 files")
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    attachments = [_attachment(await f.read(), f.filename or f"file{i}.bin") for i, f in enumerate(files)]
    msg = await _send_attachments(request, session, "send_album", chat_id, caption, reply_to_message_id,
                                  attachments, parse_mode)
    return {"status": "sent", "message_ids": [msg.id], "chat_id": chat_id, "schedule_date": None}


@router.post("/poll", name="send_poll")
async def send_poll(req: SendPollRequest, request: Request):
    """A poll (Р-46): allows_multiple → MAX's MULTISELECT flag. A quiz → 501.

    Not anonymous on MAX unless asked (Р4.1): the live MAX refused ANONYMOUS (settings 1)
    with "Invalid poll settings" and dropped the connection (19.10). The flag MAX wants
    is unknown, so is_anonymous=true sent explicitly → 501, no frame; the TG default
    (true) of the shared schema does not apply here."""
    if req.quiz_correct_option is not None:
        return param_not_supported(request, "send_poll", "quiz_correct_option")
    anonymous = req.is_anonymous if "is_anonymous" in req.model_fields_set else False
    if anonymous:
        return param_not_supported(request, "send_poll", "is_anonymous")
    if req.schedule_date is not None:
        return param_not_supported(request, "send_poll", "schedule_date")
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    poll = frames.poll_attach(req.question, req.options, anonymous=False, multiple=req.allows_multiple)
    msg = await _send_attachments(request, session, "send_poll", req.chat_id, None, req.reply_to_message_id, [poll])
    out = _sent(msg, req.chat_id)
    sent = frames.find_poll(msg)
    if sent is not None:
        view = frames.poll_view(sent)
        out.update(poll_id=view["poll_id"], answers=[{"answer_id": a["answer_id"], "text": a["text"]}
                                                     for a in view["answers"]])
    else:
        out.update(poll_id=None, answers=None,
                   warning="MAX answered without the poll: get_message shows its poll_id and answers")
    return out


@router.post("/forward", name="forward_message")
async def forward_message(req: ForwardMessageRequest, request: Request):
    """One MAX call — and one send of the limits — per message.

    When a limit stops the batch after some messages went out, the answer is
    200 status=partial with the forwarded ids and the limit, so a retry does
    not forward them twice.
    """
    ids = req.message_ids if req.message_ids else ([req.message_id] if req.message_id else [])
    if not ids:
        raise HTTPException(status_code=400, detail="Provide message_id or message_ids")
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    forwarded: list[int] = []
    for message_id in ids:
        try:
            async with write_slot(request, SEND, "forward_message", req.to_chat_id):
                msg = await call_max(session.client.forward_message(
                    req.to_chat_id, message_id, source_chat_id=req.from_chat_id,
                ))
        except (WriteLimited, HTTPException) as exc:
            if not forwarded:
                raise
            body = exc.body if isinstance(exc, WriteLimited) else {"error": exc.detail}
            return {"status": "partial", "message_ids": forwarded, "to_chat_id": req.to_chat_id,
                    "not_forwarded": ids[len(forwarded):], "stopped_by": body}
        await store_sent(session, msg, req.to_chat_id)
        forwarded.append(msg.id)
    return {"status": "forwarded", "message_ids": forwarded, "to_chat_id": req.to_chat_id}


@router.post("/edit", name="edit_message")
async def edit_message(req: EditMessageRequest, request: Request):
    if req.scheduled:
        return param_not_supported(request, "edit_message", "scheduled")
    text, elements = prepared_text(req.text, req.parse_mode)  # as is unless parse_mode (Р5 Р-78)
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    async with write_slot(request, ACTION, "edit_message", req.chat_id):
        msg = await call_max(session.client.edit_message(req.chat_id, req.message_id, text=text, elements=elements))
    await store_sent(session, msg, req.chat_id, is_edit=True)
    return {"status": "edited", "chat_id": req.chat_id, "message_id": req.message_id}


@router.delete("/{chat_id}/{message_id}", name="delete_message")
async def delete_message(
    chat_id: int,
    message_id: int,
    request: Request,
    for_all: bool = Query(True, description="false: delete only for this account"),
):
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    async with write_slot(request, ACTION, "delete_message", chat_id):
        await call_max(session.client.delete_message(chat_id, [message_id], for_me=not for_all))
    if session.ingest is not None:
        try:
            await session.ingest.apply_delete(chat_id, [message_id])
        except Exception:  # noqa: BLE001 — deleted in MAX; the delete frame repeats it
            log.warning("max[%s]: deleted %s/%s, local mark failed", session.alias, chat_id, message_id,
                        exc_info=True)
    return {"status": "deleted", "chat_id": chat_id, "message_id": message_id, "for_all": for_all}


@router.post("/{chat_id}/{message_id}/react", name="react_to_message")
async def react_to_message(chat_id: int, message_id: int, req: ReactRequest, request: Request):
    """emoticon → add_reaction (MAX reaction id = the emoji), null → remove_reaction."""
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    async with write_slot(request, ACTION, "react_to_message", chat_id):
        if req.emoticon:
            await call_max(session.client.add_reaction(chat_id, message_id, req.emoticon))
        else:
            await call_max(session.client.remove_reaction(chat_id, message_id))
    return {
        "status": "reacted" if req.emoticon else "unreacted",
        "chat_id": chat_id,
        "message_id": message_id,
        "emoticon": req.emoticon,
    }


# ---------------------------------------------------------------------------
# Votes, pins, reactions (plan 2026-10-19 Р4 Р-33, Р-45, Р-47)
# ---------------------------------------------------------------------------

UNCERTAIN = "the change may have been applied, check get_chat / get_message"


class VoteRequest(BaseModel):
    answer_ids: list[int] = Field(min_length=1, max_length=10)
    poll_id: int | None = None


async def _message(request: Request, session, chat_id: int, message_id: int):
    """The message as PyMax's Message: the stored raw, else from MAX (a read, before any slot)."""
    if new_mode():
        raw = await data_reads.stored_raw(request, chat_id, message_id)
    else:
        async with async_session() as db:
            stored = await repo.get_message(db, chat_id, message_id)
        raw = dict(stored.raw_data) if stored is not None and stored.raw_data else None
    if raw is not None:
        try:
            return normalize.parse_stored(raw)
        except Exception:  # noqa: BLE001 — a stored copy PyMax cannot read: ask MAX
            log.info("max: stored %s/%s does not parse, reading it from MAX", chat_id, message_id)
    fetched = await call_max_read(session.client.get_message(chat_id, message_id))
    if fetched is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return fetched


@router.post("/{chat_id}/{message_id}/vote", name="vote_poll")
async def vote_poll(chat_id: int, message_id: int, req: VoteRequest, request: Request):
    """Vote in the message's poll. Checked before the slot: no poll → 404, an answer not of
    the poll or several without multiselect → 422, closed → 409. A repeated vote without
    revote is MAX's to refuse (→ 502 with its text)."""
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    poll = frames.find_poll(await _message(request, session, chat_id, message_id))
    if poll is None:
        raise HTTPException(status_code=404, detail="the message has no poll")
    view = frames.poll_view(poll)
    if req.poll_id is not None and req.poll_id != view["poll_id"]:
        raise HTTPException(status_code=422, detail=f"poll_id {req.poll_id} is not the message's poll "
                                                    f"{view['poll_id']}")
    known = [a["answer_id"] for a in view["answers"]]
    answers = list(dict.fromkeys(req.answer_ids))
    unknown = [a for a in answers if a not in known]
    if unknown:
        raise HTTPException(status_code=422, detail=f"answer_ids {unknown} are not answers of the poll; "
                                                    f"its answers: {known}")
    if len(answers) > 1 and not view["multiselect"]:
        raise HTTPException(status_code=422, detail="the poll takes one answer (no multiselect)")
    if view["closed"]:
        raise HTTPException(status_code=409, detail="the poll is closed")
    # The slot covers the frame only: an answer we cannot parse does not give back the
    # slot of a vote that reached MAX (review 17 R17-1).
    async with write_slot(request, ACTION, "vote_poll", chat_id):
        payload = await call_max(frames.vote(session.client, chat_id, message_id, view["poll_id"], answers),
                                 UNCERTAIN)
    try:
        state = frames.parse_vote(payload)
    except Exception as exc:  # noqa: BLE001
        log.warning("max[%s]: vote answer not understood: %s: %s", session.alias, type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail=f"MAX took the vote, its answer was not understood "
                                                    f"({type(exc).__name__}); check get_message before retrying")
    await _refresh_poll(request, chat_id, message_id, session)  # the stored copy shows the vote too
    return {"chat_id": chat_id, "message_id": message_id, "poll_id": view["poll_id"], "answer_ids": answers,
            "state": frames.poll_state(state)}


async def _pinned_after(session, chat_id: int) -> tuple[bool, int | None]:
    """(read, pinned message id) of the fresh chat, stored; (False, None) if the read failed."""
    try:
        chat = await groups.fresh_chat(session.client, chat_id)
    except Exception:  # noqa: BLE001 — the pin is done in MAX; say it is not checked
        log.warning("max[%s]: chat %s not re-read after a pin change", session.alias, chat_id, exc_info=True)
        return False, None
    await mx_groups.store_chat(session, chat)
    return True, frames.pinned_id(chat)


def _not_as_asked(detail: str) -> HTTPException:
    """MAX took the frame, the chat says otherwise: 502, the slot stays (the call went out)."""
    error = HTTPException(status_code=502, detail=detail)
    error.keep_write_slot = True
    return error


@router.post("/{chat_id}/{message_id}/pin", name="pin_message")
async def pin_message(chat_id: int, message_id: int, request: Request, notify: bool = Query(True)):
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    async with write_slot(request, ACTION, "pin_message", chat_id):
        await call_max(session.client.pin_message(chat_id, message_id, notify_pin=notify), UNCERTAIN)
    read, pinned = await _pinned_after(session, chat_id)
    out = {"status": "pinned", "chat_id": chat_id, "message_id": message_id}
    if read and pinned is not None and pinned != message_id:
        raise _not_as_asked(f"MAX accepted the pin but chat {chat_id} has message {pinned} pinned")
    if not read or pinned is None:
        out["warning"] = "not confirmed: the chat does not show the pinned message yet, check get_chat"
    return out


@router.post("/{chat_id}/{message_id}/unpin", name="unpin_message")
async def unpin_message(chat_id: int, message_id: int, request: Request):
    """MAX has one pinned message per chat and our frame (Р-33) unpins whichever it is: the
    chat is read first, and only message_id itself is unpinned (QA-R4 D4) — nothing pinned
    → 404, another message pinned → 409, no frame. Checked by the fresh chat after."""
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    pinned_now = frames.pinned_id(await call_max_read(groups.fresh_chat(session.client, chat_id)))
    if pinned_now is None:
        raise HTTPException(status_code=404, detail=f"no message is pinned in chat {chat_id}")
    if pinned_now != message_id:
        raise HTTPException(status_code=409, detail=f"message {message_id} is not pinned (pinned: {pinned_now})")
    async with write_slot(request, ACTION, "unpin_message", chat_id):
        await call_max(frames.unpin(session.client, chat_id), UNCERTAIN)
    read, pinned = await _pinned_after(session, chat_id)
    if read and pinned == message_id:
        raise _not_as_asked("MAX accepted the frame but the message is still pinned")
    out = {"status": "unpinned", "chat_id": chat_id, "message_id": message_id}
    if not read:
        out["warning"] = "not confirmed: the chat was not re-read, check get_chat"
    return out


@router.get("/{chat_id}/{message_id}/reactions", name="get_reactions")
async def get_reactions(chat_id: int, message_id: int, request: Request):
    """Counts per reaction and the account's own; who reacted — users (181, Р-67, up to 100).
    A failed 181 does not spoil the counts: users null, users_error."""
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    found = await call_max_read(session.client.get_reactions(chat_id, [message_id])) or {}
    info = found.get(str(message_id)) or found.get(message_id)
    if info is None:
        out = {"chat_id": chat_id, "message_id": message_id, "total_count": 0, "counters": [], "my_reaction": None}
    else:
        out = {"chat_id": chat_id, "message_id": message_id, "total_count": info.total_count,
               "counters": [{"reaction": c.reaction, "count": c.count} for c in info.counters or []],
               "my_reaction": info.your_reaction}
    try:
        detail = await frames.detailed_reactions(session.client, chat_id, message_id)
        out["users"] = [{"user_id": int(r["userId"]), "reaction": r.get("reaction")}
                        for r in detail.get("reactions") or [] if isinstance(r, dict) and r.get("userId") is not None]
        out["next_marker"] = detail.get("marker")
    except Exception as exc:  # noqa: BLE001 — the counts answer anyway
        log.info("max[%s]: who reacted to %s/%s not read: %s: %s", session.alias, chat_id, message_id,
                 type(exc).__name__, exc)
        out["users"] = None
        out["users_error"] = f"{type(exc).__name__}: {exc}"
    return out


# ---------------------------------------------------------------------------
# Bot buttons (Р-64), voters, links, transcription (Р-68…Р-70)
# ---------------------------------------------------------------------------

class PressRequest(BaseModel):
    row: int = Field(ge=0)
    column: int = Field(ge=0)


@router.post("/{chat_id}/{message_id}/press", name="press_button")
async def press_button(chat_id: int, message_id: int, req: PressRequest, request: Request):
    """Press a bot button of the message, read fresh from MAX (MSG_GET). CALLBACK / CHAT / MESSAGE
    only: LINK → 422 with its url, contact / geo / mini app / clipboard → 422. An action."""
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    msg = await call_max_read(session.client.get_message(chat_id, message_id))
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    keyboard = frames.keyboard_view(msg)
    if keyboard is None:
        raise HTTPException(status_code=422, detail="the message has no bot keyboard")
    rows = keyboard["buttons"]
    if req.row >= len(rows) or req.column >= len(rows[req.row]):
        raise HTTPException(status_code=422, detail=f"no button at row {req.row}, column {req.column}; "
                                                    f"the keyboard: {[len(r) for r in rows]} buttons per row")
    button = rows[req.row][req.column]
    kind = button.get("type")
    if kind == "LINK":
        raise HTTPException(status_code=422, detail={"error": "a link button is not pressed: open its url",
                                                     "url": button.get("url")})
    if kind not in frames.PRESSABLE:
        raise HTTPException(status_code=422, detail=f"a {kind} button is not pressed by the agent (personal data, "
                                                    f"location, a mini app or the clipboard); pressable: "
                                                    f"{list(frames.PRESSABLE)}")
    if not keyboard["callback_id"]:
        raise HTTPException(status_code=422, detail="the keyboard has no callbackId: MAX would not take a press")
    async with write_slot(request, ACTION, "press_button", chat_id):
        answer = await call_max(frames.press(session.client, keyboard["callback_id"], kind, button.get("payload")),
                                UNCERTAIN)
    out = {"chat_id": chat_id, "message_id": message_id, "button": button, "success": answer.get("success")}
    try:
        sent = frames.parse_sent(answer, chat_id)
    except Exception:  # noqa: BLE001 — the press is done; its message is in the stream anyway
        log.warning("max[%s]: the message of a button answer does not parse", session.alias, exc_info=True)
        sent = None
    if sent is not None:
        await store_sent(session, sent, sent.chat_id or chat_id)
        out["message"] = {"chat_id": sent.chat_id or chat_id, "message_id": sent.id, "text": sent.text or None}
    if isinstance(answer.get("chat"), dict):
        out["chat"] = {"chat_id": answer["chat"].get("id"), "title": answer["chat"].get("title")}
    return out


@router.get("/{chat_id}/{message_id}/voters", name="get_poll_voters")
async def get_poll_voters(chat_id: int, message_id: int, request: Request, answer_id: int = Query(...),
                          limit: int = Query(100, ge=1, le=100)):
    """Who voted for an answer (305). An anonymous poll → 422, no frame."""
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    poll = frames.find_poll(await _message(request, session, chat_id, message_id))
    if poll is None:
        raise HTTPException(status_code=404, detail="the message has no poll")
    view = frames.poll_view(poll)
    if view["anonymous"]:
        raise HTTPException(status_code=422, detail="the poll is anonymous: MAX does not tell who voted")
    known = [a["answer_id"] for a in view["answers"]]
    if answer_id not in known:
        raise HTTPException(status_code=422, detail=f"answer_id {answer_id} is not an answer of the poll; "
                                                    f"its answers: {known}")
    payload = await call_max_read(frames.voters(session.client, chat_id, message_id, view["poll_id"], answer_id,
                                                count=limit))
    return {"chat_id": chat_id, "message_id": message_id, "poll_id": view["poll_id"], "answer_id": answer_id,
            "vote_count": payload.get("voteCount"), "next_marker": payload.get("marker"),
            "voters": [{"user_id": int(v["userId"]), "date": normalize.ms_to_dt(v.get("timestamp"))}
                       for v in payload.get("voters") or [] if isinstance(v, dict) and v.get("userId") is not None]}


@router.get("/{chat_id}/{message_id}/link", name="get_message_link")
async def get_message_link(chat_id: int, message_id: int, request: Request):
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    payload = await call_max_read(frames.message_link(session.client, chat_id, message_id))
    if not payload.get("link"):
        raise HTTPException(status_code=502, detail=f"MAX answered without a link: {payload!r}")
    return {"chat_id": chat_id, "message_id": message_id, "link": payload["link"]}


class TranscribeRequest(BaseModel):
    index: int | None = Field(default=None, ge=0, description="attach_index of the voice / video note")


TRANSCRIBE_POLL_SEC = 2.0
TRANSCRIBE_WAIT_SEC = 20.0
TRANSCRIBE_MIN_GAP_SEC = 10.0  # one MAX call per message in 10 s (plan Р5 §4 PR-R5-2)
TRANSCRIBE_KEEP_SEC = 3600.0
TRANSCRIBE_CACHE_MAX = 1000  # entries; the oldest go first (review 19 R19-5)
# (account, chat, message, media) → (when, answer): ready texts for an hour, the rest for the gap
_transcripts: dict[tuple, tuple[float, dict]] = {}
_sleep = asyncio.sleep
_clock = time.monotonic


def _transcription_status(payload: dict) -> int | None:
    value = payload.get("transcriptionStatus")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@router.post("/{chat_id}/{message_id}/transcribe", name="transcribe_voice")
async def transcribe_voice(chat_id: int, message_id: int, request: Request, req: TranscribeRequest | None = None):
    """MAX's transcription of a voice or video note (202, Р-70): status 0 — in work, polled every
    2 s up to 20 s, then status processing; 1 — done; anything else — failed. A read."""
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    msg = await _message(request, session, chat_id, message_id)
    found = frames.media_id(msg, req.index if req else None)
    if found is None:
        raise HTTPException(status_code=422, detail="the message has no voice or video note"
                                                    + (f" at index {req.index}" if req and req.index is not None else ""))
    index, media_id = found
    key = (session.account_id, chat_id, message_id, media_id)
    cached = _transcripts.get(key)
    if cached is not None:
        age, answer = _clock() - cached[0], cached[1]
        if (answer["status"] == "done" and age < TRANSCRIBE_KEEP_SEC) or age < TRANSCRIBE_MIN_GAP_SEC:
            return {**answer, "cached": True}
    waited = 0.0
    while True:
        payload = await call_max_read(frames.transcribe(session.client, chat_id, message_id, media_id))
        status = _transcription_status(payload)
        if status != 0 or waited >= TRANSCRIBE_WAIT_SEC:
            break
        await _sleep(TRANSCRIBE_POLL_SEC)
        waited += TRANSCRIBE_POLL_SEC
    answer = {"chat_id": chat_id, "message_id": message_id, "index": index, "media_id": media_id,
              "status": {0: "processing", 1: "done"}.get(status, "failed"), "transcription_status": status,
              "text": payload.get("transcription") if status == 1 else None}
    now = _clock()
    for old in [k for k, (at, _) in _transcripts.items() if now - at >= TRANSCRIBE_KEEP_SEC]:
        _transcripts.pop(old, None)
    _transcripts[key] = (now, answer)
    while len(_transcripts) > TRANSCRIBE_CACHE_MAX:
        _transcripts.pop(min(_transcripts, key=lambda k: _transcripts[k][0]))
    return answer
