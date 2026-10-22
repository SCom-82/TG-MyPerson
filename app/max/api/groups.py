"""MAX groups (plan 2026-10-19 Р3 §3 PR-G2): /api/v1/groups/..., MAX-only routes.

Channels (plan Р4 Р-40) use the same routes: a channel in MAX is the same Chat
with the same frames. Inviting to a channel counts in group_invites_per_day
like a group. Release 5 (plan 2026-10-22 Р-52…Р-63): admin rights are the MAX
client's 12 bits with its defaults (a bot 123 / a person 251 in a group, 1850 /
4026 in a channel); create_group makes a channel too (chat_type), delete_group
deletes a channel only while the account is its one member; extra invite
links and the ownership transfer are here.

Writes are rw only (WRITE_MESSENGER_TOOLS → 403 for ro in the middleware),
within the account's write limits: create_group takes 1 group + 1 invite per
initial member, add_group_members 1 invite per person, the rest are actions.
The target is checked before the limits and before MAX: the account knows the
chat and it is a group. Every change that MAX answered is stored at once
(chats.raw participants/admins/owner/link, my_role, members_count); a failed
store is logged, the answer stays 200 — the change is made in MAX.
"""

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from starlette.convertors import Convertor, register_url_convertor

from app.authz.write_limits import ACTION, GROUP_CREATE, GROUP_INVITE, bundle_slot, write_slot
from app.max import groups, normalize
from app.max.api.common import authorized_or_error, call_max, call_max_read, store_sent
from app.max.session import MaxSession
from app.max.store_new import NewMaxStore
from app.repo import chats
from app.services.tg_store import new_mode
from app.storage import account_db

log = logging.getLogger(__name__)

class SignedInt(Convertor):
    """{chat_id:sint}: group ids are negative; a non-numeric id does not match the route,
    so a Telegram alias gets the plain 404 of an unknown path, not 501 (QA-R3 N1)."""

    regex = "-?[0-9]+"

    def convert(self, value: str) -> int:
        return int(value)

    def to_string(self, value: int) -> str:
        return str(value)


register_url_convertor("sint", SignedInt())

router = APIRouter(prefix="/groups", tags=["max-groups"])

MAX_IDS = 20
GROUP_OR_CHANNEL = ("group", "channel")
UNCERTAIN = "the change may have been applied, check get_chat / get_chat_members"
_IDS = Field(min_length=1, max_length=MAX_IDS)


class CreateGroupRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    user_ids: list[int] = Field(default_factory=list, max_length=MAX_IDS)
    notify: bool = True
    chat_type: Literal["group", "channel"] = "group"


class MembersRequest(BaseModel):
    user_ids: list[int] = _IDS
    show_history: bool = True


class AddMembersRequest(MembersRequest):
    start_payload: str | None = Field(default=None, min_length=1, max_length=512,
                                      description="a bot: the payload of its /start (Р-58)")


class UserIdsRequest(BaseModel):
    user_ids: list[int] = _IDS


class RemoveMembersRequest(UserIdsRequest):
    delete_messages: bool = False


class AdminRequest(BaseModel):
    user_id: int
    permissions: list[str] | None = Field(default=None, description=f"of {list(groups.PERMISSIONS)}")
    permissions_mask: int | None = Field(default=None, ge=1, le=groups.FULL_MASK)


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
    join_request: bool | None = None
    disable_forward: bool | None = None
    comments: bool | None = None  # a channel only (Р-59)

    @model_validator(mode="after")
    def _one(self):
        if not self.model_dump(exclude_none=True):
            raise ValueError("pass at least one setting")
        return self


class DeleteGroupRequest(BaseModel):
    confirm_title: str
    for_all: bool = True


class TransferRequest(BaseModel):
    user_id: int
    confirm_title: str
    confirm_user_id: int


class InviteLinkRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=64)


