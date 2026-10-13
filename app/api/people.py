"""Contacts — owner key only (ТЗ 2026-10-13 §6.4). Cross-account: app/repo/people_queries.py."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.models import Identity, Person
from app.repo import people, people_queries
from app.storage import owner_db

router = APIRouter(prefix="/people", tags=["people"])


def _identity(i: Identity) -> dict:
    return {
        "id": i.id, "platform": i.platform, "external_user_id": i.external_user_id,
        "username": i.username, "first_name": i.first_name, "last_name": i.last_name,
        "phone": i.phone, "is_bot": i.is_bot, "person_pinned": i.person_pinned,
        "last_seen_at": i.last_seen_at.isoformat() if i.last_seen_at else None,
    }


def _person(p: Person, idents: list[Identity]) -> dict:
    return {
        "id": p.id, "display_name": p.display_name, "primary_phone": p.primary_phone,
        "tags": list(p.tags or []), "notes": p.notes,
        "identities": [_identity(i) for i in idents],
    }


@router.get("", name="find_person")
async def find_person(
    q: str | None = Query(None, description="name, username, phone or external id"),
    tag: str | None = Query(None),
    platform: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    async with owner_db() as db:
        rows, total = await people_queries.find_people(
            db, q=q, tag=tag, platform=platform, limit=limit, offset=offset
        )
        items = [_person(p, i) for p, i in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/{person_id}", name="get_person")
async def get_person(person_id: int):
    async with owner_db() as db:
        found = await people_queries.get_person(db, person_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Person not found")
        data = _person(*found)
        data["accounts"] = await people_queries.person_accounts(db, person_id)
    return data


@router.get("/{person_id}/messages", name="person_messages")
async def person_messages(
    person_id: int,
    account: str | None = Query(None, description="account alias"),
    chat_id: int | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    async with owner_db() as db:
        if await people_queries.get_person(db, person_id) is None:
            raise HTTPException(status_code=404, detail="Person not found")
        items, total = await people_queries.person_messages(
            db, person_id, account=account, chat_id=chat_id, date_from=date_from, date_to=date_to,
            limit=limit, offset=offset,
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/{person_id}/chats", name="person_chats")
async def person_chats(person_id: int):
    async with owner_db() as db:
        if await people_queries.get_person(db, person_id) is None:
            raise HTTPException(status_code=404, detail="Person not found")
        return {"items": await people_queries.person_chats(db, person_id)}


class PersonPatch(BaseModel):
    display_name: str | None = None
    tags: list[str] | None = None
    notes: str | None = None


@router.patch("/{person_id}", name="update_person")
async def update_person(person_id: int, body: PersonPatch):
    async with owner_db() as db:
        person = await people_queries.update_person(db, person_id, **body.model_dump())
        if person is None:
            raise HTTPException(status_code=404, detail="Person not found")
        await db.commit()
        found = await people_queries.get_person(db, person_id)
        return _person(*found)


class MergeRequest(BaseModel):
    from_id: int
    to_id: int
    reason: str | None = None


@router.post("/merge", name="merge_people")
async def merge_people(body: MergeRequest):
    async with owner_db() as db:
        try:
            await people.merge_people(db, body.from_id, body.to_id, reason=body.reason)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await db.commit()
        return _person(*(await people_queries.get_person(db, body.to_id)))


class SplitRequest(BaseModel):
    identity_id: int
    reason: str | None = None


@router.post("/split", name="split_person")
async def split_person(body: SplitRequest):
    async with owner_db() as db:
        try:
            new_id = await people.split_identity(db, body.identity_id, reason=body.reason)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        await db.commit()
        return _person(*(await people_queries.get_person(db, new_id)))
