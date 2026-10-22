"""Р-78: MAX text goes as is, markup only by parse_mode (plan 2026-10-22 Р5 PR R5-4; QA R5-27, R5-29…R5-31).

Two levels: the routes hand the service (text, elements) — recorded by the fake; the service
(our NumericForwardMessages) puts them into the 64 / 67 frame unchanged — checked on a stub App.
"""

import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pymax import ApiError
from pymax.api.messages.service import MessageService
from pymax.api.uploads.payloads import VoiceAttachPayload
from pymax.protocol import Opcode

from app.max import frames, media
from app.max.session import NumericForwardMessages
from tests.max_env import aliases, env  # noqa: F401 — pytest fixtures
from tests.test_max_bots import _frames
from tests.test_max_html_text import SAMPLES
from tests.test_max_ingest import DIALOG
from tests.test_max_write_limits import OFF, _events, _limits, clock, rw  # noqa: F401 — pytest fixtures
from tests.test_max_write_new import new_env  # noqa: F401 — pytest fixture

LINK = "token=3mbQ_wto_fO_Dcu1 my_file_name.pdf **a** __b__ ~~c~~ `d` # e [x](https://max.ru/y_z) https://example.com/a_b_c"


async def _post(w, path, body):
    return await w.http.post(f"/api/v1{path}", headers=w.env.h(), json=body)


# ---------------------------------------------------------------------------
# routes → service: (text, elements) (R5-27)
# ---------------------------------------------------------------------------

async def test_send_and_reply_go_as_is(rw):
    await _limits(rw, **OFF)
    for text in [LINK, *SAMPLES]:
        assert (await _post(rw, "/messages", {"chat_id": DIALOG, "text": text})).status_code == 200
    assert rw.env.server.texts == [("send", DIALOG, t, []) for t in [LINK, *SAMPLES]]
    mid = rw.env.server.next_message_id
    resp = await _post(rw, "/messages", {"chat_id": DIALOG, "text": "re a_b", "reply_to_message_id": mid})
    assert resp.status_code == 200 and rw.env.server.texts[-1] == ("send", DIALOG, "re a_b", [])
    assert rw.env.server.writes[-1][2:4] == ("re a_b", mid)
    stored = (await rw.http.get(f"/api/v1/messages/{DIALOG}/{mid}", headers=rw.env.h())).json()
    assert stored["text"] == SAMPLES[-1]


async def test_markdown_and_html_by_parse_mode(rw):
    await _post(rw, "/messages", {"chat_id": DIALOG, "text": LINK, "parse_mode": "markdown"})
    _, _, text, elements = rw.env.server.texts[-1]
    assert (text, elements) == frames.prepare_text(LINK, "markdown") and "_" not in text.split()[0]
    await _post(rw, "/messages", {"chat_id": DIALOG, "text": '<b>a</b> <a href="https://max.ru">ссылка</a> a_b',
                                  "parse_mode": "html"})
    _, _, text, elements = rw.env.server.texts[-1]
    assert text == "a ссылка a_b" and [e.type for e in elements] == ["STRONG", "LINK"]


async def test_bad_html_is_refused_before_the_slot(rw):
    for path, body in (("/messages", {"chat_id": DIALOG, "text": "<span>x</span>", "parse_mode": "html"}),
                       ("/messages", {"chat_id": DIALOG, "text": "<b>x", "parse_mode": "html"}),
                       ("/messages/edit", {"chat_id": DIALOG, "message_id": 1, "text": "<a>x</a>",
                                           "parse_mode": "html"}),
                       ("/messages", {"chat_id": DIALOG, "text": "<b>x", "parse_mode": "html",
                                      "schedule_date": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})):
        resp = await _post(rw, path, body)
        assert resp.status_code == 422 and "parse_mode html" in resp.text, resp.text
    resp = await rw.http.post("/api/v1/messages/send-file", headers=rw.env.h(),
                              data={"chat_id": str(DIALOG), "caption": "<u>x", "parse_mode": "html"},
                              files={"file": ("a.pdf", b"%PDF", "application/pdf")})
    assert resp.status_code == 422
    assert rw.env.server.texts == [] and rw.env.server.writes == [] and _events(rw.id) == []
    assert _frames(rw, int(Opcode.MSG_SEND)) == []


async def test_edit_and_captions_as_is(rw):
    await _post(rw, "/messages", {"chat_id": DIALOG, "text": "x"})
    mid = rw.env.server.next_message_id
    assert (await _post(rw, "/messages/edit", {"chat_id": DIALOG, "message_id": mid, "text": LINK})).status_code == 200
    assert rw.env.server.texts[-1] == ("edit", DIALOG, LINK, [])
    await _post(rw, "/messages/edit", {"chat_id": DIALOG, "message_id": mid, "text": "<i>a_b</i>", "parse_mode": "html"})
    assert rw.env.server.texts[-1][2] == "a_b" and rw.env.server.texts[-1][3][0].type == "EMPHASIZED"
    for path, files in (("send-file", {"file": ("my_file_name.pdf", b"%PDF", "application/pdf")}),
                        ("send-voice", {"file": ("v.ogg", b"OggS" + b"\0" * 64, "audio/ogg")}),
                        ("album", [("files", ("a.png", b"\x89PNG", "image/png"))] * 2)):
        resp = await rw.http.post(f"/api/v1/messages/{path}", headers=rw.env.h(),
                                  data={"chat_id": str(DIALOG), "caption": "file_a_b.pdf **x**"}, files=files)
        if resp.status_code == 422 and "duration" in resp.text:  # a voice the fake cannot time
            continue
        assert resp.status_code == 200, (path, resp.text)
        assert rw.env.server.texts[-1] == ("send", DIALOG, "file_a_b.pdf **x**", []), path