class RevokeLinksRequest(BaseModel):
    link_ids: list[int] = Field(min_length=1, max_length=MAX_IDS)


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
        result = await call_max(groups.create(session.client, req.title, ids, req.notify, req.chat_type), UNCERTAIN)
        if result is None:
            raise _maybe_created()
    if req.chat_type == "channel":
        # our frame: parsed outside the slot, the slots of a frame that reached MAX stay (R17-1)
        try:
            result = groups.parse_created(result)
        except Exception:  # noqa: BLE001
            log.warning("max[%s]: the answer of a channel creation does not parse", session.alias, exc_info=True)
            result = None
        if result is None:
            raise _maybe_created()
    chat, msg = result
    await store_chat(session, chat)
    await store_sent(session, msg, chat.id)
    return summary(chat, me_of(session), message_id=getattr(msg, "id", None), chat_type=req.chat_type)


def _maybe_created() -> HTTPException:
    """MAX accepted the frame but sent no chat back: the group (and its invites) is most likely
    made — the slots stay, a retry must not create a second one unchecked (review 15 R15-1)."""
    error = HTTPException(status_code=502, detail="MAX answered without the new group; it may have "
                                                  "been created, check list_chats before retrying")
    error.keep_write_slot = True
    return error


@router.post("/{chat_id:sint}/members", name="add_group_members")
async def add_group_members(chat_id: int, req: AddMembersRequest, request: Request):
    """A bot is added like a person (Р-58); start_payload goes in the frame as the /start payload."""
    chat_row = await known_chat(request, chat_id, kinds=GROUP_OR_CHANNEL)
    session = await connected(request)
    not_me(session, req.user_ids)
    _target(request, req.user_ids)
    ids = list(dict.fromkeys(req.user_ids))
    async with bundle_slot(request, "add_group_members", {GROUP_INVITE: len(ids)}, chat_id):
        chat = await call_max(groups.invite(session.client, chat_id, ids, req.show_history, chat_row.chat_type,
                                            req.start_payload), UNCERTAIN)
    if chat is not None:
        await store_chat(session, chat)
    else:
        chat = await refresh(session, chat_id)
    return summary(chat, me_of(session), chat_id=chat_id, added=ids)


@router.post("/{chat_id:sint}/members/remove", name="remove_group_members")
async def remove_group_members(chat_id: int, req: RemoveMembersRequest, request: Request):
    """delete_messages=true removes the members' messages too (cleanMsgPeriod -1, Р-60): MAX names
    them in its answer, they are marked deleted here."""
    await known_chat(request, chat_id, kinds=GROUP_OR_CHANNEL)
    session = await connected(request)
    not_me(session, req.user_ids)
    _target(request, req.user_ids)
    async with write_slot(request, ACTION, "remove_group_members", chat_id):
        deleted = await call_max(groups.remove(session.client, chat_id, req.user_ids, clean=req.delete_messages),
                                 UNCERTAIN)
    if deleted and session.ingest is not None:
        try:
            await session.ingest.apply_delete(chat_id, deleted)
        except Exception:  # noqa: BLE001 — deleted in MAX, the delete frames mark them anyway
            log.warning("max[%s]: deleted messages of chat %s not marked", session.alias, chat_id, exc_info=True)
    chat = await refresh(session, chat_id)
    return summary(chat, me_of(session), chat_id=chat_id, removed=req.user_ids, deleted_message_ids=deleted)


