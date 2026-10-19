"""MAX-only additions to request/response schemas. Everything else reuses app.schemas."""

import logging
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from app.max import frames, normalize
from app.schemas import BackfillRequest, ChatResponse, MediaResponse, MessageResponse

log = logging.getLogger(__name__)


class MaxMediaResponse(MediaResponse):
    """+ attach_index: the number GET …/media?index= and MCP download_media take (plan Р-9)."""

    attach_index: int | None = None


class MaxForward(BaseModel):
    """The original of a forwarded message: from_user of the message is who forwarded it."""

    from_user_id: int | None = None
    from_user_name: str | None = None
    chat_id: int | None = None  # 0: MAX hides the source (a dialog, a private chat)
    chat_name: str | None = None
    message_id: int | None = None
    date: datetime | None = None


class MaxContactCard(BaseModel):
    user_id: int | None = None
    name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    forwarded: bool = False


class MaxPollAnswer(BaseModel):
    answer_id: int | None = None
    text: str
    vote_count: int = 0


class MaxPoll(BaseModel):
    """A poll of the message (plan Р4 Р-44): poll_id and answer_id are what vote_poll takes;
    the counts are as of the stored copy, vote_poll answers the fresh ones."""

    poll_id: int
    title: str
    answers: list[MaxPollAnswer]
    total: int = 0
    anonymous: bool = False
    multiselect: bool = False
    revote: bool = False
    closed: bool = False
    quiz: bool = False


def _parsed(raw: dict | None) -> Any:
    if not raw:
        return None
    try:
        return normalize.parse_stored(raw)
    except Exception as exc:  # noqa: BLE001 — extras are best effort, the row answers anyway
        log.debug("max: stored raw does not parse for the response extras: %s", exc)
        return None


def _name(user: Any) -> str | None:
    if user is None:
        return None
    return " ".join(p for p in (user.first_name, user.last_name) if p).strip() or user.username


class MaxMessageResponse(MessageResponse):
    """MessageResponse + deleted_at (ADR §2.G), always present in MAX responses; from_user filled;
    forward, contacts and poll read from the stored raw — old rows too, without a repair (plan Р-8, Р-9, Р4 Р-44)."""

    deleted_at: datetime | None = None
    forward: MaxForward | None = None
    contacts: list[MaxContactCard] = Field(default_factory=list)
    media: list[MaxMediaResponse] = Field(default_factory=list)
    poll: MaxPoll | None = None
    always_deleted_at: ClassVar[bool] = True
    fill_from_user: ClassVar[bool] = True
    media_cls: ClassVar[type[MediaResponse]] = MaxMediaResponse

    @classmethod
    def raw_user_ids(cls, raw: dict | None) -> set[int]:
        msg = _parsed(raw)
        if msg is None:
            return set()
        ids = {c["user_id"] for c in normalize.contact_cards(msg) if c["user_id"] is not None}
        link = normalize.forward_link(msg)
        if link is not None and link.message.sender is not None:
            ids.add(link.message.sender)
        return ids

    @classmethod
    def raw_extras(cls, raw: dict | None, people: dict) -> dict:
        msg = _parsed(raw)
        if msg is None:
            return {}
        out: dict = {}
        link = normalize.forward_link(msg)
        if link is not None:
            sender = link.message.sender
            out["forward"] = MaxForward(
                from_user_id=sender,
                from_user_name=_name(people.get(sender)) if sender is not None else None,
                chat_id=link.chat_id,
                chat_name=link.chat_name,
                message_id=link.message.id,
                date=normalize.ms_to_dt(link.message.time),
            )
        cards = normalize.contact_cards(msg, lambda uid: getattr(people.get(uid), "phone", None))
        if cards:
            out["contacts"] = [MaxContactCard(**c) for c in cards]
        poll = frames.find_poll(msg)
        if poll is not None:
            out["poll"] = MaxPoll(**frames.poll_view(poll))
        return out


class MaxPinned(BaseModel):
    message_id: int
    from_user_id: int | None = None
    text: str | None = None
    date: datetime | None = None


class MaxChatResponse(ChatResponse):
    """ChatResponse + the pinned message as the stored chat has it (raw.pinnedMessage, Р4.1)."""

    pinned_message: MaxPinned | None = None

    @staticmethod
    def pinned(raw: dict | None) -> MaxPinned | None:
        p = (raw or {}).get("pinnedMessage")
        if not isinstance(p, dict) or p.get("id") is None:
            return None
        return MaxPinned(message_id=int(p["id"]), from_user_id=p.get("sender"), text=p.get("text") or None,
                         date=normalize.ms_to_dt(p.get("time")))


class MaxBackfillRequest(BackfillRequest):
    """BackfillRequest + days: limit a backward backfill by age (API spec §3).

    A subclass rather than a field on the shared schema: the TG endpoint and its
    OpenAPI stay exactly as they are.
    """

    days: int | None = Field(default=None, ge=1, le=3650)
