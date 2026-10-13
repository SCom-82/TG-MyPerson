"""Test harness for account isolation (ТЗ 2026-10-13 §12).

- The app role tg_myperson_app (NOSUPERUSER NOBYPASSRLS) is created in the
  test cluster if missing, password 'test', and gets the grants of migration
  010 (account_partitions grants). Isolation tests run through it, migrations
  and cleanup through the superuser of TEST_DB_URL.
- `accounts` fixture: three fresh accounts (two telegram, one max) with their
  partitions; app.storage.data_session is rebound to the app role for the
  test. Teardown drops the partitions, the accounts and the contacts the
  test created (identities in EXT range).
"""

from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import psycopg2
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import TEST_DB_URL

APP_ROLE = "tg_myperson_app"
APP_PASSWORD = "test"
DATA_TABLES = (
    "users", "chats", "messages", "media", "sync_state",
    "member_snapshots", "snapshot_members", "raw_events",
)
# External ids used by isolation tests; contacts in this range are removed.
EXT_LO, EXT_HI = 9_400_000_000, 9_400_999_999
SELF_A, SELF_B, SELF_M = 9_400_000_001, 9_400_000_002, 9_400_000_003


def superuser_dsn() -> str:
    return TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://")


def app_role_url() -> str:
    u = urlparse(TEST_DB_URL)
    netloc = f"{APP_ROLE}:{APP_PASSWORD}@{u.hostname}:{u.port or 5432}"
    return urlunparse(u._replace(netloc=netloc))


def sql(statement: str, params=None, *, fetch: bool = False):
    conn = psycopg2.connect(superuser_dsn())
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(statement, params)
            return cur.fetchall() if fetch else None
    finally:
        conn.close()


