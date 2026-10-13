"""Sessions for the per-account data tables (ТЗ 2026-10-13 §4.1).

Every transaction of an account session starts with

    set_config('app.account_id', <id>, true), set_config('app.scope', <scope>, true)

which the RLS policy acc_isolation on the data tables reads. The settings are
transaction-local, so the listener re-applies them after every commit.

Data sessions are a separate session class: a DataSession that carries
neither an account id nor the owner scope raises before its first statement
(fail-closed even under a superuser connection, where RLS is bypassed).
Plain async_session() stays for control tables (accounts, sessions, policies,
audit, api_keys) and, until the switch, the legacy tg_*/max_* tables.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Request
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from app.database import engine

ACCOUNT = "account"
OWNER = "owner"


class DataSession(Session):
    """Sync session class behind account_db()/owner_db(); see _set_scope."""


@event.listens_for(DataSession, "after_begin")
def _set_scope(session: Session, transaction, connection) -> None:
    scope = session.info.get("scope")
    account_id = session.info.get("account_id")
    if scope == OWNER:
        account_id = None
    elif scope != ACCOUNT or not isinstance(account_id, int):
        raise RuntimeError("data session without account_id or owner scope")
    connection.execute(
        text("SELECT set_config('app.account_id', :a, true), set_config('app.scope', :s, true)"),
        {"a": "" if account_id is None else str(account_id), "s": scope},
    )


data_session = async_sessionmaker(
    engine, class_=AsyncSession, sync_session_class=DataSession, expire_on_commit=False
)


@asynccontextmanager
async def account_db(account_id: int) -> AsyncIterator[AsyncSession]:
    """Session that sees only rows of account_id."""
    if not isinstance(account_id, int) or isinstance(account_id, bool):
        raise RuntimeError(f"account_db: invalid account id {account_id!r}")
    async with data_session(info={"scope": ACCOUNT, "account_id": account_id}) as session:
        yield session


@asynccontextmanager
async def owner_db() -> AsyncIterator[AsyncSession]:
    """Session across all accounts: contacts and owner tools only."""
    async with data_session(info={"scope": OWNER}) as session:
        yield session


async def get_account_db(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: account session for the request's account.

    The account is resolved by the authz middleware (request.state.account_id);
    without it the request cannot reach account data.
    """
    account_id = getattr(request.state, "account_id", None)
    if account_id is None:
        raise RuntimeError("get_account_db: request has no resolved account")
    async with account_db(account_id) as session:
        yield session


def session_account_id(session: AsyncSession) -> int | None:
    """Account id of a data session; None for the owner scope."""
    info = session.info
    if info.get("scope") == OWNER:
        return None
    return info.get("account_id")
