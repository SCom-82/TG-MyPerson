"""Snapshots endpoints — chat member snapshots and registry.

Authentication: X-API-Key + X-Session-Alias (standard, not admin).
Authz: snapshot_chat_members and snapshot_import_members are WRITE;
       list_chat_snapshots and get_snapshot_members are READ.
"""

import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from telethon.errors import FloodWaitError
from sqlalchemy import select, func, desc
from sqlalchemy.exc import SQLAlchemyError

from app.database import async_session
from app.models import (
    Account,
    ChatAccess,
    ChatMemberRecord,
    ChatMembersSnapshot,
    TgChat,
)
from app.services import registry_service
from app.telegram.pool import pool

router = APIRouter(prefix="/snapshots", tags=["snapshots"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _require_account_id(alias: str) -> int:
    """Resolve alias to account_id; raise 404 if not found/disabled."""
    async with async_session() as db:
        result = await db.execute(
            select(Account.id).where(
                Account.alias == alias,
                Account.is_enabled == True,  # noqa: E712
            )
        )
        account_id = result.scalar_one_or_none()
    if account_id is None:
        raise HTTPException(
            status_code=404,
            detail=f"Session alias '{alias}' not registered or disabled",
        )
    return account_id


async def _ensure_tg_chat(db, chat_id: int) -> None:
    """Ensure chat exists in tg_chats; insert stub if not."""
    result = await db.execute(select(TgChat.id).where(TgChat.id == chat_id))
    if result.scalar_one_or_none() is None:
        stub = TgChat(
            id=chat_id,
            chat_type="unknown",
            title=None,
            is_monitored=False,
        )
        db.add(stub)
        await db.flush()


def _participant_to_dict(participant) -> dict:
    """Extract member data from a Telethon participant object."""
    from telethon.tl.types import (
        ChannelParticipantAdmin,
        ChannelParticipantCreator,
        ChannelParticipantBanned,
    )

    user = getattr(participant, "user", participant)
    role = "member"
    if isinstance(participant, ChannelParticipantCreator):
        role = "creator"
    elif isinstance(participant, ChannelParticipantAdmin):
        role = "admin"
    elif isinstance(participant, ChannelParticipantBanned):
        role = "banned"

    tg_user_id = getattr(user, "id", None)
    username = getattr(user, "username", None)
    first_name = getattr(user, "first_name", None)
    last_name = getattr(user, "last_name", None)
    phone = getattr(user, "phone", None)

    return {
        "tg_user_id": tg_user_id,
        "username": username,
        "first_name": first_name,
        "last_name": last_name,
        "phone": phone,
        "role": role,
    }


# ---------------------------------------------------------------------------
# POST /snapshots/chat/{chat_id}
# ---------------------------------------------------------------------------

@router.post("/chat/{chat_id}", status_code=201, name="snapshot_chat_members")
async def snapshot_chat(
    chat_id: int,
    request: Request,
    note: str | None = Query(default=None),
) -> dict:
    """Take a snapshot of chat members via Telegram API.

    Requires an authorized session. Returns 503 if session not authorized.
    """
    alias = getattr(request.state, "session_alias", "work")
    account_id = await _require_account_id(alias)

    # Get Telegram client
    session = await pool.get(alias)
    if not session.client:
        raise HTTPException(
            status_code=503,
            detail=f"Session '{alias}' not authorized — use /auth/login?session={alias}",
        )
    try:
        is_auth = await session.client.is_user_authorized()
    except Exception:
        is_auth = False
    if not is_auth:
        raise HTTPException(
            status_code=503,
            detail=f"Session '{alias}' not authorized — use /auth/login?session={alias}",
        )

    # Collect participants from Telegram
    t0 = time.monotonic()
    members_raw: list[dict] = []
    try:
        async for participant in session.client.iter_participants(chat_id, aggressive=True):
            members_raw.append(_participant_to_dict(participant))
    except FloodWaitError as exc:
        raise HTTPException(
            status_code=429,
            detail=f"Telegram flood wait: retry after {exc.seconds}s",
            headers={"Retry-After": str(exc.seconds)},
        )
    except Exception as exc:
        err_str = str(exc).lower()
        if "chat not found" in err_str or "invalid" in err_str:
            raise HTTPException(status_code=404, detail=f"Chat {chat_id} not found: {exc}")
        if "forbidden" in err_str or "not allowed" in err_str or "admin" in err_str:
            raise HTTPException(status_code=403, detail=f"Access denied to chat {chat_id}: {exc}")
        raise HTTPException(status_code=503, detail=f"Telegram error: {exc}")

    took_ms = int((time.monotonic() - t0) * 1000)
    members_count = len(members_raw)
    now = datetime.now(timezone.utc)

    async with async_session() as db:
        # Ensure chat exists in tg_chats
        await _ensure_tg_chat(db, chat_id)

        # Create snapshot record
        snapshot = ChatMembersSnapshot(
            chat_id=chat_id,
            account_id=account_id,
            taken_at=now,
            members_count=members_count,
            source="api",
            note=note,
        )
        db.add(snapshot)
        await db.flush()  # get snapshot.id
        snapshot_id = snapshot.id

        # Bulk insert member records
        for m in members_raw:
            record = ChatMemberRecord(
                snapshot_id=snapshot_id,
                tg_user_id=m.get("tg_user_id"),
                username=m.get("username"),
                first_name=m.get("first_name"),
                last_name=m.get("last_name"),
                phone=m.get("phone"),
                role=m.get("role"),
                raw=m,
            )
            db.add(record)

        # Upsert into users_registry + registry_sources
        await registry_service.bulk_upsert_from_snapshot(
            session=db,
            snapshot_id=snapshot_id,
            members=members_raw,
            account_id=account_id,
        )

        # Upsert chat_access
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        ca_values = {
            "account_id": account_id,
            "chat_id": chat_id,
            "last_seen_at": now,
            "role": "member",
        }
        ca_stmt = (
            pg_insert(ChatAccess)
            .values(**ca_values, first_seen_at=now)
            .on_conflict_do_update(
                index_elements=["account_id", "chat_id"],
                set_={"last_seen_at": now},
            )
        )
        await db.execute(ca_stmt)

        await db.commit()

    return {
        "snapshot_id": snapshot_id,
        "members_count": members_count,
        "took_ms": took_ms,
        "chat_id": chat_id,
        "account_id": account_id,
    }


# ---------------------------------------------------------------------------
# POST /snapshots/import  — skeleton (manual import not in scope per ADR note)
# ---------------------------------------------------------------------------

@router.post("/import", status_code=501, name="snapshot_import_members")
async def import_snapshot() -> dict:
    """Manual import of member data. Not implemented in Phase 3 scope.

    TODO Phase 4+: accept multipart/form-data with JSON array or CSV.
    """
    raise HTTPException(
        status_code=501,
        detail="Manual import not implemented yet (Phase 4 scope)",
    )


# ---------------------------------------------------------------------------
# GET /snapshots/chat/{chat_id}
# ---------------------------------------------------------------------------

@router.get("/chat/{chat_id}", name="list_chat_snapshots")
async def list_snapshots(
    chat_id: int,
    limit: int = Query(default=50, le=500),
) -> list[dict]:
    """List snapshots for a chat, newest first."""
    async with async_session() as db:
        stmt = (
            select(ChatMembersSnapshot, Account.alias)
            .outerjoin(Account, ChatMembersSnapshot.account_id == Account.id)
            .where(ChatMembersSnapshot.chat_id == chat_id)
            .order_by(desc(ChatMembersSnapshot.taken_at))
            .limit(limit)
        )
        result = await db.execute(stmt)
        rows = result.all()

    return [
        {
            "snapshot_id": snap.id,
            "taken_at": snap.taken_at.isoformat() if snap.taken_at else None,
            "members_count": snap.members_count,
            "source": snap.source,
            "note": snap.note,
            "account_alias": alias,
        }
        for snap, alias in rows
    ]


# ---------------------------------------------------------------------------
# GET /snapshots/{snapshot_id}/members
# ---------------------------------------------------------------------------

@router.get("/{snapshot_id}/members", name="get_snapshot_members")
async def get_snapshot_members(
    snapshot_id: int,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, le=1000),
) -> dict:
    """Get paginated member records for a snapshot."""
    async with async_session() as db:
        # Verify snapshot exists
        snap_result = await db.execute(
            select(ChatMembersSnapshot.id).where(ChatMembersSnapshot.id == snapshot_id)
        )
        if snap_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail=f"Snapshot {snapshot_id} not found")

        # Total count
        count_result = await db.execute(
            select(func.count(ChatMemberRecord.id)).where(
                ChatMemberRecord.snapshot_id == snapshot_id
            )
        )
        total = count_result.scalar_one()

        # Paginated records
        records_result = await db.execute(
            select(ChatMemberRecord)
            .where(ChatMemberRecord.snapshot_id == snapshot_id)
            .order_by(ChatMemberRecord.id)
            .offset(offset)
            .limit(limit)
        )
        records = records_result.scalars().all()

    return {
        "snapshot_id": snapshot_id,
        "total": total,
        "offset": offset,
        "limit": limit,
        "members": [
            {
                "id": r.id,
                "tg_user_id": r.tg_user_id,
                "username": r.username,
                "first_name": r.first_name,
                "last_name": r.last_name,
                "phone": r.phone,
                "role": r.role,
            }
            for r in records
        ],
    }
