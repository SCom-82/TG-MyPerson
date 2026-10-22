"""MAX groups and channels through PyMax 2.4.1 (plan 2026-10-19 Р3 §3 PR-G2, Р-24…Р-27; Р4 Р-40;
plan 2026-10-22 Р5 Р-51…Р-63).

The one place the group protocol lives: the routes (app/max/api/groups.py)
call these functions with the session's client and never touch PyMax. Sixth
module allowed to import pymax (test_s16_pymax_import_boundary).

The source of truth is the MAX web client (Р-51, appendix «r5-protocol»):
admin rights are its 12 bits with its defaults and dependencies; frames PyMax
lacks or builds otherwise are built here (admin with a mask, members by type
and query, removal with messages, extra invite links, ownership, a channel).
"""

import logging
import time
from typing import Any

from pymax.protocol import Opcode
from pymax.types import Chat
from pymax.types.domain import Message
from pymax.types.domain.member import Member

log = logging.getLogger(__name__)

# Opcodes pymax.protocol.Opcode does not have (Р-51).
OP_CHAT_LINKS = 221
OP_CHAT_LINK_CREATE = 222
OP_CHAT_LINKS_REVOKE = 223

# The 12 bits of the client's admin class (FLAG_*, plan Р5 §1.1, Р-52): name → (group bit, channel bit);
# None — the client has no such right for that chat type. delete_message is bit 1 in a group
# (FLAG_POST_EDIT_DELETE_MESSAGE) and 1024 in a channel (FLAG_DELETE_MESSAGE).
PERMISSIONS: dict[str, tuple[int | None, int | None]] = {
    "add_remove_member": (2, 2),
    "add_admin": (4, 4),
    "change_chat_info": (8, 8),
    "pin_message": (16, 16),
    "read_all_messages": (32, 32),
    "edit_link": (128, 128),
    "delete_message": (1, 1024),
    "call": (64, None),
    "post_message": (None, 256),
    "edit_message": (None, 512),
    "view_stats": (None, 2048),
}
FULL_MASK = 4095  # the owner
GROUP_BITS = 1 | 2 | 4 | 8 | 16 | 32 | 64 | 128
CHANNEL_BITS = 2 | 4 | 8 | 16 | 32 | 128 | 256 | 512 | 1024 | 2048  # Р-55: no 1 and 64 in a channel
# The client's defaults (CHAT_/CHANNEL_DEFAULT_[BOT_]FLAGS, Р-53): (chat type, the target is a bot) → mask.
# No add_admin in any of them (Р-25 kept).
DEFAULTS = {("group", False): 251, ("group", True): 123, ("channel", False): 4026, ("channel", True): 1850}
MEMBERS_PAGE = 100
BOT_RULE = "a bot needs read_all_messages for delete_message/pin_message (MAX client rule)"
LINK_RULE = "edit_link needs add_remove_member (MAX client rule)"


class UnknownPermission(ValueError):
    """Rights the client would not give: unknown, of the other chat type, a broken dependency (→ 422)."""


def _bit(name: str, channel: bool) -> int | None:
    return PERMISSIONS[name][1 if channel else 0]


def allowed_names(*, channel: bool) -> list[str]:
    return [n for n in PERMISSIONS if _bit(n, channel) is not None]


def permission_names(mask: int | None, *, channel: bool = False) -> list[str]:
    """Names of the bits of mask for the chat type, in the PERMISSIONS order."""
    return [n for n in allowed_names(channel=channel) if mask and mask & _bit(n, channel)]


def _check_dependencies(mask: int, *, channel: bool, bot: bool) -> None:
    """Р-54: what the client's toggles do not let through, a 422 instead of a silent fix."""
    if mask & 128 and not mask & 2:
        raise UnknownPermission(LINK_RULE)
    if bot and not channel and mask & (1 | 16) and not mask & 32:
        raise UnknownPermission(BOT_RULE)


