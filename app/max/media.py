"""MAX media on demand (ADR §2.F): attachment → URL via PyMax → stream via the proxy.

Only metadata is stored at ingest (max_media). On GET …/media the attachment is
taken from the stored raw message (or fetched with get_message), its URL is
resolved (photo/audio/sticker: in the attachment; file: get_file_by_id;
video: get_video_by_id) and the body is streamed by aiohttp THROUGH THE SAME
PROXY as the MAX connection, 256 KiB chunks, with `Range: bytes=N-` resume.
"""

import asyncio
import logging
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import aiohttp
from aiohttp_socks import ProxyConnector
from pydantic import ValidationError
from pymax import ApiError
from pymax.api.response import payload_item
from pymax.api.session.enums import DeviceType
from pymax.api.session.payloads import DEFAULT_WEB_HEADER_USER_AGENT
from pymax.api.uploads.models import FileUploadResponse, PhotoUploadResponse
from pymax.api.uploads.payloads import AttachFilePayload, AttachPhotoPayload, UploadPayload
from pymax.api.uploads.service import UploadService
from pymax.exceptions import UploadError
from pymax.files import File, Photo
from pymax.protocol import Opcode

from app.max.normalize import all_attaches, parse_stored
from app.utils.raw import model_raw

log = logging.getLogger(__name__)

CHUNK_SIZE = 256 * 1024
_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)

_EXT = {"photo": "jpg", "video": "mp4", "voice": "ogg", "sticker": "webp"}


class MediaNotFound(Exception):
    """The message has no downloadable attachment at that index (→ 404)."""


class MediaUpstreamError(Exception):
    """MAX / CDN refused or failed (→ 502), e.g. video.not.ready."""


@dataclass
class ResolvedMedia:
    url: str
    file_type: str
    filename: str
    size: int | None


@dataclass
class Attachment:
    """One attachment of a stored message by its attach_index (normalize.all_attaches)."""

    attachment: Any
    forwarded: bool
    # The original of a forwarded message, when MAX names its chat (link.chatId ≠ 0).
    origin: tuple[int, int] | None = None


def attachment_ref(raw_message: dict, index: int) -> Attachment:
    msg = parse_stored(raw_message)  # stored raw: no bytes (R7-1)
    for i, att, forwarded in all_attaches(msg):
        if i == index:
            origin = None
            if forwarded and msg.link.chat_id:
                origin = (msg.link.chat_id, msg.link.message.id)
            return Attachment(att, forwarded, origin)
    raise MediaNotFound(f"no attachment #{index}")


def attachment_at(raw_message: dict, index: int) -> Any:
    return attachment_ref(raw_message, index).attachment


def message_from_client(message: Any) -> dict:
    return model_raw(message)


async def _by_id(fetch, chat_id: int, message_id: int, item_id: int, origin: tuple[int, int] | None) -> Any:
    """FILE/VIDEO URL by the message we have; a forwarded one, if MAX refuses, by
    its original — only when MAX named the original's chat (plan Р-7)."""
    try:
        return await fetch(chat_id, message_id, item_id)
    except ApiError as exc:
        if origin is None:
            raise
        log.info("max media: %s by the forward (%s) failed, trying the original: %s",
                 getattr(fetch, "__name__", "fetch"), message_id, f"{type(exc).__name__}: {exc}")
        return await fetch(origin[0], origin[1], item_id)


async def resolve(client: Any, chat_id: int, message_id: int, attachment: Any, kind: str | None,
                  *, origin: tuple[int, int] | None = None) -> ResolvedMedia:
    att_type = str(getattr(getattr(attachment, "type", None), "value", getattr(attachment, "type", "")))
    try:
        if att_type == "PHOTO":
            url, file_type, size, name = attachment.base_url, "photo", None, None
        elif att_type == "FILE":
            req = await _by_id(client.get_file_by_id, chat_id, message_id, attachment.file_id, origin)
            url, file_type, size, name = getattr(req, "url", None), "document", attachment.size, attachment.name
        elif att_type == "VIDEO":
            req = await _by_id(client.get_video_by_id, chat_id, message_id, attachment.video_id, origin)
            url, file_type, size, name = getattr(req, "url", None), "video", None, None
        elif att_type == "AUDIO":
            url, file_type, size, name = attachment.url, "voice", None, None
        elif att_type == "STICKER":
            url, file_type, size, name = attachment.url, "sticker", None, None
        else:
            raise MediaNotFound(f"attachment {att_type or kind!r} is not downloadable")
    except MediaNotFound:
        raise
    except Exception as exc:  # noqa: BLE001 — ApiError incl. video.not.ready → 502, not 500
        raise MediaUpstreamError(f"{type(exc).__name__}: {exc}") from exc
    if not url:
        raise MediaUpstreamError(f"MAX returned no URL for the {file_type}")
    filename = name or f"{file_type}_{message_id}.{_EXT.get(file_type, 'bin')}"
    return ResolvedMedia(url=url, file_type=file_type, filename=filename, size=size)


