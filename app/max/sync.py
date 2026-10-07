"""MAX history: chat list sync, backfill, gap catch-up (ADR §2.J, API spec §3).

History in MAX is paged by TIME (fetch_history(from_time=ms, forward=N,
backward=N)), so cursors are max_sync_state.{oldest,newest}_time_ms.
newest_time_ms means "everything up to here, without gaps": history (catch-up,
backfill) moves it; a live event only in a chat caught up in the current
connection (session.caught_up, filled here — ADR §2.J as fixed 07.10).

Human pace over speed: PAGE_PAUSE_S between history requests, pages of 100,
at most MAX_CATCHUP_MAX_CHATS chats per catch-up pass; the rest goes to the
next pass (every CATCHUP_TICK_S while a backlog remains). Nothing here ever
marks messages read: history requests go with interactive=False and no
read/presence calls are made (watchdog test).

Backfill tasks live in their own dict — never in the TG _backfill_tasks.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app import database
from app.max import normalize, repo
from app.models import MaxChat, MaxSyncState

if TYPE_CHECKING:
    from app.max.session import MaxSession

log = logging.getLogger(__name__)

PAGE_SIZE = 100
PAGE_PAUSE_S = 1.5
CATCHUP_TICK_S = 900.0
MAX_CHAT_PAGES = 50  # fetch_chats pages per sync — guard against a non-advancing marker

# Module attribute so tests can skip the human-pace pauses.
_sleep = asyncio.sleep

# chat_id → running backfill (MAX only; TG keeps its own dict)
_backfill_tasks: dict[int, asyncio.Task] = {}


def _page(remaining: int, has_cursor: bool) -> int:
    """Page size. With a cursor the message AT the cursor comes back too (from_time is
    inclusive), so ask for one more — otherwise a page of 1 at the tail would look
    like the end of history."""
    return min(PAGE_SIZE, remaining + (1 if has_cursor else 0))


def is_backfill_running(chat_id: int) -> bool:
    task = _backfill_tasks.get(chat_id)
    return task is not None and not task.done()


class SessionUnavailable(Exception):
    def __init__(self, state: str) -> None:
        super().__init__(state)
        self.state = state


class MaxSync:
    def __init__(self, session: "MaxSession") -> None:
        self.session = session

    def install(self) -> None:
        self.session.authorized_hooks.append(self.on_authorized)

    def _client(self) -> Any:
        client = self.session.client
        if client is None:
            raise SessionUnavailable(self.session.state)
        return client

    async def _store(self, msg: Any, chat_id: int) -> bool:
        """Upsert one history message; errors are isolated per message (eff5f41)."""
        try:
            # History is contiguous from the cursor: it may move newest_time_ms.
            await self.session.ingest.store_message(msg, chat_id=chat_id, advance_cursor=True)
            return True
        except Exception:  # noqa: BLE001
            log.exception("max[%s]: failed to store message %s in chat %s", self.session.alias,
                          getattr(msg, "id", None), chat_id)
            return False

    async def _history(self, client: Any, chat_id: int, **kwargs: Any) -> list:
        return await client.fetch_history(chat_id, interactive=False, **kwargs)

    # -- chat list ------------------------------------------------------------

    async def sync_chats(self) -> int:
        """All chats via fetch_chats, paginated by the time marker. Returns the count."""
        client = self._client()
        seen: set[int] = set()
        marker: int | None = None
        for page in range(MAX_CHAT_PAGES):
            if page:
                await _sleep(PAGE_PAUSE_S)
            chats = await client.fetch_chats(marker=marker)
            fresh = [c for c in chats if c.id not in seen]
            if not fresh:
                break
            async with database.async_session() as db:
                for chat in fresh:
                    await repo.upsert_chat(db, normalize.normalize_chat(chat))
                await db.commit()
            seen.update(c.id for c in fresh)
            times = [c.last_event_time for c in chats if c.last_event_time]
            next_marker = min(times) if times else None
            if next_marker is None or (marker is not None and next_marker >= marker):
                break
            marker = next_marker
        return len(seen)

    # -- backfill -------------------------------------------------------------

    async def start_backfill(self, chat_id: int, limit: int, direction: str, days: int | None) -> dict:
        if is_backfill_running(chat_id):
            return {"status": "already_running", "chat_id": chat_id}
        self._client()  # 503 now rather than inside the task
        if direction == "forward":
            state = await self._state(chat_id)
            if state is None or state.newest_time_ms is None:
                return {"status": "error", "chat_id": chat_id,
                        "detail": "no newest_time_ms for this chat yet: run a backward backfill first"}
        task = asyncio.create_task(self._run_backfill(chat_id, limit, direction, days))
        _backfill_tasks[chat_id] = task
        task.add_done_callback(lambda t: _backfill_tasks.get(chat_id) is t and _backfill_tasks.pop(chat_id))
        return {"status": "started", "chat_id": chat_id, "limit": limit, "direction": direction, "days": days}

    async def _state(self, chat_id: int) -> MaxSyncState | None:
        async with database.async_session() as db:
            return (await db.execute(select(MaxSyncState).where(MaxSyncState.chat_id == chat_id))).scalar_one_or_none()

    async def _run_backfill(self, chat_id: int, limit: int, direction: str, days: int | None) -> int:
        log.info("max[%s]: backfill chat %s limit=%s direction=%s days=%s",
                 self.session.alias, chat_id, limit, direction, days)
        try:
            if direction == "forward":
                stored = await self._forward(chat_id, limit)
            else:
                stored = await self._backward(chat_id, limit, days)
            log.info("max[%s]: backfill chat %s done, %s message(s)", self.session.alias, chat_id, stored)
            return stored
        except Exception:  # noqa: BLE001
            log.exception("max[%s]: backfill chat %s failed", self.session.alias, chat_id)
            return 0

    async def _backward(self, chat_id: int, limit: int, days: int | None) -> int:
        client = self._client()
        state = await self._state(chat_id)
        cursor = state.oldest_time_ms if state and state.oldest_time_ms else None  # None = now
        stop_ms = (
            normalize.dt_to_ms(datetime.now(timezone.utc) - timedelta(days=days)) if days else None
        )
        stored = 0
        fully_synced = False
        first = True
        while stored < limit:
            if not first:
                await _sleep(PAGE_PAUSE_S)
            first = False
            page = await self._history(
                client, chat_id, from_time=cursor, backward=_page(limit - stored, cursor is not None), forward=0
            )
            older = [m for m in page if cursor is None or m.time < cursor]
            if not older:
                fully_synced = True
                break
            in_range = sorted(
                (m for m in older if stop_ms is None or m.time >= stop_ms), key=lambda m: m.time, reverse=True
            )[: limit - stored]
            if not in_range:
                break  # crossed the `days` boundary
            page_stored = 0
            for msg in in_range:
                page_stored += await self._store(msg, chat_id)
            stored += page_stored
            oldest = in_range[-1]
            cursor = oldest.time
            await self._move_oldest(chat_id, oldest.id, oldest.time, stored_delta=page_stored)
            if len(in_range) < len(older):
                break  # the page reached past `days`
        await self._finish_backfill(chat_id, fully_synced)
        return stored

    async def _forward(self, chat_id: int, limit: int) -> int:
        """From newest_time_ms forward: what the chat got while we were not looking."""
        client = self._client()
        state = await self._state(chat_id)
        cursor = state.newest_time_ms
        seen_at_cursor = {state.newest_message_id}
        stored = 0
        first = True
        while stored < limit:
            if not first:
                await _sleep(PAGE_PAUSE_S)
            first = False
            page = await self._history(
                client, chat_id, from_time=cursor, forward=_page(limit - stored, True), backward=0
            )
            newer = [m for m in page if m.time > cursor or (m.time == cursor and m.id not in seen_at_cursor)]
            if not newer:
                break
            for msg in sorted(newer, key=lambda m: m.time)[: limit - stored]:
                stored += await self._store(msg, chat_id)
            newest_time = max(m.time for m in newer)
            seen_at_cursor = {m.id for m in newer if m.time == newest_time}
            cursor = newest_time
        await self._finish_backfill(chat_id, None)
        return stored

    async def _move_oldest(self, chat_id: int, message_id: int, time_ms: int, stored_delta: int) -> None:
        async with database.async_session() as db:
            state = (await db.execute(select(MaxSyncState).where(MaxSyncState.chat_id == chat_id))).scalar_one()
            if state.oldest_time_ms is None or time_ms < state.oldest_time_ms:
                state.oldest_time_ms = time_ms
                state.oldest_message_id = message_id
            state.total_messages_synced = (state.total_messages_synced or 0) + stored_delta
            await db.commit()

    async def _finish_backfill(self, chat_id: int, fully_synced: bool | None) -> None:
        async with database.async_session() as db:
            state = (await db.execute(select(MaxSyncState).where(MaxSyncState.chat_id == chat_id))).scalar_one_or_none()
            if state is None:
                return
            state.last_backfill_at = datetime.now(timezone.utc)
            if fully_synced is not None:
                state.is_fully_synced = fully_synced
            await db.commit()

    # -- gap catch-up (ADR §2.J) ------------------------------------------------

    async def on_authorized(self, client: Any) -> None:
        """Every (re)connect: catch-up runs in the background, never blocks the supervisor."""
        self.session.spawn_background("catchup", self.catchup_loop())

    async def catchup_loop(self) -> None:
        while True:
            try:
                backlog = await self.catch_up()
            except SessionUnavailable:
                return
            except Exception:  # noqa: BLE001
                log.exception("max[%s]: catch-up pass failed", self.session.alias)
                backlog = 0
            if not backlog:
                return
            await _sleep(CATCHUP_TICK_S)

    async def catch_up(self) -> int:
        """One pass. Returns how many chats are left for the next pass."""
        settings = self.session.settings
        try:
            await self.sync_chats()
        except SessionUnavailable:
            raise
        except Exception:  # noqa: BLE001 — a failed chat list must not stop the gap fill
            log.warning("max[%s]: sync_chats during catch-up failed", self.session.alias, exc_info=True)

        candidates, no_tail = await self._catchup_candidates()
        self.session.caught_up.update(no_tail)
        todo, rest = candidates[: settings.catchup_max_chats], candidates[settings.catchup_max_chats:]
        client = self._client()
        first = True
        for chat_id, state in todo:
            if not first:
                await _sleep(PAGE_PAUSE_S)
            first = False
            try:
                if state is None:
                    # First time we see this chat: a seed, not the full history.
                    page = await self._history(client, chat_id, backward=settings.catchup_seed, forward=0)
                    for msg in page:
                        await self._store(msg, chat_id)
                    if page:
                        oldest = min(page, key=lambda m: m.time)
                        await self._move_oldest(chat_id, oldest.id, oldest.time, stored_delta=len(page))
                else:
                    await self._forward(chat_id, limit=10_000)
                await self._mark_catchup(chat_id)
                # From now on live events in this chat may move its cursor (ADR §2.J).
                self.session.caught_up.add(chat_id)
            except SessionUnavailable:
                raise
            except Exception:  # noqa: BLE001
                log.exception("max[%s]: catch-up of chat %s failed", self.session.alias, chat_id)

        self.session.last_catchup_at = datetime.now(timezone.utc)
        self.session.catchup_backlog_chats = len(rest)
        return len(rest)

    async def _catchup_candidates(self) -> tuple[list[tuple[int, MaxSyncState | None]], set[int]]:
        """(chats with a tail or never synced — freshest first, chats with no tail).

        All chats: MAX keeps every chat (brief requirement), is_monitored does not
        gate the gap fill. A chat without messages has no tail either.
        """
        async with database.async_session() as db:
            rows = (
                await db.execute(
                    select(MaxChat, MaxSyncState)
                    .join(MaxSyncState, MaxSyncState.chat_id == MaxChat.id, isouter=True)
                    .order_by(MaxChat.last_message_at.desc().nullslast())
                )
            ).all()
        candidates, no_tail = [], set()
        for chat, state in rows:
            last_ms = normalize.dt_to_ms(chat.last_message_at)
            if last_ms is None:
                no_tail.add(chat.id)
            elif state is None or state.newest_time_ms is None or last_ms > state.newest_time_ms:
                candidates.append((chat.id, state))
            else:
                no_tail.add(chat.id)
        return candidates, no_tail

    async def _mark_catchup(self, chat_id: int) -> None:
        async with database.async_session() as db:
            state = (await db.execute(select(MaxSyncState).where(MaxSyncState.chat_id == chat_id))).scalar_one_or_none()
            if state is not None:
                state.last_catchup_at = datetime.now(timezone.utc)
                await db.commit()
