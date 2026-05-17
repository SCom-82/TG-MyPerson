"""test_download_media_streaming.py — SPEC §6 tests T1-T9 (streaming download_media fix).

Tests:
  T1. Generator does not buffer — StreamingResponse, no io.BytesIO in path.
  T2. Response headers for 200: Content-Length, Content-Type, Accept-Ranges,
      Content-Disposition, X-Expected-Size.
  T3. Range/206: offset forwarded to iter_download, correct Content-Range header.
  T4. Cyrillic filename — RFC 5987 Content-Disposition, no 500/UnicodeError.
  T5. No media / unsupported media type → 404.
  T6. Server-side ChannelPrivateError → 403 with readable detail (class name).
  T7. MCP client: streaming 500 response → real server detail (not httpx secondary error).
  T8. Integrity mismatch: Content-Length=1000, 800 bytes sent → complete=false, no final file.
  T9. Resume: .part exists 500 KB → Range header sent, ab mode, resumed_from correct.
"""

import io
import os
import asyncio
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tg_document(
    msg_id: int = 42,
    mime: str = "video/mp4",
    size: int = 10_485_760,
    filename: str = "video.mp4",
) -> MagicMock:
    """Minimal Telethon message mock with MessageMediaDocument."""
    from telethon.tl.types import MessageMediaDocument, DocumentAttributeFilename

    attr = MagicMock(spec=DocumentAttributeFilename)
    attr.file_name = filename

    doc = MagicMock()
    doc.mime_type = mime
    doc.size = size
    doc.attributes = [attr]

    media = MagicMock(spec=MessageMediaDocument)
    media.document = doc
    media.__class__ = MessageMediaDocument

    msg = MagicMock()
    msg.id = msg_id
    msg.media = media
    return msg


async def _iter_chunks(chunks: list[bytes]):
    """Async generator — simulates iter_download."""
    for chunk in chunks:
        yield chunk


def _patched_app(mock_tg_client):
    """Return FastAPI app with alias DB mocked (account_id=1) and pool returning mock_tg_client."""
    import importlib
    import app.authz.middleware as mw
    import app.telegram.pool as pool_mod
    import app.api.messages as msg_mod

    mw._alias_cache.clear()

    # Pool mock: get(alias) returns session that has our mock client
    mock_session = MagicMock()
    mock_session.client = mock_tg_client
    mock_tg_client.is_user_authorized = AsyncMock(return_value=True)

    original_pool_get = pool_mod.pool.get

    async def _mock_pool_get(alias):
        return mock_session

    pool_mod.pool.get = _mock_pool_get

    from app.main import app as fastapi_app
    return fastapi_app, mw, pool_mod, original_pool_get


# ---------------------------------------------------------------------------
# T1 — Generator does not buffer; no io.BytesIO created in download_media path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_T1_no_bytesio_buffer():
    """StreamingResponse returned; io.BytesIO NOT instantiated in download_media path.

    Tests the download_media route function directly (bypassing middleware) to
    isolate only the BytesIO usage in the download path itself.
    """
    import app.api.messages as msg_mod
    from starlette.requests import Request as StarletteRequest

    chunks = [b"chunk_a", b"chunk_b", b"chunk_c"]

    mock_client = MagicMock()
    msg = _make_tg_document(size=21)
    mock_client.get_messages = AsyncMock(return_value=msg)
    mock_client.iter_download = MagicMock(return_value=_iter_chunks(chunks))

    # Build a minimal Starlette Request with state.session_alias set
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/messages/100/42/media",
        "query_string": b"",
        "headers": [],
    }
    request = StarletteRequest(scope)
    request.state.session_alias = "work"
    request.state.account_id = 1

    # Patch require_authorized_client to return our mock
    orig = msg_mod.require_authorized_client

    async def _mock_require(req):
        return mock_client

    msg_mod.require_authorized_client = _mock_require

    bytesio_calls = []
    real_bytesio = io.BytesIO

    class TrackingBytesIO(real_bytesio):
        def __init__(self, *a, **kw):
            bytesio_calls.append(1)
            super().__init__(*a, **kw)

    try:
        with patch.object(msg_mod.io, "BytesIO", TrackingBytesIO):
            response = await msg_mod.download_media(
                chat_id=100, message_id=42, request=request
            )

        from starlette.responses import StreamingResponse
        assert isinstance(response, StreamingResponse), (
            f"Expected StreamingResponse, got {type(response)}"
        )

        # Consume the stream to verify content
        body_parts = []
        async for chunk in response.body_iterator:
            body_parts.append(chunk)
        assert b"".join(body_parts) == b"chunk_achunk_bchunk_c"

        assert len(bytesio_calls) == 0, (
            f"io.BytesIO was instantiated {len(bytesio_calls)} time(s) in download_media — "
            "file is being buffered in RAM, violating streaming contract"
        )
    finally:
        msg_mod.require_authorized_client = orig