@router.post("/{chat_id:sint}/admins", name="set_group_admin")
async def set_group_admin(chat_id: int, req: AdminRequest, request: Request):
    """Rights as the MAX client gives them (Р-52…Р-57): names or an exact mask, else the client's
    default for a bot / a person; the client's dependencies → 422. Again on an admin — the mask is
    replaced (previous_mask). Everything is checked before the slot and the frame."""
    chat_row = await known_chat(request, chat_id, kinds=GROUP_OR_CHANNEL)
    channel = chat_row.chat_type == "channel"
    rights = lambda bot: groups.resolve_mask(req.permissions, req.permissions_mask,  # noqa: E731
                                             channel=channel, bot=bot)
    try:
        if req.permissions or req.permissions_mask is not None:
            rights(False)  # the shape is wrong whoever the target is: no MAX read
    except groups.UnknownPermission as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session = await connected(request)
    not_me(session, [req.user_id])
    _target(request, [req.user_id])
    bot = await call_max_read(groups.is_bot(session.client, req.user_id))
    try:
        mask = rights(bot)
    except groups.UnknownPermission as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    before = await call_max_read(groups.fresh_chat(session.client, chat_id))
    previous = groups.admin_mask(before, req.user_id) if req.user_id in (before.admins or []) else None
    async with write_slot(request, ACTION, "set_group_admin", chat_id):
        await call_max(groups.set_admin(session.client, chat_id, req.user_id, mask), UNCERTAIN)
    chat = await refresh(session, chat_id)
    return summary(chat, me_of(session), chat_id=chat_id, user_id=req.user_id,
                   permissions=groups.permission_names(mask, channel=channel), mask=mask, previous_mask=previous,
                   is_bot=bot)


@router.post("/{chat_id:sint}/admins/remove", name="remove_group_admin")
async def remove_group_admin(chat_id: int, req: RemoveAdminRequest, request: Request):
    await known_chat(request, chat_id, kinds=GROUP_OR_CHANNEL)
    session = await connected(request)
    not_me(session, [req.user_id])
    _target(request, [req.user_id])
    async with write_slot(request, ACTION, "remove_group_admin", chat_id):
        await call_max(groups.remove_admin(session.client, chat_id, req.user_id), UNCERTAIN)
    chat = await refresh(session, chat_id)
    return summary(chat, me_of(session), chat_id=chat_id, user_id=req.user_id)


@router.patch("/{chat_id:sint}", name="update_group_profile")
async def update_group_profile(chat_id: int, req: ProfileRequest, request: Request):
    await known_chat(request, chat_id, kinds=GROUP_OR_CHANNEL)
    session = await connected(request)
    async with write_slot(request, ACTION, "update_group_profile", chat_id):
        await call_max(groups.profile(session.client, chat_id, req.title, req.description), UNCERTAIN)
    chat = await refresh(session, chat_id)
    return summary(chat, me_of(session), chat_id=chat_id)


@router.patch("/{chat_id:sint}/settings", name="update_group_settings")
async def update_group_settings(chat_id: int, req: SettingsRequest, request: Request):
    chat_row = await known_chat(request, chat_id, kinds=GROUP_OR_CHANNEL)
    if req.comments is not None and chat_row.chat_type != "channel":
        raise HTTPException(status_code=422, detail="comments is a channel setting, not for a group")
    session = await connected(request)
    options = req.model_dump(exclude_none=True)
    async with write_slot(request, ACTION, "update_group_settings", chat_id):
        await call_max(groups.settings(session.client, chat_id, options), UNCERTAIN)
    chat = await refresh(session, chat_id)
    return {"chat_id": chat_id, "changed": options, "options": getattr(chat, "options", None)}


@router.get("/{chat_id:sint}/invite-link", name="get_invite_link")
async def get_invite_link(chat_id: int, request: Request):
    await known_chat(request, chat_id, kinds=GROUP_OR_CHANNEL)
    session = await connected(request)
    chat = await call_max_read(groups.fresh_chat(session.client, chat_id))
    await store_chat(session, chat)
    out = {"chat_id": chat_id, "link": chat.link}
    if not chat.link:
        out["hint"] = "the group has no invite link: reset_invite_link creates one"
    return out


@router.post("/{chat_id:sint}/invite-link", name="reset_invite_link")
async def reset_invite_link(chat_id: int, request: Request):
    await known_chat(request, chat_id, kinds=GROUP_OR_CHANNEL)
    session = await connected(request)
    async with write_slot(request, ACTION, "reset_invite_link", chat_id):
        chat = await call_max(groups.reset_link(session.client, chat_id), UNCERTAIN)
    await store_chat(session, chat)
    return {"chat_id": chat_id, "link": getattr(chat, "link", None),
            "warning": "the previous link is revoked: links already sent no longer work"}


