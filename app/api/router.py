from fastapi import APIRouter

from app.api.auth import router as auth_router
from app.api.chats import router as chats_router
from app.api.messages import router as messages_router
from app.api.users import router as users_router
from app.api.stream import router as stream_router
from app.api.sync import router as sync_router

api_router = APIRouter()

api_router.include_router(auth_router)
api_router.include_router(chats_router)
api_router.include_router(messages_router)
api_router.include_router(users_router)
api_router.include_router(stream_router)
api_router.include_router(sync_router)