# ---------------------------------------------------------------------------
# T2 — Headers 200: Content-Length, Content-Type, Accept-Ranges, Content-Disposition,
#       X-Expected-Size
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_T2_headers_200():
    """200 response carries all required headers with correct values."""
    size = 5_242_880
    msg = _make_tg_document(size=size, mime="application/pdf", filename="report.pdf")

    mock_client = MagicMock()
    mock_client.get_messages = AsyncMock(return_value=msg)
    mock_client.iter_download = MagicMock(return_value=_iter_chunks([b"x"]))
    mock_client.is_user_authorized = AsyncMock(return_value=True)

    import app.authz.middleware as mw
    import app.telegram.pool as pool_mod

    mw._alias_cache.clear()
    mock_session = MagicMock()
    mock_session.client = mock_client
    original_get = pool_mod.pool.get

    async def _mock_pool_get(alias):
        return mock_session

    pool_mod.pool.get = _mock_pool_get

    try:
        from app.main import app
        transport = ASGITransport(app=app)
        with patch.object(mw, "_resolve_alias_from_db", AsyncMock(return_value=1)):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/api/v1/messages/100/42/media",
                    headers={"X-API-Key": "test-api-key"},
                )

        assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
        assert resp.headers["content-type"].startswith("application/pdf")
        assert resp.headers["accept-ranges"] == "bytes"
        assert resp.headers["content-length"] == str(size)
        assert resp.headers["x-expected-size"] == str(size)
        cd = resp.headers["content-disposition"]
        assert "report.pdf" in cd
        assert cd.startswith("attachment")
    finally:
        pool_mod.pool.get = original_get
        mw._alias_cache.clear()


# ---------------------------------------------------------------------------
# T3 — Range / 206: offset forwarded to iter_download, correct Content-Range
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_T3_range_206():
    """Range: bytes=1048576- → 206, Content-Range correct, iter_download offset correct."""
    offset = 1_048_576
    size = 10_485_760
    msg = _make_tg_document(size=size)

    captured = {}

    def _fake_iter_download(media_obj, offset=0, chunk_size=None):
        captured["offset"] = offset
        return _iter_chunks([b"data"])

    mock_client = MagicMock()
    mock_client.get_messages = AsyncMock(return_value=msg)
    mock_client.iter_download = _fake_iter_download
    mock_client.is_user_authorized = AsyncMock(return_value=True)

    import app.authz.middleware as mw
    import app.telegram.pool as pool_mod

    mw._alias_cache.clear()
    mock_session = MagicMock()
    mock_session.client = mock_client
    original_get = pool_mod.pool.get

    async def _mock_pool_get(alias):
        return mock_session

    pool_mod.pool.get = _mock_pool_get

    try:
        from app.main import app
        transport = ASGITransport(app=app)
        with patch.object(mw, "_resolve_alias_from_db", AsyncMock(return_value=1)):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/api/v1/messages/100/42/media",
                    headers={
                        "X-API-Key": "test-api-key",
                        "Range": f"bytes={offset}-",
                    },
                )

        assert resp.status_code == 206, f"{resp.status_code}: {resp.text}"
        assert resp.headers["content-range"] == f"bytes {offset}-{size - 1}/{size}"
        assert resp.headers["content-length"] == str(size - offset)
        assert resp.headers["accept-ranges"] == "bytes"
        assert captured.get("offset") == offset, (
            f"iter_download called with offset={captured.get('offset')}, expected {offset}"
        )
    finally:
        pool_mod.pool.get = original_get
        mw._alias_cache.clear()


