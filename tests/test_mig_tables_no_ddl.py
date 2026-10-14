"""mig_* tables come from migration 011; probe / copy / delta run without CREATE.

Prod defect 14.10: the in-service probe ran CREATE TABLE IF NOT EXISTS
mig_membership on the app session; under tg_myperson_app (no CREATE on schema
public) it failed with "permission denied for schema public".

A throwaway database models prod: at 010 the app role created mig_membership
itself (the temporary GRANT CREATE workaround) and wrote a probe row. Then 011:
the table is taken over by the migration role, the row survives, the app role
keeps its grants. After that:
- the probe through the app role (no CREATE) fills mig_membership;
- copy / delta / verify run as a role without CREATE on public (BYPASSRLS
  stands in for table ownership: the owner of the test tables is the superuser).
"""

import uuid
from urllib.parse import urlparse, urlunparse

import psycopg2
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.scripts import isolation_copy as ic
from app.scripts import migrate_isolation as mi
from tests.isolation_env import APP_PASSWORD, APP_ROLE, ensure_app_role, sql
from tests.test_migrate_isolation_copy import SEED, SHARED, _alembic, _exec, _opts, _url
from tests.test_migrate_isolation_probe import FakeClient, _dialogs_work, _factory

MIG_ROLE = "tg_iso_nocreate"
MIG_PASSWORD = "test"
PRE_011_CHAT = -1000000000999


def _role_url(url: str, role: str, password: str) -> str:
    u = urlparse(url)
    return urlunparse(u._replace(netloc=f"{role}:{password}@{u.hostname}:{u.port or 5432}"))


def _as(dsn: str, role: str, password: str, statement: str, params=None, fetch: bool = False):
    conn = psycopg2.connect(_role_url(dsn, role, password))
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(statement, params)
            return cur.fetchall() if fetch else None
    finally:
        conn.close()


def _can_create(dsn: str, role: str) -> bool:
    return _exec(dsn, "SELECT has_schema_privilege(%s, 'public', 'CREATE')", (role,), fetch=True)[0][0]


@pytest.fixture
def prod_like_db(monkeypatch):
    ensure_app_role()
    name = f"tg_iso_noddl_{uuid.uuid4().hex[:8]}"
    sql(f'CREATE DATABASE "{name}"')
    url = _url(name)
    dsn = url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        _alembic(url, "upgrade", "003")
        _exec(dsn, "INSERT INTO accounts (alias, phone, mode) VALUES ('work', '+71', 'rw')")
        _alembic(url, "upgrade", "010")
        # The workaround in prod: CREATE for the probe, then taken back.
        _exec(dsn, f"GRANT CREATE ON SCHEMA public TO {APP_ROLE}; GRANT REFERENCES ON accounts TO {APP_ROLE}")
        _as(dsn, APP_ROLE, APP_PASSWORD, """
            CREATE TABLE IF NOT EXISTS mig_membership (
                account_id int NOT NULL REFERENCES accounts(id), external_chat_id bigint NOT NULL,
                chat_type text NOT NULL, title text, role text, access_hash bigint,
                PRIMARY KEY (account_id, external_chat_id))""")
        _as(dsn, APP_ROLE, APP_PASSWORD, "INSERT INTO mig_membership SELECT id, %s, 'channel', 'Pre', NULL, 1"
                                         " FROM accounts WHERE alias = 'work'", (PRE_011_CHAT,))
        _exec(dsn, f"REVOKE CREATE ON SCHEMA public FROM {APP_ROLE}; REVOKE REFERENCES ON accounts FROM {APP_ROLE}")
        _alembic(url, "upgrade", "head")
        yield url, dsn
    finally:
        sql(f'DROP DATABASE "{name}" WITH (FORCE)')


