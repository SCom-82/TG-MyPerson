"""Contacts: people → identities (owner level, no RLS) — ТЗ 2026-10-13 §7.1–7.2.

upsert_identity is called by repo.users.upsert_user inside the account's
transaction; merge/split are owner operations.
"""

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Identity, PeopleMergeLog, Person
from app.repo import ensure_owner
from app.repo.rows import UserRow
from app.utils.phone import normalize_phone

PLATFORMS = ("telegram", "max")


def _display_name(row: UserRow) -> str | None:
    name = " ".join(p for p in (row.first_name, row.last_name) if p)
    return name or row.username or None


async def upsert_identity(db: AsyncSession, platform: str, row: UserRow) -> int:
    """Identity of (platform, external id); new identity → new person. Returns identity id.

    Non-empty incoming username/names/phone overwrite, empty ones keep the
    stored value. A phone that matches an identity of the other platform
    triggers the auto-merge (D13).
    """
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform {platform!r}")
    phone = normalize_phone(row.phone)
    values = {
        "username": row.username or None,
        "first_name": row.first_name or None,
        "last_name": row.last_name or None,
        "phone": phone,
        "is_bot": bool(row.is_bot),
    }

    identity_id = await _update_identity(db, platform, row.external_id, values)
    if identity_id is None:
        person_id = (await db.execute(
            pg_insert(Person)
            .values(display_name=_display_name(row), primary_phone=phone)
            .returning(Person.id)
        )).scalar_one()
        identity_id = (await db.execute(
            pg_insert(Identity)
            .values(person_id=person_id, platform=platform, external_user_id=row.external_id, **values)
            .on_conflict_do_nothing(index_elements=[Identity.platform, Identity.external_user_id])
            .returning(Identity.id)
        )).scalar_one_or_none()
        if identity_id is None:
            # Another transaction created the identity first: drop our person.
            await db.execute(delete(Person).where(Person.id == person_id))
            identity_id = await _update_identity(db, platform, row.external_id, values)

    if phone:
        # A phone that appears later also fills the person's primary phone.
        await db.execute(text(
            "UPDATE people SET primary_phone = :phone WHERE primary_phone IS NULL"
            " AND id = (SELECT person_id FROM identities WHERE id = :i)"
        ), {"phone": phone, "i": identity_id})
        await _auto_merge(db, identity_id)
    return identity_id


async def _update_identity(db: AsyncSession, platform: str, external_id: int, values: dict) -> int | None:
    t = Identity.__table__.c
    return (await db.execute(
        update(Identity)
        .where(Identity.platform == platform, Identity.external_user_id == external_id)
        .values(
            username=func.coalesce(values["username"], t.username),
            first_name=func.coalesce(values["first_name"], t.first_name),
            last_name=func.coalesce(values["last_name"], t.last_name),
            phone=func.coalesce(values["phone"], t.phone),
            is_bot=values["is_bot"],
            last_seen_at=func.now(),
        )
        .returning(Identity.id)
    )).scalar_one_or_none()


async def _auto_merge(db: AsyncSession, identity_id: int) -> None:
    """D13: same E.164 phone on the other platform, different person, nobody pinned.

    The identity on the younger person (bigger id) moves to the older one;
    tags are united, notes concatenated; a person left without identities is
    deleted. Logged as auto_merge.
    """
    me = (await db.execute(
        select(Identity.person_id, Identity.platform, Identity.phone, Identity.person_pinned)
        .where(Identity.id == identity_id)
    )).one()
    if me.person_pinned or not me.phone:
        return
    other = (await db.execute(
        select(Identity.id, Identity.person_id)
        .where(
            Identity.phone == me.phone,
            Identity.platform != me.platform,
            Identity.person_id != me.person_id,
            Identity.person_pinned.is_(False),
        )
        .order_by(Identity.person_id)
        .limit(1)
    )).one_or_none()
    if other is None:
        return

    target, source = sorted((me.person_id, other.person_id))
    moved = identity_id if me.person_id == source else other.id
    await db.execute(update(Identity).where(Identity.id == moved).values(person_id=target))
    await _absorb_person(db, target, source)
    db.add(PeopleMergeLog(
        action="auto_merge", from_person_id=source, to_person_id=target,
        identity_id=moved, reason=f"phone {me.phone}",
    ))
    await db.flush()


async def _absorb_person(db: AsyncSession, target: int, source: int) -> None:
    """Merge tags/notes/name/phone of source into target; delete source if empty."""
    await db.execute(text("""
        UPDATE people t SET
          tags = ARRAY(SELECT DISTINCT unnest(t.tags || s.tags) ORDER BY 1),
          notes = CASE WHEN s.notes IS NULL OR s.notes = '' THEN t.notes
                       WHEN t.notes IS NULL OR t.notes = '' THEN s.notes
                       ELSE t.notes || E'\\n' || s.notes END,
          display_name = coalesce(t.display_name, s.display_name),
          primary_phone = coalesce(t.primary_phone, s.primary_phone)
        FROM people s
        WHERE t.id = :target AND s.id = :source
    """), {"target": target, "source": source})
    left = (await db.execute(
        select(func.count()).select_from(Identity).where(Identity.person_id == source)
    )).scalar_one()
    if left == 0:
        await db.execute(delete(Person).where(Person.id == source))


async def merge_people(db: AsyncSession, from_id: int, to_id: int, reason: str | None = None) -> int:
    """Manual merge: every identity of from_id moves to to_id and is pinned."""
    ensure_owner(db)
    if from_id == to_id:
        raise ValueError("merge_people: from_id == to_id")
    for pid in (from_id, to_id):
        if await db.get(Person, pid) is None:
            raise LookupError(f"person {pid} not found")
    await db.execute(
        update(Identity).where(Identity.person_id == from_id).values(person_id=to_id, person_pinned=True)
    )
    await db.execute(update(Identity).where(Identity.person_id == to_id).values(person_pinned=True))
    await _absorb_person(db, to_id, from_id)
    db.add(PeopleMergeLog(action="merge", from_person_id=from_id, to_person_id=to_id, reason=reason))
    await db.flush()
    return to_id


async def split_identity(db: AsyncSession, identity_id: int, reason: str | None = None) -> int:
    """Manual split: the identity gets its own new person, pinned. Returns the new person id."""
    ensure_owner(db)
    ident = await db.get(Identity, identity_id, populate_existing=True)
    if ident is None:
        raise LookupError(f"identity {identity_id} not found")
    old_person = ident.person_id
    name = " ".join(p for p in (ident.first_name, ident.last_name) if p) or ident.username
    new_person = (await db.execute(
        pg_insert(Person).values(display_name=name or None, primary_phone=ident.phone).returning(Person.id)
    )).scalar_one()
    await db.execute(
        update(Identity).where(Identity.id == identity_id).values(person_id=new_person, person_pinned=True)
    )
    left = (await db.execute(
        select(func.count()).select_from(Identity).where(Identity.person_id == old_person)
    )).scalar_one()
    if left == 0:
        await db.execute(delete(Person).where(Person.id == old_person))
    db.add(PeopleMergeLog(
        action="split", from_person_id=old_person, to_person_id=new_person,
        identity_id=identity_id, reason=reason,
    ))
    await db.flush()
    return new_person