def client_user_agent(client: Any) -> str | None:
    """The HTTP User-Agent of the MAX session itself.

    Video URLs from VIDEO_PLAY are okcdn links signed for the requesting client
    (srcIp = the proxy egress, srcAg = its browser family, e.g. CHROME); a request
    with another agent (aiohttp's default) gets HTTP 400 from the CDN. Web
    transport: the browser UA of the session; TCP: the app's own form, as PyMax
    uses for its uploads. The path goes through PyMax's private `_app`
    (maxapi-python pinned in requirements.lock): if it moves, a WARNING says so
    instead of silently falling back to aiohttp's agent.
    """
    try:
        ua = client._app.config.device.user_agent
    except Exception as exc:  # noqa: BLE001
        log.warning("max media: no session User-Agent (%s: %s), the CDN gets aiohttp's default",
                    type(exc).__name__, exc)
        return None
    if getattr(ua, "header_user_agent", None):
        return ua.header_user_agent
    if getattr(ua, "device_type", None) == DeviceType.WEB:
        return DEFAULT_WEB_HEADER_USER_AGENT
    if getattr(ua, "app_version", None):
        return f"OKMessages/{ua.app_version} ({ua.os_version}; {ua.device_name}; {ua.screen})"
    log.warning("max media: session User-Agent %r has no usable form, the CDN gets aiohttp's default",
                type(ua).__name__)
    return None


# Values that may go to the log (review 8, R8-1); everything else is "…".
LOG_SAFE_PARAMS = frozenset({"expires", "srcag", "srcip", "type", "clienttype", "pr", "ct"})
_WORD = re.compile(r"[a-z][a-z0-9_-]{0,15}(\.[a-z0-9]{1,5})?")
_PAIR = re.compile(r"([A-Za-z_][\w.-]*)=([^&\s\"'<>]*)")
_URL = re.compile(r"https?://[^\s\"'<>]+")


def _path_for_log(path: str) -> str:
    """Path segments: safe names and the value after a safe name; a short
    lowercase word (i, getfile, cmaf, video.m3u8) as is, but the segment after a
    word that is not a safe name is a value (okcdn /sig/<value>/) → "…"."""
    out, value_next, safe_next = [], False, False
    for seg in path.split("/"):
        if not seg:
            out.append(seg)
            continue
        if safe_next:
            out.append(seg if len(seg) <= 40 else "…")
            safe_next = value_next = False
        elif value_next:
            out.append("…")
            value_next = False
        elif seg.lower() in LOG_SAFE_PARAMS:
            out.append(seg)
            safe_next = True
        elif _WORD.fullmatch(seg):
            out.append(seg)
            value_next = True
        else:
            out.append("…")
    return "/".join(out)


def _pairs_for_log(text: str) -> str:
    return _PAIR.sub(lambda m: f"{m.group(1)}={m.group(2) if m.group(1).lower() in LOG_SAFE_PARAMS else '…'}",
                     text)


def describe_url(url: str) -> str:
    """URL for the log: host, path, the NAMES of all query parameters; values
    only of LOG_SAFE_PARAMS (photo tokens live in r=, not in sig=)."""
    parts = urllib.parse.urlsplit(url)
    query = "&".join(
        f"{k}={v if k.lower() in LOG_SAFE_PARAMS else '…'}"
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    )
    return f"{parts.netloc}{_path_for_log(parts.path)}{'?' + query if query else ''}"[:400]


def body_for_log(body: str) -> str:
    """CDN error body: URLs and name=value pairs masked like describe_url."""
    return _pairs_for_log(_URL.sub(lambda m: describe_url(m.group()), body))


