"""Data access for the per-account tables (ТЗ 2026-10-13 §4.2), shared by TG and MAX.

Rules:
- every function takes the account id as its first argument after the session;
  there are no global reads of data tables;
- sessions come from app.storage.account_db / owner_db (checked by
  ensure_scope); RLS on the tables is the second line;
- ON CONFLICT only on unique keys that contain account_id (partitioned tables);
- cross-account queries live only in app/repo/people_queries.py.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.storage import ACCOUNT, OWNER


def ensure_scope(db: AsyncSession, account_id: int) -> None:
    """The session must be the account's own session or the owner session."""
    scope = db.info.get("scope")
    if scope == OWNER:
        return
    if scope != ACCOUNT or db.info.get("account_id") != account_id:
        raise RuntimeError(
            f"repo: session scope {scope!r}/{db.info.get('account_id')!r} "
            f"does not match account {account_id}"
        )


def ensure_owner(db: AsyncSession) -> None:
    if db.info.get("scope") != OWNER:
        raise RuntimeError("repo: owner session required")
