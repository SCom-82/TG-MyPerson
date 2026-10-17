"""MAX messages (API spec §3, §4).

Reads: list_messages, get_single_message, download_media.
Writes (PR-7): send_message, send_file, forward_message, edit_message,
delete_message, react_to_message — any chat, within the account's write
limits (app/authz/write_limits.py). What we sent is stored at once, not on
the echo. Parameters MAX has no counterpart for (schedule_date, voice_note,
scheduled edits) → 501.
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
from app.database import get_db
from app.api import data_reads
from app.max import media, repo
from app.services.tg_store import new_mode
from app.max.api.common import authorized_or_error, call_max, param_not_supported, store_sent
from app.max.schemas import MaxMessageResponse
from app.schemas import EditMessageRequest, ForwardMessageRequest, PaginatedResponse, ReactRequest, SendMessageRequest

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
        attachment = media.attachment_at(raw, index)
    except media.MediaNotFound:
        raise HTTPException(status_code=404, detail="Message has no media")

    # 2. Its URL.
    try:
        resolved = await media.resolve(client, chat_id, message_id, attachment, None)
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
    async with write_slot(request, SEND, "send_message", req.chat_id):
        msg = await call_max(session.client.send_message(req.chat_id, req.text, reply_to=req.reply_to_message_id))
    await store_sent(session, msg, req.chat_id)
    return {"status": "sent", "message_id": msg.id, "chat_id": req.chat_id, "schedule_date": None}


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
    """Images (by extension) go as a photo unless force_document, the rest as a file."""
    if schedule_date is not None:
        return param_not_supported(request, "send_file", "schedule_date")
    if voice_note:
        return param_not_supported(request, "send_file", "voice_note")
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    name = file.filename or "file.bin"
    attachment = media.upload_attachment(await file.read(), name, force_document=force_document)
    async with write_slot(request, SEND, "send_file", chat_id):
        msg = await call_max(session.client.send_message(
            chat_id, caption or None, reply_to=reply_to_message_id, attachments=[attachment],
        ))
    await store_sent(session, msg, chat_id)
    return {"status": "sent", "message_id": msg.id, "chat_id": chat_id, "schedule_date": None}


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
