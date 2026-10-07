"""MAX search_global (API spec §3): over our DB — MAX has no server search in PyMax."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.max import repo

router = APIRouter(prefix="/search", tags=["max-search"])


@router.get("/global", name="search_global")
async def search_global(
    q: str = Query(..., description="Search query"),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    rows = await repo.search_messages(db, q, limit)
    items = [
        {
            "message_id": m.message_id,
            "text": m.text,
            "date": m.tg_date.isoformat() if m.tg_date else None,
            "from_user_id": m.from_user_id,
            "chat": {"id": c.id, "title": c.title, "username": c.username} if c is not None else None,
        }
        for m, c in rows
    ]
    return {"items": items, "total": len(items)}
