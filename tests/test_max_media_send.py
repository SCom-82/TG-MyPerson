"""MAX voice, video, video notes and albums (plan 2026-10-19 Р4 PR-R4-1, Р-34…Р-37; QA R4-3, R4-4).

Two levels:
- frames: PyMax's own MessageService on a stub App, uploads by our
  ProxiedUploads through a local socks5 (the proxy PyMax 2.4.1 cannot use) to
  a local upload host; what MAX would get is the invoke() log;
- routes: the fake MAX of the MAX suites, the write limits, the stored message.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from pymax import ApiError
from pymax.api.messages.service import MessageService
from pymax.api.uploads.service import UploadService
from pymax.exceptions import UploadError
from pymax.files import File, Photo, Video, VideoNote, Voice
from pymax.protocol import Opcode

from app.max import media
from tests.max_env import ADMIN_KEY, API_KEY, aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_ingest import DIALOG
from tests.test_max_uploads import PNG, Socks5
from tests.test_max_write_limits import OFF, _events, _limits, clock, rw  # noqa: F401 — pytest fixtures
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture

MEDIA = Path(__file__).parent / "fixtures" / "max_media"
OGG = (MEDIA / "voice.ogg").read_bytes()  # 1.5 s opus
MP4 = (MEDIA / "clip.mp4").read_bytes()  # 3 s


# ---------------------------------------------------------------------------
# Frames: PyMax's MessageService + ProxiedUploads, socks5, a local upload host
# ---------------------------------------------------------------------------

class MediaHost:
    """MAX's video/voice upload URL (one per VIDEO_UPLOAD) and the photo/file ones."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, int, dict]] = []
        self.on_post = None  # (kind, item_id) → called while the POST is answered
        self.note_json: dict = {"thumbhash": "AAECAw"}
        self.runner: web.AppRunner | None = None
        self.port = 0

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/upload/{kind}/{item}", self._upload)
        app.router.add_post("/uploadImage", self._image)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        await self.runner.cleanup()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    async def _upload(self, request: web.Request) -> web.Response:
        body = await request.read()
        kind, item = request.match_info["kind"], int(request.match_info["item"])
        self.posts.append((kind, len(body), dict(request.headers)))
        if self.on_post:
            self.on_post(kind, item)
        if kind == "note":
            return web.json_response(self.note_json)
        return web.Response(text="<retval>1</retval>")

    async def _image(self, request: web.Request) -> web.Response:
        await request.post()
        return web.json_response({"photos": {"0": {"token": "photo-token"}}})


class StubApp:
    """The App a MessageService needs: invoke() logs the frames and answers like MAX."""

    def __init__(self, host: MediaHost, proxy: str) -> None:
        self.host = host
        self.frames: list[tuple[int, dict]] = []
        self.not_ready = 0  # MSG_SEND answers attachment.not.ready this many times
        self.on_not_ready = None
        self.next_id = 500
        self.config = SimpleNamespace(proxy=proxy, upload_timeout=30, app_version="25.10.0",
                                      device=SimpleNamespace(user_agent=SimpleNamespace(
                                          os_version="Android 13", device_name="SM-A525F", screen="405dpi")))
        self.dispatcher = SimpleNamespace(on_internal=lambda event: (lambda handler: handler))
        self.api = SimpleNamespace()
        uploads = UploadService(self)
        media.install_uploads(SimpleNamespace(api=SimpleNamespace(uploads=uploads)))
        self.api.uploads = uploads
        self.api.messages = MessageService(self)

    def sent(self, opcode: Opcode) -> list[dict]:
        return [p for o, p in self.frames if o == int(opcode)]

    async def invoke(self, opcode, payload=None):
        self.frames.append((int(opcode), payload))
        if opcode == Opcode.VIDEO_UPLOAD:
            self.next_id += 1
            kind = {(2, 1): "voice", (1, 1): "note"}.get((payload["type"], payload["uploaderType"]), "video")
            return SimpleNamespace(payload={"info": [{"url": self.host.url(f"/upload/{kind}/{self.next_id}"),
                                                      "videoId": self.next_id, "token": f"tok{self.next_id}"}]})
        if opcode == Opcode.PHOTO_UPLOAD:
            return SimpleNamespace(payload={"url": self.host.url("/uploadImage?r=x")})
        if opcode == Opcode.FILE_UPLOAD:
            self.next_id += 1
            fid = self.next_id
            asyncio.get_running_loop().call_later(
                0.05, lambda: self.api.uploads.file_upload_waiters[fid].set_result(SimpleNamespace(file_id=fid)))
            return SimpleNamespace(payload={"info": [{"url": self.host.url(f"/upload/file/{fid}"), "fileId": fid,
                                                      "token": "t"}]})
        if opcode == Opcode.MSG_SEND:
            if self.not_ready:
                self.not_ready -= 1
                if self.on_not_ready:
                    asyncio.get_running_loop().call_later(0.05, self.on_not_ready)
                raise ApiError(opcode=64, error="attachment.not.ready", message="Attachment is not ready")
            return SimpleNamespace(payload={"chatId": payload["chatId"], "message": {
                "id": 9001, "time": 1, "type": "USER", "text": "", "sender": 1, "attaches": []}})
        if opcode in (Opcode.SEND_VOTE, Opcode.MSG_GET_REACTIONS, Opcode.CHAT_UPDATE):
            return SimpleNamespace(payload=self.answer)
        raise AssertionError(opcode)

    answer: dict = {}


