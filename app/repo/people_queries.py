"""Cross-account queries over contacts (ТЗ 2026-10-13 §6.4, §7.3).

The only module allowed to read data tables without a single account_id
(ТЗ §1): owner-session reads for /people, and the reachability of people for
one rw account (/reachable) — that one returns contact fields only, never
other accounts' messages or chats.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Text, and_, case, cast, func, literal, null, or_, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, Chat, Identity, Message, Person, User
from app.repo import ensure_owner, ensure_scope


# ---------------------------------------------------------------------------
# Reachability (§7.3) — computed, never stored
# ---------------------------------------------------------------------------

_PHONE_NOTE = "ImportContacts adds the person to the account's address book"


@dataclass(frozen=True)
class Reach:
    method: str  # direct | username | phone
    note: str | None = None


def reach_of(platform: str, identity: Identity, user: User | None) -> Reach | None:
    """How an account of `platform` can write to this identity; None = unreachable."""
    if identity.platform != platform:
        return None
    if platform == "max":
        # Addressed by id, the dialog is me XOR peer.
        return Reach("direct")
    if user is not None and user.access_hash is not None:
        return Reach("direct")
    if identity.username:
        return Reach("username")
    if identity.phone:
        return Reach("phone", _PHONE_NOTE)
    return None


# ---------------------------------------------------------------------------
# Owner reads
# ---------------------------------------------------------------------------

def _search_filter(q: str):
    pattern = f"%{q}%"
    ident = select(Identity.person_id).where(or_(
        Identity.username.ilike(pattern),
        Identity.first_name.ilike(pattern),
        Identity.last_name.ilike(pattern),
        Identity.phone.ilike(pattern),
        cast(Identity.external_user_id, Text).ilike(pattern),
    ))
    return or_(Person.display_name.ilike(pattern), Person.primary_phone.ilike(pattern), Person.id.in_(ident))


async def find_people(
    db: AsyncSession, *, q: str | None = None, tag: str | None = None, platform: str | None = None,
    limit: int = 50, offset: int = 0,
) -> tuple[list[tuple[Person, list[Identity]]], int]:
    ensure_owner(db)
    flt = []
    if q:
        flt.append(_search_filter(q))
    if tag:
        flt.append(Person.tags.any(tag))
    if platform:
        flt.append(Person.id.in_(select(Identity.person_id).where(Identity.platform == platform)))
    total = (await db.execute(select(func.count()).select_from(Person).where(*flt))).scalar_one()
    people = (await db.execute(
        select(Person).where(*flt).order_by(Person.id).limit(limit).offset(offset)
    )).scalars().all()
    idents = await _identities(db, [p.id for p in people])
    return [(p, idents.get(p.id, [])) for p in people], total


async def _identities(db: AsyncSession, person_ids: list[int]) -> dict[int, list[Identity]]:
    if not person_ids:
        return {}
    rows = (await db.execute(
        select(Identity).where(Identity.person_id.in_(person_ids)).order_by(Identity.id)
    )).scalars().all()
    out: dict[int, list[Identity]] = {}
    for i in rows:
        out.setdefault(i.person_id, []).append(i)
    return out


async def get_person(db: AsyncSession, person_id: int) -> tuple[Person, list[Identity]] | None:
    ensure_owner(db)
    person = await db.get(Person, person_id, populate_existing=True)
    if person is None:
        return None
    return person, (await _identities(db, [person_id])).get(person_id, [])


async def person_accounts(db: AsyncSession, person_id: int) -> list[dict]:
    """Per enabled account: does it know the person, hash, contact, mode, reach, messages."""
    ensure_owner(db)
    idents = (await _identities(db, [person_id])).get(person_id, [])
    accounts = (await db.execute(
        select(Account).where(Account.is_enabled.is_(True)).order_by(Account.id)
    )).scalars().all()
    out = []
    for acc in accounts:
        users = (await db.execute(
            select(User).where(User.account_id == acc.id, User.identity_id.in_([i.id for i in idents]))
        )).scalars().all() if idents else []
        by_identity = {u.identity_id: u for u in users}
        messages = (await db.execute(
            select(func.count()).select_from(Message).where(
                Message.account_id == acc.id, Message.sender_user_id.in_([u.id for u in users])
            )
        )).scalar_one() if users else 0
        reach = None
        if acc.mode == "rw":
            for ident in idents:
                reach = reach_of(acc.platform, ident, by_identity.get(ident.id))
                if reach:
                    break
        out.append({
            "account_id": acc.id,
            "alias": acc.alias,
            "platform": acc.platform,
            "mode": acc.mode,
            "knows": bool(users),
            "has_access_hash": any(u.access_hash is not None for u in users),
            "is_contact": any(u.is_contact for u in users),
            "reachable": reach.method if reach else None,
            "reach_note": reach.note if reach else None,
            "messages": messages,
        })
    return out


async def person_messages(
    db: AsyncSession,
    person_id: int,
    *,
    account: str | None = None,
    chat_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Messages the person sent, across accounts, with account and chat."""
    ensure_owner(db)
    flt = [Identity.person_id == person_id]
    if account:
        flt.append(Account.alias == account)
    if chat_id is not None:
        flt.append(Chat.external_id == chat_id)
    if date_from:
        flt.append(Message.sent_at >= date_from)
    if date_to:
        flt.append(Message.sent_at <= date_to)
    base = (
        select(Message, Chat, Account.alias)
        .join(User, and_(User.account_id == Message.account_id, User.id == Message.sender_user_id))
        .join(Identity, Identity.id == User.identity_id)
        .join(Chat, and_(Chat.account_id == Message.account_id, Chat.id == Message.chat_id))
        .join(Account, Account.id == Message.account_id)
        .where(*flt)
    )
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await db.execute(
        base.order_by(Message.sent_at.desc(), Message.id.desc()).limit(limit).offset(offset)
    )).all()
    return [
        {
            "account": alias,
            "chat_id": chat.external_id,
            "chat_title": chat.title,
            "message_id": m.external_id,
            "text": m.text,
            "message_type": m.message_type,
            "date": m.sent_at.isoformat() if m.sent_at else None,
            "is_edited": m.is_edited,
            "deleted_at": m.deleted_at.isoformat() if m.deleted_at else None,
        }
        for m, chat, alias in rows
    ], total


