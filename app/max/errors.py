"""Classification of MAX API errors for the session supervisor (ADR §2.I).

Duck-typed on `.error` / `.message` / `.opcode` so this module does not import
pymax (import boundary, ADR §2.C).
"""

# The session token was revoked (logout elsewhere / "end all sessions").
# Matched on any opcode (PyMax checks LOGIN only): either way we stop, never retry.
_UNAUTHORIZED_CODES = ("FAIL_LOGIN_TOKEN", "FAIL_LOGOUT_ALL")

# Account sanctions. The real codes are unknown until the live pilot; this is a
# keyword heuristic over error/message. Extend with the exact codes seen in prod.
_BAN_MARKERS = ("ban", "block", "suspend", "restricted")


def _texts(exc: BaseException) -> list[str]:
    return [t for t in (getattr(exc, "error", None), getattr(exc, "message", None)) if t]


def is_api_error(exc: BaseException) -> bool:
    return hasattr(exc, "opcode") and hasattr(exc, "error")


def is_unauthorized(exc: BaseException) -> bool:
    return is_api_error(exc) and any(code in _texts(exc) for code in _UNAUTHORIZED_CODES)


def is_banned(exc: BaseException) -> bool:
    if not is_api_error(exc) or is_unauthorized(exc):
        return False
    text = " ".join(_texts(exc)).lower()
    return any(marker in text for marker in _BAN_MARKERS)
