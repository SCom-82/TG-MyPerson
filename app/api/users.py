from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import UserResponse, PaginatedResponse
from app.services.user_service import get_users

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
