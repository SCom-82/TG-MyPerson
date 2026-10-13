"""Service operations of the account isolation migration (ТЗ 2026-10-13 §8.1).

X-Admin-Key (admin prefix /api/v1/admin), not tools: they run inside the
service process on the pool's own Telegram clients, so no second connection
on an account's auth key is ever opened.
"""

from fastapi import APIRouter

from app.scripts import migrate_isolation

router = APIRouter(prefix="/admin/isolation", tags=["admin-isolation"])


@router.post("/probe", name="admin_isolation_probe")
async def isolation_probe() -> dict:
    """migrate_isolation probe on the running clients → mig_membership + report."""
    return await migrate_isolation.probe_in_service()
