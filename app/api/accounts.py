"""Admin CRUD endpoints for account management.

All endpoints require X-Admin-Key header (checked by admin_auth_middleware).
These endpoints do NOT create Telegram sessions — use /auth/login?session=<alias>
after creating an account record.
"""

import re
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import async_session
from app.models import Account, AuditLog
from app.telegram.pool import pool
from app.authz.middleware import invalidate_alias_cache

router = APIRouter(prefix="/accounts", tags=["accounts-admin"])

_PHONE_RE = re.compile(r"^\+\d{7,15}$")
_VALID_MODES = {"rw", "ro"}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class AccountCreate(BaseModel):
    alias: str
    phone: str
    mode: str = "rw"
    display_name: str | None = None
    notes: str | None = None
    watch_chat_ids: list[int] | None = None

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        if not _PHONE_RE.match(v):
            raise ValueError("phone must match +NNNNNNN (7-15 digits)")
        return v

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in _VALID_MODES:
            raise ValueError(f"mode must be one of {_VALID_MODES}")
        return v


class AccountPatch(BaseModel):
    mode: str | None = None
    display_name: str | None = None
    notes: str | None = None
    watch_chat_ids: list[int] | None = None
    is_enabled: bool | None = None
    # Reject alias/phone changes
    alias: str | None = None
    phone: str | None = None

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_MODES:
            raise ValueError(f"mode must be one of {_VALID_MODES}")
        return v


class AuditQueryParams(BaseModel):
    limit: int = 200
    tool: str | None = None
    status: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _account_to_dict(account: Account, pool_status_map: dict[str, dict] | None = None) -> dict:
    pool_entry = (pool_status_map or {}).get(account.alias, {})
    return {
        "id": account.id,
        "alias": account.alias,
        "phone": account.phone,
        "tg_user_id": account.tg_user_id,
        "mode": account.mode,
        "display_name": account.display_name,
        "is_enabled": account.is_enabled,
        "notes": account.notes,
        "watch_chat_ids": account.watch_chat_ids or [],
        "created_at": account.created_at.isoformat() if account.created_at else None,
        "last_started_at": account.last_started_at.isoformat() if account.last_started_at else None,
        # From pool (runtime state)
        "is_running": pool_entry.get("is_running", False),
        "last_error": pool_entry.get("last_error"),
        "last_started_at_pool": (
            datetime.fromtimestamp(pool_entry["last_started_at"], tz=timezone.utc).isoformat()
            if pool_entry.get("last_started_at")
            else None
        ),
    }


def _build_pool_map() -> dict[str, dict]:
    """Build alias→pool_entry dict from pool.pool_status()."""
    return {entry["alias"]: entry for entry in pool.pool_status()}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("", status_code=201, name="admin_create_account")
async def create_account(body: AccountCreate) -> dict:
    """Create a new account record. Does NOT start a Telegram session."""
    async with async_session() as db:
        account = Account(
            alias=body.alias,
            phone=body.phone,
            mode=body.mode,
            display_name=body.display_name,
            notes=body.notes,
            watch_chat_ids=body.watch_chat_ids,
            is_enabled=True,
        )
        db.add(account)
        try:
            await db.commit()
            await db.refresh(account)
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"Account with alias '{body.alias}' already exists",
            )

    return _account_to_dict(account)


@router.get("", name="admin_list_accounts")
async def list_accounts(include_disabled: bool = Query(default=False)) -> list[dict]:
    """List accounts. By default excludes disabled accounts."""
    async with async_session() as db:
        stmt = select(Account)
        if not include_disabled:
            stmt = stmt.where(Account.is_enabled == True)  # noqa: E712
        result = await db.execute(stmt.order_by(Account.id))
        accounts = result.scalars().all()

    pool_map = _build_pool_map()
    return [_account_to_dict(a, pool_map) for a in accounts]


@router.get("/{account_id}", name="admin_get_account")
async def get_account(account_id: int) -> dict:
    """Get single account by id."""
    async with async_session() as db:
        result = await db.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()

    if account is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")

    pool_map = _build_pool_map()
    return _account_to_dict(account, pool_map)


@router.patch("/{account_id}", name="admin_patch_account")
async def patch_account(account_id: int, body: AccountPatch) -> dict:
    """Update allowed fields on account. alias and phone cannot be changed."""
    # Reject attempts to change alias/phone
    if body.alias is not None or body.phone is not None:
        raise HTTPException(
            status_code=400,
            detail="alias and phone cannot be changed via PATCH (would break session linkage)",
        )

    async with async_session() as db:
        result = await db.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()

        if account is None:
            raise HTTPException(status_code=404, detail=f"Account {account_id} not found")

        old_alias = account.alias
        was_enabled = account.is_enabled

        if body.mode is not None:
            account.mode = body.mode
        if body.display_name is not None:
            account.display_name = body.display_name
        if body.notes is not None:
            account.notes = body.notes
        if body.watch_chat_ids is not None:
            account.watch_chat_ids = body.watch_chat_ids
        if body.is_enabled is not None:
            account.is_enabled = body.is_enabled

        await db.commit()
        await db.refresh(account)

    # Invalidate alias cache so resolve_alias picks up changes immediately
    invalidate_alias_cache(old_alias)

    # If is_enabled changed to False — stop pool session
    if body.is_enabled is False and was_enabled:
        try:
            await pool.stop_alias(old_alias)
        except Exception:
            pass  # Non-fatal: session may not be in pool

    pool_map = _build_pool_map()
    return _account_to_dict(account, pool_map)


@router.delete("/{account_id}", name="admin_delete_account")
async def delete_account(account_id: int) -> dict:
    """Disable account (soft delete). Equivalent to PATCH {is_enabled: false}."""
    return await patch_account(account_id, AccountPatch(is_enabled=False))


@router.get("/{account_id}/audit", name="admin_account_audit")
async def get_account_audit(
    account_id: int,
    limit: int = Query(default=200, le=1000),
    tool: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> list[dict]:
    """Return recent audit log entries for this account."""
    from sqlalchemy import desc

    async with async_session() as db:
        # Verify account exists
        acc_result = await db.execute(select(Account.id).where(Account.id == account_id))
        if acc_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail=f"Account {account_id} not found")

        stmt = (
            select(AuditLog)
            .where(AuditLog.account_id == account_id)
            .order_by(desc(AuditLog.ts))
            .limit(limit)
        )
        if tool:
            stmt = stmt.where(AuditLog.tool == tool)
        if status:
            if status not in {"ok", "denied", "error"}:
                raise HTTPException(status_code=400, detail="status must be ok, denied or error")
            stmt = stmt.where(AuditLog.status == status)

        result = await db.execute(stmt)
        logs = result.scalars().all()

    return [
        {
            "id": str(log.id),
            "ts": log.ts.isoformat() if log.ts else None,
            "tool": log.tool,
            "alias": log.alias,
            "is_write": log.is_write,
            "chat_id": log.chat_id,
            "target_user_id": log.target_user_id,
            "status": log.status,
            "error": log.error,
        }
        for log in logs
    ]
