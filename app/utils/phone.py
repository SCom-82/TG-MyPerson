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
    """Call parameters with "phone" masked, for digests (a JSON object; anything else as is).

    Also every contacts[].phone (import_contacts, review 17 R17-3)."""
    if not isinstance(params, dict):
        return params
    out = dict(params)
    if isinstance(out.get("phone"), (str, int)):
        out["phone"] = mask_phone(out["phone"]) or "?"
    if isinstance(out.get("contacts"), list):
        out["contacts"] = [mask_phone_params(c) for c in out["contacts"]]
    return out


# A phone inside free text: 10-15 digits, optionally +, spaces, dashes, brackets.
_PHONE_IN_TEXT = re.compile(r"\+?\d[\d\s()-]{8,}\d")


def mask_phones_in_text(text: str) -> str:
    """Every phone-like run of digits in an error text → its mask (QA-R2-D1).

    A run longer than a phone (two numbers in a row, "+7 927 … (7927…)") is split
    at brackets and its parts masked one by one; a long run without brackets is
    an id (MAX message ids have 18 digits) and stays.
    """
    def one(chunk: str) -> str:
        digits = len(_NON_DIGITS.sub("", chunk))
        return mask_phone(chunk) if 10 <= digits <= 15 else chunk

    def repl(m: re.Match) -> str:
        run = m.group()
        if len(_NON_DIGITS.sub("", run)) <= 15:
            return one(run)
        return "".join(one(part) for part in re.split(r"(\s*[()]\s*)", run))
    return _PHONE_IN_TEXT.sub(repl, text)