@pytest.fixture
async def stub():
    socks, host = Socks5(), MediaHost()
    await socks.start()
    await host.start()
    app = StubApp(host, socks.url)
    app.socks = socks
    yield app
    await host.stop()
    await socks.stop()


def _signal(app: StubApp, kind_wanted: str):
    """MAX's ready signal while the POST is still being answered (before PyMax could start waiting)."""
    def on_post(kind, item):
        if kind != kind_wanted:
            return
        waiters = app.api.uploads.voice_upload_waiters if kind == "voice" else app.api.uploads.video_upload_waiters
        waiters[item].set_result(SimpleNamespace(video_id=item, audio_id=item))
    return on_post


async def test_voice_frames_through_socks5_and_an_early_ready_signal(stub):
    stub.host.on_post = _signal(stub, "voice")
    await stub.api.messages.send_message(5, None, attachments=[media.upload_attachment(OGG, "v.ogg", voice=True)])
    assert stub.sent(Opcode.VIDEO_UPLOAD) == [{"count": 1, "type": 2, "uploaderType": 1, "profile": False}]
    [(kind, size, headers)] = stub.host.posts
    assert (kind, size, headers["Content-Range"]) == ("voice", len(OGG), f"bytes 0-{len(OGG) - 1}/{len(OGG)}")
    assert headers["User-Agent"].startswith("OKMessages/25.10.0")
    assert stub.socks.connects and stub.socks.rejected == 0  # through the socks5, not aiohttp's proxy=
    [send] = stub.sent(Opcode.MSG_SEND)
    assert send["message"]["attaches"] == [{"_type": "AUDIO", "token": "tok501", "duration": 1500,
                                            "wave": b"\x00" * 80}]
    assert stub.api.uploads.voice_upload_waiters == {}


async def test_voice_without_a_ready_signal_is_sent_anyway(stub, monkeypatch):
    monkeypatch.setattr(media, "READY_WAIT_SEC", 0.05)
    await stub.api.messages.send_message(5, None, attachments=[Voice(raw=OGG, name="v.ogg", duration=1500)])
    assert len(stub.sent(Opcode.MSG_SEND)) == 1


async def test_attachment_not_ready_is_waited_out_by_pymax(stub, monkeypatch):
    monkeypatch.setattr(media, "READY_WAIT_SEC", 0.05)
    stub.not_ready = 1
    stub.on_not_ready = lambda: stub.api.uploads.voice_upload_waiters[501].set_result(SimpleNamespace(audio_id=501))
    await stub.api.messages.send_message(5, None, attachments=[Voice(raw=OGG, name="v.ogg", duration=1500)])
    assert len(stub.sent(Opcode.MSG_SEND)) == 2  # refused once, sent after the signal


