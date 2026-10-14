"""MAX media on demand (ADR §2.F): attachment → URL via PyMax → stream via the proxy.

Only metadata is stored at ingest (max_media). On GET …/media the attachment is
taken from the stored raw message (or fetched with get_message), its URL is
resolved (photo/audio/sticker: in the attachment; file: get_file_by_id;
video: get_video_by_id) and the body is streamed by aiohttp THROUGH THE SAME
PROXY as the MAX connection, 256 KiB chunks, with `Range: bytes=N-` resume.
"""

import logging
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

import aiohttp
from aiohttp_socks import ProxyConnector
from pymax.api.session.enums import DeviceType
from pymax.api.session.payloads import DEFAULT_WEB_HEADER_USER_AGENT
from pymax.types.domain import Message

from app.utils.raw import model_raw, strip_bytes_markers

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
    msg = Message.model_validate(strip_bytes_markers(raw_message))  # stored raw: no bytes (R7-1)
    if index < 0 or index >= len(msg.attaches):
        raise MediaNotFound(f"no attachment #{index}")
    return msg.attaches[index]


def message_from_client(message: Any) -> dict:
    return model_raw(message)


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


def client_user_agent(client: Any) -> str | None:
    """The HTTP User-Agent of the MAX session itself.

    Video URLs from VIDEO_PLAY are okcdn links signed for the requesting client
    (srcIp = the proxy egress, srcAg = its browser family, e.g. CHROME); a request
    with another agent (aiohttp's default) gets HTTP 400 from the CDN. Web
    transport: the browser UA of the session; TCP: the app's own form, as PyMax
    uses for its uploads.
    """
    try:
        ua = client._app.config.device.user_agent
    except Exception:  # noqa: BLE001 — not started / a test double
        return None
    if getattr(ua, "header_user_agent", None):
        return ua.header_user_agent
    if getattr(ua, "device_type", None) == DeviceType.WEB:
        return DEFAULT_WEB_HEADER_USER_AGENT
    if getattr(ua, "app_version", None):
        return f"OKMessages/{ua.app_version} ({ua.os_version}; {ua.device_name}; {ua.screen})"
    return None


_SIGNED = re.compile(r"(sig|signature|signatureToken|tkn|token)([=/])[^&/]+", re.IGNORECASE)


def describe_url(url: str) -> str:
    """URL for the log: host, path and query without signatures."""
    parts = urllib.parse.urlsplit(url)
    rest = _SIGNED.sub(r"\1\2…", parts.path + ("?" + parts.query if parts.query else ""))
    return f"{parts.netloc}{rest[:300]}"


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
                    response.status, describe_url(url), "session" if user_agent else "default", body)
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