@router.get("/{chat_id:sint}/join-requests", name="list_join_requests")
async def list_join_requests(chat_id: int, request: Request, limit: int = Query(100, ge=1, le=100)):
    await known_chat(request, chat_id, kinds=GROUP_OR_CHANNEL)
    session = await connected(request)
    found = await call_max_read(groups.join_requests(session.client, chat_id, limit))
    users = [m.contact for m in found if getattr(m, "contact", None) is not None]
    await store_members(session, users)
    items = [_person(u) for u in users]
    return {"items": items, "total": len(items)}


@router.post("/{chat_id:sint}/join-requests/approve", name="approve_join_requests")
async def approve_join_requests(chat_id: int, req: MembersRequest, request: Request):
    await known_chat(request, chat_id, kinds=GROUP_OR_CHANNEL)
    session = await connected(request)
    _target(request, req.user_ids)
    async with write_slot(request, ACTION, "approve_join_requests", chat_id):
        chat = await call_max(groups.approve(session.client, chat_id, req.user_ids, req.show_history), UNCERTAIN)
    chat = await _stored_or_refreshed(session, chat_id, chat)
    return summary(chat, me_of(session), chat_id=chat_id, approved=req.user_ids)


@router.post("/{chat_id:sint}/join-requests/decline", name="decline_join_requests")
async def decline_join_requests(chat_id: int, req: UserIdsRequest, request: Request):
    await known_chat(request, chat_id, kinds=GROUP_OR_CHANNEL)
    session = await connected(request)
    _target(request, req.user_ids)
    async with write_slot(request, ACTION, "decline_join_requests", chat_id):
        chat = await call_max(groups.decline(session.client, chat_id, req.user_ids), UNCERTAIN)
    chat = await _stored_or_refreshed(session, chat_id, chat)
    return summary(chat, me_of(session), chat_id=chat_id, declined=req.user_ids)


@router.post("/{chat_id:sint}/delete", name="delete_group")
async def delete_group(chat_id: int, req: DeleteGroupRequest, request: Request):
    """Only the account's own group, confirmed by its current title (Р-26). A channel only while the
    account is its one member — cleaning up a test one (Р-63). Local rows stay as history."""
    chat_row = await known_chat(request, chat_id, kinds=GROUP_OR_CHANNEL)
    session = await connected(request)
    chat = await call_max_read(groups.fresh_chat(session.client, chat_id))
    if chat.owner != me_of(session):
        raise HTTPException(status_code=403, detail="owner only: the account did not create this group")
    if chat_row.chat_type == "channel" and _count(chat) > 1:
        raise HTTPException(status_code=422, detail="a channel with subscribers is not deleted by the agent")
    if req.confirm_title != chat.title:
        raise HTTPException(status_code=409, detail=f"confirm_title does not match the group's current title "
                                                    f"{chat.title!r}")
    async with write_slot(request, ACTION, "delete_group", chat_id):
        await call_max(groups.delete(session.client, chat_id, req.for_all), UNCERTAIN)
    await unmonitor(session, chat_id)
    return {"chat_id": chat_id, "deleted": True, "for_all": req.for_all}


@router.post("/{chat_id:sint}/owner", name="transfer_group_ownership")
async def transfer_group_ownership(chat_id: int, req: TransferRequest, request: Request):
    """Irreversible (Р-62): the account is the owner, the title and the user are confirmed, the new
    owner is an admin already (the client's rule); only the new owner can give it back."""
    await known_chat(request, chat_id, kinds=GROUP_OR_CHANNEL)
    session = await connected(request)
    me = me_of(session)
    if req.confirm_user_id != req.user_id:
        raise HTTPException(status_code=422, detail="confirm_user_id is not user_id")
    if req.user_id == me:
        raise HTTPException(status_code=422, detail="the account owns the chat already")
    _target(request, [req.user_id])
    chat = await call_max_read(groups.fresh_chat(session.client, chat_id))
    if chat.owner != me:
        raise HTTPException(status_code=422, detail="owner only: the account does not own this chat")
    if req.confirm_title != chat.title:
        raise HTTPException(status_code=422, detail=f"confirm_title does not match the current title {chat.title!r}")
    if req.user_id not in (chat.admins or []):
        raise HTTPException(status_code=422, detail="ownership goes to an admin only: set_group_admin first "
                                                    "(MAX client rule)")
    async with write_slot(request, ACTION, "transfer_group_ownership", chat_id):
        answered = await call_max(groups.transfer(session.client, chat_id, req.user_id), UNCERTAIN)
    chat = await _stored_or_refreshed(session, chat_id, answered)
    return summary(chat, me, chat_id=chat_id, new_owner=req.user_id)


