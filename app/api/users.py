import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import UserResponse, PaginatedResponse, ResolveUserRequest, ResolveResponse
from app.services.user_service import get_users, upsert_user
from app.telegram.pool import require_authorized_client

log = logging.getLogger(__name__)
router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=PaginatedResponse)
async def list_users(
    search: str | None = Query(None, description="Search by name or username"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    users, total = await get_users(db, search=search, limit=limit, offset=offset)
    return PaginatedResponse(
        items=[UserResponse.model_validate(u) for u in users],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/resolve", response_model=ResolveResponse)
async def resolve_user(req: ResolveUserRequest, request: Request, db: AsyncSession = Depends(get_db)):
    client = await require_authorized_client(request)
    try:
        from telethon.tl.types import User, Channel, Chat

        username = req.username.lstrip('@').strip()
        entity = await client.get_entity(username)

        if isinstance(entity, User):
            await upsert_user(db, entity)
            return ResolveResponse(
                id=entity.id,
                type='user',
                title=f"{entity.first_name or ''} {entity.last_name or ''}".strip() or None,
                username=entity.username,
                members_count=None,
                description=None,
                is_joined=None,
            )
        elif isinstance(entity, Channel):
            chat_type = 'channel' if entity.broadcast else 'supergroup'
            return ResolveResponse(
                id=entity.id,
                type=chat_type,
                title=entity.title,
                username=entity.username,
                members_count=getattr(entity, 'participants_count', None),
                description=getattr(entity, 'about', None),
                is_joined=None,
            )
        elif isinstance(entity, Chat):
            return ResolveResponse(
                id=entity.id,
                type='group',
                title=entity.title,
                username=None,
                members_count=getattr(entity, 'participants_count', None),
                description=None,
                is_joined=None,
            )
        else:
            return ResolveResponse(id=getattr(entity, 'id', None), type='unknown', title=None)
    except Exception as e:
        log.exception("Failed to resolve user: %s", req.username)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{user_id}/block")
async def block_user(user_id: int, request: Request):
    client = await require_authorized_client(request)
    try:
        from telethon.tl.functions.contacts import BlockRequest
        await client(BlockRequest(id=user_id))
        return {"status": "blocked", "user_id": user_id}
    except Exception as e:
        log.exception("Failed to block user %d", user_id)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{user_id}/unblock")
async def unblock_user(user_id: int, request: Request):
    client = await require_authorized_client(request)
    try:
        from telethon.tl.functions.contacts import UnblockRequest
        await client(UnblockRequest(id=user_id))
        return {"status": "unblocked", "user_id": user_id}
    except Exception as e:
        log.exception("Failed to unblock user %d", user_id)
        raise HTTPException(status_code=400, detail=str(e))