async def test_video_waits_for_video_ready(stub, monkeypatch):
    stub.host.on_post = _signal(stub, "video")
    await stub.api.messages.send_message(5, "клип", attachments=[media.upload_attachment(MP4, "clip.mp4")])
    assert stub.sent(Opcode.VIDEO_UPLOAD) == [{"count": 1, "type": 0, "uploaderType": 0, "profile": False}]
    [send] = stub.sent(Opcode.MSG_SEND)
    assert send["message"]["attaches"] == [{"_type": "VIDEO", "videoId": 501, "token": "tok501", "videoType": 0}]

    stub.host.on_post = None
    monkeypatch.setattr(media, "VIDEO_READY_SEC", 0.05)
    with pytest.raises(UploadError, match="Timed out waiting for video processing"):
        await stub.api.messages.send_message(5, None, attachments=[Video(raw=MP4, name="clip.mp4")])
    assert stub.api.uploads.video_upload_waiters == {}


async def test_video_note_frames_thumbhash_and_duration(stub, monkeypatch):
    monkeypatch.setattr(media, "READY_WAIT_SEC", 0.05)
    note = media.upload_attachment(MP4, "note.mp4", video_note=True, duration_ms=4000)  # the form's duration wins
    await stub.api.messages.send_message(5, None, attachments=[note])
    assert stub.sent(Opcode.VIDEO_UPLOAD) == [{"count": 1, "type": 1, "uploaderType": 1, "profile": False}]
    [send] = stub.sent(Opcode.MSG_SEND)
    assert send["message"]["attaches"] == [{"_type": "VIDEO", "token": "tok501", "videoType": 1,
                                            "thumbhash": b"\x00\x01\x02\x03", "duration": 4000}]
    assert media.upload_attachment(MP4, "note.mp4", video_note=True).duration == 3000  # tinytag


async def test_album_is_one_msg_send(stub):
    stub.host.on_post = _signal(stub, "video")
    files = [media.upload_attachment(PNG, "a.png"), media.upload_attachment(PNG, "b.png"),
             media.upload_attachment(MP4, "c.mp4"), media.upload_attachment(b"%PDF-1.4", "d.pdf")]
    assert [type(f) for f in files] == [Photo, Photo, Video, File]
    await stub.api.messages.send_message(5, "альбом", attachments=files)
    [send] = stub.sent(Opcode.MSG_SEND)
    assert [a["_type"] for a in send["message"]["attaches"]] == ["PHOTO", "PHOTO", "VIDEO", "FILE"]


def test_classification_and_unreadable_duration():
    assert type(media.upload_attachment(MP4, "c.MOV")) is Video
    assert type(media.upload_attachment(MP4, "c.mp4", force_document=True)) is File
    assert type(media.upload_attachment(OGG, "v.ogg", voice=True)) is Voice
    for name in ("voice.m4a", "voice.mp3"):
        assert media.upload_attachment((MEDIA / name).read_bytes(), name, voice=True).duration > 900
    assert type(media.upload_attachment(MP4, "n.mp4", video_note=True)) is VideoNote
    with pytest.raises(media.DurationUnknown, match=r"cannot read the duration of '\.ogg'"):
        media.upload_attachment(b"not audio", "v.ogg", voice=True)


# ---------------------------------------------------------------------------
# Routes (fake MAX)
# ---------------------------------------------------------------------------

async def _file(w, path: str, name: str, data: bytes, **form):
    return await w.http.post(f"/api/v1/messages/{path}", headers=w.env.h(),
                             data={"chat_id": str(DIALOG), **{k: str(v).lower() if isinstance(v, bool) else str(v)
                                                              for k, v in form.items()}},
                             files={"file": (name, data, "application/octet-stream")})


def _sent(w) -> list:
    return w.env.server.sent_attachments


