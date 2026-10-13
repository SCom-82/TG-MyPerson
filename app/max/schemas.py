"""MAX-only additions to request/response schemas. Everything else reuses app.schemas."""

from datetime import datetime
from typing import ClassVar

from pydantic import Field

from app.schemas import BackfillRequest, MessageResponse


class MaxMessageResponse(MessageResponse):
    """MessageResponse + deleted_at (ADR §2.G), always present in MAX responses."""

    deleted_at: datetime | None = None
    always_deleted_at: ClassVar[bool] = True


class MaxBackfillRequest(BackfillRequest):
    """BackfillRequest + days: limit a backward backfill by age (API spec §3).

    A subclass rather than a field on the shared schema: the TG endpoint and its
    OpenAPI stay exactly as they are.
    """

    days: int | None = Field(default=None, ge=1, le=3650)
