"""Messenger write limits per account (MAX PR-7, brief 2026-10-17).

Settings: account_write_limits (one row per account, admin API); without a
row the platform default applies — MAX: DEFAULT_LIMITS, Telegram: no limits.
A NULL setting turns that limit off; enabled=false stops every write.

Counters: write_events, one row per write that passed (a 'send' or an
'action'), so a restart does not reset them. Settings are read on every
write: a change through the admin API applies to the next call.

- sends (send_message, send_file, forward_message, ...): per_minute,
  per_hour, per_day (sliding 24 h), new_dialogs_per_day (the first send to a
  chat with no history at all — neither incoming nor outgoing messages, a
  cold first message; a reply to someone who wrote first is not new; counted
  once per chat) and
  min_interval_sec + random 0..jitter_sec between sends;
- actions (edit, delete, react, mark_read): actions_per_hour;
- MAX groups (plan 2026-10-19 Р-23): groups_created_per_day (3) and
  group_invites_per_day (30, one event per person added — the initial members
  of create_group too); one call reserves all its events at once
  (reserve_bundle / bundle_slot), the rest of a group's changes are actions;
- MAX joins and imports (plan 2026-10-19 Р4 Р-39): chats_joined_per_day (5,
  one per join by link) and contacts_imported_per_day (50, one per number that
  went to MAX); invites to a channel count in group_invites_per_day;
- lookups by phone (contact_by_phone, plan 2026-10-19 Р-14):
  phone_lookups_per_day, 20 by default on every platform — Telegram too,
  whose writes have no limits. A lookup that reached the messenger keeps its
  slot whatever the outcome (lookup_slot): anti-spam counts requests.

Over a limit → 429 + Retry-After with the limit, used and remaining; with
several full windows the one with the longest wait (the others in `also`); a
limit of 0 → 429 without Retry-After (that kind of write is off).
min_interval is not a refusal: the call waits up to MAX_WAIT_SEC, then 429.

reserve() runs under a per-account advisory lock, so concurrent writes of
one account see each other; a delayed send is stored with its planned time
and the next one queues after it. A write whose messenger call fails gives
its slot back (write_slot).
"""

import asyncio
import logging
import math
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Request
from sqlalchemy import delete, exists, func, insert, select, text
from starlette.responses import JSONResponse

from app.database import async_session
from app.models import Account, AccountWriteLimits, WriteEvent

log = logging.getLogger(__name__)

SEND = "send"
ACTION = "action"
LOOKUP = "lookup"
LOOKUP_DEFAULT = 20
GROUP_CREATE = "group_create"
GROUP_INVITE = "group_invite"
CHAT_JOIN = "chat_join"
CONTACT_IMPORT = "contact_import"

MAX_WAIT_SEC = 15
RETENTION = timedelta(hours=25)
_LOCK_NS = 7731  # pg_advisory_xact_lock(namespace, account_id)


@dataclass(frozen=True)
class Limits:
    enabled: bool = True
    per_minute: int | None = 5
    per_hour: int | None = 20
    per_day: int | None = 100
    new_dialogs_per_day: int | None = 10
    min_interval_sec: int | None = 3
    jitter_sec: int | None = 2
    actions_per_hour: int | None = 60
    phone_lookups_per_day: int | None = LOOKUP_DEFAULT
    # MAX groups (plan 2026-10-19 Р-23); invites count per added person
    groups_created_per_day: int | None = 3
    group_invites_per_day: int | None = 30
    # MAX joins by link and numbers imported (plan 2026-10-19 Р4 Р-39)
    chats_joined_per_day: int | None = 5
    contacts_imported_per_day: int | None = 50

    def as_dict(self) -> dict:
        return asdict(self)


