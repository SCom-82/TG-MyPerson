"""Files for remote MCP clients (ТЗ 2026-10-13 §9.2): the server never sees their disk.

- download: an HMAC-signed link GET /api/v1/files/{token} (account, chat,
  message, expiry; FILE_LINK_SECRET) streams what /messages/{c}/{m}/media does;
- upload: create_upload → PUT /api/v1/uploads/{token} (≤ 50 MB, TTL 15 min,
  /tmp/uploads/<account_id>/), then send_* with upload_id;
- or send_* with an https file_url the server downloads (≤ 50 MB, no
  redirects, no private/loopback addresses; the connection goes only to the
  addresses checked — no second DNS lookup, so no DNS rebinding).
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

from aiohttp.abc import AbstractResolver

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


def _check_public(ip: str, host: str) -> None:
    addr = ipaddress.ip_address(ip)
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast \
            or addr.is_unspecified:
        raise LinkError(f"file_url host {host} resolves to a non-public address")


async def _resolve(host: str, port: int) -> list[tuple[int, str]]:
    """(family, ip) of the host — the ONLY resolution for a download."""
    import asyncio

    infos = await asyncio.get_running_loop().getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return [(family, sockaddr[0]) for family, _, _, _, sockaddr in infos]


class PinnedResolver(AbstractResolver):
    """aiohttp resolver that only hands out the addresses checked before connecting.

    The connection goes to an IP that passed the public-address check; nothing
    resolves the name a second time, so a short-TTL DNS answer cannot turn into
    an internal address between check and connect (DNS rebinding, review R5-1).
    TLS still verifies the certificate against the host name of the URL.
    """

    def __init__(self, host: str, addresses: list[tuple[int, str]]) -> None:
        self.host = host
        self.addresses = addresses

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[dict]:
        if host != self.host:
            raise OSError(f"unexpected host {host}")
        return [
            {"hostname": host, "host": ip, "port": port, "family": fam, "proto": 0,
             "flags": socket.AI_NUMERICHOST}
            for fam, ip in self.addresses
        ]

    async def close(self) -> None:
        return None


async def fetch_url(url: str) -> tuple[str, bytes]:
    """https only, no redirects, public addresses only, ≤ 50 MB."""
    import aiohttp

    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise LinkError("file_url must be an https URL")
    try:
        addresses = await _resolve(parsed.hostname, parsed.port or 443)
    except OSError as exc:
        raise LinkError(f"file_url host {parsed.hostname} does not resolve: {exc}")
    if not addresses:
        raise LinkError(f"file_url host {parsed.hostname} does not resolve")
    for _, ip in addresses:
        _check_public(ip, parsed.hostname)
    connector = aiohttp.TCPConnector(resolver=PinnedResolver(parsed.hostname, addresses), use_dns_cache=False)
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=300)) as http:
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