async def person_chats(db: AsyncSession, person_id: int) -> list[dict]:
    """Chats where the person wrote, per account, with message counts."""
    ensure_owner(db)
    rows = (await db.execute(
        select(Account.alias, Chat.external_id, Chat.title, Chat.chat_type, func.count(Message.id),
               func.max(Message.sent_at))
        .select_from(Message)
        .join(User, and_(User.account_id == Message.account_id, User.id == Message.sender_user_id))
        .join(Identity, Identity.id == User.identity_id)
        .join(Chat, and_(Chat.account_id == Message.account_id, Chat.id == Message.chat_id))
        .join(Account, Account.id == Message.account_id)
        .where(Identity.person_id == person_id)
        .group_by(Account.alias, Chat.external_id, Chat.title, Chat.chat_type)
        .order_by(func.max(Message.sent_at).desc())
    )).all()
    return [
        {"account": a, "chat_id": cid, "title": t, "chat_type": ct, "messages": n,
         "last_message_at": last.isoformat() if last else None}
        for a, cid, t, ct, n, last in rows
    ]


async def update_person(db: AsyncSession, person_id: int, **fields) -> Person | None:
    ensure_owner(db)
    allowed = {k: v for k, v in fields.items() if k in ("display_name", "tags", "notes") and v is not None}
    if allowed:
        await db.execute(update(Person).where(Person.id == person_id).values(**allowed))
    return await db.get(Person, person_id, populate_existing=True)


# ---------------------------------------------------------------------------
# Reachable people for one rw account (§6.4, §7.3)
# ---------------------------------------------------------------------------

def _reach_method(platform: str):
    """SQL twin of reach_of(): direct | username | phone | NULL (identities i, users u)."""
    if platform == "max":
        return literal("direct")
    return case(
        (User.access_hash.is_not(None), literal("direct")),
        (func.coalesce(Identity.username, "") != "", literal("username")),
        (Identity.phone.is_not(None), literal("phone")),
        else_=null(),
    )


async def reachable(
    db: AsyncSession, account: Account, *, q: str | None = None, limit: int = 50, offset: int = 0
) -> list[dict]:
    """People of the shared base this account can write to (best way per person). Contact fields only.

    Reachability, one row per person (DISTINCT ON, best method first) and the
    page are all computed in SQL (review R3-1).
    """
    ensure_scope(db, account.id)
    method = _reach_method(account.platform).label("method")
    flt = [Identity.platform == account.platform]
    if q:
        pattern = f"%{q}%"
        flt.append(or_(
            Identity.username.ilike(pattern), Identity.first_name.ilike(pattern),
            Identity.last_name.ilike(pattern), Identity.phone.ilike(pattern),
            Person.display_name.ilike(pattern),
        ))
    candidates = (
        select(
            Person.id.label("person_id"), Person.display_name.label("name"),
            Identity.id.label("identity_id"), Identity.username, Identity.phone,
            Identity.external_user_id, method,
        )
        .join(Person, Person.id == Identity.person_id)
        .outerjoin(User, and_(User.account_id == account.id, User.identity_id == Identity.id))
        .where(*flt)
        .subquery()
    )
    rank = case(
        (candidates.c.method == "direct", 0), (candidates.c.method == "username", 1), else_=2
    )
    best = (
        select(candidates)
        .where(candidates.c.method.is_not(None))
        .ext(distinct_on(candidates.c.person_id))
        .order_by(candidates.c.person_id, rank, candidates.c.identity_id)
        .subquery()
    )
    rows = (await db.execute(
        select(best).order_by(best.c.person_id).limit(limit).offset(offset)
    )).mappings().all()
    return [
        {
            "person_id": r["person_id"],
            "name": r["name"],
            "username": r["username"],
            "phone": r["phone"],
            "external_user_id": r["external_user_id"],
            "method": r["method"],
            "note": _PHONE_NOTE if r["method"] == "phone" else None,
        }
        for r in rows
    ]


async def reach_person(db: AsyncSession, account: Account, person_id: int) -> tuple[Identity, User | None, Reach] | None:
    """The best way for this account to reach the person (send_to_person)."""
    ensure_scope(db, account.id)
    rows = (await db.execute(
        select(Identity, User)
        .outerjoin(User, and_(User.account_id == account.id, User.identity_id == Identity.id))
        .where(Identity.person_id == person_id, Identity.platform == account.platform)
        .order_by(Identity.id)
    )).all()
    order = {"direct": 0, "username": 1, "phone": 2}
    best = None
    for ident, user in rows:
        reach = reach_of(account.platform, ident, user)
        if reach and (best is None or order[reach.method] < order[best[2].method]):
            best = (ident, user, reach)
    return best
