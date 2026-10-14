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
    assert raw["media"]["photo"]["file_reference"] == {"_bytes": 2}  # D16 B
    assert raw["media"]["photo"]["sizes"][0]["bytes"] == {"_bytes": 1}
    assert raw["from_id"] == {"_": "PeerUser", "user_id": 9}


def test_sizes_report():
    msgs = [Message(id=i, peer_id=PeerChannel(7), date=D, message="hello") for i in (1, 2)]
    report = sizes(msgs)
    assert report["messages"] == 2 and report["full_raw_avg"] > report["legacy_raw_avg"]


def test_measure_reader_allows_only_iter_messages():
    import pytest

    reader = _Reader(object())
    for name in ("send_message", "connect", "disconnect", "get_me"):
        with pytest.raises(PermissionError):
            getattr(reader, name)


async def test_measure_runs_on_pool_client(monkeypatch):
    from types import SimpleNamespace

    from app.scripts.measure_raw import measure_in_service

    calls = []

    class Client:
        async def iter_messages(self, chat, limit):
            calls.append((chat, limit))
            for i in (1, 2):
                yield Message(id=i, peer_id=PeerChannel(7), date=D, message="hello")

        async def connect(self):  # pragma: no cover - must not be reached
            calls.append("connect")

    async def get(alias):
        return SimpleNamespace(client=Client())

    monkeypatch.setattr("app.telegram.pool.pool", SimpleNamespace(get=get))
    report = await measure_in_service("personal-ro", -100, 2)
    assert report["messages"] == 2 and calls == [(-100, 2)]


async def test_measure_endpoint_requires_admin_key(test_client, monkeypatch):
    import app.config

    async def fake(alias, chat, limit):
        return {"alias": alias, "chat": chat, "messages": 0}

    # other suites reload the settings with an empty admin key
    monkeypatch.setattr(app.config.settings, "admin_api_key", "test-admin-key")

    monkeypatch.setattr("app.scripts.measure_raw.measure_in_service", fake)
    body = {"alias": "work", "chat": -1}
    assert (await test_client.post("/api/v1/admin/isolation/measure_raw", json=body)).status_code == 401
    resp = await test_client.post("/api/v1/admin/isolation/measure_raw", json=body,
                                  headers={"x-admin-key": "test-admin-key"})
    assert resp.status_code == 200 and resp.json()["alias"] == "work"
