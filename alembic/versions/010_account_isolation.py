"""Account isolation: per-account data tables partitioned by account_id, contacts, API keys.

Revision ID: 010
Revises: 009
Create Date: 2026-10-13

ТЗ 2026-10-13 §2 (ADR account isolation, D15 = B′). Additive: tg_* / max_* and
the rest of the old schema stay untouched until PR-6.

- accounts.external_user_id (+ UNIQUE (platform, external_user_id)); filled by
  migrate_isolation copy (step A) and at login, not here.
- api_keys; people / identities / people_merge_log (owner level, no RLS).
- 8 data tables PARTITION BY LIST (account_id), one partition per account
  (<table>_a<id>), no DEFAULT partition — a row for an account without
  partitions fails. PK/UNIQUE include account_id; ids come from one sequence
  per parent (identity columns on partitioned tables need PG 17).
- create_account_partitions(int) / account_partitions(int): SECURITY DEFINER;
  partitions get RLS + policy and no grants for the app role (it goes through
  the parents only). Called here for every existing account.
- RLS policy acc_isolation on every parent (no FORCE: the owner bypasses it).
- Grants for tg_myperson_app if the role exists: every non-partition table.
- audit_logs partition functions become SECURITY DEFINER so rotation works
  under a non-superuser app role.

DDL is written by hand; do not autogenerate for these tables.
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None

APP_ROLE = "tg_myperson_app"

# Parent before child; create_account_partitions walks this order, drop the reverse.
DATA_TABLES = (
    "users",
    "chats",
    "messages",
    "media",
    "sync_state",
    "member_snapshots",
    "snapshot_members",
    "raw_events",
)

POLICY_EXPR = (
    "account_id = nullif(current_setting('app.account_id', true), '')::int"
    " OR current_setting('app.scope', true) = 'owner'"
)

AUDIT_FUNCTIONS = (
    "create_audit_partition(int)",
    "drop_old_audit_partitions(int)",
    "drain_audit_default_for_month(date)",
    "ensure_audit_partition(int)",
)

# Grants on every table that is not a partition (data partitions <t>_aN and
# audit_logs partitions are reached through their parents). Same block as
# `python -m app.scripts.account_partitions grants`.
GRANTS_SQL = f"""
DO $do$
DECLARE r record;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
    RETURN;
  END IF;
  FOR r IN
    SELECT c.relname FROM pg_class c
    WHERE c.relnamespace = 'public'::regnamespace
      AND c.relkind IN ('r', 'p') AND NOT c.relispartition
      AND c.relname <> 'alembic_version'
  LOOP
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO {APP_ROLE}', r.relname);
  END LOOP;
  EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}';
  EXECUTE 'GRANT EXECUTE ON FUNCTION create_account_partitions(int) TO {APP_ROLE}';
  EXECUTE 'GRANT EXECUTE ON FUNCTION create_audit_partition(int), drop_old_audit_partitions(int),
           drain_audit_default_for_month(date), ensure_audit_partition(int) TO {APP_ROLE}';
END
$do$;
"""


def _execute_script(sql: str) -> None:
    """asyncpg runs one statement per call; split a plain DDL script (no bodies)."""
    for statement in sql.split(";\n"):
        if statement.strip():
            op.execute(statement)


def upgrade() -> None:
    # ---- 2.1 accounts -------------------------------------------------------
    op.execute("ALTER TABLE accounts ADD COLUMN external_user_id bigint")
    op.execute(
        "ALTER TABLE accounts ADD CONSTRAINT uq_accounts_platform_external"
        " UNIQUE (platform, external_user_id)"
    )

    op.execute("""
CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;
""")

    # ---- 2.2 API keys -------------------------------------------------------
    op.execute("""
CREATE TABLE api_keys (
  id           int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  scope        text NOT NULL CHECK (scope IN ('account','owner')),
  account_id   int REFERENCES accounts(id) ON DELETE CASCADE,
  key_hash     text NOT NULL UNIQUE,
  label        text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz,
  revoked_at   timestamptz,
  CHECK ((scope = 'account') = (account_id IS NOT NULL))
)
""")

    # ---- 2.3 contacts (owner level, no account_id, no RLS) -------------------
    _execute_script("""
