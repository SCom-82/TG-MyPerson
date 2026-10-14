"""Platform objects → raw that is always valid jsonb (ТЗ 2026-10-13 §5.1, D16 = B).

One converter for TG (TL to_dict()) and MAX (pydantic models of pymax):
- bytes / bytearray / memoryview → {"_bytes": <len>}: file_reference, thumbs,
  waveform and unknown binary fields are not stored, only their size (D16 B);
- datetime / date → ISO, Enum → its value, set / tuple → list, dict keys → str;
- text without NUL and lone surrogates (jsonb and utf-8 reject them);
- NaN / ±inf → None (jsonb has no such numbers);
- TL objects through to_dict(), pydantic models through model_dump(python,
  by_alias) — never model_dump(mode="json"): it decodes bytes as utf-8 and
  fails on binary (prod 14.10: MAX chats with binary extra fields).
"""

import math
from datetime import date, datetime
from enum import Enum
from typing import Any


def bytes_marker(value: bytes | bytearray | memoryview) -> dict:
    return {"_bytes": len(value)}


def _text(value: str) -> str:
    if "\x00" in value:
        value = value.replace("\x00", "")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        value = value.encode("utf-8", "replace").decode("utf-8")
    return value


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {_text(str(k)): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes_marker(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return jsonable(value.value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return jsonable(model_dump(mode="python", by_alias=True))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return jsonable(to_dict())
    return _text(str(value))


def model_raw(model: Any) -> dict:
    """A pymax model → raw dict (camelCase keys, as model_dump(by_alias=True))."""
    return jsonable(model.model_dump(mode="python", by_alias=True))
