"""MessengerSession — contract of a messenger adapter session (ADR §2.C).

Implemented by MaxSession. TelegramSession is intentionally not adapted to it
(refactoring working code without benefit, ADR §6); the protocol is the point
where the two may converge if a third platform appears. Methods for history,
media and sending are added by the PRs that implement them (PR-4…PR-7).
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MessengerSession(Protocol):
    alias: str

    async def start(self) -> None:
        """Start the supervisor if a stored session exists; no-op otherwise."""
        ...

    async def stop(self) -> None:
        ...

    async def auth_status(self) -> dict[str, Any]:
        """Shape of app.schemas.AuthStatusResponse."""
        ...

    async def me(self) -> dict[str, Any]:
        """Shape of app.schemas.AuthMeResponse. Raises if not authorized."""
        ...

    def runtime(self) -> dict[str, Any]:
        """runtime block of GET /accounts (API spec §1.3)."""
        ...