CREATE TABLE people (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  display_name  text,
  primary_phone text,
  tags          text[] NOT NULL DEFAULT '{}',
  notes         text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_people_phone ON people (primary_phone);
CREATE INDEX ix_people_tags ON people USING gin (tags);

CREATE TABLE identities (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  person_id         bigint NOT NULL REFERENCES people(id),
  person_pinned     bool NOT NULL DEFAULT false,
  platform          text NOT NULL CHECK (platform IN ('telegram','max')),
  external_user_id  bigint NOT NULL,
  username          text,
  first_name        text,
  last_name         text,
  phone             text,
  is_bot            bool NOT NULL DEFAULT false,
  first_seen_at     timestamptz NOT NULL DEFAULT now(),
  last_seen_at      timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (platform, external_user_id)
);
CREATE INDEX ix_identities_person ON identities (person_id);
CREATE INDEX ix_identities_username ON identities (lower(username));
CREATE INDEX ix_identities_phone ON identities (phone);

CREATE TABLE people_merge_log (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  action text NOT NULL CHECK (action IN ('auto_merge','merge','split')),
  from_person_id bigint, to_person_id bigint, identity_id bigint,
  reason text, at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_people_updated_at BEFORE UPDATE ON people
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_identities_updated_at BEFORE UPDATE ON identities
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
""")

    # ---- 2.4 account data, partitioned by account ---------------------------
    _execute_script("""
CREATE SEQUENCE users_id_seq AS bigint;
CREATE TABLE users (
  id            bigint NOT NULL DEFAULT nextval('users_id_seq'),
  account_id    int NOT NULL REFERENCES accounts(id),
  external_id   bigint NOT NULL,
  identity_id   bigint NOT NULL REFERENCES identities(id),
  username text, first_name text, last_name text, phone text,
  is_bot        bool NOT NULL DEFAULT false,
  is_self       bool NOT NULL DEFAULT false,
  access_hash   bigint,
  is_contact    bool NOT NULL DEFAULT false,
  last_seen_via text CHECK (last_seen_via IN ('message','member','contact','resolve','dialog')),
  raw           jsonb,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (account_id, id),
  UNIQUE (account_id, external_id)
) PARTITION BY LIST (account_id);
ALTER SEQUENCE users_id_seq OWNED BY users.id;
CREATE UNIQUE INDEX uq_users_self ON users (account_id) WHERE is_self;
CREATE INDEX ix_users_identity ON users (identity_id);
CREATE INDEX ix_users_username ON users (lower(username));

CREATE SEQUENCE chats_id_seq AS bigint;
CREATE TABLE chats (
  id            bigint NOT NULL DEFAULT nextval('chats_id_seq'),
  account_id    int NOT NULL REFERENCES accounts(id),
  external_id   bigint NOT NULL,
  chat_type     text NOT NULL,
  title text, username text, description text, members_count int,
  access_hash   bigint,
  my_role       text CHECK (my_role IN ('member','admin','creator')),
  is_monitored  bool NOT NULL DEFAULT true,
  last_message_external_id bigint,
  last_message_at timestamptz,
  raw           jsonb,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (account_id, id),
  UNIQUE (account_id, external_id)
) PARTITION BY LIST (account_id);
ALTER SEQUENCE chats_id_seq OWNED BY chats.id;
CREATE INDEX ix_chats_last ON chats (last_message_at DESC NULLS LAST);
CREATE INDEX ix_chats_type ON chats (chat_type);

CREATE SEQUENCE messages_id_seq AS bigint;
CREATE TABLE messages (
  id                bigint NOT NULL DEFAULT nextval('messages_id_seq'),
  account_id        int NOT NULL,
  chat_id           bigint NOT NULL,
  external_id       bigint NOT NULL,
  sender_user_id    bigint,
  sender_chat_external_id bigint,
  reply_to_external_id bigint,
  forward_from_chat_external_id bigint,
  forward_from_message_external_id bigint,
  message_type      text NOT NULL DEFAULT 'text',
  text              text,
  text_html         text,
  sent_at           timestamptz NOT NULL,
  is_outgoing       bool NOT NULL DEFAULT false,
  is_edited         bool NOT NULL DEFAULT false,
  edited_at         timestamptz,
  deleted_at        timestamptz,
  views             int,
  raw               jsonb,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (account_id, id),
  UNIQUE (account_id, chat_id, external_id),
  FOREIGN KEY (account_id, chat_id) REFERENCES chats (account_id, id),
  FOREIGN KEY (account_id, sender_user_id) REFERENCES users (account_id, id)
) PARTITION BY LIST (account_id);
ALTER SEQUENCE messages_id_seq OWNED BY messages.id;
CREATE INDEX ix_messages_chat_sent ON messages (chat_id, sent_at DESC);
CREATE INDEX ix_messages_sent ON messages (sent_at DESC);
CREATE INDEX ix_messages_sender ON messages (sender_user_id);
CREATE INDEX ix_messages_type ON messages (message_type);
CREATE INDEX ix_messages_sender_chat ON messages (sender_chat_external_id);
CREATE INDEX ix_messages_ext ON messages (external_id);
CREATE INDEX ix_messages_updated ON messages (updated_at);

CREATE SEQUENCE media_id_seq AS bigint;
CREATE TABLE media (
  id              bigint NOT NULL DEFAULT nextval('media_id_seq'),
  account_id      int NOT NULL,
  message_id      bigint NOT NULL,
  attach_index    smallint NOT NULL DEFAULT 0,
  file_external_id text, file_unique_id text,
  file_type       text NOT NULL,
  file_name text, file_size bigint, mime_type text,
  storage_path    text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (account_id, id),
  UNIQUE (account_id, message_id, attach_index),
  FOREIGN KEY (account_id, message_id) REFERENCES messages (account_id, id) ON DELETE CASCADE
) PARTITION BY LIST (account_id);
ALTER SEQUENCE media_id_seq OWNED BY media.id;

CREATE TABLE sync_state (
  account_id         int NOT NULL,
  chat_id            bigint NOT NULL,
  oldest_external_id bigint, newest_external_id bigint,
  oldest_ts timestamptz, newest_ts timestamptz,
  is_fully_synced    bool NOT NULL DEFAULT false,
  total_synced       int NOT NULL DEFAULT 0,
  last_backfill_at   timestamptz,
  last_catchup_at    timestamptz,
  updated_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (account_id, chat_id),
  FOREIGN KEY (account_id, chat_id) REFERENCES chats (account_id, id)
) PARTITION BY LIST (account_id);

CREATE SEQUENCE member_snapshots_id_seq AS bigint;
CREATE TABLE member_snapshots (
  id            bigint NOT NULL DEFAULT nextval('member_snapshots_id_seq'),
  account_id    int NOT NULL,
  chat_id       bigint NOT NULL,
  taken_at      timestamptz NOT NULL DEFAULT now(),
  members_count int, source text, note text,
  PRIMARY KEY (account_id, id),
  FOREIGN KEY (account_id, chat_id) REFERENCES chats (account_id, id)
) PARTITION BY LIST (account_id);
ALTER SEQUENCE member_snapshots_id_seq OWNED BY member_snapshots.id;

CREATE SEQUENCE snapshot_members_id_seq AS bigint;
CREATE TABLE snapshot_members (
  id            bigint NOT NULL DEFAULT nextval('snapshot_members_id_seq'),
  account_id    int NOT NULL,
  snapshot_id   bigint NOT NULL,
  user_id       bigint,
  external_user_id bigint,
  username text, first_name text, last_name text, phone text, role text,
  raw jsonb,
  PRIMARY KEY (account_id, id),
  FOREIGN KEY (account_id, snapshot_id) REFERENCES member_snapshots (account_id, id) ON DELETE CASCADE,
  FOREIGN KEY (account_id, user_id) REFERENCES users (account_id, id)
) PARTITION BY LIST (account_id);
ALTER SEQUENCE snapshot_members_id_seq OWNED BY snapshot_members.id;
CREATE INDEX ix_snapshot_members_snap ON snapshot_members (snapshot_id);

CREATE SEQUENCE raw_events_id_seq AS bigint;
CREATE TABLE raw_events (
  id          bigint NOT NULL DEFAULT nextval('raw_events_id_seq'),
  account_id  int NOT NULL REFERENCES accounts(id),
  kind        text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  payload     jsonb,
  normalized  bool NOT NULL DEFAULT false,
  error       text,
  PRIMARY KEY (account_id, id)
) PARTITION BY LIST (account_id);
ALTER SEQUENCE raw_events_id_seq OWNED BY raw_events.id;
CREATE INDEX ix_raw_events_received ON raw_events (received_at);
CREATE INDEX ix_raw_events_pending ON raw_events (id) WHERE NOT normalized;
""")

    for table in ("users", "chats", "messages", "sync_state"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table}"
            " FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )

    # ---- 2.6 RLS on parents -------------------------------------------------
    for table in DATA_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY acc_isolation ON {table}"
            f" USING ({POLICY_EXPR}) WITH CHECK ({POLICY_EXPR})"
        )

    # ---- 2.5 partition management -------------------------------------------
    tables_array = "ARRAY[" + ", ".join(f"'{t}'" for t in DATA_TABLES) + "]"
    op.execute(f"""
CREATE FUNCTION create_account_partitions(p_account int)
RETURNS SETOF text LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_temp AS $func$
DECLARE
    t    text;
    part text;
    expr text := $expr${POLICY_EXPR}$expr$;
    has_app_role boolean := EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}');
BEGIN
    IF p_account IS NULL THEN
        RAISE EXCEPTION 'create_account_partitions: account id is required';
    END IF;
    FOREACH t IN ARRAY {tables_array} LOOP
        part := t || '_a' || p_account;
        EXECUTE format('CREATE TABLE IF NOT EXISTS %I PARTITION OF %I FOR VALUES IN (%s)',
                       part, t, p_account);
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', part);
        IF NOT EXISTS (SELECT 1 FROM pg_policies
                       WHERE schemaname = 'public' AND tablename = part
                         AND policyname = 'acc_isolation') THEN
            EXECUTE format('CREATE POLICY acc_isolation ON %I USING (%s) WITH CHECK (%s)',
                           part, expr, expr);
        END IF;
        -- Default privileges may have granted the app role access to the new
        -- table; it must reach the data through the parent only.
        EXECUTE format('REVOKE ALL ON %I FROM PUBLIC', part);
        IF has_app_role THEN
            EXECUTE format('REVOKE ALL ON %I FROM {APP_ROLE}', part);
        END IF;
        RETURN NEXT part;
    END LOOP;
END;
$func$
""")
    op.execute("REVOKE ALL ON FUNCTION create_account_partitions(int) FROM PUBLIC")
    op.execute(f"""
CREATE FUNCTION account_partitions(p_account int)
RETURNS TABLE(parent text, partition text, rows bigint) LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_temp AS $func$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY {tables_array} LOOP
        parent := t;
        partition := t || '_a' || p_account;
        IF to_regclass(partition) IS NULL THEN
            rows := NULL;
        ELSE
            EXECUTE format('SELECT count(*) FROM %I', partition) INTO rows;
        END IF;
        RETURN NEXT;
    END LOOP;
END;
$func$
""")
    op.execute("REVOKE ALL ON FUNCTION account_partitions(int) FROM PUBLIC")

    op.execute("SELECT create_account_partitions(id) FROM accounts ORDER BY id")

    # ---- audit_logs rotation under a non-superuser role ---------------------
    for fn in AUDIT_FUNCTIONS:
        op.execute(f"ALTER FUNCTION {fn} SECURITY DEFINER SET search_path = public, pg_temp")
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")

    op.execute(GRANTS_SQL)


# Tables whose rows make a downgrade destructive (review 13.10, P1-2).
GUARDED_TABLES = ("messages", "chats", "users", "people", "api_keys")


def _refuse_destructive_downgrade() -> None:
    """After the switch the new tables hold the only copy of new history: the
    rollback is STORAGE_MODE=legacy (runbook §11.3), not a downgrade."""
    if os.environ.get("ALLOW_DESTRUCTIVE_DOWNGRADE") == "1":
        return
    bind = op.get_bind()
    filled = [
        t for t in GUARDED_TABLES
        if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {t})")).scalar()
    ]
    if filled:
        raise RuntimeError(
            f"downgrade 010 refused: {', '.join(filled)} contain rows and would be dropped. "
            "Roll back with STORAGE_MODE=legacy (ТЗ runbook §11.3). To drop the data anyway "
            "set ALLOW_DESTRUCTIVE_DOWNGRADE=1."
        )


def downgrade() -> None:
    _refuse_destructive_downgrade()
    for fn in AUDIT_FUNCTIONS:
        op.execute(f"ALTER FUNCTION {fn} SECURITY INVOKER RESET search_path")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO PUBLIC")

    op.execute("DROP FUNCTION IF EXISTS account_partitions(int)")
    op.execute("DROP FUNCTION IF EXISTS create_account_partitions(int)")
    # Partitions and OWNED BY sequences go with their parents.
    for table in reversed(DATA_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DROP TABLE IF EXISTS people_merge_log")
    op.execute("DROP TABLE IF EXISTS identities")
    op.execute("DROP TABLE IF EXISTS people")
    op.execute("DROP TABLE IF EXISTS api_keys")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
    op.execute("ALTER TABLE accounts DROP CONSTRAINT IF EXISTS uq_accounts_platform_external")
    op.execute("ALTER TABLE accounts DROP COLUMN IF EXISTS external_user_id")
