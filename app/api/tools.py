"""The MCP tools the caller's account may use (QA D-3).

The same list as list_tools of /mcp for the account's key, for the stdio
wrapper (_system/scripts/tg-myperson-mcp.py): it shows only these tools, so
a MAX account does not see tools that answer 501 and a ro account does not
see writes. Mounted in the Telegram and in the MAX router.
"""

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.database import async_session
from app.models import Account

router = APIRouter(tags=["tools"])


@router.get("/tools", name="available_tools")
async def available_tools(request: Request):
    from app.mcp.tools import Caller, visible

    account_id = getattr(request.state, "account_id", None)
    async with async_session() as db:
        mode = (await db.execute(select(Account.mode).where(Account.id == account_id))).scalar_one_or_none()
    alias = getattr(request.state, "session_alias", None)
    platform = getattr(request.state, "platform", "telegram")
    caller = Caller("account", account_id, alias, platform, mode or "ro", "")
    return {
        "alias": alias,
        "platform": platform,
        "mode": mode,
        "tools": [t.name for t in visible(caller)],
    }
