"""MAX photo/file uploads through the socks5 proxy (ticket 4b4ffe3c).

Live PR-7 check on 18.10: a photo failed with "Photo upload URL does not contain
photoIds", a file with ConnectionResetError. Causes, reproduced here without MAX:
- PyMax 2.4.1 posts with aiohttp's `proxy=`, which speaks HTTP to the proxy; a
  socks5 server resets such a connection (the local socks5 below does the same);
- MAX's upload URL no longer has `photoIds` (upstream issue #108).
app.max.media.ProxiedUploads posts through aiohttp-socks and finds the token
without the id. PyMax's own message flow calls it (send_message → uploads).
"""

import asyncio
import ipaddress
import struct
from types import SimpleNamespace

import pytest
from aiohttp import web
from pymax.api.uploads.service import UploadService
from pymax.exceptions import UploadError
from pymax.files import File, Photo
from pymax.protocol import Opcode

from app.max import media
from app.max.session import build_client, build_extra_config

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)
FILE_ID = 4242


class Socks5:
    """Minimal socks5 (no auth, CONNECT). Anything that is not socks5 — e.g.
    aiohttp's HTTP CONNECT — gets the connection reset, like max-egress."""

    def __init__(self) -> None:
        self.connects: list[tuple[str, int]] = []
        self.rejected = 0
        self.server: asyncio.Server | None = None

    @property
    def url(self) -> str:
        return f"socks5://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readexactly(2)
            if head[0] != 5:
                self.rejected += 1
                writer.transport.abort()  # RST → ConnectionResetError on the client
                return
            await reader.readexactly(head[1])
            writer.write(b"\x05\x00")
            _, cmd, _, atyp = await reader.readexactly(4)
            if atyp == 1:
                host = str(ipaddress.IPv4Address(await reader.readexactly(4)))
            elif atyp == 3:
                host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
            else:
                host = str(ipaddress.IPv6Address(await reader.readexactly(16)))
            (port,) = struct.unpack("!H", await reader.readexactly(2))
            self.connects.append((host, port))
            up_reader, up_writer = await asyncio.open_connection(host, port)
            writer.write(b"\x05\x00\x00\x01" + bytes(4) + b"\x00\x00")
            await writer.drain()
            await asyncio.gather(self._pipe(reader, up_writer), self._pipe(up_reader, writer))
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.transport.abort()

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()


class UploadHost:
    """MAX's upload endpoints: iu.oneme.ru/uploadImage and the file URL."""

    def __init__(self) -> None:
        self.bodies: list[tuple[str, int]] = []
        self.photos_result: dict = {"photos": {"0": {"token": "photo-token"}}}
        self.on_file = None
        self.runner: web.AppRunner | None = None
        self.port = 0

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/uploadImage", self._image)
        app.router.add_post("/uploadFile", self._file)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        await self.runner.cleanup()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    async def _image(self, request: web.Request) -> web.Response:
        form = await request.post()
        self.bodies.append(("photo", len(form["file"].file.read())))
        return web.json_response(self.photos_result)

    async def _file(self, request: web.Request) -> web.Response:
        self.bodies.append(("file", len(await request.read())))
        if self.on_file:
            self.on_file()
        return web.Response(text="<retval>1</retval>")


@pytest.fixture
async def net():
    socks, host = Socks5(), UploadHost()
    await socks.start()
    await host.start()
    yield SimpleNamespace(socks=socks, host=host)
    await host.stop()
    await socks.stop()


def _uploads(net, *, photo_url: str | None = None, cls=media.ProxiedUploads) -> UploadService:
    """PyMax's UploadService on a stub App (invoke answers like MAX)."""

    async def invoke(opcode, payload=None):
        if opcode == Opcode.PHOTO_UPLOAD:
            return SimpleNamespace(payload={"url": photo_url or net.host.url("/uploadImage?r=one-shot")})
        if opcode == Opcode.FILE_UPLOAD:
            return SimpleNamespace(payload={"info": [
                {"url": net.host.url("/uploadFile?id=x"), "fileId": FILE_ID, "token": "t"}]})
        raise AssertionError(opcode)

    app = SimpleNamespace(
        config=SimpleNamespace(proxy=net.socks.url, upload_timeout=30),
        invoke=invoke,
        dispatcher=SimpleNamespace(on_internal=lambda event: (lambda handler: handler)),
    )
    service = UploadService(app)
    if cls is not UploadService:
        media.install_uploads(SimpleNamespace(api=SimpleNamespace(uploads=service)))
    return service


