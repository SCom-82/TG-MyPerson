import asyncio
import json
import logging

log = logging.getLogger(__name__)


class StreamManager:
    """Manages SSE subscribers for real-time message streaming."""

    def __init__(self):
        self._queues: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._queues.append(q)
        log.debug("SSE subscriber added (total=%d)", len(self._queues))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._queues:
            self._queues.remove(q)
        log.debug("SSE subscriber removed (total=%d)", len(self._queues))

    async def broadcast(self, data: dict) -> None:
        """Send data to all connected subscribers."""
        if not self._queues:
            return
        payload = json.dumps(data, ensure_ascii=False, default=str)
        dead = []
        for q in self._queues:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._queues.remove(q)
            log.warning("Dropped slow SSE subscriber")


stream_manager = StreamManager()