def resolve_mask(names: list[str] | None, mask: int | None, *, channel: bool, bot: bool) -> int:
    """The mask to send (Р-52…Р-55): names, an exact mask, or the client's default for the target."""
    if names and mask is not None:
        raise UnknownPermission("pass permissions or permissions_mask, not both")
    kind = "channel" if channel else "group"
    if mask is not None:
        if not 1 <= mask <= FULL_MASK:
            raise UnknownPermission("permissions_mask is 1…4095")
        foreign = mask & ~(CHANNEL_BITS if channel else GROUP_BITS)
        if foreign:
            raise UnknownPermission(f"bits {[b for b in (1, 64, 256, 512, 1024, 2048) if foreign & b]} are not "
                                    f"rights of a {kind}")
    elif names:
        unknown = [n for n in names if n not in PERMISSIONS]
        if unknown:
            raise UnknownPermission(f"unknown permissions {unknown}; allowed for a {kind}: "
                                    f"{allowed_names(channel=channel)}")
        foreign = sorted({n for n in names if _bit(n, channel) is None})
        if foreign:
            other = "group" if channel else "channel"
            raise UnknownPermission(f"{foreign} are {other} rights, not for a {kind}; allowed: "
                                    f"{allowed_names(channel=channel)}")
        mask = 0
        for n in names:
            mask |= _bit(n, channel)
    else:
        return DEFAULTS[(kind, bot)]
    _check_dependencies(mask, channel=channel, bot=bot)
    return mask


def admin_mask(chat: Any, user_id: int) -> int | None:
    """The user's mask in chat.adminParticipants; the owner — 4095; not an admin — None."""
    if chat is None:
        return None
    if getattr(chat, "owner", None) == user_id:
        return FULL_MASK
    entry = (getattr(chat, "admin_participants", None) or {}).get(user_id)
    if isinstance(entry, dict) and entry.get("permissions") is not None:
        return int(entry["permissions"])
    return None


def admin_inviter(chat: Any, user_id: int) -> int | None:
    """Who made the user an admin, if adminParticipants says (the key is not in the appendix: checked live)."""
    entry = (getattr(chat, "admin_participants", None) or {}).get(user_id)
    if not isinstance(entry, dict):
        return None
    value = next((entry[k] for k in ("inviterId", "inviter", "promotedBy") if entry.get(k) is not None), None)
    return int(value) if value is not None else None


def is_channel(chat: Any) -> bool:
    return str(getattr(getattr(chat, "type", None), "value", getattr(chat, "type", None))) == "CHANNEL"


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


def members_frame(chat_id: int, *, kind: str = "MEMBER", query: str | None = None, marker: int | None = None,
                  count: int = MEMBERS_PAGE) -> dict:
    """CHAT_MEMBERS 59 as the client sends it: type MEMBER/ADMIN, a server-side query (Р-56)."""
    frame = {"chatId": chat_id, "type": kind, "count": count}
    if marker:
        frame["marker"] = marker
    if query:
        frame["query"] = query
    return frame


async def find_members(client: Any, chat_id: int, limit: int, *, kind: str = "MEMBER",
                       query: str | None = None) -> list[Member]:
    """Members by type (ADMIN: admins only) and/or MAX's own search, paging by marker up to limit."""
    out: list[Member] = []
    marker = None
    while len(out) < limit:
        frame = await client._app.invoke(Opcode.CHAT_MEMBERS, members_frame(
            chat_id, kind=kind, query=query, marker=marker, count=min(MEMBERS_PAGE, limit - len(out))))
        payload = getattr(frame, "payload", None) or {}
        items = payload.get("members") or []
        out.extend(m for m in (parse_member(i) for i in items) if m is not None)
        marker = payload.get("marker")
        if not items or not marker:
            break
    return out[:limit]


def parse_member(item: Any) -> Member | None:
    """An element of 59's members (pc: contact?, presence? — both optional in the client; PyMax's
    Member requires both). No contact → skipped; no presence → empty; a broken one → skipped,
    logged: one element does not fail the page (review 18 R18-1)."""
    if not isinstance(item, dict) or not isinstance(item.get("contact"), dict):
        log.info("max: a member without contact skipped: %r", item)
        return None
    try:
        return Member.model_validate({**item, "presence": item.get("presence") or {}})
    except Exception as exc:  # noqa: BLE001
        log.warning("max: a member not understood, skipped: %s: %s", type(exc).__name__, exc)
        return None


async def join_requests(client: Any, chat_id: int, limit: int) -> list[Any]:
    return list(await client.get_join_requests(chat_id, count=limit))


async def is_bot(client: Any, user_id: int) -> bool:
    """A bot by its profile (CONTACT_INFO options BOT), like normalize_user.is_bot (Р-53). An unknown
    user is not a bot: both defaults are safe (plan §8)."""
    users = await client.get_users([user_id])
    return any(u.id == user_id and "BOT" in {str(o).upper() for o in (u.options or [])} for u in users)


