import logging

from fastapi import APIRouter, HTTPException, Query

from app.schemas import UserResponse
from app.telegram.client import tg_bridge

log = logging.getLogger(__name__)
router = APIRouter(prefix="/contacts", tags=["contacts"])


@router.get("")
async def list_contacts(
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    client = tg_bridge.client
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=503, detail="Telegram client not authorized")

    try:
        from telethon.tl.functions.contacts import GetContactsRequest

        result = await client(GetContactsRequest(hash=0))
        users = result.users if hasattr(result, 'users') else []

        items = []
        for u in users:
            items.append({
                "id": u.id,
                "username": u.username,
                "first_name": u.first_name,
                "last_name": u.last_name,
                "phone": u.phone,
                "is_bot": u.bot or False,
                "is_self": u.is_self or False,
            })

        total = len(items)
        paginated = items[offset:offset + limit]

        return {"items": paginated, "total": total, "limit": limit, "offset": offset}
    except Exception as e:
        log.exception("Failed to get contacts")
        raise HTTPException(status_code=500, detail=str(e))
