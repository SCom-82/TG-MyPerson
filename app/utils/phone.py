"""Phone normalisation to E.164 for contacts matching (ТЗ 2026-10-13 §7.2).

Digits only; a leading 8 of an 11-digit number becomes 7 (Russian trunk
prefix); result '+<digits>'. Empty or shorter than 10 digits → None.
TG sends '79001234567', MAX '+7 900 123-45-67' — both end up '+79001234567'.
"""

import re

_NON_DIGITS = re.compile(r"\D")


def normalize_phone(raw: str | int | None) -> str | None:
    if raw is None:
        return None
    digits = _NON_DIGITS.sub("", str(raw))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) < 10:
        return None
    return "+" + digits
