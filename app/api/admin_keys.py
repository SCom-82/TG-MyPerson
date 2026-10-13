"""API keys (ТЗ 2026-10-13 §6.1) — admin endpoints, X-Admin-Key.

A key is returned once, at creation; listings never show it.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.authz import keys
from app.database import async_session
from app.models import Account

router = APIRouter(tags=["admin-keys"])


class KeyCreate(BaseModel):
    label: str | None = None


@router.post("/accounts/{account_id}/keys", status_code=201, name="admin_create_account_key")
async def create_account_key(account_id: int, body: KeyCreate | None = None) -> dict:
    async with async_session() as db:
        if (await db.execute(select(Account.id).where(Account.id == account_id))).scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail=f"Account {account_id} not found")
    row, key = await keys.create_key(scope="account", account_id=account_id, label=(body or KeyCreate()).label)
    return {**keys.key_to_dict(row), "key": key}


@router.get("/accounts/{account_id}/keys", name="admin_list_account_keys")
async def list_account_keys(account_id: int) -> list[dict]:
    return [keys.key_to_dict(k) for k in await keys.list_keys(account_id=account_id)]


@router.post("/owner-keys", status_code=201, name="admin_create_owner_key")
async def create_owner_key(body: KeyCreate | None = None) -> dict:
    row, key = await keys.create_key(scope="owner", account_id=None, label=(body or KeyCreate()).label)
    return {**keys.key_to_dict(row), "key": key}


@router.get("/owner-keys", name="admin_list_owner_keys")
async def list_owner_keys() -> list[dict]:
    return [keys.key_to_dict(k) for k in await keys.list_keys(scope="owner")]


@router.delete("/keys/{key_id}", name="admin_revoke_key")
async def revoke_key(key_id: int) -> dict:
    if not await keys.revoke_key(key_id):
        raise HTTPException(status_code=404, detail=f"Key {key_id} not found or already revoked")
    return {"status": "revoked", "id": key_id}