def ensure_app_role() -> None:
    sql(f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
          END IF;
        END $$;
        ALTER ROLE {APP_ROLE} PASSWORD '{APP_PASSWORD}';
        GRANT CONNECT ON DATABASE {urlparse(TEST_DB_URL).path.lstrip('/')} TO {APP_ROLE};
        GRANT USAGE ON SCHEMA public TO {APP_ROLE};
    """)
    from app.scripts.account_partitions import GRANTS_SQL

    sql(GRANTS_SQL)


def drop_account(account_id: int) -> None:
    # A referenced partition carries FK dependencies of the parent: detach first.
    for t in reversed(DATA_TABLES):
        part = f"{t}_a{account_id}"
        [(exists,)] = sql("SELECT to_regclass(%s) IS NOT NULL", (part,), fetch=True)
        if exists:
            sql(f'ALTER TABLE "{t}" DETACH PARTITION "{part}"')
            sql(f'DROP TABLE "{part}"')
    for t in ("api_keys", "account_sessions", "account_tool_policy"):
        sql(f"DELETE FROM {t} WHERE account_id = %s", (account_id,))
    sql("DELETE FROM accounts WHERE id = %s", (account_id,))


def drop_test_contacts() -> None:
    rows = sql(
        "SELECT DISTINCT person_id FROM identities WHERE external_user_id BETWEEN %s AND %s",
        (EXT_LO, EXT_HI), fetch=True,
    )
    persons = [r[0] for r in rows]
    sql("DELETE FROM identities WHERE external_user_id BETWEEN %s AND %s", (EXT_LO, EXT_HI))
    if persons:
        sql("DELETE FROM people_merge_log WHERE from_person_id = ANY(%s) OR to_person_id = ANY(%s)",
            (persons, persons))
        sql("DELETE FROM people p WHERE p.id = ANY(%s)"
            " AND NOT EXISTS (SELECT 1 FROM identities i WHERE i.person_id = p.id)", (persons,))


def create_account(alias: str, platform: str = "telegram", external_user_id: int | None = None,
                   mode: str = "rw", partitions: bool = True) -> int:
    sql("DELETE FROM accounts WHERE alias = %s AND NOT EXISTS"
        " (SELECT 1 FROM audit_logs WHERE account_id = accounts.id)", (alias,))
    [(account_id,)] = sql(
        "INSERT INTO accounts (alias, phone, mode, platform, external_user_id)"
        " VALUES (%s, '+7', %s, %s, %s) RETURNING id",
        (alias, mode, platform, external_user_id), fetch=True,
    )
    if partitions:
        sql("SELECT create_account_partitions(%s)", (account_id,))
    return account_id


@dataclass
class Accounts:
    a: int
    b: int
    m: int
    app_engine: object
    app_sessions: async_sessionmaker


@pytest.fixture(scope="session")
def app_role():
    ensure_app_role()
    return app_role_url()


@pytest.fixture(scope="module")
def _module_accounts(app_role):
    """Accounts + partitions once per module (partition DDL is slow)."""
    drop_test_contacts()
    ids = (
        create_account("iso-a", external_user_id=SELF_A),
        create_account("iso-b", external_user_id=SELF_B, mode="ro"),
        create_account("iso-max", platform="max", external_user_id=SELF_M, mode="ro"),
    )
    try:
        yield ids
    finally:
        for acc in ids:
            drop_account(acc)
        drop_test_contacts()


def truncate_accounts(ids) -> None:
    # DELETE child-first: TRUNCATE of a referenced partition needs CASCADE,
    # which would empty the referencing parents for every account.
    for t in reversed(DATA_TABLES):
        for acc in ids:
            sql(f'DELETE FROM "{t}_a{acc}"')
    drop_test_contacts()


@pytest_asyncio.fixture
async def accounts(app_role, _module_accounts, monkeypatch):
    a, b, m = _module_accounts
    truncate_accounts(_module_accounts)

    import app.storage as storage

    engine = create_async_engine(app_role, poolclass=NullPool)
    sessions = async_sessionmaker(
        engine, class_=AsyncSession, sync_session_class=storage.DataSession, expire_on_commit=False
    )
    monkeypatch.setattr(storage, "data_session", sessions)
    try:
        yield Accounts(a=a, b=b, m=m, app_engine=engine, app_sessions=sessions)
    finally:
        await engine.dispose()
        truncate_accounts(_module_accounts)


async def app_scalar(engine, statement: str, **params):
    """One statement as the app role, without any set_config."""
    async with engine.connect() as conn:
        return (await conn.execute(text(statement), params)).scalar()


def has_privilege(table: str, priv: str = "SELECT") -> bool:
    [(ok,)] = sql("SELECT has_table_privilege(%s, %s, %s)", (APP_ROLE, table, priv), fetch=True)
    return ok



# ---------------------------------------------------------------------------
# STORAGE_MODE=new test runs: accounts created by any test get partitions
# ---------------------------------------------------------------------------

_NEW_MODE_TRIGGERS = """
CREATE OR REPLACE FUNCTION test_iso_account_ins() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    PERFORM create_account_partitions(NEW.id);
    RETURN NEW;
END $f$;
CREATE OR REPLACE FUNCTION test_iso_account_del() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['raw_events','snapshot_members','member_snapshots','sync_state',
                             'media','messages','chats','users'] LOOP
        IF to_regclass(t || '_a' || OLD.id) IS NOT NULL THEN
            EXECUTE format('ALTER TABLE %I DETACH PARTITION %I', t, t || '_a' || OLD.id);
            EXECUTE format('DROP TABLE %I', t || '_a' || OLD.id);
        END IF;
    END LOOP;
    RETURN OLD;
END $f$;
DROP TRIGGER IF EXISTS test_iso_account_ins ON accounts;
DROP TRIGGER IF EXISTS test_iso_account_del ON accounts;
CREATE TRIGGER test_iso_account_ins AFTER INSERT ON accounts FOR EACH ROW EXECUTE FUNCTION test_iso_account_ins();
CREATE TRIGGER test_iso_account_del BEFORE DELETE ON accounts FOR EACH ROW EXECUTE FUNCTION test_iso_account_del();
"""

_NEW_MODE_TRIGGERS_DROP = """
DROP TRIGGER IF EXISTS test_iso_account_ins ON accounts;
DROP TRIGGER IF EXISTS test_iso_account_del ON accounts;
DROP FUNCTION IF EXISTS test_iso_account_ins();
DROP FUNCTION IF EXISTS test_iso_account_del();
"""


@pytest.fixture(scope="session", autouse=True)
def _new_mode_account_partitions():
    """Test infrastructure only (not part of migration 010): with
    STORAGE_MODE=new, every account a test inserts gets its partitions and
    loses them again on delete, so suites written for legacy run unchanged."""
    from app.config import settings

    if settings.storage_mode != "new":
        yield
        return
    sql(_NEW_MODE_TRIGGERS)
    try:
        yield
    finally:
        sql(_NEW_MODE_TRIGGERS_DROP)
