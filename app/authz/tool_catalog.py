"""Tool catalog: maps FastAPI route names to read/write classification.

Route names come from the Python function names in app/api/*.py routers.
They match FastAPI's route.name attribute.

Classification:
  READ_ONLY_TOOLS   — returns data only, no state mutation anywhere.
  WRITE_MESSENGER_TOOLS — sends data to an external messenger (Telegram, MAX)
                      or mutates its state. Blocked on ro accounts.
                      WRITE_TG_TOOLS is the historical name, kept as an alias
                      of the same object.
  WRITE_DB_TOOLS    — writes ONLY to our local database, not to Telegram
                      (snapshots, imports). Allowed on ro accounts.
  MANAGE_SESSION_TOOLS — session lifecycle (login, code, logout, import).
                      Semantically management, not Telegram writes.
                      Allowed on ro accounts so that a ro account can be
                      authorised in the first place.

Authz decision for ro accounts:
  - WRITE_MESSENGER_TOOLS → 403
  - WRITE_DB_TOOLS  → pass
  - MANAGE_SESSION_TOOLS → pass
  - READ_ONLY_TOOLS → pass
"""

# MAX groups (plan 2026-10-19 Р-20…Р-22): routes only in the MAX router.
GROUP_WRITE_TOOLS = (
    "create_group", "add_group_members", "remove_group_members", "set_group_admin", "remove_group_admin",
    "update_group_profile", "update_group_settings", "reset_invite_link", "approve_join_requests",
    "decline_join_requests", "delete_group",
)
GROUP_READ_TOOLS = ("get_invite_link", "list_join_requests")
# MAX release 4 without a TG counterpart (plan 2026-10-19 Р4 Р-32): MAX router only.
MAX_R4_WRITE_TOOLS = (
    "send_video_note", "vote_poll", "import_contacts", "remove_contact", "delete_dialog",
    "create_folder", "update_folder", "delete_folder",
)
MAX_R4_READ_TOOLS = ("get_reactions", "list_folders")
# MAX release 5 without a TG counterpart (plan 2026-10-22 Р5 Р-61, Р-62): MAX router only.
MAX_R5_WRITE_TOOLS = ("transfer_group_ownership", "create_invite_link", "revoke_invite_links")
MAX_R5_READ_TOOLS = ("list_invite_links",)

# ---------------------------------------------------------------------------
# Category 1: writes to the external messenger — blocked on ro
# ---------------------------------------------------------------------------
WRITE_MESSENGER_TOOLS: frozenset[str] = frozenset({
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
    # contacts (account isolation §6.4): write to a person from the shared base
    "send_to_person",
    # find by phone + add to the address book (plan 2026-10-19 Р-13): ro → 403
    "contact_by_phone",
    # MAX groups (plan 2026-10-19 Р-22): rw only
    *GROUP_WRITE_TOOLS,
    # MAX release 4 (plan 2026-10-19 Р4): rw only
    *MAX_R4_WRITE_TOOLS,
    # MAX release 5 (plan 2026-10-22 Р5): rw only
    *MAX_R5_WRITE_TOOLS,
})

# Backward-compatible name: same object, so `in` checks and imports keep working.
WRITE_TG_TOOLS: frozenset[str] = WRITE_MESSENGER_TOOLS

# ---------------------------------------------------------------------------
# Category 2: writes only to our local DB — allowed on ro
# ---------------------------------------------------------------------------
WRITE_DB_TOOLS: frozenset[str] = frozenset({
    "snapshot_chat_members",    # reads TG members, writes to our DB
    "snapshot_import_members",  # manual import, writes to our DB
    "sync_chats",               # iter_dialogs (TG read), writes to our DB
    "trigger_backfill",         # iter_messages (TG read), writes to our DB
    # MCP file upload (account isolation §9.2): a temp file on our side
    "upload_file",
    # contacts, owner key (account isolation §6.4): our DB only
    "update_person",
    "merge_people",
    "split_person",
})

# ---------------------------------------------------------------------------
# Category 3: session management — allowed on ro
# ---------------------------------------------------------------------------
MANAGE_SESSION_TOOLS: frozenset[str] = frozenset({
    "auth_login",
    "auth_code",
    "auth_session",
    "auth_logout",
    # MAX-only: QR login (WebClient)
    "auth_qr_start",
    "auth_qr_status",
})

# ---------------------------------------------------------------------------
# Tools that exist only for platform 'max'. For a Telegram alias they answer
# 501 (platform_dispatch middleware), see API spec §2.3.
# ---------------------------------------------------------------------------
MAX_ONLY_TOOLS: frozenset[str] = frozenset({
    "auth_qr_start",
    "auth_qr_status",
    # groups (plan 2026-10-19 Р-20, Р-21): /api/v1/groups/... only in the MAX router
    *GROUP_WRITE_TOOLS,
    *GROUP_READ_TOOLS,
    # release 4 (plan 2026-10-19 Р4 Р-32)
    *MAX_R4_WRITE_TOOLS,
    *MAX_R4_READ_TOOLS,
    # release 5 (plan 2026-10-22 Р5)
    *MAX_R5_WRITE_TOOLS,
    *MAX_R5_READ_TOOLS,
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
    # MCP signed file link (account isolation §9.2)
    "download_file",
    # MCP tools of the caller's account, for the stdio wrapper (QA D-3)
    "available_tools",
    # contacts (account isolation §6.4): owner key / rw account
    "find_person",
    "get_person",
    "person_messages",
    "person_chats",
    "reachable_contacts",
    # MAX groups, reads (plan 2026-10-19 Р-22): ro sees them
    *GROUP_READ_TOOLS,
    # MAX release 4, reads (plan 2026-10-19 Р4 Р-47)
    *MAX_R4_READ_TOOLS,
    # MAX release 5, reads (plan 2026-10-22 Р5)
    *MAX_R5_READ_TOOLS,
})

# ---------------------------------------------------------------------------
# Convenience alias kept for backwards-compatibility with middleware imports
# ---------------------------------------------------------------------------
# WRITE_TOOLS used by middleware to build ALL_TOOLS set.  We expose all three
# "write-ish" categories under this name so unknown-tool detection still works.
WRITE_TOOLS: frozenset[str] = WRITE_MESSENGER_TOOLS | WRITE_DB_TOOLS | MANAGE_SESSION_TOOLS

# All known tool names (for validation)
ALL_TOOLS: frozenset[str] = READ_ONLY_TOOLS | WRITE_TOOLS


def tool_is_write(tool_name: str) -> bool:
    """Return True if tool_name is a messenger write operation (blocks ro).

    Only WRITE_MESSENGER_TOOLS returns True.
    WRITE_DB_TOOLS and MANAGE_SESSION_TOOLS return False (allowed on ro).

    Raises KeyError for unknown tools so missing catalog entries are caught
    at registration time rather than silently passed through.
    """
    if tool_name in WRITE_MESSENGER_TOOLS:
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
