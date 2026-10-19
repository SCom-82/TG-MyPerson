"""MAX chat folders (plan 2026-10-19 Р4 Р-41): /api/v1/folders, MAX-only.

Folders are what Sergey sees in the app. update_folder reads the folder first and
merges: PyMax's update_folder sends None as [] and would empty a folder whose
title alone is changed. delete_folder is confirmed by the folder's current title.
chat_ids: chats the account knows (≤ 100). Writes are actions of the limits.
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from app.authz.write_limits import ACTION, write_slot
from app.max.api.common import call_max, call_max_read
from app.max.api.groups import connected
from app.repo import chats as chats_repo
from app.services.tg_store import new_mode
from app.storage import account_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/folders", tags=["max-folders"])

MAX_CHATS = 100
UNCERTAIN = "the folder may have been changed, check list_folders"
_CHAT_IDS = Field(default=None, max_length=MAX_CHATS)


class CreateFolderRequest(BaseModel):
    title: str = Field(min_length=1, max_length=64)
    chat_ids: list[int] = Field(default_factory=list, max_length=MAX_CHATS)


class UpdateFolderRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=64)
    chat_ids: list[int] | None = _CHAT_IDS

    @model_validator(mode="after")
    def _one(self):
        if self.title is None and self.chat_ids is None:
            raise ValueError("pass title and/or chat_ids")
        return self


class DeleteFolderRequest(BaseModel):
    confirm_title: str


def _view(folder: Any) -> dict:
    return {"folder_id": folder.id, "title": folder.title, "chat_ids": list(folder.include or []),
            "filters": list(folder.filters or []), "options": list(folder.options or [])}


async def _known_chats(request: Request, chat_ids: list[int]) -> list[int]:
    """Only chats the account knows (422 otherwise), duplicates dropped."""
    ids = list(dict.fromkeys(chat_ids))
    if not ids or not new_mode():
        return ids
    account_id = getattr(request.state, "account_id", None)
    async with account_db(account_id) as db:
        known = set(await chats_repo.statuses(db, account_id, ids))
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise HTTPException(status_code=422, detail=f"chats {unknown} are not chats of this account")
    return ids


async def _folder(session, folder_id: str) -> Any:
    found = await call_max_read(session.client.get_folders())
    for folder in found.folders or []:
        if folder.id == folder_id:
            return folder
    raise HTTPException(status_code=404, detail="Folder not found")


@router.get("", name="list_folders")
async def list_folders(request: Request):
    session = await connected(request)
    found = await call_max_read(session.client.get_folders())
    return {"items": [_view(f) for f in found.folders or []], "order": list(found.folders_order or [])}


@router.post("", name="create_folder")
async def create_folder(req: CreateFolderRequest, request: Request):
    ids = await _known_chats(request, req.chat_ids)
    session = await connected(request)
    async with write_slot(request, ACTION, "create_folder"):
        update = await call_max(session.client.create_folder(req.title, ids), UNCERTAIN)
    if update.folder is None:
        return {"folder_id": None, "title": req.title, "chat_ids": ids, "filters": [], "options": [],
                "warning": "MAX answered without the folder: list_folders shows it"}
    return _view(update.folder)


@router.patch("/{folder_id}", name="update_folder")
async def update_folder(folder_id: str, req: UpdateFolderRequest, request: Request):
    ids = await _known_chats(request, req.chat_ids) if req.chat_ids is not None else None
    session = await connected(request)
    current = await _folder(session, folder_id)
    title = req.title if req.title is not None else current.title
    include = ids if ids is not None else list(current.include or [])
    async with write_slot(request, ACTION, "update_folder"):
        update = await call_max(session.client.update_folder(
            folder_id, title, chat_include=include, filters=list(current.filters or []),
            options=list(current.options or []),
        ), UNCERTAIN)
    if update.folder is not None:
        return _view(update.folder)
    return {"folder_id": folder_id, "title": title, "chat_ids": include, "filters": list(current.filters or []),
            "options": list(current.options or [])}


@router.post("/{folder_id}/delete", name="delete_folder")
async def delete_folder(folder_id: str, req: DeleteFolderRequest, request: Request):
    """The chats stay; only the folder goes."""
    session = await connected(request)
    current = await _folder(session, folder_id)
    if req.confirm_title != current.title:
        raise HTTPException(status_code=409, detail=f"confirm_title does not match the folder's current title "
                                                    f"{current.title!r}")
    async with write_slot(request, ACTION, "delete_folder"):
        await call_max(session.client.delete_folder(folder_id), UNCERTAIN)
    return {"folder_id": folder_id, "deleted": True}