def typed_chat_id(chat_id: int, *, channel: bool) -> dict:
    """The typed chatId of 221-223: {type: 1 MULTICHAT / 2 CHANNEL, primaryId} (Р-61)."""
    return {"type": 2 if channel else 1, "primaryId": chat_id}


def links_frame(chat_id: int, *, channel: bool) -> dict:
    return {"chatId": typed_chat_id(chat_id, channel=channel)}


async def links_list(client: Any, chat_id: int, *, channel: bool) -> list[dict]:
    """Extra invite links (221): [{id, title, link, joinCount}]."""
    frame = await client._app.invoke(OP_CHAT_LINKS, links_frame(chat_id, channel=channel))
    return list((getattr(frame, "payload", None) or {}).get("links") or [])


# -- writes --------------------------------------------------------------------

def create_frame(title: str, user_ids: list[int], chat_type: str, notify: bool) -> dict:
    """MSG_SEND 64 with a CONTROL "new" attach, as the client creates a chat (Р-63): chatType CHAT/CHANNEL."""
    return {"message": {"cid": int(time.time() * 1000),
                        "attaches": [{"_type": "CONTROL", "event": "new",
                                      "chatType": "CHANNEL" if chat_type == "channel" else "CHAT",
                                      "title": title, "userIds": list(user_ids)}]},
            "notify": notify}


async def create(client: Any, title: str, user_ids: list[int], notify: bool,
                 chat_type: str = "group") -> tuple[Any, Any] | dict | None:
    """A group through PyMax as before: (Chat, Message) or None. A channel — our frame (PyMax has
    none), MAX's raw answer {chatId, chat, message}, parsed apart by parse_created (R17-1)."""
    if chat_type != "channel":
        return await client.create_group(title, participant_ids=user_ids or None, notify=notify)
    frame = await client._app.invoke(Opcode.MSG_SEND, create_frame(title, user_ids, chat_type, notify))
    return getattr(frame, "payload", None) or {}


def parse_created(payload: dict) -> tuple[Chat, Message | None] | None:
    """MSG_SEND's answer of a created channel → (Chat, its service message); no chat → None.
    The chat and the message apart: a service message PyMax cannot read does not lose the
    channel (review 18 R18-2) — it is None then."""
    if not payload.get("chat"):
        return None
    chat = Chat.model_validate(payload["chat"])
    msg = payload.get("message")
    if not msg:
        return chat, None
    try:
        return chat, Message.model_validate({**msg, "chatId": payload.get("chatId") or chat.id})
    except Exception as exc:  # noqa: BLE001
        log.warning("max: the service message of a new channel not understood: %s: %s", type(exc).__name__, exc)
        return chat, None


def invite_frame(chat_id: int, user_ids: list[int], show_history: bool, start_payload: str) -> dict:
    """PyMax's invite frame + startPayload (77, Р-58): a bot added with /start's payload."""
    return {"chatId": chat_id, "userIds": list(user_ids), "showHistory": show_history, "operation": "add",
            "startPayload": start_payload}


async def invite(client: Any, chat_id: int, user_ids: list[int], show_history: bool, chat_type: str = "group",
                 start_payload: str | None = None) -> Any:
    if start_payload is not None:
        frame = await client._app.invoke(Opcode.CHAT_MEMBERS_UPDATE,
                                         invite_frame(chat_id, user_ids, show_history, start_payload))
        _forget(client, chat_id)
        chat = (getattr(frame, "payload", None) or {}).get("chat")
        return Chat.model_validate(chat) if chat else None
    if chat_type == "channel":
        return await client.invite_users_to_channel(chat_id, user_ids, show_history=show_history)
    return await client.invite_users_to_group(chat_id, user_ids, show_history=show_history)


def remove_frame(chat_id: int, user_ids: list[int], *, clean: bool) -> dict:
    """PyMax's removal frame; cleanMsgPeriod -1 removes the member's messages too (Р-60, the client's tick)."""
    return {"chatId": chat_id, "userIds": list(user_ids), "operation": "remove", "cleanMsgPeriod": -1 if clean else 0}


async def remove(client: Any, chat_id: int, user_ids: list[int], *, clean: bool = False) -> list[int]:
    """Removes; with clean — our frame, whose answer names the deleted messages (deletedMessageIds)."""
    if not clean:
        # clean_msg_period is required by PyMax: 0 = keep the removed member's messages.
        await client.remove_users_from_group(chat_id, user_ids, clean_msg_period=0)
        return []
    frame = await client._app.invoke(Opcode.CHAT_MEMBERS_UPDATE, remove_frame(chat_id, user_ids, clean=True))
    _forget(client, chat_id)
    return [int(i) for i in (getattr(frame, "payload", None) or {}).get("deletedMessageIds") or []]


