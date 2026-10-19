"""People a rw account can write to, writing to a person, finding one by phone
(ТЗ 2026-10-13 §6.4, §7.3; plan 2026-10-19 §4).

Account key (or alias). Contact fields of the shared base only — no messages
or chats of other accounts. ro accounts: 403. Mounted for Telegram and MAX
(Р-17): the tools a platform shows follow from its routes.
"""

import logging
import random

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import and_, select

from app.api.data_reads import account_of
from app.authz import write_limits
from app.authz.write_limits import SEND, lookup_slot, write_slot
from app.database import async_session
from app.models import Account, Identity, User
from app.repo import people_queries
from app.services import tg_store
from app.storage import account_db
from app.utils.phone import mask_phone, normalize_phone

log = logging.getLogger(__name__)

router = APIRouter(tags=["reach"])


async def _rw_account(request: Request) -> Account:
    a = account_of(request)
    async with async_session() as db:
        account = (await db.execute(select(Account).where(Account.id == a))).scalar_one()
    if account.mode != "rw":
        raise HTTPException(status_code=403, detail=f"account '{account.alias}' is read-only")
    return account


@router.get("/reachable", name="reachable_contacts")
async def reachable_contacts(
    request: Request,
    q: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    account = await _rw_account(request)
    async with account_db(account.id) as db:
        items = await people_queries.reachable(db, account, q=q, limit=limit, offset=offset)
    return {"items": items, "limit": limit, "offset": offset}


class ToPersonRequest(BaseModel):
    person_id: int
    text: str


@router.post("/messages/to-person", name="send_to_person")
async def send_to_person(req: ToPersonRequest, request: Request):
    account = await _rw_account(request)
    if account.platform == "max":
        return await _send_to_person_max(request, account, req)
    async with account_db(account.id) as db:
        best = await people_queries.reach_person(db, account, req.person_id)
    if best is None:
        raise HTTPException(status_code=404, detail="person is not reachable from this account")
    identity, user, reach = best

    from app.telegram.pool import require_authorized_client

    client = await require_authorized_client(request)
    async with write_slot(request, SEND, "send_to_person", identity.external_user_id):
        return await _send_to_person(client, account, identity, user, reach, req)


async def _send_to_person_max(request: Request, account: Account, req: ToPersonRequest) -> dict:
    """MAX addresses a person by id: the dialog is me XOR peer, known before the
    first message (plan 2026-10-19 PR-D2). A first message to a chat without
    history counts in new_dialogs_per_day like any send."""
    from app.max.api.common import authorized_or_error, send_text

    if account.external_user_id is None:
        raise HTTPException(status_code=409, detail="account identity unknown: log the MAX session in first")
    async with account_db(account.id) as db:
        best = await people_queries.reach_person(db, account, req.person_id)
    if best is None:
        raise HTTPException(status_code=404, detail="person is not reachable from this account")
    identity, _, reach = best
    chat_id = account.external_user_id ^ identity.external_user_id
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    msg = await send_text(request, session, "send_to_person", chat_id, req.text)
    return {"status": "sent", "person_id": req.person_id, "method": reach.method, "note": reach.note,
            "chat_id": chat_id, "message_id": msg.id}


async def _send_to_person(client, account: Account, identity, user, reach, req: ToPersonRequest) -> dict:
    from telethon.tl.functions.contacts import ImportContactsRequest
    from telethon.tl.types import InputPeerUser, InputPhoneContact

    if reach.method == "direct":
        peer = InputPeerUser(identity.external_user_id, user.access_hash)
    elif reach.method == "username":
        peer = identity.username
    else:
        result = await client(ImportContactsRequest([InputPhoneContact(
            client_id=random.getrandbits(63), phone=identity.phone,
            first_name=identity.first_name or identity.username or "", last_name=identity.last_name or "",
        )]))
        if not result.users:
            raise HTTPException(status_code=404, detail="phone is not registered in Telegram")
        peer = result.users[0]
        await tg_store.store_contacts(account.id, [peer], full=False)
    sent = await client.send_message(peer, req.text)
    return {
        "status": "sent",
        "person_id": req.person_id,
        "method": reach.method,
        "note": reach.note,
        "chat_id": getattr(sent, "chat_id", None),
        "message_id": getattr(sent, "id", None),
    }


# ---------------------------------------------------------------------------
# Find a person by phone (Р-13…Р-19)
# ---------------------------------------------------------------------------

class ByPhoneRequest(BaseModel):
    phone: str
    first_name: str | None = None
    last_name: str | None = None
    refresh: bool = False


NOT_FOUND_REASON = "not registered or hidden by privacy"


async def _known(account: Account, e164: str, user_id: int | None = None) -> dict | None:
    """The account's users row for this phone (or this user) on its platform, with the person."""
    async with account_db(account.id) as db:
        match = Identity.phone == e164 if user_id is None else Identity.external_user_id == user_id
        row = (await db.execute(
            select(Identity, User)
            .join(User, and_(User.account_id == account.id, User.identity_id == Identity.id))
            .where(Identity.platform == account.platform, match)
            .order_by(User.is_contact.desc(), Identity.id)
            .limit(1)
        )).one_or_none()
    if row is None:
        return None
    identity, user = row
    if account.platform == "max":
        chat_id = account.external_user_id ^ user.external_id if account.external_user_id is not None else None
    else:
        chat_id = user.external_id
    name = " ".join(p for p in (user.first_name, user.last_name) if p) or user.username
    return {"person_id": identity.person_id, "user_id": user.external_id, "chat_id": chat_id, "name": name,
            "phone": identity.phone or e164, "is_contact": user.is_contact}


async def _connected(request: Request, account: Account):
    """The account's messenger client (TG) / session (MAX), before a slot is taken:
    a session that is down spends no lookup."""
    if account.platform == "telegram":
        from app.telegram.pool import require_authorized_client

        return await require_authorized_client(request)
    from app.max.api.common import authorized_or_error

    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        raise HTTPException(status_code=session.status_code, detail=session.body.decode())
    return session


async def _messenger_lookup(conn, account: Account, req: ByPhoneRequest, e164: str,
                            masked: str) -> tuple[int | None, dict]:
    """(found user id or None, extra answer fields). Errors of the messenger → 502."""
    if account.platform == "telegram":
        from app.telegram import lookup as tg_lookup

        try:
            return await tg_lookup.by_phone(conn, account.id, e164, req.first_name, req.last_name), {}
        except tg_lookup.ImportLimited:
            raise HTTPException(status_code=429, detail="Telegram import limit, retry later "
                                                        "(the number was not checked; the lookup is spent)")
        except Exception as exc:  # noqa: BLE001 — FloodWait, RPC errors: the slot is spent anyway
            raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")

    from app.max import lookup as max_lookup

    try:
        found = await max_lookup.by_phone(conn, e164, masked)
    except max_lookup.NotFound:
        return None, {}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")
    return found.user_id, ({"warning": found.warning} if found.warning else {})


@router.post("/contacts/by-phone", name="contact_by_phone")
async def contact_by_phone(req: ByPhoneRequest, request: Request):
    """Find a person by phone in this account's messenger, add them to its contacts
    and to the contacts base; a number the account already knows answers from the
    base without a lookup (cached), refresh=true looks it up anyway.

    The number reaches logs and the audit only masked (+7******5956).
    """
    e164 = normalize_phone(req.phone)
    if e164 is None or len(e164) > 16:
        raise HTTPException(status_code=422, detail="phone: a number with the country code, e.g. +79001234567")
    masked = mask_phone(e164)
    account = await _rw_account(request)

    if not req.refresh:
        known = await _known(account, e164)
        if known is not None:
            request.state.audit_target_user_id = known["user_id"]
            return {"found": True, "cached": True, "platform": account.platform, **known,
                    "lookups_left_today": await write_limits.lookups_left(account.id)}

    conn = await _connected(request, account)
    async with lookup_slot(request, "contact_by_phone"):
        user_id, extra = await _messenger_lookup(conn, account, req, e164, masked)
    left = await write_limits.lookups_left(account.id)
    if user_id is None:
        log.info("contact_by_phone[%s]: %s not found", account.alias, masked)
        return JSONResponse(status_code=404, content={
            "found": False, "phone": masked, "reason": NOT_FOUND_REASON, "lookups_left_today": left})
    log.info("contact_by_phone[%s]: %s → user %s", account.alias, masked, user_id)
    request.state.audit_target_user_id = user_id
    known = await _known(account, e164, user_id=user_id) or {"user_id": user_id}
    return {"found": True, "cached": False, "platform": account.platform, **known, **extra,
            "lookups_left_today": left}
