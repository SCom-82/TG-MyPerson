"""Utilities for handling Telegram sender entities safely.

Telegram senders can be User, Channel, or Chat objects.  Only User has
first_name/last_name; Channel has title; Chat has title too.
Calling sender.first_name on a Channel raises AttributeError — this module
provides safe helpers.
"""


def sender_display_name(entity) -> str:
    """Return a human-readable display name for any Telegram entity.

    - User → "First Last" (or "First", or "(no name)")
    - Channel / Chat → entity.title (or "(unknown)")
    """
    if entity is None:
        return "(unknown)"
    if hasattr(entity, "first_name"):
        # User or similar — has first_name attribute
        parts = [
            (entity.first_name or "").strip(),
            (entity.last_name or "").strip(),
        ]
        name = " ".join(p for p in parts if p)
        return name or "(no name)"
    # Channel, Chat, etc. — use title
    return (getattr(entity, "title", None) or "").strip() or "(unknown)"


def is_user_entity(entity) -> bool:
    """Return True if the entity is a User (not Channel/Chat)."""
    try:
        from telethon.tl.types import User
        return isinstance(entity, User)
    except ImportError:
        # Fallback: check for first_name attribute (User-specific)
        return hasattr(entity, "first_name") and hasattr(entity, "username")
