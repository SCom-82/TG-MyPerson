from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session


# Control tables only (accounts, sessions, policies, audit, api_keys) once the
# legacy data reads are gone; account data goes through app.storage. The
# endpoints switch to this name in PR-6 together with the legacy reads.
get_control_db = get_db
