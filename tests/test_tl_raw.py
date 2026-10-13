"""Full TG raw: tl_to_json and the measure_raw sizes (ТЗ 2026-10-13 §5.1)."""

import json
from datetime import datetime, timezone

from telethon.tl.types import Message, MessageMediaPhoto, PeerChannel, PeerUser, Photo, PhotoStrippedSize

from app.scripts.measure_raw import _Reader, sizes
from app.telegram.normalize import tl_to_json

D = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_tl_to_json_bytes_datetimes_nested():
    photo = Photo(id=1, access_hash=2, file_reference=b"\x01\x02", date=D,
                  sizes=[PhotoStrippedSize("i", b"\xff")], dc_id=2)
    msg = Message(id=5, peer_id=PeerChannel(7), date=D, message="x", from_id=PeerUser(9),
                  media=MessageMediaPhoto(photo=photo))
    raw = tl_to_json(msg.to_dict())
    json.dumps(raw)  # serialisable
    assert raw["date"] == D.isoformat()
    assert raw["media"]["photo"]["file_reference"] == "AQI="
    assert raw["media"]["photo"]["sizes"][0]["bytes"] == "/w=="
    assert raw["from_id"] == {"_": "PeerUser", "user_id": 9}


def test_sizes_report():
    msgs = [Message(id=i, peer_id=PeerChannel(7), date=D, message="hello") for i in (1, 2)]
    report = sizes(msgs)
    assert report["messages"] == 2 and report["full_raw_avg"] > report["legacy_raw_avg"]


def test_measure_reader_allows_only_reads():
    import pytest

    reader = _Reader(object())
    with pytest.raises(PermissionError):
        reader.send_message
