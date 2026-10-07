from fastapi import APIRouter

from app.api.accounts import router as accounts_router
from app.api.auth import router as auth_router
from app.api.chats import router as chats_router
from app.api.contacts import router as contacts_router
from app.api.messages import router as messages_router
from app.api.search import router as search_router
from app.api.snapshots import router as snapshots_router
from app.api.users import router as users_router
from app.api.stream import router as stream_router
from app.api.sync import router as sync_router

# Leaf routers in inclusion order. Single source for both include_router below and
# the authz route table (app/authz/route_table.py) — a router missing here would
# be served but invisible to tool authz.
API_ROUTERS = (
    # Admin endpoints (X-Admin-Key auth, separate from user API)
    accounts_router,
    # User endpoints (X-API-Key + X-Session-Alias)
    auth_router,
    chats_router,
    contacts_router,
    messages_router,
    search_router,
    snapshots_router,
    users_router,
    stream_router,
    sync_router,
)

api_router = APIRouter()

for _router in API_ROUTERS:
    api_router.include_router(_router)
