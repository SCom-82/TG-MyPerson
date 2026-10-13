"""media of an account's messages (ТЗ 2026-10-13 §2.4)."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Media
from app.repo import ensure_scope
from app.repo.rows import MediaRow


async def upsert_media(db: AsyncSession, account_id: int, message_id: int, rows: list[MediaRow]) -> None:
    """ON CONFLICT (account_id, message_id, attach_index); storage_path is kept."""
    ensure_scope(db, account_id)
    for row in rows:
        stmt = pg_insert(Media).values(
            account_id=account_id,
            message_id=message_id,
            attach_index=row.attach_index,
            file_external_id=row.file_external_id,
            file_unique_id=row.file_unique_id,
            file_type=row.file_type,
            file_name=row.file_name,
            file_size=row.file_size,
            mime_type=row.mime_type,
        )
        ex = stmt.excluded
        await db.execute(stmt.on_conflict_do_update(
            index_elements=[Media.account_id, Media.message_id, Media.attach_index],
            set_={
                "file_external_id": ex.file_external_id,
                "file_unique_id": ex.file_unique_id,
                "file_type": ex.file_type,
                "file_name": ex.file_name,
                "file_size": ex.file_size,
                "mime_type": ex.mime_type,
            },
        ))


async def list_media(db: AsyncSession, account_id: int, message_id: int) -> list[Media]:
    ensure_scope(db, account_id)
    return list((await db.execute(
        select(Media)
        .where(Media.account_id == account_id, Media.message_id == message_id)
        .order_by(Media.attach_index)
    )).scalars().all())