async def test_scheduled_text_as_is(rw):
    when = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert (await _post(rw, "/messages", {"chat_id": DIALOG, "text": LINK, "schedule_date": when})).status_code == 200
    frame = _frames(rw, int(Opcode.MSG_SEND))[-1]["message"]
    assert (frame["text"], frame["elements"]) == (LINK, [])
    await _post(rw, "/messages", {"chat_id": DIALOG, "text": "<b>a_b</b>", "parse_mode": "html", "schedule_date": when})
    frame = _frames(rw, int(Opcode.MSG_SEND))[-1]["message"]
    assert (frame["text"], frame["elements"]) == ("a_b", [{"type": "STRONG", "from": 0, "length": 3}])


# ---------------------------------------------------------------------------
# service → frame (R5-27, R5-30)
# ---------------------------------------------------------------------------

def _service(answers=None):
    sent = []

    async def invoke(opcode, payload, *a, **kw):
        sent.append((int(opcode), payload))
        if answers:
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
        msg = {"id": 9, "time": 1, "type": "USER", "text": (payload.get("message") or payload).get("text") or "",
               "sender": 1}
        return SimpleNamespace(payload={"chatId": payload["chatId"], "message": msg}, opcode=int(opcode), cmd=1)

    app = SimpleNamespace(invoke=invoke, api=SimpleNamespace(uploads=None))
    service = NumericForwardMessages(app)
    app.api.messages = service
    return service, sent


@pytest.mark.parametrize("text", [LINK, *SAMPLES])
async def test_service_frames_carry_the_text_byte_for_byte(text):
    service, sent = _service()
    await service.send_message(5, text)
    await service.send_message(5, text, reply_to=7)
    await service.edit_message(5, 9, text)
    (op1, send), (op2, reply), (op3, edit) = sent
    assert (op1, op2, op3) == (int(Opcode.MSG_SEND), int(Opcode.MSG_SEND), int(Opcode.MSG_EDIT))
    assert (send["message"]["text"], send["message"]["elements"]) == (text, [])
    assert reply["message"]["link"] == {"type": "REPLY", "messageId": 7} and reply["message"]["text"] == text
    assert (edit["text"], edit["elements"], edit["messageId"]) == (text, [], 9)


async def test_service_takes_elements_and_send_at():
    service, sent = _service()
    _, elements = frames.prepare_text("<b>a</b>", "html")
    when = datetime(2026, 11, 1, tzinfo=timezone.utc)
    await service.send_message(5, "a", elements=elements, send_at=when)
    message = sent[0][1]["message"]
    assert message["elements"] == [{"type": "STRONG", "from": 0, "length": 1}]
    assert message["delayedAttributes"] == {"timeToFire": int(when.timestamp() * 1000), "notifySender": True}


async def test_facade_path_does_not_strip_markers_either():
    """A call that bypasses our routes (PyMax's facade passes no elements) is as is too (R5-30)."""
    from pymax.infra.message import MessageMixin

    service, sent = _service()
    facade = SimpleNamespace(_app=SimpleNamespace(api=SimpleNamespace(messages=service)))
    await MessageMixin.send_message(facade, 5, "a_b_c **x**")
    await MessageMixin.edit_message(facade, 5, 9, "a_b_c")
    assert sent[0][1]["message"]["text"] == "a_b_c **x**" and sent[1][1]["text"] == "a_b_c"


async def test_attachment_not_ready_goes_through_our_wait(monkeypatch):
    waited = []

    async def wait_ready_any(uploads, media_id, timeout):
        waited.append(media_id)

    monkeypatch.setattr(media, "wait_ready_any", wait_ready_any)
    service, sent = _service([ApiError(opcode=64, error="attachment.not.ready", message="later"), None])
    voice = VoiceAttachPayload(video_id=77, token="t")

    async def uploaded(attachments):
        return [voice]

    service._upload_attachments = uploaded
    await service.send_message(5, "a_b", attachments=["v.ogg"])
    assert len(sent) == 2 and sent[1][1]["message"]["text"] == "a_b" and waited


# ---------------------------------------------------------------------------
# canary: PyMax's signatures our overrides copy (R5-31)
# ---------------------------------------------------------------------------

def test_pymax_message_service_signatures_are_2_4_1():
    assert list(inspect.signature(MessageService.send_message).parameters) == [
        "self", "chat_id", "text", "reply_to", "attachments", "notify", "send_at"]
    assert list(inspect.signature(MessageService.edit_message).parameters) == [
        "self", "chat_id", "message_id", "text", "attachments"]
    ours = inspect.signature(NumericForwardMessages.send_message).parameters
    assert list(ours)[:7] == ["self", "chat_id", "text", "reply_to", "attachments", "notify", "send_at"]
    assert "elements" in ours and "elements" in inspect.signature(NumericForwardMessages.edit_message).parameters