def test_011_takes_over_runtime_table_and_keeps_rows(prod_like_db):
    _, dsn = prod_like_db
    owners = dict(_exec(dsn, "SELECT tablename, tableowner FROM pg_tables WHERE tablename LIKE 'mig\\_%%'",
                        fetch=True))
    current = _exec(dsn, "SELECT current_user", fetch=True)[0][0]
    assert owners == {t: current for t in ic.MIG_TABLES}
    assert _exec(dsn, "SELECT external_chat_id FROM mig_membership", fetch=True) == [(PRE_011_CHAT,)]
    for table in ic.MIG_TABLES:
        assert _exec(dsn, "SELECT has_table_privilege(%s, %s, 'INSERT, DELETE, SELECT')",
                     (APP_ROLE, table), fetch=True) == [(True,)]


async def test_probe_under_app_role_without_create(prod_like_db, monkeypatch):
    url, dsn = prod_like_db
    assert not _can_create(dsn, APP_ROLE)
    engine = create_async_engine(_role_url(url, APP_ROLE, APP_PASSWORD), poolclass=NullPool)
    monkeypatch.setattr(mi, "async_session", async_sessionmaker(engine, expire_on_commit=False))
    _exec(dsn, "INSERT INTO account_sessions (account_id, session_plaintext)"
               " SELECT id, 'S1' FROM accounts WHERE alias = 'work'")
    try:
        report = await mi.probe(_factory({"S1": FakeClient(7, _dialogs_work())}))
    finally:
        await engine.dispose()

    assert report["accounts"]["work"]["status"] == "ok", report
    assert report["accounts"]["work"]["dialogs"] == 4
    chats = {r[0] for r in _exec(dsn, "SELECT external_chat_id FROM mig_membership", fetch=True)}
    assert PRE_011_CHAT not in chats and len(chats) == 4  # the account's rows were replaced


@pytest.fixture
def nocreate_role(prod_like_db):
    url, dsn = prod_like_db
    _exec(dsn, f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{MIG_ROLE}') THEN
            CREATE ROLE {MIG_ROLE} LOGIN NOSUPERUSER BYPASSRLS PASSWORD '{MIG_PASSWORD}';
          END IF;
        END $$;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {MIG_ROLE};
        GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {MIG_ROLE};
        GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {MIG_ROLE};
    """)
    try:
        yield _role_url(url, MIG_ROLE, MIG_PASSWORD)
    finally:
        _exec(dsn, f"DROP OWNED BY {MIG_ROLE}")
        sql(f"DROP ROLE IF EXISTS {MIG_ROLE}")


def test_copy_delta_verify_without_create(prod_like_db, nocreate_role, monkeypatch):
    _, dsn = prod_like_db
    _exec(dsn, SEED)
    assert not _can_create(dsn, MIG_ROLE)
    monkeypatch.setenv("MIGRATIONS_DATABASE_URL", nocreate_role)

    assert ic.copy(_opts())["ok"]
    assert ic.delta(_opts())["ok"]
    assert ic.verify(_opts())["ok"]
    assert _exec(dsn, "SELECT count(*) FROM messages m JOIN chats c"
                      " ON c.account_id = m.account_id AND c.id = m.chat_id WHERE c.external_id = %s",
                 (SHARED,), fetch=True)[0][0] > 0


def test_copy_refuses_before_011(monkeypatch):
    name = f"tg_iso_noddl_{uuid.uuid4().hex[:8]}"
    sql(f'CREATE DATABASE "{name}"')
    url = _url(name)
    try:
        _alembic(url, "upgrade", "003")
        _exec(url.replace("postgresql+asyncpg://", "postgresql://"),
              "INSERT INTO accounts (alias, phone, mode) VALUES ('work', '+71', 'rw')")
        _alembic(url, "upgrade", "010")
        monkeypatch.setenv("MIGRATIONS_DATABASE_URL", url)
        with pytest.raises(SystemExit, match="migration 011"):
            ic.open_ctx(_opts())
    finally:
        sql(f'DROP DATABASE "{name}" WITH (FORCE)')
