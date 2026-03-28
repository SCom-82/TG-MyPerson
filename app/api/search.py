import logging

from fastapi import APIRouter, HTTPException, Query

from app.telegram.client import tg_bridge

log = logging.getLogger(__name__)
router = APIRouter(prefix="/search", tags=["search"])


@router.get("/global")
async def search_global(
    q: str = Query(..., description="Search query"),
    limit: int = Query(20, ge=1, le=100),
):
    client = tg_bridge.client
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=503, detail="Telegram client not authorized")

    try:
        from telethon.tl.functions.messages import SearchGlobalRequest
        from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty

        result = await client(SearchGlobalRequest(
            q=q,
            filter=InputMessagesFilterEmpty(),
            min_date=None,
            max_date=None,
            offset_rate=0,
            offset_peer=InputPeerEmpty(),
            offset_id=0,
            limit=limit,
        ))

        messages = []
        # Build a lookup for chats and users from the result
        chats_map = {}
        users_map = {}
        for c in getattr(result, 'chats', []):
            chats_map[c.id] = {
                "id": c.id,
                "title": getattr(c, 'title', None),
                "username": getattr(c, 'username', None),
            }
        for u in getattr(result, 'users', []):
            users_map[u.id] = {
                "id": u.id,
                "username": u.username,
                "first_name": u.first_name,
                "last_name": u.last_name,
            }

        for msg in getattr(result, 'messages', []):
            peer_id = None
            if hasattr(msg, 'peer_id'):
                peer = msg.peer_id
                if hasattr(peer, 'channel_id'):
                    peer_id = peer.channel_id
                elif hasattr(peer, 'chat_id'):
                    peer_id = peer.chat_id
                elif hasattr(peer, 'user_id'):
                    peer_id = peer.user_id

            messages.append({
                "message_id": msg.id,
                "text": msg.message,
                "date": msg.date.isoformat() if msg.date else None,
                "from_user_id": msg.from_id.user_id if msg.from_id and hasattr(msg.from_id, 'user_id') else None,
                "chat": chats_map.get(peer_id) or users_map.get(peer_id),
            })

        return {"items": messages, "total": len(messages)}
    except Exception as e:
        log.exception("Failed to search globally for: %s", q)
        raise HTTPException(status_code=500, detail=str(e))
