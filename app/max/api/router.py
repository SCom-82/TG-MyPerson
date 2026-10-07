"""Internal router for platform 'max', mounted at /api/v1/_max.

Requests reach it only through the platform_dispatch middleware, which rewrites
the path of a MAX alias' request (ADR §2.B). Routes added here must keep the
Telegram path and route `name=`, so tool_authz and audit resolve the same tool.

MAX_API_ROUTERS lists the leaf routers: the single source for include_router
below and for the authz route table (app/authz/route_table.py), like API_ROUTERS.
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from app.authz.middleware import _not_supported
from app.max.api.auth import router as auth_router
from app.max.api.chats import router as chats_router
from app.max.api.contacts import router as contacts_router
from app.max.api.messages import router as messages_router
from app.max.api.search import router as search_router
from app.max.api.stream import router as stream_router
from app.max.api.sync import router as sync_router
from app.max.api.users import router as users_router


async def require_max_rewrite(request: Request) -> None:
    """Defense in depth: the middleware already 404s external /_max requests."""
    if not getattr(request.state, "max_rewritten", False):
        raise HTTPException(status_code=404, detail="Not Found")


unsupported_router = APIRouter()


@unsupported_router.api_route(
    "/_unsupported",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    name="max_unsupported",
)
async def max_unsupported(request: Request):
    tool_name = getattr(request.state, "forced_tool_name", None) or "unknown"
    return _not_supported(request, tool_name, "max")


MAX_API_ROUTERS = (
    auth_router,
    chats_router,
    contacts_router,
    messages_router,
    search_router,
    users_router,
    stream_router,
    sync_router,
    unsupported_router,
)

max_api_router = APIRouter(dependencies=[Depends(require_max_rewrite)])
for _router in MAX_API_ROUTERS:
    max_api_router.include_router(_router)