def http_session(
    proxy_url: str | None, timeout: aiohttp.ClientTimeout = _TIMEOUT
) -> tuple[aiohttp.ClientSession, dict]:
    """aiohttp session + per-request kwargs that route through MAX_PROXY_URL.

    socks4/5 → aiohttp-socks connector; http(s) → aiohttp's own `proxy=`.
    """
    if proxy_url and proxy_url.split("://", 1)[0].lower().startswith("socks"):
        return aiohttp.ClientSession(connector=ProxyConnector.from_url(proxy_url), timeout=timeout), {}
    if proxy_url:
        return aiohttp.ClientSession(timeout=timeout), {"proxy": proxy_url}
    return aiohttp.ClientSession(timeout=timeout), {}


@dataclass
class UpstreamStream:
    status: int
    content_type: str
    offset: int
    total: int | None
    session: aiohttp.ClientSession
    response: aiohttp.ClientResponse

    async def chunks(self):
        skip = self.offset if self.response.status == 200 and self.offset else 0
        try:
            async for chunk in self.response.content.iter_chunked(CHUNK_SIZE):
                if skip:  # CDN ignored Range: drop the prefix ourselves
                    if len(chunk) <= skip:
                        skip -= len(chunk)
                        continue
                    chunk, skip = chunk[skip:], 0
                yield chunk
        except Exception:  # noqa: BLE001 — headers are sent; the client sees the short body
            log.exception("max media: stream interrupted")
        finally:
            self.response.release()
            await self.session.close()


async def open_stream(url: str, *, proxy_url: str | None, offset: int,
                      user_agent: str | None = None) -> UpstreamStream:
    """Start the upstream request; raise before any byte is sent to our client."""
    session, kwargs = http_session(proxy_url)
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    if user_agent:
        headers["User-Agent"] = user_agent
    try:
        response = await session.get(url, headers=headers, **kwargs)
    except Exception as exc:  # noqa: BLE001
        await session.close()
        raise MediaUpstreamError(f"{type(exc).__name__}: {exc}") from exc

    total: int | None = None
    if response.status == 206:
        content_range = response.headers.get("Content-Range", "")
        if "/" in content_range and content_range.rsplit("/", 1)[1].isdigit():
            total = int(content_range.rsplit("/", 1)[1])
    elif response.status == 200 and response.headers.get("Content-Length", "").isdigit():
        total = int(response.headers["Content-Length"])
    elif response.status == 416:
        response.release()
        await session.close()
        content_range = response.headers.get("Content-Range", "")
        size = content_range.rsplit("/", 1)[1] if "/" in content_range else "*"
        raise RangeNotSatisfiable(size)
    else:
        try:
            body = (await response.content.read(300)).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        response.release()
        await session.close()
        log.warning("max media: CDN answered HTTP %s for %s (ua=%s): %r",
                    response.status, describe_url(url), "session" if user_agent else "default",
                    body_for_log(body))
        raise MediaUpstreamError(f"CDN answered HTTP {response.status}")

    return UpstreamStream(
        status=response.status,
        content_type=response.headers.get("Content-Type", "application/octet-stream"),
        offset=offset,
        total=total,
        session=session,
        response=response,
    )


class RangeNotSatisfiable(Exception):
    def __init__(self, size: str) -> None:
        super().__init__(size)
        self.size = size


# ---------------------------------------------------------------------------
# Upload (send_file, PR-7)
# ---------------------------------------------------------------------------

def upload_attachment(data: bytes, name: str, *, force_document: bool = False) -> Any:
    """A file to send: Photo for the image extensions PyMax accepts, File otherwise."""
    from pymax.files.static import ALLOWED_EXTENSIONS

    if not force_document and PurePosixPath(name).suffix.lower() in ALLOWED_EXTENSIONS:
        return Photo(raw=data, name=name)
    return File(raw=data, name=name)