LIMIT_FIELDS = tuple(f.name for f in fields(Limits))
DEFAULT_LIMITS = Limits()
# Every limit off: the base of the first change for a platform without a default.
# Lookups keep their default: a PATCH of a TG account does not switch them off.
NO_LIMITS = Limits(**{f: None for f in LIMIT_FIELDS if f not in ("enabled", "phone_lookups_per_day")})
# None: the platform writes without limits unless the account has its own row.
PLATFORM_DEFAULTS: dict[str, Limits | None] = {"max": DEFAULT_LIMITS, "telegram": None}


@dataclass(frozen=True)
class Window:
    name: str
    kind: str
    seconds: int
    new_dialogs_only: bool = False
    noun: str = "sends"


WINDOWS = (
    Window("per_minute", SEND, 60),
    Window("per_hour", SEND, 3600),
    Window("per_day", SEND, 86400),
    Window("new_dialogs_per_day", SEND, 86400, new_dialogs_only=True, noun="new dialogs"),
    Window("actions_per_hour", ACTION, 3600, noun="actions"),
    Window("phone_lookups_per_day", LOOKUP, 86400, noun="phone lookups"),
    Window("groups_created_per_day", GROUP_CREATE, 86400, noun="groups created"),
    Window("group_invites_per_day", GROUP_INVITE, 86400, noun="group invites"),
    Window("chats_joined_per_day", CHAT_JOIN, 86400, noun="chats joined"),
    Window("contacts_imported_per_day", CONTACT_IMPORT, 86400, noun="contacts imported"),
)
LOOKUP_WINDOW = next(w for w in WINDOWS if w.kind == LOOKUP)
_SPAN = {60: "minute", 3600: "hour", 86400: "24 hours"}


# Test seams: the clock, the pause and the jitter.
def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _jitter(upper: int) -> float:
    return random.uniform(0, upper)


class WriteLimited(Exception):
    """A write refused by the limits: 429 (+ Retry-After) or 403 (writes disabled)."""

    def __init__(self, status: int, body: dict, retry_after: int | None = None) -> None:
        super().__init__(body.get("message") or body.get("error"))
        self.status = status
        self.body = body
        self.retry_after = retry_after

    def response(self) -> JSONResponse:
        headers = {"Retry-After": str(self.retry_after)} if self.retry_after is not None else None
        return JSONResponse(status_code=self.status, content=self.body, headers=headers)


async def write_limited_handler(request: Request, exc: WriteLimited) -> JSONResponse:
    return exc.response()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Effective:
    account_id: int
    alias: str
    platform: str
    limits: Limits | None
    source: str  # 'account' (own row) | 'default' (platform default) | 'none'


async def effective(db, account_id: int) -> Effective | None:
    row = (await db.execute(
        select(Account.alias, Account.platform, AccountWriteLimits)
        .join(AccountWriteLimits, AccountWriteLimits.account_id == Account.id, isouter=True)
        .where(Account.id == account_id)
    )).one_or_none()
    if row is None:
        return None
    alias, platform, own = row
    if own is not None:
        limits = Limits(**{f: getattr(own, f) for f in LIMIT_FIELDS})
        return Effective(account_id, alias, platform, limits, "account")
    default = PLATFORM_DEFAULTS.get(platform)
    return Effective(account_id, alias, platform, default, "default" if default else "none")


async def set_limits(account_id: int, changes: dict[str, Any]) -> None:
    """Own row = current effective limits + changes.

    The first change starts from the platform default; a platform without one
    (Telegram) starts from NO_LIMITS: fields not given stay off, so setting
    per_hour of a TG account does not switch on MAX's pause or new-dialog limit.
    """
    async with async_session() as db:
        current = await effective(db, account_id)
        base = current.limits if current is not None and current.limits is not None else NO_LIMITS
        values = replace(base, **changes).as_dict()
        own = await db.get(AccountWriteLimits, account_id)
        if own is None:
            # Core INSERT: an explicit NULL (limit off) must not become the column's
            # server DEFAULT (013/014/015: 20, 3, 30, 5, 50), which the ORM would let it.
            await db.execute(insert(AccountWriteLimits).values(account_id=account_id, **values))
        else:
            for k, v in values.items():
                setattr(own, k, v)
            own.updated_at = func.now()
        await db.commit()


