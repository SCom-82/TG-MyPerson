"""Tool catalog: maps FastAPI route names to read/write classification.

Route names come from the Python function names in app/api/*.py routers.
They match FastAPI's route.name attribute.

Classification rules:
  - READ: returns data, no state mutation in Telegram.
  - WRITE: sends messages, mutates chat/account state, modifies Telegram data.

Note: auth_* routes are write (they mutate session state), except auth_me and
auth_status which are pure reads.

sync_status is read. trigger_backfill and sync_chats are write (they call
Telegram APIs and mutate DB state).
"""

READ_ONLY_TOOLS: frozenset[str] = frozenset({
    # auth reads
    "auth_me",
    "auth_status",
    # chat reads
    "list_chats",
    "get_chat_detail",
    "list_members",
    "get_my_rights",
    "resolve_chat_endpoint",
    # message reads
    "list_messages",
    "get_single_message",
    "list_scheduled",
    "download_media",
    # user reads
    "list_users",
    "list_contacts",
    "resolve_user",
    # search
    "search_global",
    # sync reads
    "sync_status",
    # stream
    "stream_messages",
})

WRITE_TOOLS: frozenset[str] = frozenset({
    # auth writes (mutate session state)
    "auth_login",
    "auth_code",
    "auth_session",
    "auth_logout",
    # message writes
    "send_message",
    "send_file",
    "send_voice",
    "send_album",
    "send_poll",
    "forward_message",
    "edit_message",
    "delete_message",
    "pin_message",
    "unpin_message",
    "react_to_message",
    "cancel_scheduled",
    "send_scheduled_now",
    # chat writes
    "join_chat_endpoint",
    "leave_chat_endpoint",
    "archive_chat",
    "update_chat_settings",
    "mark_read",
    # user writes
    "block_user",
    "unblock_user",
    # sync writes (call Telegram APIs, mutate DB)
    "sync_chats",
    "trigger_backfill",
})

# All known tool names (for validation)
ALL_TOOLS: frozenset[str] = READ_ONLY_TOOLS | WRITE_TOOLS


def tool_is_write(tool_name: str) -> bool:
    """Return True if tool_name is a write operation.

    Raises KeyError for unknown tools so missing catalog entries are caught
    at registration time rather than silently passed through.
    """
    if tool_name in WRITE_TOOLS:
        return True
    if tool_name in READ_ONLY_TOOLS:
        return False
    raise KeyError(
        f"Unknown tool: '{tool_name}'. Update app/authz/tool_catalog.py"
    )
