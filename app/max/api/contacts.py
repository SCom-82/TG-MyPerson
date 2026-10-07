"""MAX list_contacts (API spec §3): contacts from the login response."""

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.max import normalize
from app.max.api.common import authorized_or_error

router = APIRouter(prefix="/contacts", tags=["max-contacts"])


@router.get("", name="list_contacts")
async def list_contacts(
    request: Request,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        return session
    items = []
    for user in session.client.contacts or []:
        if user is None:
            continue
        row = normalize.normalize_user(user, me_id=session.user_id)
        items.append({k: row[k] for k in ("id", "username", "first_name", "last_name", "phone", "is_bot", "is_self")})
    return {"items": items[offset:offset + limit], "total": len(items), "limit": limit, "offset": offset}
