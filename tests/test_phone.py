"""E.164 normalisation for contacts (ТЗ 2026-10-13 §7.2)."""

import pytest

from app.utils.phone import normalize_phone


@pytest.mark.parametrize("raw, expected", [
    ("79001234567", "+79001234567"),          # TG
    ("+7 900 123-45-67", "+79001234567"),     # MAX
    ("8 (900) 123-45-67", "+79001234567"),    # trunk 8 → 7
    (79001234567, "+79001234567"),
    ("+44 20 7946 0958", "+442079460958"),
    ("", None),
    (None, None),
    ("12345", None),
    ("+7 900 12", None),
])
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected
