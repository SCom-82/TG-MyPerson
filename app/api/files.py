"""Signed file links and uploads for MCP clients (ТЗ 2026-10-13 §9.2).

The token is the credential (no API key): the account and the message are
inside it, signed with FILE_LINK_SECRET, valid 15 minutes.
"""

import json
import time

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.database import async_session
from app.mcp import files
from app.models import Account

router = APIRouter(tags=["files"])


async def _account_state(account_id: int) -> dict:
    async with async_session() as db:
        row = (await db.execute(
            select(Account.alias, Account.platform).where(Account.id == account_id, Account.is_enabled)
        )).one_or_none()
    if row is None:
        raise HTTPException(status_code=403, detail="account disabled")
    return {"account_id": account_id, "session_alias": row.alias, "platform": row.platform, "scope": "account"}


@router.get("/files/{token}", name="download_file")
async def download_file(token: str, request: Request):
    """Same stream as GET /messages/{chat}/{msg}/media of the token's account."""
    from app.mcp import invoke

    try:
        data = files.verify(token, "file")
    except files.LinkError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    state = await _account_state(data["a"])
    for key, value in state.items():
        setattr(request.state, key, value)  # audit sees the account
    headers = {"range": request.headers["range"]} if "range" in request.headers else None
    query = {"index": data.get("i", 0)} if state["platform"] == "max" else None
    return invoke.forward(state["platform"], "GET", f"/messages/{data['c']}/{data['m']}/media",
                          state=state, headers=headers, query=query)


@router.put("/uploads/{token}", name="upload_file")
async def upload_file(token: str, request: Request, name: str | None = None):
    """Body = the file (curl -T). ≤ 50 MB, kept until the token expires."""
    try:
        data = files.verify(token, "upload")
    except files.LinkError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    await _account_state(data["a"])
    path = files.upload_path(data["a"], data["u"])
    path.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with open(path, "wb") as out:
        async for chunk in request.stream():
            size += len(chunk)
            if size > files.MAX_BYTES:
                out.close()
                path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="upload is larger than 50 MB")
            out.write(chunk)
    path.with_suffix(".json").write_text(json.dumps({
        "file_name": name or request.headers.get("x-file-name") or data["u"],
        "expires_at": data["x"], "size": size, "at": time.time(),
    }))
    files.purge_expired()
    return {"upload_id": data["u"], "size": size}
