"""MCP tools → REST routes (ТЗ 2026-10-13 §9.1–9.3).

Every tool is a REST route of the service, called in-process (app/mcp/invoke.py)
for the platform of the key's account. `route` is the route name of the
tool catalog: authorize() and audit() use it, so MCP and REST are allowed,
denied and logged identically.
"""

import copy
import dataclasses
import functools
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.authz.tool_catalog import WRITE_MESSENGER_TOOLS
from app.mcp import files, invoke
from app.max.config import max_settings
from app.mcp.stdio_catalog import STDIO_TOOLS

# MAX_SCHEDULED_ENABLED=false hides these from a MAX account (plan 2026-10-22 Р5 Р-73)
SCHEDULED_TOOLS = frozenset({"list_scheduled", "cancel_scheduled", "send_scheduled_now"})


@dataclass
class Caller:
    """Who calls: the key's account (or the owner) and the public base URL."""

    scope: str  # 'account' | 'owner'
    account_id: int | None
    alias: str | None
    platform: str
    mode: str
    base_url: str

    @property
    def state(self) -> dict:
        return {"account_id": self.account_id, "session_alias": self.alias,
                "platform": self.platform, "scope": self.scope}


Handler = Callable[[Caller, dict], Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    route: str
    call: Handler
    audience: str = "account"  # 'account' | 'rw' | 'owner'

    @property
    def is_write(self) -> bool:
        return self.route in WRITE_MESSENGER_TOOLS


class ToolError(Exception):
    """Shown to the agent as the tool result text."""


# ---------------------------------------------------------------------------
# Ids (plan 2026-10-19 Р-1, Р-2): MAX ids are ~1.2e17 > 2^53, a JSON client
# that parses numbers as doubles rounds them (…689 → …700) and gets a 404.
# Inputs take a string or a number, the server makes it an int; outputs carry
# such ids as strings. TG ids stay below 2^53: TG answers do not change.
# ---------------------------------------------------------------------------

MAX_SAFE_INT = 2**53 - 1
ID_KEYS = frozenset({
    "chat_id", "message_id", "message_ids", "reply_to_message_id", "from_user_id", "user_id",
    "to_chat_id", "from_chat_id", "forward_from_chat_id", "forward_from_message_id", "user_ids",
    "chat_ids", "answer_ids",  # folders, votes (plan 2026-10-19 Р4)
    "link_ids",  # extra invite links (plan 2026-10-22 Р5 Р-61)
})
# Internal ids of the contacts base: small, stay plain integers.
SMALL_IDS = frozenset({"person_id", "identity_id", "from_id", "to_id"})
ID_NOTE = "MAX ids exceed 2^53: pass them as strings."
ID_TYPE = {"type": ["string", "integer"], "pattern": r"^-?\d+$"}


def _is_id(name: str, prop: dict) -> bool:
    if name in SMALL_IDS:
        return False
    if prop.get("type") == "array":
        return name in ID_KEYS and (prop.get("items") or {}).get("type") == "integer"
    return prop.get("type") == "integer" and (name in ID_KEYS or name.endswith("_id"))


def widen_ids(schema: dict) -> dict:
    """integer id properties → string or integer (digits only)."""
    schema = copy.deepcopy(schema)
    for name, prop in (schema.get("properties") or {}).items():
        if not _is_id(name, prop):
            continue
        if prop["type"] == "array":
            prop["items"] = {**prop["items"], **ID_TYPE}
        else:
            prop.update(ID_TYPE)
        text = (prop.get("description") or "").rstrip()
        sep = "" if not text else (" " if text.endswith((".", "!", "?")) else ". ")  # QA-R1-D2
        prop["description"] = f"{text}{sep}{ID_NOTE}"
    return schema


def _to_id(key: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ToolError(f"Error: {key} must be an integer id")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer() and abs(value) <= MAX_SAFE_INT:
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
        return int(value)
    raise ToolError(f"Error: {key} must be an integer id")


def coerce_ids(args: dict, schema: dict) -> dict:
    """Id arguments of a widened schema → int; anything but digits → ToolError (REST is not called)."""
    out = dict(args)
    for name, prop in (schema.get("properties") or {}).items():
        value = out.get(name)
        if value is None:
            continue
        if prop.get("type") == ID_TYPE["type"]:
            out[name] = _to_id(name, value)
        elif prop.get("type") == "array" and (prop.get("items") or {}).get("type") == ID_TYPE["type"]:
            if not isinstance(value, list):
                raise ToolError(f"Error: {name} must be a list of integer ids")
            out[name] = [_to_id(name, v) for v in value]
    return out


def safe_ints(obj: Any) -> Any:
    """Integers beyond 2^53 → strings, everywhere in a tool result (Р-2)."""
    if isinstance(obj, dict):
        return {k: safe_ints(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [safe_ints(v) for v in obj]
    if isinstance(obj, int) and not isinstance(obj, bool) and abs(obj) > MAX_SAFE_INT:
        return str(obj)
    return obj


def _clean(args: dict, *drop: str) -> dict:
    return {k: v for k, v in args.items() if v is not None and k not in drop}


async def _rest(caller: Caller, method: str, path: str, **kw) -> Any:
    result = await invoke.call(caller.platform, method, path, state=caller.state, **kw)
    if not result.ok:
        body = result.body
        if result.status in (403, 429) and isinstance(body, dict) and body.get("message"):
            # Write limits (app/authz/write_limits.py): the sentence first, then the details.
            raise ToolError(f"HTTP {result.status}: {body['message']}. Details: {_text(body)}")
        raise ToolError(f"HTTP {result.status}: {_text(body)}")
    return result.body


def _text(body: Any) -> str:
    import json

    return body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)


def rest(method: str, path: str, *, query: Callable[[dict], dict] | None = None,
         body: Callable[[dict], Any] | None = None) -> Handler:
    async def handler(caller: Caller, args: dict) -> Any:
        return await _rest(
            caller, method, path.format(**args),
            query=query(args) if query else None,
            json_body=body(args) if body else None,
        )
    handler.rest_route = (method, path)  # for the wiring test
    return handler


ALL = lambda a: _clean(a)  # noqa: E731
NOTHING = None


# ---------------------------------------------------------------------------
# Files (§9.2)
# ---------------------------------------------------------------------------

FILE_SOURCE = {
    "file_url": {"type": "string", "description": "https URL the server downloads (≤ 50 MB)"},
    "upload_id": {"type": "string", "description": "id from create_upload after PUT of the file"},
}


async def _file(caller: Caller, args: dict, *, url_key: str = "file_url", id_key: str = "upload_id"):
    if args.get("file_path") or args.get("photo_path") or args.get("file_paths"):
        raise ToolError("local paths are not visible to the server: use create_upload (or file_url) "
                        "and pass upload_id")
    if args.get(id_key):
        try:
            return files.read_upload(caller.account_id, args[id_key])
        except files.LinkError as exc:
            raise ToolError(str(exc))
    if args.get(url_key):
        try:
            return await files.fetch_url(args[url_key])
        except files.LinkError as exc:
            raise ToolError(str(exc))
    raise ToolError("pass file_url or upload_id")


def _form(args: dict, keys: tuple[str, ...]) -> list:
    return [(k, args[k]) for k in keys if args.get(k) is not None]


async def send_file(caller: Caller, args: dict) -> Any:
    name, data = await _file(caller, args)
    parts = [("chat_id", args["chat_id"]), ("file", (name, data, None))]
    parts += _form(args, ("caption", "reply_to_message_id", "parse_mode", "schedule_date"))
    if args.get("voice_note"):
        parts.append(("voice_note", True))
    return await _rest(caller, "POST", "/messages/send-file", multipart=parts)


async def send_voice(caller: Caller, args: dict) -> Any:
    name, data = await _file(caller, args)
    parts = [("chat_id", args["chat_id"]), ("file", (name, data, None))]
    parts += _form(args, ("caption", "reply_to_message_id", "schedule_date"))
    return await _rest(caller, "POST", "/messages/send-voice", multipart=parts)


async def send_video_note(caller: Caller, args: dict) -> Any:
    name, data = await _file(caller, args)
    parts = [("chat_id", args["chat_id"]), ("file", (name, data, None))]
    parts += _form(args, ("reply_to_message_id", "duration_ms"))
    return await _rest(caller, "POST", "/messages/send-video-note", multipart=parts)


async def send_album(caller: Caller, args: dict) -> Any:
    sources = args.get("upload_ids") or []
    urls = args.get("file_urls") or []
    if args.get("file_paths"):
        raise ToolError("local paths are not visible to the server: use create_upload and upload_ids")
    if not 2 <= len(sources) + len(urls) <= 10:
        raise ToolError("an album needs 2-10 files (upload_ids / file_urls)")
    parts: list = [("chat_id", args["chat_id"])]
    for uid in sources:
        name, data = await _file(caller, {"upload_id": uid})
        parts.append(("files", (name, data, None)))
    for url in urls:
        name, data = await _file(caller, {"file_url": url})
        parts.append(("files", (name, data, None)))
    parts += _form(args, ("caption", "reply_to_message_id", "parse_mode", "schedule_date"))
    return await _rest(caller, "POST", "/messages/album", multipart=parts)


async def download_media(caller: Caller, args: dict) -> Any:
    msg = await _rest(caller, "GET", f"/messages/{args['chat_id']}/{args['message_id']}")
    media = (msg or {}).get("media") or []
    if not media:
        raise ToolError("HTTP 404: message has no stored media")
    item, index = _media_item(media, args.get("index"))
    try:
        link = files.file_link(caller.base_url, caller.account_id, args["chat_id"], args["message_id"], index)
    except files.LinkError as exc:
        raise ToolError(str(exc))
    target = args.get("save_to") or item.get("file_name") or f"media_{args['message_id']}"
    return {
        **link,
        "file_name": item.get("file_name"),
        "file_size": item.get("file_size"),
        "mime_type": item.get("mime_type"),
        "curl": f"curl -fL -o '{target}' '{link['url']}'",
        "note": "the server cannot write to your disk: download with the curl command"
                + (f" (save_to '{args['save_to']}' is not used)" if args.get("save_to") else ""),
    }


def _media_item(media: list[dict], index: Any) -> tuple[dict, int]:
    """The media item and the index for GET …/media (plan 2026-10-19, PR-C).

    MAX items carry attach_index — the number of the attachment in the message,
    which the REST ?index= takes; an [INLINE_KEYBOARD, PHOTO] message has its
    photo at 1, a forwarded file after the own attachments. No index → the
    first item. TG items have no attach_index: index is the position, as before.
    """
    if media[0].get("attach_index") is None:
        index = int(index or 0)
        return media[min(index, len(media) - 1)], index
    if index is None:
        return media[0], media[0]["attach_index"]
    for item in media:
        if item["attach_index"] == int(index):
            return item, item["attach_index"]
    raise ToolError(f"HTTP 404: no media with attach_index {index}; the message has "
                    f"{[m['attach_index'] for m in media]}")


async def create_upload(caller: Caller, args: dict) -> Any:
    try:
        return files.new_upload(caller.base_url, caller.account_id)
    except files.LinkError as exc:
        raise ToolError(str(exc))


def _file_schema(base: dict, *, drop: str, required_extra: tuple[str, ...] = ()) -> dict:
    schema = copy.deepcopy(base)
    schema["properties"].pop(drop, None)
    schema["properties"].update(FILE_SOURCE)
    schema["required"] = [r for r in schema.get("required", []) if r != drop] + list(required_extra)
    return schema


send_file.rest_route = ("POST", "/messages/send-file")
send_voice.rest_route = ("POST", "/messages/send-voice")
send_video_note.rest_route = ("POST", "/messages/send-video-note")
send_album.rest_route = ("POST", "/messages/album")
download_media.rest_route = ("GET", "/messages/{chat_id}/{message_id}")


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

# tool → (route name, handler) for the 42 stdio tools
ROUTES: dict[str, tuple[str, Handler]] = {
    "auth_status": ("auth_status", rest("GET", "/auth/status")),
    "auth_me": ("auth_me", rest("GET", "/auth/me")),
    "auth_login": ("auth_login", rest("POST", "/auth/login", body=ALL)),
    "auth_code": ("auth_code", rest("POST", "/auth/code", body=ALL)),
    "auth_logout": ("auth_logout", rest("POST", "/auth/logout")),
    "list_chats": ("list_chats", rest("GET", "/chats", query=ALL)),
    "get_chat": ("get_chat_detail", rest("GET", "/chats/{chat_id}")),
    "update_chat": ("update_chat_settings", rest("PATCH", "/chats/{chat_id}",
                                                 body=lambda a: {"is_monitored": a["is_monitored"]})),
    "join_chat": ("join_chat_endpoint", rest("POST", "/chats/join", body=lambda a: {"target": a["target"]})),
    "leave_chat": ("leave_chat_endpoint", rest("POST", "/chats/leave", body=lambda a: {"chat_id": a["chat_id"]})),
    "resolve_chat": ("resolve_chat_endpoint", rest("POST", "/chats/resolve",
                                                   body=lambda a: {"target": a["target"]})),
    "get_chat_members": ("list_members", rest("GET", "/chats/{chat_id}/members",
                                              query=lambda a: _clean(a, "chat_id"))),
    "mark_chat_read": ("mark_read", rest("POST", "/chats/{chat_id}/read")),
    "archive_chat": ("archive_chat", rest("POST", "/chats/{chat_id}/archive",
                                          body=lambda a: {"archived": a["archived"]})),
    "get_my_rights": ("get_my_rights", rest("GET", "/chats/{chat_id}/my_rights")),
    "list_messages": ("list_messages", rest("GET", "/messages", query=ALL)),
    "get_message": ("get_single_message", rest("GET", "/messages/{chat_id}/{message_id}")),
    "send_message": ("send_message", rest("POST", "/messages", body=ALL)),
    "send_photo": ("send_file", send_file),
    "send_file": ("send_file", send_file),
    "send_album": ("send_album", send_album),
    "send_voice": ("send_voice", send_voice),
    "send_poll": ("send_poll", rest("POST", "/messages/poll", body=ALL)),
    "forward_message": ("forward_message", rest("POST", "/messages/forward", body=ALL)),
    "edit_message": ("edit_message", rest("POST", "/messages/edit", body=ALL)),
    "delete_message": ("delete_message", rest("DELETE", "/messages/{chat_id}/{message_id}")),
    "pin_message": ("pin_message", rest("POST", "/messages/{chat_id}/{message_id}/pin",
                                        query=lambda a: {"notify": a.get("notify", True)})),
    "unpin_message": ("unpin_message", rest("POST", "/messages/{chat_id}/{message_id}/unpin")),
    "react_to_message": ("react_to_message", rest("POST", "/messages/{chat_id}/{message_id}/react",
                                                  body=lambda a: {"emoticon": a.get("emoticon")})),
    "download_media": ("download_media", download_media),
    "list_scheduled": ("list_scheduled", rest("GET", "/messages/scheduled",
                                              query=lambda a: {"chat_id": a["chat_id"]})),
    "cancel_scheduled": ("cancel_scheduled", rest("DELETE", "/messages/scheduled/{chat_id}/{message_id}")),
    "send_scheduled_now": ("send_scheduled_now", rest("POST", "/messages/scheduled/{chat_id}/{message_id}/send-now")),
    "list_users": ("list_users", rest("GET", "/users", query=ALL)),
    "resolve_user": ("resolve_user", rest("POST", "/users/resolve", body=lambda a: {"username": a["username"]})),
    "block_user": ("block_user", rest("POST", "/users/{user_id}/block")),
    "unblock_user": ("unblock_user", rest("POST", "/users/{user_id}/unblock")),
    "list_contacts": ("list_contacts", rest("GET", "/contacts", query=ALL)),
    "global_search": ("search_global", rest("GET", "/search/global",
                                            query=lambda a: {"q": a["q"], "limit": a.get("limit", 20)})),
    "sync_chats": ("sync_chats", rest("POST", "/sync/chats")),
    "backfill_history": ("trigger_backfill", rest("POST", "/sync/backfill",
                                                  body=lambda a: {"chat_id": a["chat_id"],
                                                                  "limit": a.get("limit", 1000)})),
    "sync_status": ("sync_status", rest("GET", "/sync/status")),
}


def _account_tools() -> list[Tool]:
    out = []
    for spec in STDIO_TOOLS:
        name = spec["name"]
        route, handler = ROUTES[name]
        schema = copy.deepcopy(spec["inputSchema"])
        description = spec["description"]
        if name == "send_photo":
            schema = _file_schema(schema, drop="photo_path")
            description += " The file comes from file_url or upload_id (create_upload)."
        elif name in ("send_file", "send_voice"):
            schema = _file_schema(schema, drop="file_path")
            description += " The file comes from file_url or upload_id (create_upload)."
        elif name == "send_album":
            schema["properties"].pop("file_paths", None)
            schema["properties"]["upload_ids"] = {"type": "array", "items": {"type": "string"}, "maxItems": 10}
            schema["properties"]["file_urls"] = {"type": "array", "items": {"type": "string"}, "maxItems": 10}
            schema["required"] = ["chat_id"]
            description += " Files: upload_ids (create_upload) and/or https file_urls, 2-10 in total."
        elif name == "download_media":
            schema["required"] = ["chat_id", "message_id"]
            schema["properties"]["save_to"]["description"] = "ignored: the server returns a link to download"
            schema["properties"]["index"] = {
                "type": "integer",
                "description": "attachment number: media[].attach_index of the message (MAX); "
                               "TG: position in media. Default: the first media item",
            }
            description = ("Link to download media of a message: {url, expires_at, file_name, file_size, "
                           "mime_type, curl} (signed, 15 minutes).")
        out.append(Tool(name, description, widen_ids(schema), route, handler))
    return out


def _owner_tools() -> list[Tool]:
    pid = {"person_id": {"type": "integer"}}
    obj = lambda props, req=(): {"type": "object", "properties": props, "required": list(req)}  # noqa: E731

    async def person_accounts(caller: Caller, args: dict) -> Any:
        person = await _rest(caller, "GET", f"/people/{args['person_id']}")
        return {"person_id": person["id"], "accounts": person.get("accounts", [])}

    return _widened([
        Tool("find_person", "Find people in the contacts base by name, username, phone or external id.",
             obj({"q": {"type": "string"}, "tag": {"type": "string"},
                  "platform": {"type": "string", "enum": ["telegram", "max"]},
                  "limit": {"type": "integer", "default": 50}, "offset": {"type": "integer", "default": 0}}),
             "find_person", rest("GET", "/people", query=ALL), "owner"),
        Tool("get_person", "A person: identities, tags, notes and what every account knows about them.",
             obj(pid, ["person_id"]), "get_person", rest("GET", "/people/{person_id}"), "owner"),
        Tool("person_accounts", "Per account: knows the person, access hash, contact, mode, reachability, messages.",
             obj(pid, ["person_id"]), "get_person", person_accounts, "owner"),
        Tool("person_messages", "Messages the person sent, across accounts, with account and chat.",
             obj({**pid, "account": {"type": "string"}, "chat_id": {"type": "integer"},
                  "date_from": {"type": "string"}, "date_to": {"type": "string"},
                  "limit": {"type": "integer", "default": 50}, "offset": {"type": "integer", "default": 0}},
                 ["person_id"]),
             "person_messages", rest("GET", "/people/{person_id}/messages", query=lambda a: _clean(a, "person_id")),
             "owner"),
        Tool("person_chats", "Chats where the person wrote, per account.", obj(pid, ["person_id"]),
             "person_chats", rest("GET", "/people/{person_id}/chats"), "owner"),
        Tool("update_person", "Set display_name, tags, notes of a person.",
             obj({**pid, "display_name": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
                  "notes": {"type": "string"}}, ["person_id"]),
             "update_person", rest("PATCH", "/people/{person_id}", body=lambda a: _clean(a, "person_id")), "owner"),
        Tool("merge_people", "Merge person from_id into to_id (identities pinned, logged).",
             obj({"from_id": {"type": "integer"}, "to_id": {"type": "integer"}, "reason": {"type": "string"}},
                 ["from_id", "to_id"]),
             "merge_people", rest("POST", "/people/merge", body=ALL), "owner"),
        Tool("split_person", "Move one identity to a new person (pinned, logged).",
             obj({"identity_id": {"type": "integer"}, "reason": {"type": "string"}}, ["identity_id"]),
             "split_person", rest("POST", "/people/split", body=ALL), "owner"),
    ])


def _rw_tools() -> list[Tool]:
    return _widened([
        Tool("reachable_contacts", "People from the shared contacts base this account can write to, and how "
             "(direct / username / phone).",
             {"type": "object", "properties": {"q": {"type": "string"},
                                               "limit": {"type": "integer", "default": 50},
                                               "offset": {"type": "integer", "default": 0}}},
             "reachable_contacts", rest("GET", "/reachable", query=ALL), "rw"),
        Tool("send_to_person", "Write to a person of the contacts base by person_id (the best way this account "
             "can reach them).",
             {"type": "object", "required": ["person_id", "text"],
              "properties": {"person_id": {"type": "integer"}, "text": {"type": "string"}}},
             "send_to_person", rest("POST", "/messages/to-person", body=ALL), "rw"),
        Tool("contact_by_phone", "Find a person by phone in this account's messenger, add to contacts and to the "
             "contacts base; returns person_id/user_id/chat_id (then send_to_person or send_message). Limit 20 "
             "lookups/day per account; known numbers answer from the base without a lookup (refresh=true looks "
             "up anyway).",
             {"type": "object", "required": ["phone"],
              "properties": {"phone": {"type": "string", "description": "+7…, 7…, 8…; spaces and dashes are fine"},
                             "first_name": {"type": "string", "description": "name for the address book (TG)"},
                             "last_name": {"type": "string"},
                             "refresh": {"type": "boolean", "default": False}}},
             "contact_by_phone", rest("POST", "/contacts/by-phone", body=ALL), "rw"),
        Tool("create_upload", "Get a URL to PUT a local file to (curl -T), then pass upload_id to send_file / "
             "send_photo / send_voice / send_album. Valid 15 minutes, ≤ 50 MB.",
             {"type": "object", "properties": {}}, "send_file", create_upload, "rw"),
    ])


def _widened(tools: list[Tool]) -> list[Tool]:
    for t in tools:
        t.schema = widen_ids(t.schema)
    return tools


# Admin rights of MAX as its app shows them (plan 2026-10-22 Р5 §1.1, Р-52): name → (what it gives, where).
ADMIN_RIGHTS = {
    "read_all_messages": ("read messages — a bot gets the group's messages only with it", "group, channel"),
    "delete_message": ("delete messages (group) / others' posts (channel)", "group, channel"),
    "pin_message": ("pin messages", "group, channel"),
    "add_remove_member": ("add and remove members, join requests", "group, channel"),
    "change_chat_info": ("photo, title, description", "group, channel"),
    "edit_link": ("renew the invite link, extra links; needs add_remove_member", "group, channel"),
    "add_admin": ("appoint and remove admins", "group, channel"),
    "call": ("call when only admins may call", "group"),
    "post_message": ("write posts", "channel"),
    "edit_message": ("edit others' posts", "channel"),
    "view_stats": ("channel statistics", "channel"),
}
SET_ADMIN_DOC = (
    "Make a member or a bot an admin of the group or channel, or replace an admin's rights (previous_mask). "
    "A bot: add it with add_group_members, then set_group_admin without permissions: a bot gets "
    "read_all_messages + delete + pin + members + info + call (123), like in the MAX app; a person gets the "
    "same + edit_link (251). Channel defaults: a bot 1850, a person 4026. A bot in a group needs "
    "read_all_messages for delete_message / pin_message. Rights: "
    + "; ".join(f"{name} — {what} ({where})" for name, (what, where) in ADMIN_RIGHTS.items())
    + ". The answer: permissions, mask, is_bot."
)


def _max_group_tools() -> list[Tool]:
    """MAX groups and channels (plan 2026-10-19 Р3; Р4 Р-40): routes only in the MAX router —
    visible() hides them from TG, the writes from ro."""
    cid = {"chat_id": {"type": "integer", "description": "group or channel chat id"}}
    ids = {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 20,
           "description": "user ids, ≤ 20"}
    obj = lambda props, req=("chat_id",): {"type": "object", "properties": props, "required": list(req)}  # noqa: E731
    path = lambda a: {k: v for k, v in a.items() if k != "chat_id" and v is not None}  # noqa: E731
    perms = {"type": "array", "items": {"type": "string", "enum": list(ADMIN_RIGHTS)},
             "description": "rights by name (see the table in the tool description); omit for the MAX app's "
                            "default"}
    flag = {"type": "boolean"}
    return _widened([
        Tool("create_group", "Create a MAX group (chat_type=group, default) or channel (chat_type=channel) with "
             "this account as owner; user_ids are invited at once. Limits: 3 groups/channels and 30 invited "
             "people a day.",
             obj({"title": {"type": "string"}, "user_ids": {**ids, "minItems": 0},
                  "notify": {"type": "boolean", "default": True},
                  "chat_type": {"type": "string", "enum": ["group", "channel"], "default": "group"}}, ["title"]),
             "create_group", rest("POST", "/groups", body=ALL)),
        Tool("add_group_members", "Add people or a bot to a group or channel (each counts in the 30 invites a "
             "day). A bot: add it here, then set_group_admin without permissions.",
             obj({**cid, "user_ids": ids, "show_history": {"type": "boolean", "default": True},
                  "start_payload": {"type": "string", "maxLength": 512,
                                    "description": "a bot only: the payload of its /start (not checked live)"}},
                 ["chat_id", "user_ids"]),
             "add_group_members", rest("POST", "/groups/{chat_id}/members", body=path)),
        Tool("remove_group_members", "Remove people from a group or channel. delete_messages=true deletes their "
             "messages in the chat too (deleted_message_ids); by default the messages stay.",
             obj({**cid, "user_ids": ids, "delete_messages": {"type": "boolean", "default": False}},
                 ["chat_id", "user_ids"]),
             "remove_group_members", rest("POST", "/groups/{chat_id}/members/remove", body=path)),
        Tool("set_group_admin", SET_ADMIN_DOC,
             obj({**cid, "user_id": {"type": "integer"}, "permissions": perms,
                  "permissions_mask": {"type": "integer", "minimum": 1, "maximum": 4095,
                                       "description": "exact MAX bit mask instead of permissions (not both)"}},
                 ["chat_id", "user_id"]),
             "set_group_admin", rest("POST", "/groups/{chat_id}/admins", body=path)),
        Tool("remove_group_admin", "Take admin rights from a member.",
             obj({**cid, "user_id": {"type": "integer"}}, ["chat_id", "user_id"]),
             "remove_group_admin", rest("POST", "/groups/{chat_id}/admins/remove", body=path)),
        Tool("update_group_profile", "Change the title and/or description of a group or channel.",
             obj({**cid, "title": {"type": "string"}, "description": {"type": "string"}}),
             "update_group_profile", rest("PATCH", "/groups/{chat_id}", body=path)),
        Tool("update_group_settings", "Change settings of a group or channel (only the ones passed). "
             "join_request: people join by request (approve_join_requests) — a private channel only (MAX has no "
             "such option for groups); disable_forward: messages cannot be "
             "forwarded; comments: a channel only.",
             obj({**cid, "all_can_pin_message": flag, "only_owner_can_change_icon_title": flag,
                  "only_admin_can_add_member": flag, "only_admin_can_call": flag,
                  "members_can_see_private_link": flag, "join_request": flag, "disable_forward": flag,
                  "comments": flag}),
             "update_group_settings", rest("PATCH", "/groups/{chat_id}/settings", body=path)),
        Tool("get_invite_link", "The current invite link of a group or channel (null if it has none).", obj(cid),
             "get_invite_link", rest("GET", "/groups/{chat_id}/invite-link")),
        Tool("reset_invite_link", "Make a new invite link; the previous one stops working.", obj(cid),
             "reset_invite_link", rest("POST", "/groups/{chat_id}/invite-link")),
        Tool("list_join_requests", "People asking to join: only private channels with join requests on "
             "(update_group_settings join_request); for groups always empty.",
             obj({**cid, "limit": {"type": "integer", "default": 100, "maximum": 100}}),
             "list_join_requests", rest("GET", "/groups/{chat_id}/join-requests", query=path)),
        Tool("approve_join_requests", "Let people who asked in (private channels with join requests on; groups "
             "have no requests).",
             obj({**cid, "user_ids": ids, "show_history": {"type": "boolean", "default": True}},
                 ["chat_id", "user_ids"]),
             "approve_join_requests", rest("POST", "/groups/{chat_id}/join-requests/approve", body=path)),
        Tool("decline_join_requests", "Decline join requests (private channels with join requests on).", obj({**cid, "user_ids": ids}, ["chat_id", "user_ids"]),
             "decline_join_requests", rest("POST", "/groups/{chat_id}/join-requests/decline", body=path)),
        Tool("delete_group", "Delete a group: owner only; confirm_title must equal the current title. "
             "for_all=true deletes it for every member. A channel only while the account is its one member.",
             obj({**cid, "confirm_title": {"type": "string"}, "for_all": {"type": "boolean", "default": True}},
                 ["chat_id", "confirm_title"]),
             "delete_group", rest("POST", "/groups/{chat_id}/delete", body=path)),
        Tool("transfer_group_ownership", "Make an admin the owner of the group or channel. IRREVERSIBLE: only the "
             "new owner can give it back. The account must be the owner; confirm_title = the current title, "
             "confirm_user_id = user_id; user_id must be an admin already (set_group_admin first; MAX itself asks it "
             "for a group only, for a channel it is this service's rule).",
             obj({**cid, "user_id": {"type": "integer"}, "confirm_title": {"type": "string"},
                  "confirm_user_id": {"type": "integer"}}, ["chat_id", "user_id", "confirm_title", "confirm_user_id"]),
             "transfer_group_ownership", rest("POST", "/groups/{chat_id}/owner", body=path)),
        # list / create / revoke_invite_links: not offered (Р5.1 Р-79) — MAX drops the session on 221-223
        # while its additional-links flag is off; the REST routes answer 501
    ])


def _max_r4_tools() -> list[Tool]:
    """MAX release 4 without a TG counterpart (plan 2026-10-19 Р4 Р-32): routes only in
    the MAX router; the TG ones it serves (pin_message, join_chat, ...) need no entry here."""
    obj = lambda props, req=(): {"type": "object", "properties": props, "required": list(req)}  # noqa: E731
    path = lambda *keys: (lambda a: {k: v for k, v in a.items() if k not in keys and v is not None})  # noqa: E731
    msg = {"chat_id": {"type": "integer"}, "message_id": {"type": "integer"}}
    note_schema = obj({"chat_id": {"type": "integer"}, "reply_to_message_id": {"type": "integer"},
                       "duration_ms": {"type": "integer", "description": "milliseconds; default: read from the file"},
                       **FILE_SOURCE}, ["chat_id"])
    chat_ids = {"type": "array", "items": {"type": "integer"}, "maxItems": 100,
                "description": "chats of this account, ≤ 100"}
    folder = {"folder_id": {"type": "string"}}
    return _widened([
        Tool("send_video_note", "Send a round video message (.mp4, square, up to 60 s). The file comes from "
             "file_url or upload_id (create_upload).", note_schema, "send_video_note", send_video_note),
        Tool("vote_poll", "Vote in the poll of a message: answer_ids from the message's poll.answers "
             "(get_message / list_messages). Returns the poll's current counts.",
             obj({**msg, "answer_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 10},
                  "poll_id": {"type": "integer"}}, ["chat_id", "message_id", "answer_ids"]),
             "vote_poll", rest("POST", "/messages/{chat_id}/{message_id}/vote",
                               body=path("chat_id", "message_id"))),
        Tool("get_reactions", "Reactions of a message: counts per reaction, this account's own, and who "
             "reacted (users: user_id + reaction, up to 100; users null + users_error if MAX did not say).",
             obj(msg, ["chat_id", "message_id"]),
             "get_reactions", rest("GET", "/messages/{chat_id}/{message_id}/reactions")),
        Tool("import_contacts", "Add up to 20 numbers to the address book and the contacts base in one call; "
             "per number: found, user_id, person_id, chat_id. Limit 50 numbers/day; known numbers answer from "
             "the base (refresh=true sends them anyway).",
             obj({"contacts": {"type": "array", "minItems": 1, "maxItems": 20, "items": {
                     "type": "object", "required": ["phone", "first_name"],
                     "properties": {"phone": {"type": "string"}, "first_name": {"type": "string"},
                                    "last_name": {"type": "string"}}}},
                  "refresh": {"type": "boolean", "default": False}}, ["contacts"]),
             "import_contacts", rest("POST", "/contacts/import", body=ALL)),
        Tool("remove_contact", "Remove a user from this account's MAX address book (the person stays in the "
             "contacts base).", obj({"user_id": {"type": "integer"}}, ["user_id"]),
             "remove_contact", rest("POST", "/contacts/{user_id}/remove")),
        Tool("delete_dialog", "Delete a private dialog: confirm_user_id must be the peer; for_all=false (default) "
             "deletes it for this account only. Groups: delete_group.",
             obj({"chat_id": {"type": "integer"}, "confirm_user_id": {"type": "integer"},
                  "for_all": {"type": "boolean", "default": False}}, ["chat_id", "confirm_user_id"]),
             "delete_dialog", rest("POST", "/chats/{chat_id}/delete", body=path("chat_id"))),
        Tool("list_folders", "Chat folders of the account: folder_id, title, chat_ids.", obj({}),
             "list_folders", rest("GET", "/folders")),
        Tool("create_folder", "Create a chat folder.",
             obj({"title": {"type": "string"}, "chat_ids": chat_ids}, ["title"]),
             "create_folder", rest("POST", "/folders", body=ALL)),
        Tool("update_folder", "Rename a folder and/or replace its chats; what is not passed stays.",
             obj({**folder, "title": {"type": "string"}, "chat_ids": chat_ids}, ["folder_id"]),
             "update_folder", rest("PATCH", "/folders/{folder_id}", body=path("folder_id"))),
        Tool("delete_folder", "Delete a folder (its chats stay): confirm_title must equal its current title.",
             obj({**folder, "confirm_title": {"type": "string"}}, ["folder_id", "confirm_title"]),
             "delete_folder", rest("POST", "/folders/{folder_id}/delete", body=path("folder_id"))),
    ])


def _max_r5_tools() -> list[Tool]:
    """MAX release 5 without a TG counterpart (plan 2026-10-22 Р5 Р-64…Р-71, Р-77); block_user /
    unblock_user are the TG tools, served by the MAX router now."""
    obj = lambda props, req=(): {"type": "object", "properties": props, "required": list(req)}  # noqa: E731
    path = lambda *keys: (lambda a: {k: v for k, v in a.items() if k not in keys and v is not None})  # noqa: E731
    msg = {"chat_id": {"type": "integer"}, "message_id": {"type": "integer"}}
    return _widened([
        Tool("press_button", "Press a button of a bot's keyboard (message.keyboard from get_message / "
             "list_messages): row and column from 0. A button may trigger an action in the bot (an order, a "
             "subscription): read the message first. CALLBACK, CHAT and MESSAGE buttons only; a LINK button "
             "answers with its url to open; contact, location, mini app and clipboard buttons are refused.",
             obj({**msg, "row": {"type": "integer", "minimum": 0}, "column": {"type": "integer", "minimum": 0}},
                 ["chat_id", "message_id", "row", "column"]),
             "press_button", rest("POST", "/messages/{chat_id}/{message_id}/press",
                                  body=path("chat_id", "message_id"))),
        Tool("start_bot", "Start a MAX bot (/start), optionally with the payload of a max.ru/<bot>?start=<payload> "
             "link; the bot's answer comes to the dialog with it (list_messages). Counts as a message sent "
             "(a new dialog the first time).",
             obj({"bot_id": {"type": "integer"}, "start_payload": {"type": "string", "maxLength": 512}},
                 ["bot_id"]),
             "start_bot", rest("POST", "/bots/start", body=ALL)),
        Tool("get_bot_info", "A bot's commands, profile and start message (bot_id), or the bots of a chat with "
             "their commands (chat_id). One of the two.",
             obj({"bot_id": {"type": "integer"}, "chat_id": {"type": "integer"}}),
             "get_bot_info", rest("GET", "/bots/info", query=ALL)),
        Tool("get_message_link", "A link to a message of a group or channel (https://max.ru/...); a private chat's "
             "link opens for its members only; dialogs have none.", obj(msg, ["chat_id", "message_id"]),
             "get_message_link", rest("GET", "/messages/{chat_id}/{message_id}/link")),
        Tool("get_poll_voters", "Who voted for an answer of a poll (answer_id from the message's poll.answers); "
             "not for an anonymous poll.",
             obj({**msg, "answer_id": {"type": "integer"},
                  "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 100}},
                 ["chat_id", "message_id", "answer_id"]),
             "get_poll_voters", rest("GET", "/messages/{chat_id}/{message_id}/voters",
                                     query=path("chat_id", "message_id"))),
        Tool("transcribe_voice", "MAX's text of a voice message or video note: status done (text), processing "
             "(ask again later) or failed. index: attach_index if the message has several.",
             obj({**msg, "index": {"type": "integer", "minimum": 0}}, ["chat_id", "message_id"]),
             "transcribe_voice", rest("POST", "/messages/{chat_id}/{message_id}/transcribe",
                                      body=path("chat_id", "message_id"))),
        Tool("search_chat_messages", "Search one chat on the MAX server (also messages older than our copy); "
             "found messages are stored and readable by get_message. global_search searches our copy of all "
             "chats.",
             obj({"chat_id": {"type": "integer"}, "query": {"type": "string"},
                  "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 30},
                  "marker": {"type": "integer", "description": "next_marker of the previous page"}},
                 ["chat_id", "query"]),
             "search_chat_messages", rest("GET", "/messages/{chat_id}/search", query=path("chat_id"))),
        Tool("get_post_stats", "Views and forwards of channel posts; reading does not count a view. 1-100 "
             "message ids; a post MAX did not answer for is in missing.",
             obj({"chat_id": {"type": "integer"},
                  "message_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 100}},
                 ["chat_id", "message_ids"]),
             "get_post_stats", rest("GET", "/messages/{chat_id}/stats", query=path("chat_id"))),
    ])


ACCOUNT_TOOLS = _account_tools() + _max_group_tools() + _max_r4_tools() + _max_r5_tools()


def _max_variants() -> dict[str, Tool]:
    """Shared tools as a MAX account sees them (review 17.2 R17.2-1): the TG schema of the
    stdio wrapper stays as it is, a MAX key gets the MAX facts."""
    poll = next(t for t in ACCOUNT_TOOLS if t.name == "send_poll")
    schema = copy.deepcopy(poll.schema)
    schema["properties"]["is_anonymous"] = {
        "type": "boolean", "default": False,
        "description": "MAX: false only — MAX rejects an anonymous poll (answer 501)",
    }
    members = next(t for t in ACCOUNT_TOOLS if t.name == "get_chat_members")
    members_schema = copy.deepcopy(members.schema)
    members_schema["properties"]["search"] = {"type": "string", "description": "MAX searches its members itself"}
    members_schema["properties"]["role"] = {"type": "string", "enum": ["all", "admins"], "default": "all",
                                            "description": "admins: only admins, with their rights (mask)"}
    keyboard = (" MAX: a bot's message has keyboard {callback_id, buttons: [[{text, type, url?, payload?}]]}: "
                "press with press_button(row, column).")
    by_name = {t.name: t for t in ACCOUNT_TOOLS}
    send = by_name["send_message"]
    send_schema = copy.deepcopy(send.schema)
    send_schema["properties"]["schedule_date"] = {
        **send_schema["properties"]["schedule_date"],
        "description": "ISO datetime or unix time, now + 60 s … 365 days. MAX: text only (files, albums and "
                       "polls are not scheduled); list_scheduled / cancel_scheduled / send_scheduled_now manage it",
    }
    blocked = " MAX: the answer has blocked — whether the user is in the account's blocked list after it."
    as_is = (' MAX: text is sent as is; pass parse_mode="markdown" or "html" (b, i, u, s, code, pre, '
             'a href, blockquote, br) for formatting.')
    texts = {name: dataclasses.replace(by_name[name], description=by_name[name].description + as_is)
             for name in ("edit_message", "send_photo", "send_file", "send_voice", "send_album")}
    rights = by_name["get_my_rights"]
    texts["get_my_rights"] = dataclasses.replace(rights, description=rights.description + (
        " MAX: mask and permissions are the MAX app's admin bits; null — MAX did not tell the rights "
        "(then the can_* flags are not known either)."))
    return {**texts,
            "send_message": dataclasses.replace(send, schema=send_schema, description=send.description + as_is),
            "list_messages": dataclasses.replace(by_name["list_messages"],
                                                 description=by_name["list_messages"].description + keyboard),
            "get_message": dataclasses.replace(by_name["get_message"],
                                               description=by_name["get_message"].description + keyboard),
            "block_user": dataclasses.replace(by_name["block_user"],
                                              description=by_name["block_user"].description + blocked),
            "unblock_user": dataclasses.replace(by_name["unblock_user"],
                                                description=by_name["unblock_user"].description + blocked),
            "send_poll": dataclasses.replace(poll, schema=schema),
            "get_chat_members": dataclasses.replace(
                members, schema=members_schema,
                description=members.description + " MAX: an admin's item has permissions, mask, inviter_id.")}


MAX_VARIANTS = _max_variants()
RW_TOOLS = _rw_tools()
OWNER_TOOLS = _owner_tools()
BY_NAME = {t.name: t for t in (*ACCOUNT_TOOLS, *RW_TOOLS, *OWNER_TOOLS)}


@functools.cache
def route_names(platform: str) -> frozenset[str]:
    """Names of the REST routes the platform serves (MAX: its internal router)."""
    return frozenset(getattr(r, "name", None) for _, r in invoke._routes(platform)) - {None}


def supported(tool: Tool, platform: str) -> bool:
    """The platform has the tool's route; otherwise the call would answer 501 (QA D-3)."""
    return tool.route in route_names(platform)


def visible(caller: Caller) -> list[Tool]:
    """Owner: contacts only. ro account: no messenger writes. rw: everything + contacts reach.

    Accounts see only the tools their platform serves (a MAX account does not
    get send_album, reachable_contacts, ...).
    """
    if caller.scope == "owner":
        return OWNER_TOOLS
    if caller.mode != "rw":
        tools = [t for t in ACCOUNT_TOOLS if not t.is_write]
    else:
        tools = [*ACCOUNT_TOOLS, *RW_TOOLS]
    variants = MAX_VARIANTS if caller.platform == "max" else {}
    off = SCHEDULED_TOOLS if caller.platform == "max" and not max_settings.scheduled_enabled else frozenset()
    return [variants.get(t.name, t) for t in tools if supported(t, caller.platform) and t.name not in off]