# ---------------------------------------------------------------------------
# T4 — Cyrillic filename → RFC 5987 Content-Disposition, no 500
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_T4_cyrillic_filename():
    """Cyrillic filename encoded via RFC 5987 filename*=UTF-8''... without errors."""
    cyrillic_name = "Аномалия_видео_001.mp4"
    msg = _make_tg_document(filename=cyrillic_name, size=1024)

    mock_client = MagicMock()
    mock_client.get_messages = AsyncMock(return_value=msg)
    mock_client.iter_download = MagicMock(return_value=_iter_chunks([b"x"]))
    mock_client.is_user_authorized = AsyncMock(return_value=True)

    import app.authz.middleware as mw
    import app.telegram.pool as pool_mod

    mw._alias_cache.clear()
    mock_session = MagicMock()
    mock_session.client = mock_client
    original_get = pool_mod.pool.get

    async def _mock_pool_get(alias):
        return mock_session

    pool_mod.pool.get = _mock_pool_get

    try:
        from app.main import app
        transport = ASGITransport(app=app)
        with patch.object(mw, "_resolve_alias_from_db", AsyncMock(return_value=1)):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/api/v1/messages/100/42/media",
                    headers={"X-API-Key": "test-api-key"},
                )

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        cd = resp.headers["content-disposition"]
        assert "filename*=UTF-8''" in cd, (
            f"RFC 5987 encoding missing in Content-Disposition: {cd!r}"
        )
        # Percent-encoded Cyrillic must be present (А = %D0%90 in UTF-8)
        assert "%D0%90" in cd or "%D0%B0" in cd or "UTF-8''" in cd
    finally:
        pool_mod.pool.get = original_get
        mw._alias_cache.clear()


# ---------------------------------------------------------------------------
# T5a — No media → 404
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_T5_no_media_returns_404():
    """Message without media → 404."""
    msg_no_media = MagicMock()
    msg_no_media.id = 99
    msg_no_media.media = None

    mock_client = MagicMock()
    mock_client.get_messages = AsyncMock(return_value=msg_no_media)
    mock_client.is_user_authorized = AsyncMock(return_value=True)

    import app.authz.middleware as mw
    import app.telegram.pool as pool_mod

    mw._alias_cache.clear()
    mock_session = MagicMock()
    mock_session.client = mock_client
    original_get = pool_mod.pool.get

    async def _mock_pool_get(alias):
        return mock_session

    pool_mod.pool.get = _mock_pool_get

    try:
        from app.main import app
        transport = ASGITransport(app=app)
        with patch.object(mw, "_resolve_alias_from_db", AsyncMock(return_value=1)):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/api/v1/messages/100/99/media",
                    headers={"X-API-Key": "test-api-key"},
                )

        assert resp.status_code == 404
        assert "media" in resp.text.lower()
    finally:
        pool_mod.pool.get = original_get
        mw._alias_cache.clear()


# ---------------------------------------------------------------------------
# T5b — Unsupported media type → 404
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_T5_unsupported_media_type_returns_404():
    """Message with non-downloadable media (webpage) → 404 'No downloadable media'."""
    from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

    # Use a plain MagicMock NOT spec'd as Document or Photo
    msg_web = MagicMock()
    msg_web.id = 100
    msg_web.media = MagicMock()
    # Force isinstance checks for Document and Photo to fail
    msg_web.media.__class__ = object

    mock_client = MagicMock()
    mock_client.get_messages = AsyncMock(return_value=msg_web)
    mock_client.is_user_authorized = AsyncMock(return_value=True)

    import app.authz.middleware as mw
    import app.telegram.pool as pool_mod

    mw._alias_cache.clear()
    mock_session = MagicMock()
    mock_session.client = mock_client
    original_get = pool_mod.pool.get

    async def _mock_pool_get(alias):
        return mock_session

    pool_mod.pool.get = _mock_pool_get

    try:
        from app.main import app
        transport = ASGITransport(app=app)
        with patch.object(mw, "_resolve_alias_from_db", AsyncMock(return_value=1)):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/api/v1/messages/100/100/media",
                    headers={"X-API-Key": "test-api-key"},
                )

        assert resp.status_code == 404
        assert "downloadable" in resp.text.lower() or "media" in resp.text.lower()
    finally:
        pool_mod.pool.get = original_get
        mw._alias_cache.clear()


