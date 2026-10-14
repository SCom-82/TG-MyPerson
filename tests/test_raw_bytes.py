"""raw is always valid jsonb (D16 = B): bytes → {"_bytes": len}, one helper for TG and MAX.

Prod 14.10 (STORAGE_MODE=new): MAX chats carry binary fields the pymax models do
not declare (extra='allow', msgpack bin over TCP); model_dump(mode="json")
decoded them as utf-8 → UnicodeDecodeError in normalize_chat, the authorized
hook and sync_chats failed, the MAX chat list stopped updating.
"""

import json
import math
from datetime import datetime, timezone
from enum import Enum

from pymax.types import Chat, User
from pymax.types.domain import Message

from app.max import media, normalize
from app.utils.raw import jsonable, model_raw
from tests.test_max_ingest import fx

BIN = b"\xff\xfe\x00\x01"


class Color(Enum):
    RED = "red"


def test_jsonable_rules():
    d = datetime(2026, 10, 14, tzinfo=timezone.utc)
    raw = jsonable({1: BIN, "b": bytearray(b"ab"), "m": memoryview(b"abc"), "d": d, "e": Color.RED,
                    "s": {3}, "t": (1, 2), "nul": "a\x00b", "sur": "x\ud800y", "nan": math.nan,
                    "inf": math.inf, "f": 1.5, "n": None, "ok": True})
    assert raw == {"1": {"_bytes": 4}, "b": {"_bytes": 2}, "m": {"_bytes": 3}, "d": d.isoformat(),
                   "e": "red", "s": [3], "t": [1, 2], "nul": "ab", "sur": "x?y", "nan": None,
                   "inf": None, "f": 1.5, "n": None, "ok": True}
    json.dumps(raw, allow_nan=False).encode("utf-8")  # strict JSON, encodable


def _chat_payload(**extra):
    return {**fx("c01_chat")["frame"]["payload"]["chat"], **extra}


def test_normalize_chat_with_binary_extra_field():
    chat = Chat.model_validate(_chat_payload(iconBlob=BIN, nested={"thumb": BIN, "list": [BIN]}))
    row = normalize.normalize_chat(chat)  # was: UnicodeDecodeError
    raw = row["raw_data"]
    assert raw["iconBlob"] == {"_bytes": 4}
    assert raw["nested"] == {"thumb": {"_bytes": 4}, "list": [{"_bytes": 4}]}
    json.dumps(raw, allow_nan=False)


def test_normalize_message_and_user_with_binary():
    payload = {**fx("n03_photo")["frame"]["payload"]}
    payload["message"] = {**payload["message"], "waveform": BIN}
    msg = normalize.parse_message(payload)
    row, _ = normalize.normalize_message(msg, chat_id=payload["chatId"], me_id=None, received_at=None)
    assert row["raw_data"]["waveform"] == {"_bytes": 4}
    user = User.model_validate({"id": 5, "names": [{"firstName": "A"}], "avatarBlob": BIN})
    assert normalize.normalize_user(user)["raw_data"]["avatarBlob"] == {"_bytes": 4}


def test_model_raw_is_unchanged_without_bytes():
    """Existing raw stays byte-for-byte: same keys and values as model_dump(mode="json")."""
    for model in (Chat.model_validate(_chat_payload()),
                  normalize.parse_message(fx("n03_photo")["frame"]["payload"])):
        assert model_raw(model) == model.model_dump(mode="json", by_alias=True)


def test_media_round_trip_with_binary():
    payload = fx("n03_photo")["frame"]["payload"]
    msg = Message.model_validate({**payload["message"], "waveform": BIN})
    raw = media.message_from_client(msg)
    assert raw["waveform"] == {"_bytes": 4}
    assert media.attachment_at(raw, 0) is not None