async def reset_limits(account_id: int) -> None:
    """Back to the platform default."""
    async with async_session() as db:
        await db.execute(delete(AccountWriteLimits).where(AccountWriteLimits.account_id == account_id))
        await db.commit()


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------

def _events(account_id: int, kind: str, since: datetime, *, new_dialogs_only: bool = False):
    cond = [WriteEvent.account_id == account_id, WriteEvent.kind == kind, WriteEvent.at > since]
    if new_dialogs_only:
        cond.append(WriteEvent.new_dialog.is_(True))
    return cond


async def _count(db, account_id: int, w: Window, now: datetime) -> int:
    since = now - timedelta(seconds=w.seconds)
    return (await db.execute(
        select(func.count()).select_from(WriteEvent)
        .where(*_events(account_id, w.kind, since, new_dialogs_only=w.new_dialogs_only))
    )).scalar_one()


async def _retry_after(db, account_id: int, w: Window, now: datetime, used: int, limit: int, n: int = 1) -> int:
    """Seconds until the window holds limit - n events: the (used - limit + n)-th oldest leaves it."""
    since = now - timedelta(seconds=w.seconds)
    at = (await db.execute(
        select(WriteEvent.at)
        .where(*_events(account_id, w.kind, since, new_dialogs_only=w.new_dialogs_only))
        .order_by(WriteEvent.at).offset(used - limit + n - 1).limit(1)
    )).scalar_one_or_none()
    if at is None:  # cannot happen for limit ≥ 1 (the window holds ≥ limit events); be conservative
        return w.seconds
    return max(1, math.ceil((at + timedelta(seconds=w.seconds) - now).total_seconds()))


async def _last_send(db, account_id: int) -> datetime | None:
    return (await db.execute(
        select(func.max(WriteEvent.at)).where(WriteEvent.account_id == account_id, WriteEvent.kind == SEND)
    )).scalar_one()


async def _sent_to(db, account_id: int, chat_id: int) -> bool:
    """A send of ours to the chat in write_events (the 'once per chat' part of a new dialog)."""
    return (await db.execute(select(exists().where(
        WriteEvent.account_id == account_id, WriteEvent.kind == SEND, WriteEvent.chat_id == chat_id,
    )))).scalar_one()


async def _has_history(eff: Effective, chat_id: int) -> bool:
    from app.services.tg_store import new_mode

    if new_mode():
        from app.models import Chat, Message
        from app.storage import account_db

        async with account_db(eff.account_id) as db:
            return (await db.execute(select(exists().where(
                Message.account_id == eff.account_id,
                Message.chat_id == select(Chat.id).where(
                    Chat.account_id == eff.account_id, Chat.external_id == chat_id
                ).scalar_subquery(),
            )))).scalar_one()
    table = "max_messages" if eff.platform == "max" else "tg_messages"
    async with async_session() as db:
        return (await db.execute(
            text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE chat_id = :c)"), {"c": chat_id}
        )).scalar_one()


def _refusal(eff: Effective, tool: str, w: Window, limit: int, used: int, retry_after: int | None,
             also: list[str], n: int = 1) -> WriteLimited:
    """429 for the window that decides; also = the other windows that are full too.

    limit 0 (QA PR-7 W-D2): the kind of write is off — no Retry-After, nothing to wait for.
    n > limit (a bundle that never fits): no Retry-After either.
    """
    span = _SPAN[w.seconds]
    if limit == 0:
        message = f"{w.noun} of account '{eff.alias}' are off: {w.name} = 0"
    elif retry_after is None:
        message = f"{n} {w.noun} never fit write limit {w.name} = {limit} of account '{eff.alias}'"
    elif n > 1:
        message = (f"write limit {w.name} of account '{eff.alias}': {used} of {limit} {w.noun} in the last "
                   f"{span}, {n} more do not fit; retry in {retry_after} s")
    else:
        message = (f"write limit {w.name} of account '{eff.alias}' reached: {used} of {limit} {w.noun} "
                   f"in the last {span}; retry in {retry_after} s")
    return WriteLimited(429, {
        "error": "rate limit",
        "limit": w.name,
        "max": limit,
        "used": used,
        "remaining": max(0, limit - used),
        "requested": n,
        "retry_after": retry_after,
        "also": also,
        "tool": tool,
        "alias": eff.alias,
        "platform": eff.platform,
        "message": message,
    }, retry_after)


