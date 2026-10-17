"""Write limits of accounts: settings and counters (X-Admin-Key, brief 2026-10-17).

GET    /accounts/write-limits               every account: limits, counters, remaining
GET    /accounts/{account_id}/write-limits  one account
PATCH  /accounts/{account_id}/write-limits  change settings (null = that limit off);
                                            the first change copies the current limits
DELETE /accounts/{account_id}/write-limits  back to the platform default
                                            (MAX: defaults, Telegram: no limits)

Applies to the next write, no restart.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.authz import write_limits
from app.database import async_session
from app.models import Account

router = APIRouter(prefix="/accounts", tags=["accounts-admin"])

_COUNT = Field(default=None, ge=0, le=100_000)
_SECONDS = Field(default=None, ge=0, le=3600)


class WriteLimitsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    per_minute: int | None = _COUNT
    per_hour: int | None = _COUNT
    per_day: int | None = _COUNT
    new_dialogs_per_day: int | None = _COUNT
    min_interval_sec: int | None = _SECONDS
    jitter_sec: int | None = _SECONDS
    actions_per_hour: int | None = _COUNT


async def _status_or_404(account_id: int) -> dict:
    result = await write_limits.status(account_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")
    return result


@router.get("/write-limits", name="admin_list_write_limits")
async def list_write_limits():
    async with async_session() as db:
        ids = (await db.execute(select(Account.id).order_by(Account.id))).scalars().all()
    return {"items": [await write_limits.status(i) for i in ids]}


@router.get("/{account_id}/write-limits", name="admin_get_write_limits")
async def get_write_limits(account_id: int):
    return await _status_or_404(account_id)


@router.patch("/{account_id}/write-limits", name="admin_patch_write_limits")
async def patch_write_limits(account_id: int, body: WriteLimitsPatch):
    await _status_or_404(account_id)
    changes = {k: getattr(body, k) for k in body.model_fields_set}
    if changes.get("enabled", True) is None:
        raise HTTPException(status_code=422, detail="enabled must be true or false")
    await write_limits.set_limits(account_id, changes)
    return await _status_or_404(account_id)


@router.delete("/{account_id}/write-limits", name="admin_reset_write_limits")
async def reset_write_limits(account_id: int):
    await _status_or_404(account_id)
    await write_limits.reset_limits(account_id)
    return await _status_or_404(account_id)
