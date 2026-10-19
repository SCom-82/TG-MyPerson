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


def mask_phone(phone: str | int | None) -> str | None:
    """'+79278975956' → '+7******5956' for logs and audit (plan 2026-10-19 Р-19).

    The full number never goes to a log or a digest: phone numbers are a small
    space, a sha256 of one is reversed by enumeration.
    """
    e164 = normalize_phone(phone)
    if e164 is None:
        return None
    digits = e164[1:]
    return "+" + digits[0] + "*" * (len(digits) - 5) + digits[-4:]


def mask_phone_params(params):
    """Call parameters with "phone" masked, for digests (a JSON object; anything else as is)."""
    if isinstance(params, dict) and isinstance(params.get("phone"), (str, int)):
        return {**params, "phone": mask_phone(params["phone"]) or "?"}
    return params