# ---------------------------------------------------------------------------
# Reservation
# ---------------------------------------------------------------------------

@dataclass
class Slot:
    event_ids: list[int]
    account_id: int

    async def release(self) -> None:
        try:
            async with async_session() as db:
                await db.execute(delete(WriteEvent).where(WriteEvent.id.in_(self.event_ids)))
                await db.commit()
        except Exception:  # noqa: BLE001 — the counter stays too high, the call's error wins
            log.exception("write limits: failed to release slot %s", self.event_ids)


async def _check_windows(db, eff: Effective, tool: str, needs: dict[str, int], now: datetime,
                         new_dialog: bool = False) -> None:
    """Every window of the kinds needed, n events each (QA PR-7 W-D1): the refusal names
    the window a retry has to wait for longest, so a retry after Retry-After passes them
    all; a window with limit 0 (or one the bundle can never fit) decides over any wait."""
    full = []
    for w in WINDOWS:
        limit = getattr(eff.limits, w.name)
        n = needs.get(w.kind, 0)
        if limit is None or not n or (w.new_dialogs_only and not new_dialog):
            continue
        used = await _count(db, eff.account_id, w, now)
        if used + n > limit:
            wait_for = None if n > limit else await _retry_after(db, eff.account_id, w, now, used, limit, n)
            full.append((w, limit, used, wait_for, n))
    if full:
        w, limit, used, wait_for, n = max(full, key=lambda f: math.inf if f[3] is None else f[3])
        raise _refusal(eff, tool, w, limit, used, wait_for, [f[0].name for f in full if f[0] is not w], n)


async def reserve(account_id: int, kind: str, tool: str, chat_id: int | None = None) -> Slot | None:
    """Check the limits and take a slot; waits out min_interval. None: no limits for the account."""
    assert kind in (SEND, ACTION, LOOKUP), kind
    wait = 0.0
    # Before the lock, each step on its own short-lived connection (review R10-1):
    # a call never holds one connection while waiting for another, so parallel
    # sends cannot drain the pool. A new dialog is a cold first message — no
    # send of ours and no message at all (incoming or outgoing) in the chat;
    # the history is only read here, the decisive "once per chat" check
    # (write_events) is repeated under the lock.
    async with async_session() as db:
        eff = await effective(db, account_id)
        if eff is not None and eff.limits is None and kind == LOOKUP:
            eff = replace(eff, limits=NO_LIMITS)  # writes unlimited (TG), lookups are not
        if eff is None or eff.limits is None:
            return None
        sent_before = kind == SEND and chat_id is not None and await _sent_to(db, account_id, chat_id)
    limits = eff.limits
    if not limits.enabled:
        raise _disabled(eff, tool)
    history = kind == SEND and chat_id is not None and not sent_before and await _has_history(eff, chat_id)

    async with async_session() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(:ns, :a)"), {"ns": _LOCK_NS, "a": account_id})
        now = _now()
        await db.execute(delete(WriteEvent).where(
            WriteEvent.account_id == account_id, WriteEvent.at < now - RETENTION
        ))
        new_dialog = (kind == SEND and chat_id is not None and not history
                      and not await _sent_to(db, account_id, chat_id))

        await _check_windows(db, eff, tool, {kind: 1}, now, new_dialog)

        at = now
        if kind == SEND and limits.min_interval_sec:
            last = await _last_send(db, account_id)
            if last is not None:
                pause = limits.min_interval_sec + (_jitter(limits.jitter_sec) if limits.jitter_sec else 0)
                at = max(now, last + timedelta(seconds=pause))
            wait = (at - now).total_seconds()
            if wait > MAX_WAIT_SEC:
                retry_after = max(1, math.ceil(wait - MAX_WAIT_SEC))
                raise WriteLimited(429, {
                    "error": "rate limit",
                    "limit": "min_interval_sec",
                    "max": limits.min_interval_sec,
                    "queued_wait_sec": math.ceil(wait),
                    "retry_after": retry_after,
                    "tool": tool,
                    "alias": eff.alias,
                    "platform": eff.platform,
                    "message": (f"sends of account '{eff.alias}' are queued for {math.ceil(wait)} s "
                                f"(min_interval {limits.min_interval_sec} s, wait limit {MAX_WAIT_SEC} s); "
                                f"retry in {retry_after} s"),
                }, retry_after)

        event_id = (await db.execute(
            insert(WriteEvent).values(
                account_id=account_id, kind=kind, tool=tool, chat_id=chat_id, new_dialog=new_dialog, at=at,
            ).returning(WriteEvent.id)
        )).scalar_one()
        await db.commit()
    if wait > 0:
        await _sleep(wait)
    return Slot([event_id], account_id)


