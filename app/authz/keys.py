"""API keys per account and the owner key (ТЗ 2026-10-13 §6.1).

The key itself is shown once at creation; the DB keeps sha256 hex. Lookups
are cached in-process for 60 s (create/revoke drop the cache); last_used_at
is written at most once a minute per key, in the background.
"""

import asyncio
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.database import async_session
from app.models import Account, ApiKey

log = logging.getLogger(__name__)

CACHE_TTL = 60.0
TOUCH_INTERVAL = 60.0

_cache: dict[str, tuple["KeyInfo | None", float]] = {}
_touched: dict[int, float] = {}


@dataclass(frozen=True)
class KeyInfo:
    id: int
    scope: str  # 'account' | 'owner'
    account_id: int | None = None
    alias: str | None = None
    platform: str | None = None


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_key() -> str:
    return "tgmp_" + secrets.token_urlsafe(32)


def invalidate() -> None:
    _cache.clear()


async def _load(key_hash: str) -> KeyInfo | None:
    async with async_session() as db:
        row = (await db.execute(
            select(ApiKey, Account.alias, Account.platform, Account.is_enabled)
            .outerjoin(Account, Account.id == ApiKey.account_id)
            .where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None))
        )).one_or_none()
    if row is None:
        return None
    key, alias, platform, enabled = row
    if key.scope == "account" and not enabled:
        return None  # a disabled account's keys do not authenticate
    return KeyInfo(id=key.id, scope=key.scope, account_id=key.account_id, alias=alias, platform=platform)


async def lookup(key: str | None) -> KeyInfo | None:
    if not key:
        return None
    key_hash = hash_key(key)
    now = time.monotonic()
    cached = _cache.get(key_hash)
    if cached and cached[1] > now:
        info = cached[0]
    else:
        info = await _load(key_hash)
        _cache[key_hash] = (info, now + CACHE_TTL)
    if info is not None:
        _touch_later(info.id, now)
    return info


def _touch_later(key_id: int, now: float) -> None:
    if now - _touched.get(key_id, -TOUCH_INTERVAL) < TOUCH_INTERVAL:
        return
    _touched[key_id] = now

    async def touch() -> None:
        try:
            async with async_session() as db:
                await db.execute(
                    update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=datetime.now(timezone.utc))
                )
                await db.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("api key %s: last_used_at not updated: %s", key_id, exc)

    try:
        asyncio.get_running_loop().create_task(touch())
    except RuntimeError:
        pass


async def create_key(*, scope: str, account_id: int | None, label: str | None) -> tuple[ApiKey, str]:
    """(row, plaintext key) — the plaintext is never stored."""
    key = generate_key()
    async with async_session() as db:
        row = ApiKey(scope=scope, account_id=account_id, key_hash=hash_key(key), label=label)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    invalidate()
    return row, key


async def revoke_key(key_id: int) -> bool:
    async with async_session() as db:
        result = await db.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id, ApiKey.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )
        await db.commit()
    invalidate()
    return bool(result.rowcount)


async def list_keys(*, account_id: int | None = None, scope: str | None = None) -> list[ApiKey]:
    q = select(ApiKey).order_by(ApiKey.id)
    if account_id is not None:
        q = q.where(ApiKey.account_id == account_id)
    if scope is not None:
        q = q.where(ApiKey.scope == scope)
    async with async_session() as db:
        return list((await db.execute(q)).scalars().all())


def key_to_dict(row: ApiKey) -> dict:
    return {
        "id": row.id,
        "scope": row.scope,
        "account_id": row.account_id,
        "label": row.label,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
    }
