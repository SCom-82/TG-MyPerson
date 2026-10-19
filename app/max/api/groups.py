"""MAX groups (plan 2026-10-19 Р3 §3 PR-G2): /api/v1/groups/..., MAX-only routes.

Writes are rw only (WRITE_MESSENGER_TOOLS → 403 for ro in the middleware),
within the account's write limits: create_group takes 1 group + 1 invite per
initial member, add_group_members 1 invite per person, the rest are actions.
The target is checked before the limits and before MAX: the account knows the
chat and it is a group. Every change that MAX answered is stored at once
(chats.raw participants/admins/owner/link, my_role, members_count); a failed
store is logged, the answer stays 200 — the change is made in MAX.
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from app.authz.write_limits import ACTION, GROUP_CREATE, GROUP_INVITE, bundle_slot, write_slot
from app.max import groups, normalize
from app.max.api.common import authorized_or_error, call_max, call_max_read, store_sent
from app.max.session import MaxSession
from app.max.store_new import NewMaxStore
from app.repo import chats
from app.services.tg_store import new_mode
from app.storage import account_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/groups", tags=["max-groups"])

MAX_IDS = 20
UNCERTAIN = "the change may have been applied, check get_chat / get_chat_members"
_IDS = Field(min_length=1, max_length=MAX_IDS)


class CreateGroupRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    user_ids: list[int] = Field(default_factory=list, max_length=MAX_IDS)
    notify: bool = True


class MembersRequest(BaseModel):
    user_ids: list[int] = _IDS
    show_history: bool = True


class UserIdsRequest(BaseModel):
    user_ids: list[int] = _IDS


class AdminRequest(BaseModel):
    user_id: int
    permissions: list[str] | None = Field(default=None, description=f"of {sorted(groups.PERMISSIONS)}")


class RemoveAdminRequest(BaseModel):
    user_id: int


class ProfileRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _one(self):
        if self.title is None and self.description is None:
            raise ValueError("pass title and/or description")
        return self


class SettingsRequest(BaseModel):
    all_can_pin_message: bool | None = None
    only_owner_can_change_icon_title: bool | None = None
    only_admin_can_add_member: bool | None = None
    only_admin_can_call: bool | None = None
    members_can_see_private_link: bool | None = None

    @model_validator(mode="after")
    def _one(self):
        if not self.model_dump(exclude_none=True):
            raise ValueError("pass at least one setting")
        return self


class DeleteGroupRequest(BaseModel):
    confirm_title: str
    for_all: bool = True


# ---------------------------------------------------------------------------
# Shared by the group routes and the MAX chats routes (members, leave, rights)
# ---------------------------------------------------------------------------

async def connected(request: Request) -> MaxSession:
    session = await authorized_or_error(request)
    if isinstance(session, JSONResponse):
        raise HTTPException(status_code=session.status_code, detail=session.body.decode())
    return session


def me_of(session: MaxSession) -> int | None:
    return getattr(getattr(getattr(session.client, "me", None), "contact", None), "id", None)


async def known_chat(request: Request, chat_id: int, *, kinds: tuple[str, ...] = ("group",)) -> Any:
    """The account's chat row, checked before limits and MAX: unknown → 404, another type → 422."""
    account_id = getattr(request.state, "account_id", None)
    async with account_db(account_id) as db:
        chat = await chats.get_chat(db, account_id, chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    if chat.chat_type not in kinds:
        raise HTTPException(status_code=422, detail=f"chat {chat_id} is a {chat.chat_type}, not a "
                                                    f"{' or '.join(kinds)}")
    return chat


def not_me(session: MaxSession, user_ids: list[int]) -> None:
    me = me_of(session)
    if me is not None and me in user_ids:
        raise HTTPException(status_code=422, detail="cannot change own role (the account itself is in user_ids)")


async def store_chat(session: MaxSession, chat: Any) -> None:
    if chat is None or not new_mode():
        return
    try:
        await NewMaxStore(session.account_id).upsert_chats([normalize.normalize_chat(chat, me_of(session))])
    except Exception:  # noqa: BLE001 — the change is made in MAX, the answer stays 200
        log.warning("max[%s]: group %s not stored", session.alias, getattr(chat, "id", None), exc_info=True)


async def refresh(session: MaxSession, chat_id: int) -> Any:
    """After a change MAX answered without the Chat: the fresh one, stored (None if that read fails)."""
    try:
        chat = await groups.fresh_chat(session.client, chat_id)
    except Exception:  # noqa: BLE001
        log.warning("max[%s]: group %s not re-read after a change", session.alias, chat_id, exc_info=True)
        return None
    await store_chat(session, chat)
    return chat


async def store_members(session: MaxSession, users: list[Any]) -> None:
    if not users or not new_mode():
        return
    try:
        await NewMaxStore(session.account_id).upsert_users([normalize.normalize_user(u) for u in users], "member")
    except Exception:  # noqa: BLE001
        log.warning("max[%s]: members not stored", session.alias, exc_info=True)


def summary(chat: Any, me: int | None, **extra) -> dict:
    if chat is None:
        return extra
    return {
        "chat_id": chat.id,
        "title": chat.title,
        "description": chat.description,
        "link": chat.link,
        "members_count": chat.participants_count or len(chat.participants or {}) or None,
        "my_role": normalize.my_role(chat, me),
        "owner": chat.owner,
        "admins": list(chat.admins or []),
        "participants": sorted(int(p) for p in (chat.participants or {})),
        **extra,
    }


def _target(request: Request, user_ids: list[int]) -> None:
    request.state.audit_target_user_id = user_ids[0] if user_ids else None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("", name="create_group")
async def create_group(req: CreateGroupRequest, request: Request):
    session = await connected(request)
    not_me(session, req.user_ids)
    _target(request, req.user_ids)
    ids = list(dict.fromkeys(req.user_ids))
    async with bundle_slot(request, "create_group", {GROUP_CREATE: 1, GROUP_INVITE: len(ids)}):
        result = await call_max(groups.create(session.client, req.title, ids, req.notify), UNCERTAIN)
        if result is None:
            raise HTTPException(status_code=502, detail="MAX answered without the new group")
    chat, msg = result
    await store_chat(session, chat)
    await store_sent(session, msg, chat.id)
    return summary(chat, me_of(session), message_id=getattr(msg, "id", None))


@router.post("/{chat_id}/members", name="add_group_members")
async def add_group_members(chat_id: int, req: MembersRequest, request: Request):
    await known_chat(request, chat_id)
    session = await connected(request)
    not_me(session, req.user_ids)
    _target(request, req.user_ids)
    ids = list(dict.fromkeys(req.user_ids))
    async with bundle_slot(request, "add_group_members", {GROUP_INVITE: len(ids)}, chat_id):
        chat = await call_max(groups.invite(session.client, chat_id, ids, req.show_history), UNCERTAIN)
    if chat is not None:
        await store_chat(session, chat)
    else:
        chat = await refresh(session, chat_id)
    return summary(chat, me_of(session), chat_id=chat_id, added=ids)


@router.post("/{chat_id}/members/remove", name="remove_group_members")
async def remove_group_members(chat_id: int, req: UserIdsRequest, request: Request):
    await known_chat(request, chat_id)
    session = await connected(request)
    not_me(session, req.user_ids)
    _target(request, req.user_ids)
    async with write_slot(request, ACTION, "remove_group_members", chat_id):
        await call_max(groups.remove(session.client, chat_id, req.user_ids), UNCERTAIN)
    chat = await refresh(session, chat_id)
    return summary(chat, me_of(session), chat_id=chat_id, removed=req.user_ids)


@router.post("/{chat_id}/admins", name="set_group_admin")
async def set_group_admin(chat_id: int, req: AdminRequest, request: Request):
    await known_chat(request, chat_id)
    try:
        names = groups.permission_names(groups.permission_mask(req.permissions))
    except groups.UnknownPermission as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session = await connected(request)
    not_me(session, [req.user_id])
    _target(request, [req.user_id])
    async with write_slot(request, ACTION, "set_group_admin", chat_id):
        await call_max(groups.set_admin(session.client, chat_id, req.user_id, req.permissions), UNCERTAIN)
    chat = await refresh(session, chat_id)
    return summary(chat, me_of(session), chat_id=chat_id, user_id=req.user_id, permissions=names)


@router.post("/{chat_id}/admins/remove", name="remove_group_admin")
async def remove_group_admin(chat_id: int, req: RemoveAdminRequest, request: Request):
    await known_chat(request, chat_id)
    session = await connected(request)
    not_me(session, [req.user_id])
    _target(request, [req.user_id])
    async with write_slot(request, ACTION, "remove_group_admin", chat_id):
        await call_max(groups.remove_admin(session.client, chat_id, req.user_id), UNCERTAIN)
    chat = await refresh(session, chat_id)
    return summary(chat, me_of(session), chat_id=chat_id, user_id=req.user_id)


@router.patch("/{chat_id}", name="update_group_profile")
async def update_group_profile(chat_id: int, req: ProfileRequest, request: Request):
    await known_chat(request, chat_id)
    session = await connected(request)
    async with write_slot(request, ACTION, "update_group_profile", chat_id):
        await call_max(groups.profile(session.client, chat_id, req.title, req.description), UNCERTAIN)
    chat = await refresh(session, chat_id)
    return summary(chat, me_of(session), chat_id=chat_id)


@router.patch("/{chat_id}/settings", name="update_group_settings")
async def update_group_settings(chat_id: int, req: SettingsRequest, request: Request):
    await known_chat(request, chat_id)
    session = await connected(request)
    options = req.model_dump(exclude_none=True)
    async with write_slot(request, ACTION, "update_group_settings", chat_id):
        await call_max(groups.settings(session.client, chat_id, options), UNCERTAIN)
    chat = await refresh(session, chat_id)
    return {"chat_id": chat_id, "changed": options, "options": getattr(chat, "options", None)}


@router.get("/{chat_id}/invite-link", name="get_invite_link")
async def get_invite_link(chat_id: int, request: Request):
    await known_chat(request, chat_id)
    session = await connected(request)
    chat = await call_max_read(groups.fresh_chat(session.client, chat_id))
    await store_chat(session, chat)
    out = {"chat_id": chat_id, "link": chat.link}
    if not chat.link:
        out["hint"] = "the group has no invite link: reset_invite_link creates one"
    return out


@router.post("/{chat_id}/invite-link", name="reset_invite_link")
async def reset_invite_link(chat_id: int, request: Request):
    await known_chat(request, chat_id)
    session = await connected(request)
    async with write_slot(request, ACTION, "reset_invite_link", chat_id):
        chat = await call_max(groups.reset_link(session.client, chat_id), UNCERTAIN)
    await store_chat(session, chat)
    return {"chat_id": chat_id, "link": getattr(chat, "link", None),
            "warning": "the previous link is revoked: links already sent no longer work"}


@router.get("/{chat_id}/join-requests", name="list_join_requests")
async def list_join_requests(chat_id: int, request: Request, limit: int = Query(100, ge=1, le=100)):
    await known_chat(request, chat_id)
    session = await connected(request)
    found = await call_max_read(groups.join_requests(session.client, chat_id, limit))
    users = [m.contact for m in found if getattr(m, "contact", None) is not None]
    await store_members(session, users)
    items = [_person(u) for u in users]
    return {"items": items, "total": len(items)}


@router.post("/{chat_id}/join-requests/approve", name="approve_join_requests")
async def approve_join_requests(chat_id: int, req: MembersRequest, request: Request):
    await known_chat(request, chat_id)
    session = await connected(request)
    _target(request, req.user_ids)
    async with write_slot(request, ACTION, "approve_join_requests", chat_id):
        chat = await call_max(groups.approve(session.client, chat_id, req.user_ids, req.show_history), UNCERTAIN)
    chat = await _stored_or_refreshed(session, chat_id, chat)
    return summary(chat, me_of(session), chat_id=chat_id, approved=req.user_ids)


@router.post("/{chat_id}/join-requests/decline", name="decline_join_requests")
async def decline_join_requests(chat_id: int, req: UserIdsRequest, request: Request):
    await known_chat(request, chat_id)
    session = await connected(request)
    _target(request, req.user_ids)
    async with write_slot(request, ACTION, "decline_join_requests", chat_id):
        chat = await call_max(groups.decline(session.client, chat_id, req.user_ids), UNCERTAIN)
    chat = await _stored_or_refreshed(session, chat_id, chat)
    return summary(chat, me_of(session), chat_id=chat_id, declined=req.user_ids)


@router.post("/{chat_id}/delete", name="delete_group")
async def delete_group(chat_id: int, req: DeleteGroupRequest, request: Request):
    """Only the account's own group, confirmed by its current title (Р-26). Local rows stay as history."""
    await known_chat(request, chat_id)
    session = await connected(request)
    chat = await call_max_read(groups.fresh_chat(session.client, chat_id))
    if chat.owner != me_of(session):
        raise HTTPException(status_code=403, detail="owner only: the account did not create this group")
    if req.confirm_title != chat.title:
        raise HTTPException(status_code=409, detail=f"confirm_title does not match the group's current title "
                                                    f"{chat.title!r}")
    async with write_slot(request, ACTION, "delete_group", chat_id):
        await call_max(groups.delete(session.client, chat_id, req.for_all), UNCERTAIN)
    await unmonitor(session, chat_id)
    return {"chat_id": chat_id, "deleted": True, "for_all": req.for_all}


async def _stored_or_refreshed(session: MaxSession, chat_id: int, chat: Any) -> Any:
    if chat is not None:
        await store_chat(session, chat)
        return chat
    return await refresh(session, chat_id)


async def unmonitor(session: MaxSession, chat_id: int) -> None:
    try:
        async with account_db(session.account_id) as db:
            await chats.set_monitored(db, session.account_id, chat_id, False)
            await db.commit()
    except Exception:  # noqa: BLE001
        log.warning("max[%s]: chat %s not unmonitored", session.alias, chat_id, exc_info=True)


def _person(user: Any) -> dict:
    row = normalize.normalize_user(user)
    return {"user_id": row["id"], "first_name": row["first_name"], "last_name": row["last_name"],
            "username": row["username"]}