def _disabled(eff: Effective, tool: str) -> WriteLimited:
    return WriteLimited(403, {
        "error": "writes disabled",
        "reason": "write limits: enabled = false",
        "tool": tool,
        "alias": eff.alias,
        "platform": eff.platform,
        "message": f"writes of account '{eff.alias}' are stopped (write limits enabled = false)",
    })


BUNDLE_KINDS = (ACTION, GROUP_CREATE, GROUP_INVITE, CHAT_JOIN, CONTACT_IMPORT)


async def reserve_bundle(account_id: int, tool: str, needs: dict[str, int], chat_id: int | None = None) -> Slot | None:
    """Several events of one call under one lock (create_group: 1 group + n invites):
    all fit or none is taken. No min_interval and no new-dialog logic (those are sends).
    None: no limits for the account."""
    assert set(needs) <= set(BUNDLE_KINDS), needs
    async with async_session() as db:
        eff = await effective(db, account_id)
    if eff is None or eff.limits is None:
        return None
    if not eff.limits.enabled:
        raise _disabled(eff, tool)
    rows = [{"account_id": account_id, "kind": kind, "tool": tool, "chat_id": chat_id, "new_dialog": False}
            for kind, n in needs.items() for _ in range(n)]
    async with async_session() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(:ns, :a)"), {"ns": _LOCK_NS, "a": account_id})
        now = _now()
        await db.execute(delete(WriteEvent).where(
            WriteEvent.account_id == account_id, WriteEvent.at < now - RETENTION
        ))
        await _check_windows(db, eff, tool, needs, now)
        ids = list((await db.execute(
            insert(WriteEvent).values([{**r, "at": now} for r in rows]).returning(WriteEvent.id)
        )).scalars()) if rows else []
        await db.commit()
    return Slot(ids, account_id)


@asynccontextmanager
async def lookup_slot(request: Request, tool: str) -> AsyncIterator[Slot | None]:
    """A phone lookup's slot: never given back — the request reached the messenger,
    found or not, failed or not (Р-14). Refusals of ours (a bad number) come before it."""
    account_id = getattr(request.state, "account_id", None)
    yield await reserve(account_id, LOOKUP, tool) if account_id is not None else None


