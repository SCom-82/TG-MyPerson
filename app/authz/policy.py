"""authorize() and audit() — one decision for HTTP and MCP (ТЗ 2026-10-13 §6.2).

The HTTP middleware (app/authz/middleware.py) and the MCP wrapper call the
same functions, so a tool is allowed or denied the same way on both
transports and both leave the same audit row.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from starlette.responses import JSONResponse

from app.authz.tool_catalog import WRITE_MESSENGER_TOOLS

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthContext:
    account_id: int | None
    alias: str | None
    platform: str = "telegram"
    scope: str = "account"  # 'account' | 'owner'


@dataclass(frozen=True)
class Decision:
    allowed: bool
    status: int = 200
    reason: str | None = None

    def response(self, ctx: AuthContext, tool_name: str) -> JSONResponse:
        if self.status == 403 and self.reason in ("denied by account policy", "read-only account"):
            # Body of the pre-refactor middleware, kept for clients and golden tests.
            return JSONResponse(status_code=403, content={
                "error": f"tool '{tool_name}' not allowed on read-only account '{ctx.alias}'",
                "tool": tool_name,
                "alias": ctx.alias,
                "mode": "ro",
                "reason": self.reason,
            })
        return JSONResponse(status_code=self.status, content={
            "error": f"tool '{tool_name}' not allowed for account '{ctx.alias}': {self.reason}",
            "tool": tool_name,
            "alias": ctx.alias,
            "reason": self.reason,
        })


ALLOW = Decision(True)


async def authorize(
    ctx: AuthContext,
    tool_name: str,
    *,
    chat_external_id: int | None = None,
    is_write: bool,
) -> Decision:
    """account_tool_policy (deny > allow) → mode ro → legacy write guards of messenger writes.

    Legacy guards on accounts, both optional (NULL = off): write_chat_ids (a list
    restricts the chats) and write_rate_per_hour (successful writes in audit_logs
    during the last hour). A chat that is unknown at decision time (HTTP body)
    skips the list check. MAX writes may go to any chat (owner's decision
    17.10); the per-account limits live in app/authz/write_limits.py and are
    applied inside the write routes.
    """
    from app.authz import middleware as mw  # patched by tests; cached lookups live there

    if not is_write or ctx.account_id is None:
        return ALLOW

    effect = await mw._get_tool_policy(ctx.account_id, tool_name)
    if effect == "deny":
        return Decision(False, 403, "denied by account policy")
    if effect != "allow" and await mw._get_account_mode(ctx.account_id) == "ro":
        return Decision(False, 403, "read-only account")

    if tool_name not in WRITE_MESSENGER_TOOLS:
        return ALLOW
    limits = await _write_limits(ctx.account_id)
    if limits is None:
        return ALLOW
    _, chat_ids, rate = limits
    if chat_external_id is not None and chat_ids is not None and chat_external_id not in chat_ids:
        return Decision(False, 403, f"chat {chat_external_id} is not in write_chat_ids")
    if rate is not None and await _writes_last_hour(ctx.account_id) >= rate:
        return Decision(False, 429, f"write rate limit {rate}/h reached")
    return ALLOW


async def _write_limits(account_id: int) -> tuple[str, list[int] | None, int | None] | None:
    from sqlalchemy import select

    from app.database import async_session
    from app.models import Account

    async with async_session() as db:
        row = (await db.execute(
            select(Account.platform, Account.write_chat_ids, Account.write_rate_per_hour)
            .where(Account.id == account_id)
        )).one_or_none()
    return tuple(row) if row is not None else None


async def _writes_last_hour(account_id: int) -> int:
    from sqlalchemy import func, select

    from app.database import async_session
    from app.models import AuditLog

    since = datetime.now(timezone.utc) - timedelta(hours=1)
    async with async_session() as db:
        return (await db.execute(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.account_id == account_id,
                AuditLog.is_write.is_(True),
                AuditLog.status == "ok",
                AuditLog.tool.in_(WRITE_MESSENGER_TOOLS),
                AuditLog.ts >= since,
            )
        )).scalar_one()


def audit(
    ctx: AuthContext,
    tool_name: str,
    *,
    is_write: bool,
    status: str,
    chat_id: int | None = None,
    target_user_id: int | None = None,
    params_digest: str | None = None,
    error: str | None = None,
) -> None:
    """One audit_logs row, written in the background (never blocks the call)."""
    from app.authz.middleware import _write_audit_log

    asyncio.create_task(_write_audit_log(
        alias=ctx.alias if ctx.scope != "owner" else "owner",
        account_id=ctx.account_id,
        tool=tool_name,
        is_write=is_write,
        chat_id=chat_id,
        target_user_id=target_user_id,
        params_digest=params_digest,
        status=status,
        error=error,
    ))
