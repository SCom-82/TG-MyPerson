"""MAX groups and channels through PyMax 2.4.1 (plan 2026-10-19 Р3 §3 PR-G2, Р-24…Р-27; Р4 Р-40).

The one place the group protocol lives: the routes (app/max/api/groups.py)
call these functions with the session's client and never touch PyMax. Sixth
module allowed to import pymax (test_s16_pymax_import_boundary): removing an
admin has no PyMax method, its frame is built here; admin rights are PyMax's
ChannelPermissions bits.
"""

import logging
from functools import reduce
from operator import or_
from typing import Any

from pymax.api.chats.enums import ChannelPermissions
from pymax.protocol import Opcode

log = logging.getLogger(__name__)

PERMISSIONS = {
    "add_remove_member": ChannelPermissions.ADD_REMOVE_MEMBER,
    "add_admin": ChannelPermissions.ADD_ADMIN,
    "change_chat_info": ChannelPermissions.CHANGE_CHAT_INFO,
    "pin_message": ChannelPermissions.PIN_MESSAGE,
    "delete_message": ChannelPermissions.DELETE_MESSAGE,
    # channel rights (plan Р4 Р-40): refused for a group
    "post_message": ChannelPermissions.POST_MESSAGE,
    "edit_message": ChannelPermissions.EDIT_MESSAGE,
}
CHANNEL_ONLY = ("post_message", "edit_message")
# Р-25: no add_admin by default (an admin who appoints admins is an escalation).
DEFAULT_PERMISSIONS = ("add_remove_member", "change_chat_info", "pin_message", "delete_message")
# Р-40: a channel admin posts, edits and deletes posts (1792).
CHANNEL_DEFAULT_PERMISSIONS = ("post_message", "edit_message", "delete_message")
MEMBERS_PAGE = 100


class UnknownPermission(ValueError):
    pass


def permission_values(names: list[str] | None, *, channel: bool = False) -> list[ChannelPermissions]:
    names = list(names) if names else list(CHANNEL_DEFAULT_PERMISSIONS if channel else DEFAULT_PERMISSIONS)
    unknown = [n for n in names if n not in PERMISSIONS]
    if unknown:
        raise UnknownPermission(f"unknown permissions {unknown}; allowed: {sorted(PERMISSIONS)}")
    if not channel and set(names) & set(CHANNEL_ONLY):
        raise UnknownPermission(f"{sorted(set(names) & set(CHANNEL_ONLY))} are channel rights, not for a group")
    return [PERMISSIONS[n] for n in names]


def permission_mask(names: list[str] | None, *, channel: bool = False) -> int:
    return reduce(or_, (int(p) for p in permission_values(names, channel=channel)))


def permission_names(mask: int | None) -> list[str]:
    return [name for name, bit in PERMISSIONS.items() if mask and mask & int(bit)]


def _forget(client: Any, chat_id: int) -> None:
    """Drop PyMax's cached Chat: get_chat answers from the cache otherwise (plan §6)."""
    try:
        client._app.api.chats._remove_cached_chat(chat_id)
    except Exception:  # noqa: BLE001 — another PyMax / a test client: the cache is not there
        pass


# -- reads ---------------------------------------------------------------------

async def fresh_chat(client: Any, chat_id: int) -> Any:
    _forget(client, chat_id)
    return await client.get_chat(chat_id)


async def members(client: Any, chat_id: int, limit: int) -> list[Any]:
    """Members (pymax Member: contact + presence), paging by marker up to limit."""
    out: list[Any] = []
    marker = None
    while len(out) < limit:
        page, marker = await client.get_chat_members(chat_id, marker=marker, count=min(MEMBERS_PAGE, limit - len(out)))
        out.extend(page)
        if not page or not marker:
            break
    return out[:limit]


async def join_requests(client: Any, chat_id: int, limit: int) -> list[Any]:
    return list(await client.get_join_requests(chat_id, count=limit))


# -- writes --------------------------------------------------------------------

async def create(client: Any, title: str, user_ids: list[int], notify: bool) -> tuple[Any, Any] | None:
    return await client.create_group(title, participant_ids=user_ids or None, notify=notify)


async def invite(client: Any, chat_id: int, user_ids: list[int], show_history: bool, chat_type: str = "group") -> Any:
    if chat_type == "channel":
        return await client.invite_users_to_channel(chat_id, user_ids, show_history=show_history)
    return await client.invite_users_to_group(chat_id, user_ids, show_history=show_history)


async def remove(client: Any, chat_id: int, user_ids: list[int]) -> None:
    # clean_msg_period is required by PyMax: 0 = keep the removed member's messages.
    await client.remove_users_from_group(chat_id, user_ids, clean_msg_period=0)


async def set_admin(client: Any, chat_id: int, user_id: int, names: list[str] | None, *, channel: bool = False) -> None:
    await client.add_admin(chat_id, user_id, permission_values(names, channel=channel))


def remove_admin_frame(chat_id: int, user_id: int) -> dict:
    """PyMax's AddAdminPayload with operation "remove" (Р-24): not in PyMax 2.4.1, checked live."""
    return {"chatId": chat_id, "userIds": [user_id], "type": "ADMIN", "operation": "remove"}


async def remove_admin(client: Any, chat_id: int, user_id: int) -> None:
    await client._app.invoke(Opcode.CHAT_MEMBERS_UPDATE, remove_admin_frame(chat_id, user_id))
    _forget(client, chat_id)


async def profile(client: Any, chat_id: int, title: str | None, description: str | None) -> None:
    await client.change_group_profile(chat_id, name=title, description=description)


async def settings(client: Any, chat_id: int, options: dict[str, bool]) -> None:
    await client.change_group_settings(chat_id, **options)


async def reset_link(client: Any, chat_id: int) -> Any:
    return await client.rework_invite_link(chat_id)


async def approve(client: Any, chat_id: int, user_ids: list[int], show_history: bool) -> Any:
    return await client.confirm_join_requests(chat_id, user_ids, show_history=show_history)


async def decline(client: Any, chat_id: int, user_ids: list[int]) -> Any:
    return await client.decline_join_requests(chat_id, user_ids)


async def leave(client: Any, chat_id: int, chat_type: str) -> None:
    if chat_type == "channel":
        await client.leave_channel(chat_id)
    else:
        await client.leave_group(chat_id)


async def delete(client: Any, chat_id: int, for_all: bool) -> None:
    await client.delete_chat(chat_id, for_all=for_all)
