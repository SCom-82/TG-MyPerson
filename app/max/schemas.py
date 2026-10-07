"""MAX-only response additions. Everything else reuses app.schemas as is."""

from datetime import datetime

from app.schemas import MessageResponse


class MaxMessageResponse(MessageResponse):
    """MessageResponse + deleted_at (ADR §2.G). Only MAX responses carry it."""

    deleted_at: datetime | None = None