class ProxiedUploads(UploadService):
    """PyMax uploads of photos and files, fixed for the live MAX (ticket 4b4ffe3c).

    maxapi-python 2.4.1 (the latest release) breaks both on production:
    - it posts with `aiohttp.ClientSession(proxy=MAX_PROXY_URL)`; aiohttp speaks
      only HTTP to a proxy, so the socks5 max-egress resets the connection
      (ConnectionResetError) — the body never leaves through the proxy. Here the
      POST goes through http_session(), like media downloads;
    - it takes the photo id from `photoIds` in the upload URL, which MAX no
      longer sends (upstream issue #108; dev/2.5.0 reads `photos`, PR #107
      takes the only entry). Here: photoIds, then photos, then the only entry.
    Video and voice stay PyMax's: send_file sends video as a document, voice is 501.
    """

    def _post_session(self) -> tuple[aiohttp.ClientSession, dict]:
        timeout = aiohttp.ClientTimeout(total=self.app.config.upload_timeout, sock_connect=30, sock_read=60)
        return http_session(self.app.config.proxy, timeout)

    async def upload_photo(self, photo: Photo, profile: bool = False) -> AttachPhotoPayload:
        try:
            frame = await self.app.invoke(Opcode.PHOTO_UPLOAD, payload=UploadPayload(profile=profile).to_payload())
            url = payload_item(frame, "url", str)
        except Exception as exc:  # noqa: BLE001
            raise UploadError(f"Failed to request photo upload URL: {type(exc).__name__}: {exc}") from exc
        if not url:
            raise UploadError("No upload URL received")
        checked = photo.validate_photo()
        if not checked:
            raise UploadError("Photo validation failed")
        extension, content_type = checked
        form = aiohttp.FormData()
        form.add_field("file", await photo.read(), filename=f"image.{urllib.parse.quote(extension)}", content_type=content_type)
        session, kwargs = self._post_session()
        try:
            async with session, session.post(url, data=form, **kwargs) as response:
                if response.status != 200:
                    raise UploadError(f"Photo upload failed with status {response.status}")
                result = await response.json(content_type=None)
        except UploadError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise UploadError(f"HTTP error during photo upload: {type(exc).__name__}: {exc}") from exc
        return AttachPhotoPayload(photo_token=photo_token(url, result))

    async def upload_file(self, file: File) -> AttachFilePayload:
        try:
            frame = await self.app.invoke(Opcode.FILE_UPLOAD, payload=UploadPayload().to_payload())
            info = FileUploadResponse.model_validate(frame.payload).info[0]
        except Exception as exc:  # noqa: BLE001
            raise UploadError(f"Failed to request file upload URL: {type(exc).__name__}: {exc}") from exc
        size = await file.size()
        headers = {
            "Content-Disposition": f"attachment; filename={urllib.parse.quote(file.name)}",
            "Content-Length": str(size),
            "Content-Range": f"0-{size - 1}/{size}",
        }
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.file_upload_waiters[info.file_id] = future
        session, kwargs = self._post_session()
        try:
            async with session, session.post(
                info.url, headers=headers, data=file.iter_chunks(1024 * 1024), **kwargs
            ) as response:
                if response.status != 200:
                    raise UploadError(f"File upload failed with status {response.status} file_id={info.file_id}")
            # MAX confirms with NOTIF_ATTACH (PyMax's on_file_attach resolves the future).
            await asyncio.wait_for(future, 60)
        except UploadError:
            raise
        except TimeoutError as exc:
            raise UploadError(f"Timed out during file upload file_id={info.file_id}") from exc
        except aiohttp.ClientError as exc:
            raise UploadError(f"HTTP error during file upload file_id={info.file_id}: "
                              f"{type(exc).__name__}: {exc}") from exc
        finally:
            self.file_upload_waiters.pop(info.file_id, None)
        return AttachFilePayload(file_id=info.file_id)


def photo_token(url: str, result: Any) -> str:
    """The token of the uploaded photo in the POST result {"photos": {id: {"token"}}}."""
    try:
        photos = PhotoUploadResponse.model_validate(result).photos
    except ValidationError as exc:
        raise UploadError(f"Invalid photo upload response: {exc.error_count()} errors") from exc
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    for key in ("photoIds", "photos"):
        photo_id = (query.get(key) or [None])[0]
        if photo_id in photos:
            return photos[photo_id].token
    if len(photos) == 1:
        return next(iter(photos.values())).token
    raise UploadError(f"Photo upload response has {len(photos)} photos and the URL names none of them")


def install_uploads(app: Any) -> None:
    """Swap the class of PyMax's own upload service (same instance: its upload
    waiters and NOTIF_ATTACH handlers stay registered once)."""
    app.api.uploads.__class__ = ProxiedUploads