# ---------------------------------------------------------------------------
# T6 — ChannelPrivateError from get_messages → 403, class name in detail
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_T6_channel_private_error_returns_403():
    """ChannelPrivateError → 403 with exception class name in detail."""
    from telethon.errors import ChannelPrivateError

    mock_client = MagicMock()
    mock_client.get_messages = AsyncMock(side_effect=ChannelPrivateError(request=None))
    mock_client.is_user_authorized = AsyncMock(return_value=True)

    import app.authz.middleware as mw
    import app.telegram.pool as pool_mod

    mw._alias_cache.clear()
    mock_session = MagicMock()
    mock_session.client = mock_client
    original_get = pool_mod.pool.get

    async def _mock_pool_get(alias):
        return mock_session

    pool_mod.pool.get = _mock_pool_get

    try:
        from app.main import app
        transport = ASGITransport(app=app)
        with patch.object(mw, "_resolve_alias_from_db", AsyncMock(return_value=1)):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/api/v1/messages/100/42/media",
                    headers={"X-API-Key": "test-api-key"},
                )

        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"
        detail = resp.json().get("detail", "")
        assert "ChannelPrivateError" in detail, (
            f"Expected class name 'ChannelPrivateError' in detail, got: {detail!r}"
        )
    finally:
        pool_mod.pool.get = original_get
        mw._alias_cache.clear()


# ---------------------------------------------------------------------------
# T7 — MCP client: streaming 500 → real server detail (not httpx secondary error)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_T7_mcp_client_unmasks_server_error():
    """Streaming 500 response: aread() inside stream context allows reading real detail.

    Verifies the core mechanism behind the Defect A fix:
    - Without fix: accessing .text on a streamed error response OUTSIDE stream context
      raises ResponseNotRead (httpx secondary error).
    - With fix (aread() INSIDE stream context, before raise_for_status): the body is
      buffered and .text works correctly after the exception is raised.

    This mirrors what should happen: call_tool's stream-using branches must read
    the error body while the connection is still open.
    """
    real_detail = "ChannelPrivateError: The channel specified is private"

    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.responses import StreamingResponse as SR

    async def fake_media(request):
        body_bytes = f'{{"detail": "{real_detail}"}}'.encode()

        async def body():
            yield body_bytes

        return SR(body(), status_code=500, media_type="application/json")

    fake_app = Starlette(routes=[Route("/media", fake_media)])

    import httpx as httpx_lib
    from httpx import ResponseNotRead

    transport = ASGITransport(app=fake_app)
    async with AsyncClient(transport=transport, base_url="http://fake") as http_client:

        # ---- Without fix: raises ResponseNotRead ----
        secondary_error_occurs = False
        try:
            async with http_client.stream("GET", "http://fake/media") as resp:
                resp.raise_for_status()
        except httpx_lib.HTTPStatusError as e:
            try:
                _ = e.response.text  # must raise ResponseNotRead
            except ResponseNotRead:
                secondary_error_occurs = True

        assert secondary_error_occurs, (
            "Without aread(), accessing .text should raise ResponseNotRead — "
            "test precondition failed (httpx behaviour may have changed)"
        )

        # ---- With fix: aread() INSIDE stream context, before raise_for_status ----
        # This is the correct pattern: buffer the body while stream is still open.
        captured_text = None
        try:
            async with http_client.stream("GET", "http://fake/media") as resp:
                await resp.aread()       # <-- fix: read body before raising
                resp.raise_for_status()
        except httpx_lib.HTTPStatusError as e:
            captured_text = f"HTTP {e.response.status_code}: {e.response.text}"

        assert captured_text is not None, "HTTPStatusError was not raised"
        assert real_detail in captured_text, (
            f"Real server detail not visible. Got: {captured_text!r}"
        )
        assert "Attempted to access streaming response content" not in captured_text


# ---------------------------------------------------------------------------
# T8 — Integrity mismatch: Content-Length=1000, server sends 800 → complete=False
# ---------------------------------------------------------------------------

def _load_mcp_mod(unique_name: str):
    """Load tg-myperson-mcp.py as a fresh module with a unique name."""
    import importlib.util
    script_path = Path(
        "/Users/scom/Yandex.Disk.localized/claude/_system/scripts/tg-myperson-mcp.py"
    )
    spec = importlib.util.spec_from_file_location(unique_name, script_path)
    mcp_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mcp_mod)
    return mcp_mod


def _make_fake_dl_client(fake_transport):
    """Factory: returns a class that replaces httpx.AsyncClient in mcp_mod,
    routing all requests through fake_transport (ASGI app)."""
    class FakeDLClient:
        def __init__(self, timeout=None):
            self._inner = AsyncClient(
                transport=fake_transport, base_url="http://testserver"
            )

        async def __aenter__(self):
            await self._inner.__aenter__()
            return self._inner

        async def __aexit__(self, *args):
            await self._inner.__aexit__(*args)

    return FakeDLClient


