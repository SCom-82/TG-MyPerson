"""MAX users (API spec §3): list_users, resolve_user, resolve_by_id."""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from types import SimpleNamespace

from sqlalchemy import func, select

from app.api import data_reads
from app.max import normalize, repo
from app.max.store_new import NewMaxStore
from app.models import User
from app.services.tg_store import new_mode, shadow
from app.storage import account_db
from app.max.api.common import authorized_or_error, session_or_error
from app.schemas import (
    BulkResolveByIdRequest,
    BulkResolveResponse,
    BulkResolveStats,
    PaginatedResponse,
    ResolvedUserItem,
    ResolveResponse,
    ResolveUserRequest,
    UnresolvedUserItem,
    UserResponse,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/users", tags=["max-users"])


@router.get("", response_model=PaginatedResponse, name="list_users")
async def list_users(
    search: str | None = Query(None, description="Search by name or username"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    if new_mode():
        return await data_reads.list_users(request, search=search, limit=limit, offset=offset)
    users, total = await repo.get_users(db, search=search, limit=limit, offset=offset)
    return PaginatedResponse(
        items=[UserResponse.model_validate(u) for u in users], total=total, limit=limit, offset=offset
    )


def _display(user) -> str | None:  # noqa: ANN001
    return f"{user.first_name or ''} {user.last_name or ''}".strip() or None


async def _fetch_and_store(session, db: AsyncSession, user_ids: list[int]) -> dict:  # noqa: ANN001
    users = [u for u in await session.client.get_users(user_ids) if u is not None]
    rows = [normalize.normalize_user(u, me_id=session.user_id) for u in users]
    if new_mode():
        await NewMaxStore(session.account_id).upsert_users(rows, "resolve")

        async def legacy():
            await repo.upsert_users(db, rows)
            await db.commit()
        await shadow("max resolve", legacy)
    else:
        await repo.upsert_users(db, rows)
        await db.commit()
    return {row["id"]: row for row in rows}


def _as_legacy(u: User) -> SimpleNamespace:
    """users row of the account in the shape the handlers below read (max_users)."""
    return SimpleNamespace(id=u.external_id, username=u.username, first_name=u.first_name,
                           last_name=u.last_name, phone=u.phone)


async def _find_user(request: Request, db: AsyncSession, **by):
    """repo.find_user; STORAGE_MODE=new: among the account's own users."""
    if not new_mode():
        return await repo.find_user(db, **by)
    a = data_reads.account_of(request)
    q = select(User).where(User.account_id == a)
    if by.get("user_id") is not None:
        q = q.where(User.external_id == by["user_id"])
    elif by.get("username") is not None:
        q = q.where(func.lower(User.username) == by["username"].lower())
    elif by.get("phone") is not None:
        q = q.where(User.phone == by["phone"])
    else:
        return None
    async with account_db(a) as adb:
        u = (await adb.execute(q.limit(1))).scalar_one_or_none()
    return _as_legacy(u) if u is not None else None


async def _users_by_ids(request: Request, db: AsyncSession, user_ids: list[int]) -> dict:
    if not new_mode():
        return await repo.get_users_by_ids(db, user_ids)
    a = data_reads.account_of(request)
    async with account_db(a) as adb:
        rows = (await adb.execute(
            select(User).where(User.account_id == a, User.external_id.in_(user_ids))
        )).scalars().all()
    return {u.external_id: _as_legacy(u) for u in rows}


@router.post("/resolve", response_model=ResolveResponse, name="resolve_user")
async def resolve_user(req: ResolveUserRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """By id (digits), @link/link, or +phone. Network only for an id miss (no @username lookup in MAX)."""
    value = req.username.strip().lstrip("@")
    if value.startswith("+"):
        user = await _find_user(request, db, phone=value)
    elif value.lstrip("-").isdigit():
        user = await _find_user(request, db, user_id=int(value))
        if user is None:
            session = await authorized_or_error(request)
            if isinstance(session, JSONResponse):
                return session
            try:
                fetched = await _fetch_and_store(session, db, [int(value)])
            except Exception as exc:  # noqa: BLE001
                log.warning("max resolve_user %s failed: %s", value, exc)
                raise HTTPException(status_code=502, detail=str(exc))
            row = fetched.get(int(value))
            if row is None:
                raise HTTPException(status_code=404, detail="User not found")
            return ResolveResponse(
                id=row["id"], type="user",
                title=f"{row['first_name'] or ''} {row['last_name'] or ''}".strip() or None,
                username=row["username"],
            )
    else:
        user = await _find_user(request, db, username=value)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return ResolveResponse(id=user.id, type="user", title=_display(user), username=user.username)


@router.post("/resolve_by_id", response_model=BulkResolveResponse, name="resolve_by_id")
async def bulk_resolve_by_id(req: BulkResolveByIdRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """From max_users; missing ids are fetched in one PyMax call. persist → max_users
    (not users_registry: that registry is keyed by tg_user_id)."""
    if len(req.user_ids) > 500:
        raise HTTPException(status_code=400, detail="max 500 user_ids per request")
    session = await session_or_error(request)
    if isinstance(session, JSONResponse):
        return session

    t0 = time.monotonic()
    known = await _users_by_ids(request, db, req.user_ids)
    found = {uid: {"id": u.id, "username": u.username, "first_name": u.first_name,
                   "last_name": u.last_name, "phone": u.phone}
             for uid, u in known.items() if u.first_name or u.username}
    missing = [uid for uid in req.user_ids if uid not in found]
    errors: dict[int, str] = {}
    if missing:
        if session.client is None:
            errors = {uid: f"session not available: {session.state}" for uid in missing}
        else:
            try:
                fetched = await _fetch_and_store(session, db, missing) if req.persist else {
                    u.id: normalize.normalize_user(u)
                    for u in await session.client.get_users(missing) if u is not None
                }
                found.update(fetched)
            except Exception as exc:  # noqa: BLE001
                errors = {uid: f"{type(exc).__name__}: {exc}" for uid in missing}

    resolved, unresolved = [], []
    for uid in req.user_ids:
        row = found.get(uid)
        if row is not None:
            resolved.append(ResolvedUserItem(
                user_id=uid, username=row["username"], first_name=row["first_name"],
                last_name=row["last_name"], phone=row["phone"],
            ))
        else:
            unresolved.append(UnresolvedUserItem(user_id=uid, error=errors.get(uid, "NotFound")))
    return BulkResolveResponse(
        session=session.alias,
        resolved=resolved,
        unresolved=unresolved,
        stats=BulkResolveStats(
            requested=len(req.user_ids), resolved=len(resolved), unresolved=len(unresolved),
            took_ms=int((time.monotonic() - t0) * 1000),
        ),
    )
