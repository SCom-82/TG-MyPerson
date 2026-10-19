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
"""

import logging
import re
import urllib.parse
from datetime import datetime
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
from app.max.api import groups as mx_groups
from app.services.tg_store import new_mode
from app.max.api.common import (
    authorized_or_error,
    call_max,
    call_max_read,
    param_not_supported,
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


@router.get("/{chat_id}/{message_id}", response_model=MaxMessageResponse, name="get_single_message")
async def get_single_message(chat_id: int, message_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if new_mode():
        return await data_reads.get_message(request, chat_id, message_id, MaxMessageResponse)
    msg = await repo.get_message(db, chat_id, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return MaxMessageResponse.model_validate(msg)


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

@router.post("", name="send_message")
async def send_message(req: SendMessageRequest, request: Request):
    """parse_mode is ignored: PyMax sends the text as its markdown."""
    if req.schedule_date is not None:
        return param_not_supported(request, "send_message", "schedule_date")
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    msg = await send_text(request, session, "send_message", req.chat_id, req.text, reply_to=req.reply_to_message_id)
    return {"status": "sent", "message_id": msg.id, "chat_id": req.chat_id, "schedule_date": None}


def _attachment(data: bytes, name: str, **kind) -> object:
    """media.upload_attachment, an unreadable duration → 422 before the slot (Р-35)."""
    try:
        return media.upload_attachment(data, name, **kind)
    except media.DurationUnknown as exc:
        raise HTTPException(status_code=422, detail=str(exc))


async def _send_attachments(request: Request, session, tool: str, chat_id: int, text: str | None,
                            reply_to: int | None, attachments: list) -> object:
    """One MSG_SEND with the attachments, one send of the limits; stored at once.
    A failed upload (UploadError) is a 502 and gives the slot back: nothing was sent."""
    async with write_slot(request, SEND, tool, chat_id):
        msg = await call_max(session.client.send_message(
            chat_id, text or None, reply_to=reply_to, attachments=attachments,
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
                                  [attachment])
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
                                  [attachment])
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
                                  attachments)
    return {"status": "sent", "message_ids": [msg.id], "chat_id": chat_id, "schedule_date": None}


@router.post("/poll", name="send_poll")
async def send_poll(req: SendPollRequest, request: Request):
    """A poll (Р-46): is_anonymous / allows_multiple → MAX's poll flags. A quiz → 501."""
    if req.quiz_correct_option is not None:
        return param_not_supported(request, "send_poll", "quiz_correct_option")
    if req.schedule_date is not None:
        return param_not_supported(request, "send_poll", "schedule_date")
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    poll = frames.poll_attach(req.question, req.options, anonymous=req.is_anonymous, multiple=req.allows_multiple)
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
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    async with write_slot(request, ACTION, "edit_message", req.chat_id):
        msg = await call_max(session.client.edit_message(req.chat_id, req.message_id, text=req.text))
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
    async with write_slot(request, ACTION, "vote_poll", chat_id):
        state = await call_max(session.client.vote_poll(chat_id, message_id, view["poll_id"], answers), UNCERTAIN)
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
    """MAX has one pinned message per chat; the frame is ours (Р-33), checked by the fresh chat."""
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
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
    """Counts per reaction and the account's own (MAX does not say who reacted)."""
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    found = await call_max_read(session.client.get_reactions(chat_id, [message_id])) or {}
    info = found.get(str(message_id)) or found.get(message_id)
    if info is None:
        return {"chat_id": chat_id, "message_id": message_id, "total_count": 0, "counters": [], "my_reaction": None}
    return {"chat_id": chat_id, "message_id": message_id, "total_count": info.total_count,
            "counters": [{"reaction": c.reaction, "count": c.count} for c in info.counters or []],
            "my_reaction": info.your_reaction}
