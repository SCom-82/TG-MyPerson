import io
import logging
import os
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse
from telethon.tl import functions, types as tl_types

from app.database import get_db
from app.schemas import (
    MessageResponse,
    SendMessageRequest,
    ForwardMessageRequest,
    PaginatedResponse,
    EditMessageRequest,
    ReactRequest,
    SendPollRequest,
    ScheduledMessageItem,
)
from app.services.message_service import get_messages, get_message
from app.telegram.pool import require_authorized_client

log = logging.getLogger(__name__)
router = APIRouter(prefix="/messages", tags=["messages"])


def _to_utc(dt: datetime | None) -> datetime | None:
    """Normalize datetime to UTC (Telethon expects UTC-aware datetime)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _random_id() -> int:
    return int.from_bytes(os.urandom(8), "big", signed=True)


@router.get("", response_model=PaginatedResponse)
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
):
    messages, total = await get_messages(
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
        items=[MessageResponse.model_validate(m) for m in messages],
        total=total,
        limit=limit,
        offset=offset,
    )


# --- Scheduled queue (должно быть ПЕРЕД /{chat_id}/{message_id} чтобы не поймать "scheduled" как int) ---

@router.get("/scheduled")
async def list_scheduled(chat_id: int = Query(...), request: Request = None):
    """List messages scheduled for future delivery in a chat."""
    client = await require_authorized_client(request)
    try:
        peer = await client.get_input_entity(chat_id)
        result = await client(functions.messages.GetScheduledHistoryRequest(peer=peer, hash=0))

        items: list[ScheduledMessageItem] = []
        for msg in getattr(result, "messages", []):
            media_type = None
            has_media = False
            if getattr(msg, "media", None):
                has_media = True
                media_type = type(msg.media).__name__
            items.append(
                ScheduledMessageItem(
                    message_id=msg.id,
                    chat_id=chat_id,
                    text=getattr(msg, "message", None),
                    date=msg.date,
                    has_media=has_media,
                    media_type=media_type,
                )
            )
        return {"items": items, "total": len(items), "chat_id": chat_id}
    except Exception as e:
        log.exception("Failed to list scheduled messages for chat %d", chat_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/scheduled/{chat_id}/{message_id}")
async def cancel_scheduled(chat_id: int, message_id: int, request: Request):
    """Cancel a scheduled message before it is sent."""
    client = await require_authorized_client(request)
    try:
        peer = await client.get_input_entity(chat_id)
        await client(
            functions.messages.DeleteScheduledMessagesRequest(peer=peer, id=[message_id])
        )
        return {"status": "cancelled", "chat_id": chat_id, "message_id": message_id}
    except Exception as e:
        log.exception("Failed to cancel scheduled message %d in chat %d", message_id, chat_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/scheduled/{chat_id}/{message_id}/send-now")
async def send_scheduled_now(chat_id: int, message_id: int, request: Request):
    """Send a scheduled message immediately (before its scheduled time)."""
    client = await require_authorized_client(request)
    try:
        peer = await client.get_input_entity(chat_id)
        await client(
            functions.messages.SendScheduledMessagesRequest(peer=peer, id=[message_id])
        )
        return {"status": "sent", "chat_id": chat_id, "message_id": message_id}
    except Exception as e:
        log.exception("Failed to send scheduled message %d in chat %d", message_id, chat_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{chat_id}/{message_id}", response_model=MessageResponse)
async def get_single_message(chat_id: int, message_id: int, db: AsyncSession = Depends(get_db)):
    msg = await get_message(db, chat_id, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return MessageResponse.model_validate(msg)


# --- Sending ---

@router.post("")
async def send_message(req: SendMessageRequest, request: Request):
    client = await require_authorized_client(request)

    try:
        schedule = _to_utc(req.schedule_date)
        result = await client.send_message(
            entity=req.chat_id,
            message=req.text,
            reply_to=req.reply_to_message_id,
            parse_mode=req.parse_mode,
            schedule=schedule,
        )
        return {
            "status": "scheduled" if schedule else "sent",
            "message_id": result.id,
            "chat_id": req.chat_id,
            "schedule_date": schedule.isoformat() if schedule else None,
        }
    except Exception as e:
        log.exception("Failed to send message to chat %d", req.chat_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/forward")
async def forward_message(req: ForwardMessageRequest, request: Request):
    client = await require_authorized_client(request)

    ids = req.message_ids if req.message_ids else ([req.message_id] if req.message_id else [])
    if not ids:
        raise HTTPException(status_code=400, detail="Provide message_id or message_ids")

    try:
        result = await client.forward_messages(
            entity=req.to_chat_id,
            messages=ids if len(ids) > 1 else ids[0],
            from_peer=req.from_chat_id,
        )
        if isinstance(result, list):
            forwarded_ids = [m.id for m in result]
        else:
            forwarded_ids = [result.id]
        return {
            "status": "forwarded",
            "message_ids": forwarded_ids,
            "to_chat_id": req.to_chat_id,
        }
    except Exception as e:
        log.exception("Failed to forward message")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/edit")
async def edit_message(req: EditMessageRequest, request: Request):
    client = await require_authorized_client(request)
    try:
        kwargs: dict = {
            "entity": req.chat_id,
            "message": req.message_id,
            "text": req.text,
        }
        if req.parse_mode:
            kwargs["parse_mode"] = req.parse_mode
        if req.scheduled:
            kwargs["schedule"] = True
        await client.edit_message(**kwargs)
        return {"status": "edited", "chat_id": req.chat_id, "message_id": req.message_id}
    except Exception as e:
        log.exception("Failed to edit message %d in chat %d", req.message_id, req.chat_id)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/send-file")
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
    """Send any file. By default auto-detects images and sends them as photos."""
    client = await require_authorized_client(request)
    try:
        file_bytes = await file.read()
        bio = io.BytesIO(file_bytes)
        bio.name = file.filename or "file.bin"
        schedule = _to_utc(schedule_date)
        result = await client.send_file(
            entity=chat_id,
            file=bio,
            caption=caption,
            reply_to=reply_to_message_id,
            parse_mode=parse_mode,
            schedule=schedule,
            voice_note=voice_note,
            force_document=force_document,
        )
        return {
            "status": "scheduled" if schedule else "sent",
            "message_id": result.id,
            "chat_id": chat_id,
            "schedule_date": schedule.isoformat() if schedule else None,
        }
    except Exception as e:
        log.exception("Failed to send file to chat %d", chat_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/send-voice")
async def send_voice(
    request: Request,
    chat_id: int = Form(...),
    file: UploadFile = File(...),
    caption: str | None = Form(None),
    reply_to_message_id: int | None = Form(None),
    parse_mode: Literal["markdown", "html"] | None = Form(None),
    schedule_date: datetime | None = Form(None),
):
    """Convenience wrapper: forces voice_note=True for sending as voice message."""
    client = await require_authorized_client(request)
    try:
        file_bytes = await file.read()
        bio = io.BytesIO(file_bytes)
        bio.name = file.filename or "voice.ogg"
        schedule = _to_utc(schedule_date)
        result = await client.send_file(
            entity=chat_id,
            file=bio,
            caption=caption,
            reply_to=reply_to_message_id,
            parse_mode=parse_mode,
            schedule=schedule,
            voice_note=True,
        )
        return {
            "status": "scheduled" if schedule else "sent",
            "message_id": result.id,
            "chat_id": chat_id,
            "schedule_date": schedule.isoformat() if schedule else None,
        }
    except Exception as e:
        log.exception("Failed to send voice to chat %d", chat_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/album")
async def send_album(
    request: Request,
    chat_id: int = Form(...),
    files: list[UploadFile] = File(...),
    caption: str | None = Form(None),
    reply_to_message_id: int | None = Form(None),
    parse_mode: Literal["markdown", "html"] | None = Form(None),
    schedule_date: datetime | None = Form(None),
):
    """Send multiple photos/files as a single album (media group)."""
    client = await require_authorized_client(request)
    if len(files) < 2:
        raise HTTPException(status_code=400, detail="Album requires at least 2 files")
    if len(files) > 10:
        raise HTTPException(status_code=400, detail="Album supports max 10 files")
    try:
        file_objects = []
        for f in files:
            data = await f.read()
            bio = io.BytesIO(data)
            bio.name = f.filename or "file"
            file_objects.append(bio)

        schedule = _to_utc(schedule_date)
        result = await client.send_file(
            entity=chat_id,
            file=file_objects,
            caption=caption,
            reply_to=reply_to_message_id,
            parse_mode=parse_mode,
            schedule=schedule,
        )
        msg_ids = [m.id for m in result] if isinstance(result, list) else [result.id]
        return {
            "status": "scheduled" if schedule else "sent",
            "message_ids": msg_ids,
            "chat_id": chat_id,
            "schedule_date": schedule.isoformat() if schedule else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Failed to send album to chat %d", chat_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/poll")
async def send_poll(req: SendPollRequest, request: Request):
    """Create a poll (regular or quiz) and send or schedule it to a chat."""
    client = await require_authorized_client(request)
    try:
        peer = await client.get_input_entity(req.chat_id)

        answers = [
            tl_types.PollAnswer(text=opt, option=bytes([i]))
            for i, opt in enumerate(req.options)
        ]
        poll = tl_types.Poll(
            id=0,
            question=req.question,
            answers=answers,
            closed=False,
            public_voters=not req.is_anonymous,
            multiple_choice=req.allows_multiple,
            quiz=req.quiz_correct_option is not None,
        )

        correct_answers = None
        if req.quiz_correct_option is not None:
            correct_answers = [bytes([req.quiz_correct_option])]

        media = tl_types.InputMediaPoll(
            poll=poll,
            correct_answers=correct_answers,
        )

        schedule = _to_utc(req.schedule_date)
        result = await client(
            functions.messages.SendMediaRequest(
                peer=peer,
                media=media,
                message="",
                random_id=_random_id(),
                reply_to=(
                    tl_types.InputReplyToMessage(reply_to_msg_id=req.reply_to_message_id)
                    if req.reply_to_message_id
                    else None
                ),
                schedule_date=schedule,
            )
        )
        msg_id = None
        for upd in getattr(result, "updates", []):
            if hasattr(upd, "id") and isinstance(getattr(upd, "id", None), int):
                msg_id = upd.id
                break
            if hasattr(upd, "message") and hasattr(upd.message, "id"):
                msg_id = upd.message.id
                break
        return {
            "status": "scheduled" if schedule else "sent",
            "message_id": msg_id,
            "chat_id": req.chat_id,
            "schedule_date": schedule.isoformat() if schedule else None,
        }
    except Exception as e:
        log.exception("Failed to send poll to chat %d", req.chat_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{chat_id}/{message_id}")
async def delete_message(chat_id: int, message_id: int, request: Request):
    client = await require_authorized_client(request)

    try:
        await client.delete_messages(entity=chat_id, message_ids=[message_id])
        return {"status": "deleted", "chat_id": chat_id, "message_id": message_id}
    except Exception as e:
        log.exception("Failed to delete message %d in chat %d", message_id, chat_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{chat_id}/{message_id}/media")
async def download_media(chat_id: int, message_id: int, request: Request):
    client = await require_authorized_client(request)
    try:
        msgs = await client.get_messages(entity=chat_id, ids=message_id)
        msg = msgs if not isinstance(msgs, list) else msgs[0] if msgs else None
        if not msg or not msg.media:
            raise HTTPException(status_code=404, detail="Message has no media")

        buffer = io.BytesIO()
        await client.download_media(msg, file=buffer)
        buffer.seek(0)

        content_type = "application/octet-stream"
        filename = f"media_{message_id}"
        if hasattr(msg.media, "document") and msg.media.document:
            doc = msg.media.document
            content_type = doc.mime_type or content_type
            for attr in doc.attributes:
                if hasattr(attr, "file_name") and attr.file_name:
                    filename = attr.file_name
                    break
        elif hasattr(msg.media, "photo"):
            content_type = "image/jpeg"
            filename = f"photo_{message_id}.jpg"

        return StreamingResponse(
            buffer,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Failed to download media for message %d in chat %d", message_id, chat_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{chat_id}/{message_id}/pin")
async def pin_message(chat_id: int, message_id: int, request: Request, notify: bool = Query(True)):
    client = await require_authorized_client(request)
    try:
        await client.pin_message(entity=chat_id, message=message_id, notify=notify)
        return {"status": "pinned", "chat_id": chat_id, "message_id": message_id}
    except Exception as e:
        log.exception("Failed to pin message %d in chat %d", message_id, chat_id)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{chat_id}/{message_id}/unpin")
async def unpin_message(chat_id: int, message_id: int, request: Request):
    client = await require_authorized_client(request)
    try:
        await client.unpin_message(entity=chat_id, message=message_id)
        return {"status": "unpinned", "chat_id": chat_id, "message_id": message_id}
    except Exception as e:
        log.exception("Failed to unpin message %d in chat %d", message_id, chat_id)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{chat_id}/{message_id}/react")
async def react_to_message(chat_id: int, message_id: int, req: ReactRequest, request: Request):
    client = await require_authorized_client(request)
    try:
        peer = await client.get_input_entity(chat_id)
        reaction: list = []
        if req.emoticon:
            reaction = [tl_types.ReactionEmoji(emoticon=req.emoticon)]
        await client(
            functions.messages.SendReactionRequest(
                peer=peer,
                msg_id=message_id,
                reaction=reaction,
            )
        )
        return {
            "status": "reacted" if req.emoticon else "unreacted",
            "chat_id": chat_id,
            "message_id": message_id,
            "emoticon": req.emoticon,
        }
    except Exception as e:
        log.exception("Failed to react to message %d in chat %d", message_id, chat_id)
        raise HTTPException(status_code=400, detail=str(e))
