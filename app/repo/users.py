"""users — a person as one account sees it (ТЗ 2026-10-13 §2.4, §7.1)."""

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, User
from app.repo import ensure_scope
from app.repo.people import upsert_identity
from app.repo.rows import UserRow
from app.utils.phone import normalize_phone

VIA = ("message", "member", "contact", "resolve", "dialog")


async def upsert_user(db: AsyncSession, account_id: int, platform: str, row: UserRow, via: str) -> int:
    """identity (+ person) and the account's users row in the caller's transaction.

    is_self is recomputed from accounts.external_user_id; access_hash and
    is_contact keep the stored value when the row carries None.
    """
    ensure_scope(db, account_id)
    if via not in VIA:
        raise ValueError(f"unknown via {via!r}")
    identity_id = await upsert_identity(db, platform, row)

    is_self = func.coalesce(
        select(Account.external_user_id == row.external_id)
        .where(Account.id == account_id)
        .scalar_subquery(),
        False,
    )
    stmt = pg_insert(User).values(
        account_id=account_id,
        external_id=row.external_id,
        identity_id=identity_id,
        username=row.username or None,
        first_name=row.first_name or None,
        last_name=row.last_name or None,
        phone=normalize_phone(row.phone),
        is_bot=bool(row.is_bot),
        is_self=is_self,
        access_hash=row.access_hash,
        is_contact=bool(row.is_contact),
        last_seen_via=via,
        raw=row.raw,
    )
    ex = stmt.excluded
    t = User.__table__.c
    set_ = {
        "identity_id": ex.identity_id,
        "username": func.coalesce(ex.username, t.username),
        "first_name": func.coalesce(ex.first_name, t.first_name),
        "last_name": func.coalesce(ex.last_name, t.last_name),
        "phone": func.coalesce(ex.phone, t.phone),
        "is_bot": ex.is_bot,
        "is_self": ex.is_self,
        "access_hash": func.coalesce(ex.access_hash, t.access_hash),
        "last_seen_via": ex.last_seen_via,
        "raw": func.coalesce(ex.raw, t.raw),
    }
    if row.is_contact is not None:
        set_["is_contact"] = ex.is_contact
    stmt = stmt.on_conflict_do_update(
        index_elements=[User.account_id, User.external_id], set_=set_
    ).returning(User.id)
    return (await db.execute(stmt)).scalar_one()


async def user_pk(db: AsyncSession, account_id: int, external_id: int) -> int | None:
    ensure_scope(db, account_id)
    return (await db.execute(
        select(User.id).where(User.account_id == account_id, User.external_id == external_id)
    )).scalar_one_or_none()


async def get_user(db: AsyncSession, account_id: int, external_id: int) -> User | None:
    ensure_scope(db, account_id)
    return (await db.execute(
        select(User).where(User.account_id == account_id, User.external_id == external_id)
    )).scalar_one_or_none()


async def list_users(
    db: AsyncSession, account_id: int, *, search: str | None = None, limit: int = 50, offset: int = 0
) -> tuple[list[User], int]:
    ensure_scope(db, account_id)
    flt = [User.account_id == account_id]
    if search:
        pattern = f"%{search}%"
        flt.append(or_(
            User.username.ilike(pattern), User.first_name.ilike(pattern), User.last_name.ilike(pattern)
        ))
    total = (await db.execute(select(func.count()).select_from(User).where(*flt))).scalar_one()
    rows = (await db.execute(
        select(User).where(*flt).order_by(User.first_seen_at.desc(), User.id.desc())
        .limit(limit).offset(offset)
    )).scalars().all()
    return list(rows), total
