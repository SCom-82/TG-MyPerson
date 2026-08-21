"""Размер фото в байтах — ровно так, как его считает telethon при скачивании.

`iter_download` резолвит `Photo` через `telethon.utils._get_file_info`, который берёт
**последний** элемент `photo.sizes` и меряет его `_photo_size_byte_count`. Любой другой
способ подсчёта даёт Content-Length, не совпадающий с телом ответа: uvicorn обрывает
поток на первом лишнем байте, клиент видит `200` и пустой файл.

Функция telethon приватная, поэтому импортируем её с падением на локальную копию
логики — так апгрейд telethon не уронит эндпоинт молча.
"""
from telethon.tl import types

try:  # pragma: no cover - зависит от версии telethon
    from telethon.utils import _photo_size_byte_count as _telethon_byte_count
except ImportError:  # pragma: no cover
    _telethon_byte_count = None


def _fallback_byte_count(size) -> int | None:
    """Копия telethon.utils._photo_size_byte_count на случай его исчезновения."""
    if isinstance(size, types.PhotoSize):
        return size.size
    if isinstance(size, types.PhotoStrippedSize):
        if len(size.bytes) < 3 or size.bytes[0] != 1:
            return len(size.bytes)
        return len(size.bytes) + 622
    if isinstance(size, types.PhotoCachedSize):
        return len(size.bytes)
    if isinstance(size, types.PhotoSizeEmpty):
        return 0
    if isinstance(size, types.PhotoSizeProgressive):
        return max(size.sizes)
    return None


def photo_size_byte_count(size) -> int | None:
    """Размер одного PhotoSize-варианта в байтах."""
    if _telethon_byte_count is not None:
        return _telethon_byte_count(size)
    return _fallback_byte_count(size)


def photo_byte_count(photo) -> int | None:
    """Размер фото, которое отдаст iter_download, или None если посчитать нельзя.

    None — допустимый результат: эндпоинт тогда не ставит Content-Length и отдаёт
    ответ chunked. Это хуже (нет докачки по Range), но не рвёт поток.
    """
    sizes = getattr(photo, "sizes", None)
    if not sizes:
        return None
    return photo_size_byte_count(sizes[-1])
