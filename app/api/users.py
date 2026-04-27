import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import (
    UserResponse,
    PaginatedResponse,
    ResolveUserRequest,
    ResolveResponse,
    BulkResolveByIdRequest,
    BulkResolveResponse,
    BulkResolveStats,
    ResolvedUserItem,
    UnresolvedUserItem,
)
from app.services.user_service import get_users, upsert_user
from app.services import registry_service
from app.telegram.pool import require_authorized_client, pool

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


@router.post("/resolve_by_id", response_model=BulkResolveResponse, name="resolve_by_id")
async def bulk_resolve_by_id(
    req: BulkResolveByIdRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BulkResolveResponse:
    """Bulk-resolve Telegram user IDs via Telethon get_entity.

    Accepts up to 500 user_ids per request.  For each id, tries
    client.get_entity(id).  If persist=True (default), resolved users are
    upserted into users_registry.

    Returns:
      - resolved: list of user dicts with username, names, etc.
      - unresolved: list of {user_id, error} for failures.
      - stats: counts and elapsed time.
    """
    from telethon.tl.types import User
    from telethon.errors import FloodWaitError

    if len(req.user_ids) > 500:
        raise HTTPException(status_code=400, detail="max 500 user_ids per request")

    alias = getattr(request.state, "session_alias", "work")
    account_id = getattr(request.state, "account_id", None)

    tg_session = await pool.get(alias)
    if not tg_session.client:
        raise HTTPException(
            status_code=503,
            detail=f"Session '{alias}' not authorized — use /auth/login?session={alias}",
        )
    try:
        is_auth = await tg_session.client.is_user_authorized()
    except Exception:
        is_auth = False
    if not is_auth:
        raise HTTPException(
            status_code=503,
            detail=f"Session '{alias}' not authorized — use /auth/login?session={alias}",
        )

    client = tg_session.client
    resolved: list[ResolvedUserItem] = []
    unresolved: list[UnresolvedUserItem] = []
    t0 = time.monotonic()

    for uid in req.user_ids:
        retry = False
        for attempt in range(2):
            try:
                entity = await client.get_entity(uid)
                if not isinstance(entity, User):
                    unresolved.append(
                        UnresolvedUserItem(
                            user_id=uid,
                            error=f"NotAUser: {type(entity).__name__}",
                        )
                    )
                else:
                    item = ResolvedUserItem(
                        user_id=entity.id,
                        username=getattr(entity, "username", None),
                        first_name=getattr(entity, "first_name", None),
                        last_name=getattr(entity, "last_name", None),
                        phone=getattr(entity, "phone", None),
                        is_premium=bool(getattr(entity, "premium", False)),
                    )
                    resolved.append(item)
                    if req.persist:
                        member_data = {
                            "tg_user_id": entity.id,
                            "username": item.username,
                            "first_name": item.first_name,
                            "last_name": item.last_name,
                            "phone": item.phone,
                        }
                        await registry_service.upsert_user_from_member(
                            session=db,
                            member_data=member_data,
                            account_id=account_id,
                            snapshot_id=None,
                        )
                break  # success or non-retryable error
            except FloodWaitError as e:
                if e.seconds > 30:
                    raise HTTPException(
                        status_code=429,
                        detail=f"Telegram FloodWait: retry after {e.seconds}s",
                        headers={"Retry-After": str(e.seconds)},
                    )
                if attempt == 0:
                    # Short wait — sleep and retry once
                    await asyncio.sleep(e.seconds)
                    retry = True
                    continue
                # Second attempt also flood-waited — record as unresolved
                unresolved.append(
                    UnresolvedUserItem(user_id=uid, error=f"FloodWaitError: {e.seconds}s")
                )
                break
            except Exception as e:
                unresolved.append(
                    UnresolvedUserItem(
                        user_id=uid,
                        error=f"{type(e).__name__}: {str(e)}",
                    )
                )
                break

    if req.persist:
        await db.commit()

    took_ms = int((time.monotonic() - t0) * 1000)

    return BulkResolveResponse(
        session=alias,
        resolved=resolved,
        unresolved=unresolved,
        stats=BulkResolveStats(
            requested=len(req.user_ids),
            resolved=len(resolved),
            unresolved=len(unresolved),
            took_ms=took_ms,
        ),
    )


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
