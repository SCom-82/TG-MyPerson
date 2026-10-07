"""Internal router for platform 'max', mounted at /api/v1/_max.

Requests reach it only through the platform_dispatch middleware, which rewrites
the path of a MAX alias' request (ADR §2.B). Routes added here must keep the
Telegram path and route `name=`, so tool_authz and audit resolve the same tool.

PR-2: only the catch-all for tools not implemented for MAX (→ 501).
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from app.authz.middleware import _not_supported


async def require_max_rewrite(request: Request) -> None:
    """Defense in depth: the middleware already 404s external /_max requests."""
    if not getattr(request.state, "max_rewritten", False):
        raise HTTPException(status_code=404, detail="Not Found")


max_api_router = APIRouter(dependencies=[Depends(require_max_rewrite)])


@max_api_router.api_route(
    "/_unsupported",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    name="max_unsupported",
)
async def max_unsupported(request: Request):
    tool_name = getattr(request.state, "forced_tool_name", None) or "unknown"
    return _not_supported(request, tool_name, "max")
