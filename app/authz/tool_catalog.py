"""Tool catalog: maps FastAPI route names to read/write classification.

Route names come from the Python function names in app/api/*.py routers.
They match FastAPI's route.name attribute.

Classification:
  READ_ONLY_TOOLS   — returns data only, no state mutation anywhere.
  WRITE_TG_TOOLS    — sends data to Telegram or mutates Telegram state.
                      Blocked on ro accounts.
  WRITE_DB_TOOLS    — writes ONLY to our local database, not to Telegram
                      (snapshots, imports). Allowed on ro accounts.
  MANAGE_SESSION_TOOLS — session lifecycle (login, code, logout, import).
                      Semantically management, not Telegram writes.
                      Allowed on ro accounts so that a ro account can be
                      authorised in the first place.

Authz decision for ro accounts:
  - WRITE_TG_TOOLS  → 403
  - WRITE_DB_TOOLS  → pass
  - MANAGE_SESSION_TOOLS → pass
  - READ_ONLY_TOOLS → pass
"""

# ---------------------------------------------------------------------------
# Category 1: pure Telegram writes — blocked on ro
# ---------------------------------------------------------------------------
WRITE_TG_TOOLS: frozenset[str] = frozenset({
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
})

# ---------------------------------------------------------------------------
# Category 2: writes only to our local DB — allowed on ro
# ---------------------------------------------------------------------------
WRITE_DB_TOOLS: frozenset[str] = frozenset({
    "snapshot_chat_members",    # reads TG members, writes to our DB
    "snapshot_import_members",  # manual import, writes to our DB
    "sync_chats",               # iter_dialogs (TG read), writes to our DB
    "trigger_backfill",         # iter_messages (TG read), writes to our DB
})

# ---------------------------------------------------------------------------
# Category 3: session management — allowed on ro
# ---------------------------------------------------------------------------
MANAGE_SESSION_TOOLS: frozenset[str] = frozenset({
    "auth_login",
    "auth_code",
    "auth_session",
    "auth_logout",
})

# ---------------------------------------------------------------------------
# Category 4: reads — always allowed
# ---------------------------------------------------------------------------
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
    "resolve_by_id",         # bulk tg_user_id resolution (new in PR #2)
    # search
    "search_global",
    # sync reads
    "sync_status",
    # stream
    "stream_messages",
    # snapshots reads
    "list_chat_snapshots",
    "get_snapshot_members",
})

# ---------------------------------------------------------------------------
# Convenience alias kept for backwards-compatibility with middleware imports
# ---------------------------------------------------------------------------
# WRITE_TOOLS used by middleware to build ALL_TOOLS set.  We expose all three
# "write-ish" categories under this name so unknown-tool detection still works.
WRITE_TOOLS: frozenset[str] = WRITE_TG_TOOLS | WRITE_DB_TOOLS | MANAGE_SESSION_TOOLS

# All known tool names (for validation)
ALL_TOOLS: frozenset[str] = READ_ONLY_TOOLS | WRITE_TOOLS


def tool_is_write(tool_name: str) -> bool:
    """Return True if tool_name is a Telegram write operation (blocks ro).

    Only WRITE_TG_TOOLS returns True.
    WRITE_DB_TOOLS and MANAGE_SESSION_TOOLS return False (allowed on ro).

    Raises KeyError for unknown tools so missing catalog entries are caught
    at registration time rather than silently passed through.
    """
    if tool_name in WRITE_TG_TOOLS:
        return True
    if tool_name in READ_ONLY_TOOLS or tool_name in WRITE_DB_TOOLS or tool_name in MANAGE_SESSION_TOOLS:
        return False
    raise KeyError(
        f"Unknown tool: '{tool_name}'. Update app/authz/tool_catalog.py"
    )


def tool_allowed_on_ro(tool_name: str) -> bool:
    """Return True if the tool is allowed on ro accounts.

    Convenience helper for documentation / policy UI; not used by middleware
    directly (middleware calls tool_is_write).
    """
    return not tool_is_write(tool_name)
