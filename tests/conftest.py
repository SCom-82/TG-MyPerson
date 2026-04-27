"""Shared fixtures for TG-MyPerson test suite.

Provides:
  - async SQLAlchemy session connected to tg_myperson_pg_test:5433
  - FastAPI TestClient with mocked pool
  - Common DB seed helpers
"""

import os
import asyncio
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
    async_sessionmaker,
)
from sqlalchemy.pool import NullPool
from sqlalchemy import text

# Point at the test container. Override via env if needed.
TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:test@localhost:5433/tg_myperson",
)

# Force test settings BEFORE importing app modules
os.environ.setdefault("DATABASE_URL", TEST_DB_URL)
os.environ.setdefault("TG_API_ID", "12345")
os.environ.setdefault("TG_API_HASH", "deadbeef")
os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("TG_ADMIN_API_KEY", "test-admin-key")


# ---------------------------------------------------------------------------
# Engine + session factory pointing at test DB
# NullPool: each test gets a fresh connection, no cross-test state leakage.
# ---------------------------------------------------------------------------

_test_engine = create_async_engine(TEST_DB_URL, echo=False, poolclass=NullPool)
_TestSessionFactory = async_sessionmaker(_test_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional async session; rolls back after each test."""
    async with _TestSessionFactory() as session:
        async with session.begin():
            yield session
            await session.rollback()


# ---------------------------------------------------------------------------
# FastAPI app fixture with pool mocked out
# ---------------------------------------------------------------------------

@pytest.fixture
def app_with_mocked_pool(monkeypatch):
    """Return FastAPI app with TelegramClientPool stubbed (no real Telethon)."""
    from unittest.mock import AsyncMock, MagicMock
    from fastapi import HTTPException

    # Patch the pool singleton before importing main so lifespan is safe
    mock_pool = MagicMock()
    mock_pool.start_all = AsyncMock()
    mock_pool.stop_all = AsyncMock()
    mock_pool._pool = {}

    async def _mock_get(alias: str):
        raise HTTPException(status_code=404, detail=f"Session alias '{alias}' not registered or disabled")

    mock_pool.get = _mock_get

    monkeypatch.setattr("app.telegram.pool.pool", mock_pool)

    # Re-import main to pick up patched pool
    import importlib
    import app.main as main_module
    importlib.reload(main_module)

    return main_module.app


@pytest_asyncio.fixture
async def test_client(app_with_mocked_pool):
    """Async HTTP client wrapping the FastAPI app."""
    transport = ASGITransport(app=app_with_mocked_pool)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------

async def create_account(
    session: AsyncSession,
    alias: str = "work",
    phone: str = "+79001234567",
    mode: str = "rw",
    is_enabled: bool = True,
) -> int:
    """Insert an Account and return its id."""
    result = await session.execute(
        text("""
            INSERT INTO accounts (alias, phone, mode, is_enabled)
            VALUES (:alias, :phone, :mode, :is_enabled)
            RETURNING id
        """),
        {"alias": alias, "phone": phone, "mode": mode, "is_enabled": is_enabled},
    )
    account_id = result.scalar_one()
    return account_id


async def create_tool_policy(
    session: AsyncSession,
    account_id: int,
    tool_name: str,
    effect: str,  # 'allow' | 'deny'
) -> None:
    await session.execute(
        text("""
            INSERT INTO account_tool_policy (account_id, tool_name, effect)
            VALUES (:account_id, :tool_name, :effect)
        """),
        {"account_id": account_id, "tool_name": tool_name, "effect": effect},
    )