async def lookups_left(account_id: int) -> int | None:
    """phone_lookups_per_day minus the lookups of the last 24 h; None: no limit."""
    async with async_session() as db:
        eff = await effective(db, account_id)
        if eff is None:
            return None
        limit = (eff.limits or NO_LIMITS).phone_lookups_per_day
        if limit is None:
            return None
        used = await _count(db, account_id, LOOKUP_WINDOW, _now())
    return max(0, limit - used)


@asynccontextmanager
async def bundle_slot(request: Request, tool: str, needs: dict[str, int],
                      chat_id: int | None = None) -> AsyncIterator[Slot | None]:
    """reserve_bundle for the request's account; given back like write_slot."""
    account_id = getattr(request.state, "account_id", None)
    slot = await reserve_bundle(account_id, tool, needs, chat_id) if account_id is not None else None
    try:
        yield slot
    except BaseException as exc:
        uncertain = isinstance(exc, asyncio.CancelledError) or getattr(exc, "keep_write_slot", False)
        if slot is not None and not uncertain:
            await slot.release()
        raise


@asynccontextmanager
async def write_slot(request: Request, kind: str, tool: str, chat_id: int | None = None) -> AsyncIterator[Slot | None]:
    """Take a slot for the request's account.

    The slot is given back when the body fails for sure (an error answer, a
    refused upload). It is kept when the outcome is unknown — the exception
    carries keep_write_slot (a timeout or a dropped connection after the frame
    went out) or the request was cancelled: the message may have been sent.
    """
    account_id = getattr(request.state, "account_id", None)
    slot = await reserve(account_id, kind, tool, chat_id) if account_id is not None else None
    try:
        yield slot
    except BaseException as exc:
        uncertain = isinstance(exc, asyncio.CancelledError) or getattr(exc, "keep_write_slot", False)
        if slot is not None and not uncertain:
            await slot.release()
        raise


# ---------------------------------------------------------------------------
# Status (admin API, watchdog)
# ---------------------------------------------------------------------------

async def status(account_id: int) -> dict | None:
    async with async_session() as db:
        eff = await effective(db, account_id)
        if eff is None:
            return None
        now = _now()
        used = {w.name: await _count(db, account_id, w, now) for w in WINDOWS}
        last = await _last_send(db, account_id)
    limits = eff.limits
    remaining = None
    if limits is not None:
        remaining = {
            w.name: None if getattr(limits, w.name) is None else max(0, getattr(limits, w.name) - used[w.name])
            for w in WINDOWS
        }
    next_send = None
    if limits is not None and limits.min_interval_sec and last is not None:
        candidate = last + timedelta(seconds=limits.min_interval_sec)
        next_send = candidate if candidate > now else None
    return {
        "account_id": eff.account_id,
        "alias": eff.alias,
        "platform": eff.platform,
        "source": eff.source,
        "limits": limits.as_dict() if limits is not None else None,
        "counters": {
            "sends_last_minute": used["per_minute"],
            "sends_last_hour": used["per_hour"],
            "sends_last_day": used["per_day"],
            "new_dialogs_last_day": used["new_dialogs_per_day"],
            "actions_last_hour": used["actions_per_hour"],
            "lookups_last_day": used["phone_lookups_per_day"],
            "groups_created_last_day": used["groups_created_per_day"],
            "group_invites_last_day": used["group_invites_per_day"],
            "chats_joined_last_day": used["chats_joined_per_day"],
            "contacts_imported_last_day": used["contacts_imported_per_day"],
            "last_send_at": last.isoformat() if last else None,
            "next_send_at": next_send.isoformat() if next_send else None,
        },
        "remaining": remaining,
        # Every account, Telegram without own limits too (Р-14).
        "phone_lookups": {
            "limit": (limits or NO_LIMITS).phone_lookups_per_day,
            "used": used["phone_lookups_per_day"],
            "remaining": None if (limits or NO_LIMITS).phone_lookups_per_day is None
            else max(0, (limits or NO_LIMITS).phone_lookups_per_day - used["phone_lookups_per_day"]),
        },
    }
