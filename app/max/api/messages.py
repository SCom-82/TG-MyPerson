"""MAX messages (API spec §3): list_messages, get_single_message, download_media."""

import logging
import re
import urllib.parse
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.database import get_db
from app.max import media, repo
from app.max.api.common import authorized_or_error
from app.max.schemas import MaxMessageResponse
from app.schemas import PaginatedResponse

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
):
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
async def get_single_message(chat_id: int, message_id: int, db: AsyncSession = Depends(get_db)):
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
        upstream = await media.open_stream(resolved.url, proxy_url=session.settings.proxy_url, offset=offset)
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
