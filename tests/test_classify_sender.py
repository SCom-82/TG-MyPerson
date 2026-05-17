"""test_classify_sender.py — Unit tests for classify_sender() (SPEC §8, tests 1-4).

Tests:
  1. classify_sender(None)   -> (None, None)
  2. classify_sender(-1002175364727) -> (None, -1002175364727)  broadcast/anon-admin
  3. classify_sender(123456789)      -> (123456789, None)        user
  4. classify_sender(-1)             -> (None, -1)               boundary negative
"""

import pytest
from app.utils.sender import classify_sender


def test_classify_sender_none():
    """None sender_id (service message) -> (None, None)."""
    assert classify_sender(None) == (None, None)


def test_classify_sender_negative_large():
    """Broadcast channel sender_id -> (None, sender_id)."""
    assert classify_sender(-1002175364727) == (None, -1002175364727)


def test_classify_sender_positive_user():
    """User sender_id -> (sender_id, None)."""
    assert classify_sender(123456789) == (123456789, None)


def test_classify_sender_negative_boundary():
    """Boundary: smallest negative value -> treated as channel/chat."""
    assert classify_sender(-1) == (None, -1)
