"""Files for remote MCP clients (ТЗ 2026-10-13 §9.2): the server never sees their disk.

- download: an HMAC-signed link GET /api/v1/files/{token} (account, chat,
  message, expiry; FILE_LINK_SECRET) streams what /messages/{c}/{m}/media does;
- upload: create_upload → PUT /api/v1/uploads/{token} (≤ 50 MB, TTL 15 min,
  /tmp/uploads/<account_id>/), then send_* with upload_id;
- or send_* with an https file_url the server downloads (≤ 50 MB, no
  redirects, no private/loopback addresses).
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

from app.config import settings

LINK_TTL = 15 * 60
MAX_BYTES = 50 * 1024 * 1024
UPLOAD_ROOT = Path("/tmp/uploads")


class LinkError(Exception):
    """Bad, foreign or expired token."""


def _secret() -> bytes:
    if not settings.file_link_secret:
        raise LinkError("FILE_LINK_SECRET is not configured")
    return settings.file_link_secret.encode()


def sign(payload: dict, ttl: int | None = None) -> tuple[str, int]:
    exp = int(time.time()) + (LINK_TTL if ttl is None else ttl)
    body = base64.urlsafe_b64encode(json.dumps({**payload, "x": exp}, separators=(",", ":")).encode()).decode().rstrip("=")
    mac = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}", exp


def verify(token: str, kind: str) -> dict:
    try:
        body, mac = token.rsplit(".", 1)
    except ValueError:
        raise LinkError("malformed token")
    expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        raise LinkError("bad signature")
    data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    if data.get("k") != kind:
        raise LinkError("wrong token kind")
    if data["x"] < time.time():
        raise LinkError("link expired")
    return data


def file_link(base_url: str, account_id: int, chat_id: int, message_id: int, index: int = 0) -> dict:
    token, exp = sign({"k": "file", "a": account_id, "c": chat_id, "m": message_id, "i": index})
    url = f"{base_url.rstrip('/')}/api/v1/files/{token}"
    return {"url": url, "expires_at": exp}


def new_upload(base_url: str, account_id: int) -> dict:
    upload_id = secrets.token_hex(16)
    token, exp = sign({"k": "upload", "a": account_id, "u": upload_id})
    url = f"{base_url.rstrip('/')}/api/v1/uploads/{token}"
    return {
        "upload_id": upload_id,
        "upload_url": url,
        "expires_at": exp,
        "curl": f"curl -T <file> '{url}?name=<file name>'",
    }


def upload_path(account_id: int, upload_id: str) -> Path:
    if not upload_id.isalnum():
        raise LinkError("bad upload_id")
    return UPLOAD_ROOT / str(account_id) / upload_id


def read_upload(account_id: int, upload_id: str) -> tuple[str, bytes]:
    path = upload_path(account_id, upload_id)
    meta = path.with_suffix(".json")
    if not path.exists() or not meta.exists():
        raise LinkError(f"upload {upload_id} not found (expired or another account)")
    info = json.loads(meta.read_text())
    if info["expires_at"] < time.time():
        raise LinkError(f"upload {upload_id} expired")
    return info["file_name"], path.read_bytes()


def purge_expired(now: float | None = None) -> int:
    now = now or time.time()
    removed = 0
    for meta in UPLOAD_ROOT.glob("*/*.json"):
        try:
            if json.loads(meta.read_text())["expires_at"] < now:
                meta.with_suffix("").unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
                removed += 1
        except (OSError, ValueError, KeyError):
            continue
    return removed


def _public_ip(host: str) -> None:
    for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast \
                or ip.is_unspecified:
            raise LinkError(f"file_url host {host} resolves to a non-public address")


async def fetch_url(url: str) -> tuple[str, bytes]:
    """https only, no redirects, public addresses only, ≤ 50 MB."""
    import aiohttp

    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise LinkError("file_url must be an https URL")
    _public_ip(parsed.hostname)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as http:
        async with http.get(url, allow_redirects=False) as resp:
            if resp.status != 200:
                raise LinkError(f"file_url answered HTTP {resp.status}")
            data = bytearray()
            async for chunk in resp.content.iter_chunked(1 << 16):
                data += chunk
                if len(data) > MAX_BYTES:
                    raise LinkError("file_url is larger than 50 MB")
    name = Path(parsed.path).name or "file"
    return name, bytes(data)