@pytest.mark.asyncio
async def test_photo_goes_through_socks5_without_photo_ids(net):
    uploads = _uploads(net)
    payload = await uploads.upload_photo(Photo(raw=PNG, name="dot.png"))
    assert payload.photo_token == "photo-token"
    assert net.host.bodies == [("photo", len(PNG))]
    assert net.socks.connects == [("127.0.0.1", net.host.port)] and net.socks.rejected == 0


@pytest.mark.asyncio
async def test_file_goes_through_socks5_and_waits_for_notif_attach(net):
    uploads = _uploads(net)
    # MAX confirms the file with NOTIF_ATTACH; PyMax's handler resolves the waiter.
    net.host.on_file = lambda: asyncio.get_running_loop().call_soon(
        uploads.file_upload_waiters[FILE_ID].set_result, SimpleNamespace(file_id=FILE_ID))
    payload = await uploads.upload_file(File(raw=b"%PDF-1.4 tiny", name="отчёт 1.pdf"))
    assert payload.file_id == FILE_ID
    assert net.host.bodies == [("file", 13)]
    assert net.socks.connects == [("127.0.0.1", net.host.port)]
    assert uploads.file_upload_waiters == {}


@pytest.mark.asyncio
async def test_upload_refused_is_an_upload_error(net):
    uploads = _uploads(net, photo_url=f"http://127.0.0.1:{net.host.port}/missing")
    with pytest.raises(UploadError, match="status 404"):
        await uploads.upload_photo(Photo(raw=PNG, name="dot.png"))


@pytest.mark.asyncio
async def test_pymax_241_own_upload_is_reset_by_socks5(net):
    """The cause on production, kept as a canary: if a PyMax upgrade fixes the
    proxy, this fails and ProxiedUploads can go."""
    uploads = _uploads(net, cls=UploadService)
    net.host.on_file = lambda: None
    # The proxy got HTTP, not socks5, and reset it (to an https URL, as on
    # production, aiohttp sends CONNECT and sees ConnectionResetError).
    with pytest.raises(UploadError, match="HTTP error during file upload"):
        await uploads.upload_file(File(raw=b"x", name="a.txt"))
    assert net.socks.rejected == 1 and net.socks.connects == []
    with pytest.raises(UploadError, match="does not contain photoIds"):
        await uploads.upload_photo(Photo(raw=PNG, name="dot.png"))


@pytest.mark.parametrize(("url", "result", "token"), [
    ("https://iu.oneme.ru/uploadImage?r=x", {"photos": {"0": {"token": "a"}}}, "a"),
    ("https://iu.oneme.ru/uploadImage?photoIds=7&r=x", {"photos": {"7": {"token": "b"}, "8": {"token": "c"}}}, "b"),
    ("https://iu.oneme.ru/uploadImage?photos=8", {"photos": {"7": {"token": "b"}, "8": {"token": "c"}}}, "c"),
    ("https://iu.oneme.ru/uploadImage?photoIds=1", {"photos": {"5": {"token": "d"}}}, "d"),
])
def test_photo_token(url, result, token):
    assert media.photo_token(url, result) == token


@pytest.mark.parametrize("result", [{"photos": {}}, {"photos": {"1": {"token": "a"}, "2": {"token": "b"}}}, {"x": 1}])
def test_photo_token_refuses_to_guess(result):
    with pytest.raises(UploadError):
        media.photo_token("https://iu.oneme.ru/uploadImage?r=x", result)


@pytest.mark.asyncio
@pytest.mark.parametrize(("transport", "phone"), [("web", ""), ("tcp", "+79000000000")])
async def test_session_clients_use_proxied_uploads(transport, phone):
    client = build_client(transport=transport, phone=phone,
                          extra_config=build_extra_config(proxy="socks5://max-egress:1080", store=None),
                          auth_flow=None)
    client._config = await client._prepare_config()
    client._reset_runtime()  # what connect() does on every (re)connect
    assert type(client._app.api.uploads) is media.ProxiedUploads
    assert client._app.config.proxy == "socks5://max-egress:1080"
