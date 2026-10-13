"""Member snapshots of an account's chats (ТЗ 2026-10-13 §2.4)."""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MemberSnapshot, SnapshotMember, User
from app.repo import ensure_scope
from app.repo.rows import SnapshotMemberRow


async def create_snapshot(
    db: AsyncSession,
    account_id: int,
    chat_id: int,
    members: list[SnapshotMemberRow],
    *,
    members_count: int | None = None,
    source: str | None = None,
    note: str | None = None,
) -> int:
    """Snapshot + its members; user_id links to the account's users row if known."""
    ensure_scope(db, account_id)
    snapshot_id = (await db.execute(
        pg_insert(MemberSnapshot)
        .values(account_id=account_id, chat_id=chat_id, members_count=members_count, source=source, note=note)
        .returning(MemberSnapshot.id)
    )).scalar_one()
    for m in members:
        user_id = (
            select(User.id)
            .where(User.account_id == account_id, User.external_id == m.external_user_id)
            .scalar_subquery()
            if m.external_user_id is not None
            else None
        )
        await db.execute(pg_insert(SnapshotMember).values(
            account_id=account_id,
            snapshot_id=snapshot_id,
            user_id=user_id,
            external_user_id=m.external_user_id,
            username=m.username,
            first_name=m.first_name,
            last_name=m.last_name,
            phone=m.phone,
            role=m.role,
            raw=m.raw,
        ))
    return snapshot_id


async def list_snapshots(db: AsyncSession, account_id: int, chat_id: int) -> list[MemberSnapshot]:
    ensure_scope(db, account_id)
    return list((await db.execute(
        select(MemberSnapshot)
        .where(MemberSnapshot.account_id == account_id, MemberSnapshot.chat_id == chat_id)
        .order_by(MemberSnapshot.taken_at.desc(), MemberSnapshot.id.desc())
    )).scalars().all())


async def get_snapshot(db: AsyncSession, account_id: int, snapshot_id: int) -> MemberSnapshot | None:
    """None for another account's snapshot id."""
    ensure_scope(db, account_id)
    return (await db.execute(
        select(MemberSnapshot).where(MemberSnapshot.account_id == account_id, MemberSnapshot.id == snapshot_id)
    )).scalar_one_or_none()


async def list_members(
    db: AsyncSession, account_id: int, snapshot_id: int, *, limit: int = 1000, offset: int = 0
) -> tuple[list[SnapshotMember], int]:
    ensure_scope(db, account_id)
    flt = (SnapshotMember.account_id == account_id, SnapshotMember.snapshot_id == snapshot_id)
    total = (await db.execute(select(func.count()).select_from(SnapshotMember).where(*flt))).scalar_one()
    rows = (await db.execute(
        select(SnapshotMember).where(*flt).order_by(SnapshotMember.id).limit(limit).offset(offset)
    )).scalars().all()
    return list(rows), total