def admin_frame(chat_id: int, user_id: int, mask: int) -> dict:
    """CHAT_MEMBERS_UPDATE 77 ADMIN add with the exact mask (Р-52): PyMax's add_admin takes 7 enum bits."""
    return {"chatId": chat_id, "userIds": [user_id], "type": "ADMIN", "operation": "add", "permissions": mask}


async def set_admin(client: Any, chat_id: int, user_id: int, mask: int) -> None:
    await client._app.invoke(Opcode.CHAT_MEMBERS_UPDATE, admin_frame(chat_id, user_id, mask))
    _forget(client, chat_id)


def remove_admin_frame(chat_id: int, user_id: int) -> dict:
    """PyMax's AddAdminPayload with operation "remove" (Р-24): not in PyMax 2.4.1, checked live."""
    return {"chatId": chat_id, "userIds": [user_id], "type": "ADMIN", "operation": "remove"}


async def remove_admin(client: Any, chat_id: int, user_id: int) -> None:
    await client._app.invoke(Opcode.CHAT_MEMBERS_UPDATE, remove_admin_frame(chat_id, user_id))
    _forget(client, chat_id)


async def profile(client: Any, chat_id: int, title: str | None, description: str | None) -> None:
    await client.change_group_profile(chat_id, name=title, description=description)


# PyMax's change_group_settings knows these five; the rest go in our frame (Р-59).
PYMAX_SETTINGS = frozenset({"all_can_pin_message", "only_owner_can_change_icon_title", "only_admin_can_add_member",
                            "only_admin_can_call", "members_can_see_private_link"})


def settings_frame(chat_id: int, options: dict[str, bool]) -> dict:
    """CHAT_UPDATE 55 options as the client names them (JOIN_REQUEST, DISABLE_FORWARD, COMMENTS, ...)."""
    return {"chatId": chat_id, "options": {k.upper(): v for k, v in options.items()}}


async def settings(client: Any, chat_id: int, options: dict[str, bool]) -> None:
    if set(options) <= PYMAX_SETTINGS:
        await client.change_group_settings(chat_id, **options)
        return
    await client._app.invoke(Opcode.CHAT_UPDATE, settings_frame(chat_id, options))
    _forget(client, chat_id)


def transfer_frame(chat_id: int, user_id: int) -> dict:
    """CHAT_UPDATE 55 changeOwnerId (Р-62)."""
    return {"chatId": chat_id, "changeOwnerId": user_id}


async def transfer(client: Any, chat_id: int, user_id: int) -> Any:
    frame = await client._app.invoke(Opcode.CHAT_UPDATE, transfer_frame(chat_id, user_id))
    _forget(client, chat_id)
    chat = (getattr(frame, "payload", None) or {}).get("chat")
    return Chat.model_validate(chat) if chat else None


async def reset_link(client: Any, chat_id: int) -> Any:
    return await client.rework_invite_link(chat_id)


def link_create_frame(chat_id: int, *, channel: bool, title: str | None) -> dict:
    frame = {"chatId": typed_chat_id(chat_id, channel=channel)}
    if title is not None:
        frame["title"] = title
    return frame


async def link_create(client: Any, chat_id: int, *, channel: bool, title: str | None) -> dict:
    """An extra invite link (222) → MAX's raw answer {link: {id, title, link, joinCount}}."""
    frame = await client._app.invoke(OP_CHAT_LINK_CREATE, link_create_frame(chat_id, channel=channel, title=title))
    return getattr(frame, "payload", None) or {}


def links_revoke_frame(chat_id: int, *, channel: bool, link_ids: list[int]) -> dict:
    return {"chatId": typed_chat_id(chat_id, channel=channel), "linkIds": list(link_ids)}


async def links_revoke(client: Any, chat_id: int, *, channel: bool, link_ids: list[int]) -> dict:
    """Revoke extra links (223) → MAX's raw answer {linkIds}."""
    frame = await client._app.invoke(OP_CHAT_LINKS_REVOKE, links_revoke_frame(chat_id, channel=channel,
                                                                              link_ids=link_ids))
    return getattr(frame, "payload", None) or {}


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
