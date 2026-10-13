"""Service operations of the account isolation migration (ТЗ 2026-10-13 §8.1).

X-Admin-Key (admin prefix /api/v1/admin), not tools: they run inside the
service process on the pool's own Telegram clients, so no second connection
on an account's auth key is ever opened.
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.scripts import migrate_isolation

router = APIRouter(prefix="/admin/isolation", tags=["admin-isolation"])


@router.post("/probe", name="admin_isolation_probe")
async def isolation_probe() -> dict:
    """migrate_isolation probe on the running clients → mig_membership + report."""
    return await migrate_isolation.probe_in_service()


class MeasureRawRequest(BaseModel):
    alias: str
    chat: int
    limit: int = Field(default=1000, ge=1, le=5000)


@router.post("/measure_raw", name="admin_isolation_measure_raw")
async def isolation_measure_raw(req: MeasureRawRequest) -> dict:
    """Size of the full TG raw on live messages of one chat (read only)."""
    from app.scripts.measure_raw import measure_in_service

    return await measure_in_service(req.alias, req.chat, req.limit)
