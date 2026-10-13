import asyncio
import json
import logging

log = logging.getLogger(__name__)


class StreamManager:
    """Manages SSE subscribers for real-time message streaming.

    Account scoping (ТЗ 2026-10-13 §5.5): a subscriber gets an event only when
    both belong to the same account. legacy mode subscribes and broadcasts
    without an account (None == None: everybody, as before); new mode always
    passes one, so an account never sees another account's events and TG/MAX
    no longer need separate managers.
    """

    def __init__(self):
        self._queues: list[asyncio.Queue] = []
        self._accounts: dict[int, int | None] = {}  # id(queue) → account

    def subscribe(self, account_id: int | None = None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._queues.append(q)
        self._accounts[id(q)] = account_id
        log.debug("SSE subscriber added (total=%d)", len(self._queues))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._queues:
            self._queues.remove(q)
        self._accounts.pop(id(q), None)
        log.debug("SSE subscriber removed (total=%d)", len(self._queues))

    async def broadcast(self, data: dict, account_id: int | None = None) -> None:
        """Send data to the subscribers of account_id."""
        if not self._queues:
            return
        payload = json.dumps(data, ensure_ascii=False, default=str)
        dead = []
        for q in self._queues:
            if self._accounts.get(id(q)) != account_id:
                continue
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.unsubscribe(q)
            log.warning("Dropped slow SSE subscriber")


stream_manager = StreamManager()
