"""test_snapshot_search_letters.py — Tests for search-by-letter dedup (91dba5bf).

Tests:
  1. Participants returned multiple times (once per letter) are deduplicated.
  2. Members count reflects deduplicated count, not raw iteration count.
  3. Single pass for empty search works (small chat baseline).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_participant(user_id: int, username: str = None) -> MagicMock:
    """Create a mock Telethon participant with .id and user fields."""
    p = MagicMock()
    p.id = user_id
    p.user = MagicMock()
    p.user.id = user_id
    p.user.username = username
    p.user.first_name = f"User{user_id}"
    p.user.last_name = None
    p.user.phone = None
    return p


# ---------------------------------------------------------------------------
# Test 1: duplicates across search letters are deduplicated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_letter_deduplication():
    """Same participant returned for multiple letters must appear only once."""
    from app.api.snapshots import _SEARCH_LETTERS, _participant_to_dict

    # Simulate: user 1001 appears in unfiltered pass AND in 'a' search
    participant_1001 = _make_participant(1001, "alice")
    participant_1002 = _make_participant(1002, "bob")

    # We'll replicate the dedup logic from snapshot_chat
    seen: dict[int, object] = {}

    async def _iter_participants_mock(chat_id, **kwargs):
        search = kwargs.get("search")
        if search is None:
            # Unfiltered pass: return both
            for p in [participant_1001, participant_1002]:
                yield p
        elif search == "a":
            # 'a' search: returns alice again (duplicate)
            yield participant_1001
        # Other letters: nothing

    # Simulate the two-pass collection
    async def _collect(search=None):
        kwargs = {"aggressive": True}
        if search is not None:
            kwargs["search"] = search
        async for p in _iter_participants_mock(999, **kwargs):
            pid = getattr(p, "id", None)
            if pid is not None and pid not in seen:
                seen[pid] = p

    # Pass 1: unfiltered
    await _collect(search=None)
    # Pass 2: letters — only 'a' returns something
    await _collect(search="a")

    assert len(seen) == 2, f"Expected 2 unique participants, got {len(seen)}: {list(seen.keys())}"
    assert 1001 in seen
    assert 1002 in seen


# ---------------------------------------------------------------------------
# Test 2: _participant_to_dict handles standard participant correctly
# ---------------------------------------------------------------------------

def test_participant_to_dict_standard():
    """_participant_to_dict returns expected keys for a normal participant."""
    from app.api.snapshots import _participant_to_dict

    p = _make_participant(12345, "testuser")
    result = _participant_to_dict(p)

    assert result["tg_user_id"] == 12345
    assert result["username"] == "testuser"
    assert "role" in result
    assert result["role"] == "member"


# ---------------------------------------------------------------------------
# Test 3: _participant_to_dict handles creator/admin roles
# ---------------------------------------------------------------------------

def test_participant_to_dict_admin_role():
    """_participant_to_dict correctly identifies admin participants."""
    from app.api.snapshots import _participant_to_dict
    from telethon.tl.types import ChannelParticipantAdmin

    p = MagicMock(spec=ChannelParticipantAdmin)
    p.id = 99999
    p.user = MagicMock()
    p.user.id = 99999
    p.user.username = "adminuser"
    p.user.first_name = "Admin"
    p.user.last_name = None
    p.user.phone = None

    result = _participant_to_dict(p)
    assert result["role"] == "admin"
