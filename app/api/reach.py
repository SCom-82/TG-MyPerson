"""People a rw account can write to, and writing to a person (ТЗ 2026-10-13 §6.4, §7.3).

Account key (or alias). Contact fields of the shared base only — no messages
or chats of other accounts. ro accounts: 403.
"""

import random

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select

from app.api.data_reads import account_of
from app.database import async_session
from app.models import Account
from app.repo import people_queries
from app.services import tg_store
from app.storage import account_db

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
):
    account = await _rw_account(request)
    async with account_db(account.id) as db:
        items = await people_queries.reachable(db, account, q=q, limit=limit)
    return {"items": items, "total": len(items)}


class ToPersonRequest(BaseModel):
    person_id: int
    text: str


@router.post("/messages/to-person", name="send_to_person")
async def send_to_person(req: ToPersonRequest, request: Request):
    account = await _rw_account(request)
    if account.platform != "telegram":
        raise HTTPException(status_code=501, detail=f"send_to_person is not supported for {account.platform} yet")
    async with account_db(account.id) as db:
        best = await people_queries.reach_person(db, account, req.person_id)
    if best is None:
        raise HTTPException(status_code=404, detail="person is not reachable from this account")
    identity, user, reach = best

    from telethon.tl.functions.contacts import ImportContactsRequest
    from telethon.tl.types import InputPeerUser, InputPhoneContact

    from app.telegram.pool import require_authorized_client

    client = await require_authorized_client(request)
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