async def test_send_file_voice_video_and_document(rw):
    await _limits(rw, **OFF)
    resp = await _file(rw, "send-file", "v.ogg", OGG, voice_note=True)
    assert resp.status_code == 200, resp.text
    [[voice]] = _sent(rw)
    assert (type(voice), voice.duration) == (Voice, 1500)
    stored = (await rw.http.get(f"/api/v1/messages/{DIALOG}/{resp.json()['message_id']}", headers=rw.env.h())).json()
    assert stored["message_type"] == "voice" and stored["media"][0]["file_type"] == "voice"

    assert (await _file(rw, "send-file", "c.mp4", MP4)).status_code == 200
    assert (await _file(rw, "send-file", "c.mp4", MP4, force_document=True)).status_code == 200
    assert [type(a[0]) for a in _sent(rw)[1:]] == [Video, File]
    assert [e[0] for e in _events(rw.id)] == ["send", "send", "send"]


async def test_unreadable_duration_is_422_before_the_slot(rw):
    for path, extra in (("send-file", {"voice_note": True}), ("send-voice", {}), ("send-video-note", {})):
        resp = await _file(rw, path, "x.ogg", b"garbage", **extra)
        assert resp.status_code == 422 and "cannot read the duration" in resp.text, (path, resp.text)
    assert rw.env.server.writes == [] and _events(rw.id) == []


async def test_send_voice_and_video_note(rw):
    await _limits(rw, **OFF)
    resp = await _file(rw, "send-voice", "voice.mp3", (MEDIA / "voice.mp3").read_bytes(), caption="слушай")
    assert resp.status_code == 200 and resp.json()["schedule_date"] is None, resp.text
    resp = await _file(rw, "send-video-note", "n.mp4", MP4, duration_ms=4000)
    assert resp.status_code == 200, resp.text
    assert [(type(a[0]), a[0].duration) for a in _sent(rw)] == [(Voice, 1080), (VideoNote, 4000)]
    assert [w[2] for w in rw.env.server.writes] == ["слушай", None]  # the caption is the text
    resp = await _file(rw, "send-voice", "v.ogg", OGG, schedule_date="2026-10-20T10:00:00Z")
    assert resp.status_code == 501 and resp.json()["parameter"] == "schedule_date"


async def test_album_one_message_one_send(rw):
    await _limits(rw, **OFF)
    h = rw.env.h()
    for n in (1, 11):
        resp = await rw.http.post("/api/v1/messages/album", headers=h, data={"chat_id": str(DIALOG)},
                                  files=[("files", (f"{i}.png", PNG, "image/png")) for i in range(n)])
        assert resp.status_code == 400, resp.text
    files = [("files", ("a.png", PNG, "image/png")), ("files", ("b.png", PNG, "image/png")),
             ("files", ("c.mp4", MP4, "video/mp4")), ("files", ("d.pdf", b"%PDF", "application/pdf"))]
    resp = await rw.http.post("/api/v1/messages/album", headers=h, data={"chat_id": str(DIALOG), "caption": "4"},
                              files=files)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["message_ids"]) == 1 and body["status"] == "sent"
    assert [type(a) for a in _sent(rw)[0]] == [Photo, Photo, Video, File]
    assert [e[0] for e in _events(rw.id)] == ["send"]
    stored = (await rw.http.get(f"/api/v1/messages/{DIALOG}/{body['message_ids'][0]}", headers=h)).json()
    assert [m["attach_index"] for m in stored["media"]] == [0, 1, 2, 3]
    resp = await rw.http.post("/api/v1/messages/album", headers=h,
                              data={"chat_id": str(DIALOG), "schedule_date": "2026-10-20T10:00:00Z"},
                              files=files[:2])
    assert resp.status_code == 501 and resp.json()["tool"] == "send_album"


async def test_upload_error_is_502_and_gives_the_slot_back(rw):
    rw.env.server.write_errors = [UploadError("Video upload failed with status 500 id=1")]
    resp = await _file(rw, "send-file", "c.mp4", MP4)
    assert resp.status_code == 502 and "UploadError" in resp.text
    assert _events(rw.id) == []
