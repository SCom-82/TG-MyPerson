"""Tables of migrate_isolation (mig_*) created by a migration, not at run time.

Revision ID: 011
Revises: 010
Create Date: 2026-10-14

Until now probe / copy / delta ran CREATE TABLE IF NOT EXISTS on their own
connection. The service runs as tg_myperson_app without CREATE on schema
public, so the in-service probe failed with "permission denied for schema
public". The DDL moves here; the code only reads and writes the tables.

- mig_watermark, mig_chat_owner, mig_unassigned, mig_membership — the same
  definitions as before, IF NOT EXISTS: in prod mig_membership (and maybe the
  others) already exist with the probe result, which must survive.
- A table created at run time belongs to whoever created it (possibly the app
  role under a temporary GRANT CREATE). It is handed to the migration role so
  copy/delta, running as the owner, can read it; if the migration role may not
  take it over, a WARNING names the table and the upgrade goes on.
- tg_myperson_app (if the role exists): SELECT, INSERT, UPDATE, DELETE on all
  four — the probe writes mig_membership through the app session.
- Dropped in PR-6 together with the legacy tables; downgrade keeps them (they
  hold the probe result and the copy watermarks).
"""

from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None

APP_ROLE = "tg_myperson_app"
MIG_TABLES = ("mig_watermark", "mig_chat_owner", "mig_unassigned", "mig_membership")

MIG_DDL = (
    """CREATE TABLE IF NOT EXISTS mig_watermark (
        tbl text PRIMARY KEY, max_id bigint, at timestamptz NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS mig_chat_owner (
        platform text NOT NULL, legacy_chat_id bigint NOT NULL, account_id int NOT NULL,
        rule text NOT NULL, PRIMARY KEY (platform, legacy_chat_id, account_id))""",
    """CREATE TABLE IF NOT EXISTS mig_unassigned (
        legacy_chat_id bigint PRIMARY KEY, chat_type text, title text, messages bigint)""",
    """CREATE TABLE IF NOT EXISTS mig_membership (
        account_id int NOT NULL REFERENCES accounts(id), external_chat_id bigint NOT NULL,
        chat_type text NOT NULL, title text, role text, access_hash bigint,
        PRIMARY KEY (account_id, external_chat_id))""",
)

_TABLE_LIST = ", ".join(f"'{t}'" for t in MIG_TABLES)

OWNER_AND_GRANTS_SQL = f"""
DO $do$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT c.relname, pg_get_userbyid(c.relowner) AS owner FROM pg_class c
    WHERE c.relnamespace = 'public'::regnamespace AND c.relname IN ({_TABLE_LIST})
      AND pg_get_userbyid(c.relowner) <> current_user
  LOOP
    BEGIN
      EXECUTE format('ALTER TABLE %I OWNER TO %I', r.relname, current_user);
    EXCEPTION WHEN insufficient_privilege THEN
      RAISE WARNING '% is owned by % and % cannot take it over: run as superuser '
                    '"ALTER TABLE % OWNER TO %" before migrate_isolation copy/delta',
                    r.relname, r.owner, current_user, r.relname, current_user;
    END;
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
    FOR r IN
      SELECT c.relname FROM pg_class c
      WHERE c.relnamespace = 'public'::regnamespace AND c.relname IN ({_TABLE_LIST})
        AND pg_get_userbyid(c.relowner) = current_user
    LOOP
      EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO {APP_ROLE}', r.relname);
    END LOOP;
  END IF;
END
$do$;
"""


def upgrade() -> None:
    for ddl in MIG_DDL:
        op.execute(ddl)
    op.execute(OWNER_AND_GRANTS_SQL)


def downgrade() -> None:
    # Kept on purpose: the probe result and the copy watermarks live there, and
    # 010 code recreates the same tables IF NOT EXISTS. PR-6 drops them.
    pass
