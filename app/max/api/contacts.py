"""MAX list_contacts (API spec §3).

STORAGE_MODE=new: from the account's users (is_contact). The live list of the
client is not the address book: PyMax logs in with the stored contacts_sync
marker, and MAX then sends only the contacts changed since — usually none, so
list_contacts answered 0 with hundreds stored. Ingest keeps users.is_contact
from every login. Legacy mode: the login response, as before.
"""

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.max import normalize
from app.max.api.common import authorized_or_error
from app.repo import users as users_repo
from app.services.tg_store import new_mode
from app.storage import account_db

router = APIRouter(prefix="/contacts", tags=["max-contacts"])

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