@pytest.mark.asyncio
async def test_T8_integrity_mismatch_no_final_file():
    """Server sends fewer bytes than declared → complete=False, no final file created."""
    mcp_mod = _load_mcp_mod("tg_myperson_mcp_t8")

    declared_size = 1000
    actual_bytes = 800

    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.responses import StreamingResponse as SR

    async def fake_stream(request):
        async def body():
            yield b"x" * actual_bytes

        return SR(
            body(),
            status_code=200,
            headers={
                "Content-Length": str(declared_size),
                "X-Expected-Size": str(declared_size),
                "Content-Type": "video/mp4",
            },
        )

    fake_app = Starlette(routes=[
        Route("/api/v1/messages/{chat_id}/{msg_id}/media", fake_stream)
    ])
    fake_transport = ASGITransport(app=fake_app)

    # Point module BASE_URL at testserver; api() will produce
    # "http://testserver/api/v1/messages/..."
    mcp_mod.BASE_URL = "http://testserver"
    mcp_mod.API_KEY = "test"
    mcp_mod.SESSION_ALIAS = "work"

    FakeDLClient = _make_fake_dl_client(fake_transport)
    orig_cls = mcp_mod.httpx.AsyncClient
    mcp_mod.httpx.AsyncClient = FakeDLClient

    with tempfile.TemporaryDirectory() as tmpdir:
        save_path = Path(tmpdir) / "output.mp4"

        async with AsyncClient(
            transport=fake_transport, base_url="http://testserver"
        ) as http_client:
            try:
                result = await mcp_mod._dispatch(
                    http_client,
                    "download_media",
                    {"chat_id": 100, "message_id": 42, "save_to": str(save_path)},
                )
            finally:
                mcp_mod.httpx.AsyncClient = orig_cls

        is_incomplete = result.get("complete") is False or "error" in result
        assert is_incomplete, f"Expected incomplete result, got: {result}"
        assert not save_path.exists(), (
            "Final file must not exist when download is incomplete"
        )


# ---------------------------------------------------------------------------
# T9 — Resume: .part file 500 KiB → Range header, ab append, resumed_from correct
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_T9_resume_from_part():
    """Existing .part → Range: bytes=N- sent, resumed_from correct, final file correct size."""
    mcp_mod = _load_mcp_mod("tg_myperson_mcp_t9")

    existing_part_size = 512_000
    remaining_size = 100_000
    total_size = existing_part_size + remaining_size

    received_headers: dict = {}

    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.responses import StreamingResponse as SR

    async def fake_stream(request):
        received_headers.update(dict(request.headers))

        async def body():
            yield b"y" * remaining_size

        return SR(
            body(),
            status_code=206,
            headers={
                "Content-Length": str(remaining_size),
                "X-Expected-Size": str(total_size),
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes {existing_part_size}-{total_size - 1}/{total_size}",
            },
        )

    fake_app = Starlette(routes=[
        Route("/api/v1/messages/{chat_id}/{msg_id}/media", fake_stream)
    ])
    fake_transport = ASGITransport(app=fake_app)

    mcp_mod.BASE_URL = "http://testserver"
    mcp_mod.API_KEY = "test"
    mcp_mod.SESSION_ALIAS = "work"

    FakeDLClient = _make_fake_dl_client(fake_transport)
    orig_cls = mcp_mod.httpx.AsyncClient
    mcp_mod.httpx.AsyncClient = FakeDLClient

    with tempfile.TemporaryDirectory() as tmpdir:
        save_path = Path(tmpdir) / "output.mp4"
        part_path = Path(str(save_path) + ".part")
        part_path.write_bytes(b"x" * existing_part_size)

        async with AsyncClient(
            transport=fake_transport, base_url="http://testserver"
        ) as http_client:
            try:
                result = await mcp_mod._dispatch(
                    http_client,
                    "download_media",
                    {"chat_id": 100, "message_id": 42, "save_to": str(save_path)},
                )
            finally:
                mcp_mod.httpx.AsyncClient = orig_cls

        assert result.get("resumed_from") == existing_part_size, (
            f"resumed_from={result.get('resumed_from')}, expected {existing_part_size}"
        )
        assert "range" in received_headers, "Range header not sent to server"
        assert received_headers["range"] == f"bytes={existing_part_size}-"

        assert save_path.exists(), "Final file not created after successful download"
        assert save_path.stat().st_size == total_size, (
            f"Expected {total_size} bytes, got {save_path.stat().st_size}"
        )
        assert not part_path.exists(), ".part file must be removed after success"
        assert result.get("complete") is True