# ---------------------------------------------------------------------------
# Extra invite links (Р-61): 221 / 222 / 223, behind MAX's server flag additional-links
# ---------------------------------------------------------------------------

def _link(item: dict) -> dict:
    return {"link_id": item.get("id"), "title": item.get("title"), "link": item.get("link"),
            "join_count": item.get("joinCount")}


@router.get("/{chat_id:sint}/invite-links", name="list_invite_links")
async def list_invite_links(chat_id: int, request: Request):
    chat_row = await known_chat(request, chat_id, kinds=GROUP_OR_CHANNEL)
    session = await connected(request)
    found = await call_max_read(groups.links_list(session.client, chat_id, channel=chat_row.chat_type == "channel"))
    return {"chat_id": chat_id, "main_link": (chat_row.raw or {}).get("link"),
            "links": [_link(i) for i in found if isinstance(i, dict)]}


@router.post("/{chat_id:sint}/invite-links", name="create_invite_link")
async def create_invite_link(chat_id: int, req: InviteLinkRequest, request: Request):
    chat_row = await known_chat(request, chat_id, kinds=GROUP_OR_CHANNEL)
    session = await connected(request)
    async with write_slot(request, ACTION, "create_invite_link", chat_id):
        answer = await call_max(groups.link_create(session.client, chat_id, channel=chat_row.chat_type == "channel",
                                                   title=req.title), UNCERTAIN)
    item = answer.get("link") if isinstance(answer, dict) else None
    if not isinstance(item, dict):
        raise HTTPException(status_code=502, detail=f"MAX answered without the link: {answer!r}; "
                                                    f"check list_invite_links")
    out = _link(item)
    out.pop("join_count")
    return {"chat_id": chat_id, **out}


@router.post("/{chat_id:sint}/invite-links/revoke", name="revoke_invite_links")
async def revoke_invite_links(chat_id: int, req: RevokeLinksRequest, request: Request):
    chat_row = await known_chat(request, chat_id, kinds=GROUP_OR_CHANNEL)
    session = await connected(request)
    async with write_slot(request, ACTION, "revoke_invite_links", chat_id):
        answer = await call_max(groups.links_revoke(session.client, chat_id, channel=chat_row.chat_type == "channel",
                                                    link_ids=req.link_ids), UNCERTAIN)
    revoked = answer.get("linkIds") if isinstance(answer, dict) else None
    return {"chat_id": chat_id, "revoked": [int(i) for i in revoked or []],
            "warning": "the revoked links no longer work"}


def _count(chat: Any) -> int:
    return chat.participants_count or len(chat.participants or {})


async def _stored_or_refreshed(session: MaxSession, chat_id: int, chat: Any) -> Any:
    if chat is not None:
        await store_chat(session, chat)
        return chat
    return await refresh(session, chat_id)


async def mark_left(session: MaxSession, chat_id: int) -> None:
    """The account left: raw.status LEFT (R3.1 statuses), monitoring off — a later join is a
    real one, not already_member (QA-R4 D3)."""
    try:
        async with account_db(session.account_id) as db:
            old = (await chats.statuses(db, session.account_id, [chat_id])).get(chat_id)
            await chats.set_status(db, session.account_id, chat_id, "LEFT", old)
            await db.commit()
    except Exception:  # noqa: BLE001
        log.warning("max[%s]: chat %s not marked left", session.alias, chat_id, exc_info=True)


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
