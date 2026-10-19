"""forward_message sends the message id as a number (MAX 19.10: "Expected number at 61 [proto.payload]").

maxapi-python 2.4.1 builds link.messageId with str(); the live MAX rejects the
frame (msgpack byte 61 = the value of messageId). app.max.session swaps the
messages service of every App for NumericForwardMessages — the frame is checked
here at the point it goes to app.invoke.
"""

from types import SimpleNamespace

import pytest
from pymax.protocol import Opcode

from app.max.session import NumericForwardMessages, build_client, build_extra_config, forward_payload

BIG = 117461068428223441  # the id of the refused live call (max-work, «Избранное»)


class Sent(Exception):
    pass


def _service():
    frames = []

    async def invoke(opcode, payload):
        frames.append((opcode, payload))
        raise Sent()

    return NumericForwardMessages(SimpleNamespace(invoke=invoke)), frames


@pytest.mark.parametrize("message_id", [BIG, str(BIG)])
async def test_forward_frame_carries_numbers(message_id):
    service, frames = _service()
    with pytest.raises(Sent):
        await service.forward_message(0, message_id, 0)
    [(opcode, payload)] = frames
    assert opcode == Opcode.MSG_SEND
    link = payload["message"]["link"]
    assert link == {"type": "FORWARD", "messageId": BIG, "chatId": 0}
    assert type(link["messageId"]) is int and type(link["chatId"]) is int and type(payload["chatId"]) is int
    assert payload["chatId"] == 0 and payload["notify"] is True and payload["message"]["cid"] < 0


async def test_forward_source_chat_defaults_to_the_target():
    service, frames = _service()
    with pytest.raises(Sent):
        await service.forward_message(-70000000001, 5, notify=False)
    [(_, payload)] = frames
    assert payload["chatId"] == -70000000001 and payload["message"]["link"]["chatId"] == -70000000001
    assert payload["notify"] is False


def test_forward_payload_is_pymax_frame_but_the_id():
    payload = forward_payload(100500, "7", -7, cid=-1, notify=True)
    assert payload == {"chatId": 100500, "message": {"cid": -1, "link": {"type": "FORWARD", "messageId": 7,
                                                                         "chatId": -7}, "attaches": []},
                       "notify": True}


@pytest.mark.parametrize(("transport", "phone"), [("web", ""), ("tcp", "+79000000000")])
async def test_session_clients_forward_with_numbers(transport, phone):
    client = build_client(transport=transport, phone=phone,
                          extra_config=build_extra_config(proxy="socks5://max-egress:1080", store=None),
                          auth_flow=None)
    client._config = await client._prepare_config()
    client._reset_runtime()  # what connect() does on every (re)connect
    assert type(client._app.api.messages) is NumericForwardMessages
