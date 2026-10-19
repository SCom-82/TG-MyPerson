"""MAX contacts: list_contacts (API spec §3); import_contacts, remove_contact (plan Р4 Р-38, Р-43).

STORAGE_MODE=new: from the account's users (is_contact). The live list of the
client is not the address book: PyMax logs in with the stored contacts_sync
marker, and MAX then sends only the contacts changed since — usually none, so
list_contacts answered 0 with hundreds stored. Ingest keeps users.is_contact
from every login. Legacy mode: the login response, as before.
"""

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.reach import _known, _rw_account
from app.authz import write_limits
from app.authz.write_limits import ACTION, CONTACT_IMPORT, bundle_slot, write_slot
from app.max import frames, lookup, normalize
from app.max.api.common import authorized_or_error, call_max
from app.max.api.groups import connected
from app.repo import users as users_repo
from app.services.tg_store import new_mode
from app.storage import account_db
from app.utils.phone import mask_phone, normalize_phone

log = logging.getLogger(__name__)

router = APIRouter(prefix="/contacts", tags=["max-contacts"])

MAX_IMPORT = 20

_FIELDS = ("id", "username", "first_name", "last_name", "phone", "is_bot", "is_self")


@router.get("", name="list_contacts")
async def list_contacts(
    request: Request,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    if new_mode():
        account_id = getattr(request.state, "account_id", None)
        async with account_db(account_id) as db:
            rows, total = await users_repo.list_contacts(db, account_id, limit=limit, offset=offset)
        items = [{"id": u.external_id, "username": u.username, "first_name": u.first_name,
                  "last_name": u.last_name, "phone": u.phone, "is_bot": u.is_bot, "is_self": u.is_self}
                 for u in rows]
        return {"items": items, "total": total, "limit": limit, "offset": offset}
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    items = []
    for user in session.client.contacts or []:
        if user is None:
            continue
        row = normalize.normalize_user(user, me_id=session.user_id)
        items.append({k: row[k] for k in _FIELDS})
    return {"items": items[offset:offset + limit], "total": len(items), "limit": limit, "offset": offset}


class ImportContact(BaseModel):
    phone: str
    first_name: str = Field(min_length=1, max_length=64)
    last_name: str | None = Field(default=None, max_length=64)


class ImportContactsRequest(BaseModel):
    contacts: list[ImportContact] = Field(min_length=1, max_length=MAX_IMPORT)
    refresh: bool = False


@router.post("/import", name="import_contacts")
async def import_contacts(req: ImportContactsRequest, request: Request):
    """A batch of numbers into the address book and the contacts base (Р-38), one SYNC.

    Numbers the account knows answer from the base (cached) and take no slot;
    refresh=true sends them too. Every number sent counts in contacts_imported_per_day
    whatever MAX answers. Numbers reach logs and the audit masked only.
    """
    numbers: dict[str, ImportContact] = {}
    for c in req.contacts:
        e164 = normalize_phone(c.phone)
        if e164 is None or len(e164) > 16:
            raise HTTPException(status_code=422, detail=f"phone {mask_phone(c.phone) or '?'}: a number with the "
                                                        f"country code, e.g. +79001234567")
        numbers.setdefault(e164, c)
    account = await _rw_account(request)

    items: dict[str, dict] = {}
    if not req.refresh:
        for e164 in numbers:
            known = await _known(account, e164)
            if known is not None and known.get("is_contact"):
                items[e164] = {"phone": e164, "found": True, "cached": True, **_item(known)}
    to_send = [(e164, numbers[e164].first_name) for e164 in numbers if e164 not in items]

    warnings: list[str] = []
    if to_send:
        session = await connected(request)
        # The slot covers the frame only: once SYNC is out, the numbers count whatever
        # happens to its answer here (review 17 R17-1).
        async with bundle_slot(request, "import_contacts", {CONTACT_IMPORT: len(to_send)}):
            payload = await call_max(frames.sync_contacts(session.client, to_send),
                                     "the import may have been applied, check list_contacts")
        try:
            users, phones = frames.parse_sync(payload)
        except Exception as exc:  # noqa: BLE001 — the shape of SYNC's answer is not checked live
            log.warning("import_contacts[%s]: SYNC answer not understood: %s: %s", account.alias,
                        type(exc).__name__, exc)
            raise HTTPException(status_code=502, detail=f"MAX took the numbers, its answer was not understood "
                                                        f"({type(exc).__name__}); check list_contacts / "
                                                        f"contact_by_phone before retrying")
        found, warnings = await lookup.store_import(session, to_send, users, phones)
        for e164, _ in to_send:
            if e164 not in found:
                items[e164] = {"phone": e164, "found": False, "cached": False, "user_id": None, "person_id": None,
                               "chat_id": None, "name": None}
                continue
            user_id, chat_id = found[e164]
            known = await _known(account, e164, user_id=user_id) or {"user_id": user_id, "chat_id": chat_id}
            items[e164] = {"phone": e164, "found": True, "cached": False, **_item(known)}
        log.info("import_contacts[%s]: %s sent, %s found: %s", account.alias, len(to_send), len(found),
                 ", ".join(mask_phone(e) for e, _ in to_send))

    ordered = [items[e164] for e164 in numbers]
    first = next((i["user_id"] for i in ordered if i["found"]), None)
    request.state.audit_target_user_id = first
    out = {"items": ordered,
           "found": sum(1 for i in ordered if i["found"]),
           "not_found": sum(1 for i in ordered if not i["found"]),
           "cached": sum(1 for i in ordered if i["cached"]),
           "imports_left_today": await write_limits.window_left(account.id, "contacts_imported_per_day")}
    if warnings:
        out["warning"] = "; ".join(warnings)
    return out


def _item(known: dict) -> dict:
    return {k: known.get(k) for k in ("user_id", "person_id", "chat_id", "name")}


@router.post("/{user_id}/remove", name="remove_contact")
async def remove_contact(user_id: int, request: Request):
    """Out of the account's MAX address book (Р-43): the person stays in the contacts base."""
    account_id = getattr(request.state, "account_id", None)
    if new_mode():
        async with account_db(account_id) as db:
            if await users_repo.get_user(db, account_id, user_id) is None:
                raise HTTPException(status_code=404, detail="the account does not know this user")
    session = await connected(request)
    request.state.audit_target_user_id = user_id
    async with write_slot(request, ACTION, "remove_contact"):
        await call_max(session.client.remove_contact(user_id), "the contact may have been removed, check list_contacts")
    if new_mode():
        try:
            async with account_db(account_id) as db:
                await users_repo.set_contact(db, account_id, user_id, False)
                await db.commit()
        except Exception:  # noqa: BLE001 — removed in MAX; the next login's contacts repeat it
            log.warning("max[%s]: contact %s removed, local mark failed", session.alias, user_id, exc_info=True)
    return {"user_id": user_id, "is_contact": False}
