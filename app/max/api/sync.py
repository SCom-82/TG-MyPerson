"""MAX sync_status (API spec §3). trigger_backfill / sync_chats — PR-5."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.max import repo

router = APIRouter(prefix="/sync", tags=["max-sync"])


def is_backfill_running(chat_id: int) -> bool:
    return False  # MAX backfill arrives in PR-5


@router.get("/status", name="sync_status")
async def sync_status(db: AsyncSession = Depends(get_db)):
    states = []
    for state, title in await repo.get_sync_states(db):
        states.append({
            "chat_id": state.chat_id,
            "chat_title": title,
            "oldest_message_id": state.oldest_message_id,
            "newest_message_id": state.newest_message_id,
            "is_fully_synced": state.is_fully_synced,
            "total_messages_synced": state.total_messages_synced,
            "last_backfill_at": state.last_backfill_at,
            "is_running": is_backfill_running(state.chat_id),
            # MAX-only: time cursors (history is paged by time)
            "oldest_time_ms": state.oldest_time_ms,
            "newest_time_ms": state.newest_time_ms,
            "last_catchup_at": state.last_catchup_at,
        })
    return {"states": states}
