"""MAX media on demand (ADR §2.F): attachment → URL via PyMax → stream via the proxy.

Only metadata is stored at ingest (max_media). On GET …/media the attachment is
taken from the stored raw message (or fetched with get_message), its URL is
resolved (photo/audio/sticker: in the attachment; file: get_file_by_id;
video: get_video_by_id) and the body is streamed by aiohttp THROUGH THE SAME
PROXY as the MAX connection, 256 KiB chunks, with `Range: bytes=N-` resume.
"""

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp
from aiohttp_socks import ProxyConnector
from pymax.types.domain import Message

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


def attachment_at(raw_message: dict, index: int) -> Any:
    msg = Message.model_validate(raw_message)
    if index < 0 or index >= len(msg.attaches):
        raise MediaNotFound(f"no attachment #{index}")
    return msg.attaches[index]


def message_from_client(message: Any) -> dict:
    return message.model_dump(mode="json", by_alias=True)


async def resolve(client: Any, chat_id: int, message_id: int, attachment: Any, kind: str | None) -> ResolvedMedia:
    att_type = str(getattr(getattr(attachment, "type", None), "value", getattr(attachment, "type", "")))
    try:
        if att_type == "PHOTO":
            url, file_type, size, name = attachment.base_url, "photo", None, None
        elif att_type == "FILE":
            req = await client.get_file_by_id(chat_id, message_id, attachment.file_id)
            url, file_type, size, name = getattr(req, "url", None), "document", attachment.size, attachment.name
        elif att_type == "VIDEO":
            req = await client.get_video_by_id(chat_id, message_id, attachment.video_id)
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


def http_session(proxy_url: str | None) -> tuple[aiohttp.ClientSession, dict]:
    """aiohttp session + per-request kwargs that route through MAX_PROXY_URL.

    socks4/5 → aiohttp-socks connector; http(s) → aiohttp's own `proxy=`.
    """
    if proxy_url and proxy_url.split("://", 1)[0].lower().startswith("socks"):
        return aiohttp.ClientSession(connector=ProxyConnector.from_url(proxy_url), timeout=_TIMEOUT), {}
    if proxy_url:
        return aiohttp.ClientSession(timeout=_TIMEOUT), {"proxy": proxy_url}
    return aiohttp.ClientSession(timeout=_TIMEOUT), {}


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


async def open_stream(url: str, *, proxy_url: str | None, offset: int) -> UpstreamStream:
    """Start the upstream request; raise before any byte is sent to our client."""
    session, kwargs = http_session(proxy_url)
    headers = {"Range": f"bytes={offset}-"} if offset else {}
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
        response.release()
        await session.close()
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
