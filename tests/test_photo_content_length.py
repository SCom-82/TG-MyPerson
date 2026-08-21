"""Content-Length для фото обязан совпадать с тем, что отдаёт iter_download.

Регрессия: прежний расчёт брал max по PhotoSize.size и отбрасывал
PhotoSizeProgressive (у него нет .size), из-за чего Content-Length приходил от
мелкого превью, uvicorn рвал поток -> клиент получал HTTP 200 и 0 байт.
"""
import pytest
from telethon.tl import types
from telethon import utils as tl_utils

from app.telegram.photo_size import photo_byte_count, _fallback_byte_count


def _progressive_photo():
    """Фото, как его отдаёт Telegram: превью + прогрессивный полноразмер последним."""
    return types.Photo(
        id=1, access_hash=2, file_reference=b"", date=None, dc_id=2,
        sizes=[
            types.PhotoStrippedSize(type="i", bytes=b"\x01\x02\x03"),
            types.PhotoSize(type="m", size=7794, w=320, h=240),
            types.PhotoSizeProgressive(
                type="y", w=1280, h=960, sizes=[1000, 20000, 240000]
            ),
        ],
    )


def test_matches_what_iter_download_will_stream():
    """Главное свойство: наш размер == размер, который резолвит сам telethon."""
    photo = _progressive_photo()
    assert photo_byte_count(photo) == tl_utils._get_file_info(photo).size


def test_progressive_size_wins_not_preview():
    """Регрессия: раньше отдавали 7794 (превью) вместо 240000 (полное фото)."""
    photo = _progressive_photo()
    assert photo_byte_count(photo) == 240000

    # старое поведение, зафиксированное как заведомо неверное
    legacy = max(
        s.size for s in photo.sizes if hasattr(s, "size") and isinstance(s.size, int)
    )
    assert legacy == 7794
    assert legacy != photo_byte_count(photo)


def test_plain_photo_without_progressive():
    photo = types.Photo(
        id=1, access_hash=2, file_reference=b"", date=None, dc_id=2,
        sizes=[types.PhotoSize(type="x", size=51234, w=800, h=600)],
    )
    assert photo_byte_count(photo) == 51234 == tl_utils._get_file_info(photo).size


def test_no_sizes_returns_none_not_crash():
    """None допустим — эндпоинт тогда отдаёт chunked, а не рвёт поток."""
    photo = types.Photo(
        id=1, access_hash=2, file_reference=b"", date=None, dc_id=2, sizes=[]
    )
    assert photo_byte_count(photo) is None
    assert photo_byte_count(None) is None


@pytest.mark.parametrize(
    "size,expected",
    [
        (types.PhotoSize(type="x", size=999, w=1, h=1), 999),
        (types.PhotoSizeProgressive(type="y", w=1, h=1, sizes=[5, 900, 300]), 900),
        (types.PhotoCachedSize(type="c", w=1, h=1, bytes=b"abcd"), 4),
        (types.PhotoSizeEmpty(type="e"), 0),
    ],
)
def test_fallback_matches_telethon(size, expected):
    """Локальная копия логики не должна разойтись с telethon."""
    assert _fallback_byte_count(size) == expected
    assert tl_utils._photo_size_byte_count(size) == expected
