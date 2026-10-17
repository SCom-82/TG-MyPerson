"""Write limits per account and their counters (MAX PR-7, brief 2026-10-17).

Revision ID: 012
Revises: 011
Create Date: 2026-10-17

- account_write_limits: one row per account that overrides the platform
  default (MAX: the brief's defaults; Telegram: no limits). A NULL column turns
  that limit off; enabled=false stops every messenger write of the account.
  Changed through the admin API, read on every write (no restart).
- write_events: one row per messenger write that passed the limits ('send' or
  'action'), with the chat and whether it opened a new dialog. The counters are
  windows over these rows, so a restart does not reset them. A send that is
  delayed by min_interval is stored with its planned time. Rows older than a
  day are deleted by the writes themselves.
- Control tables (like audit_logs): no account_id partitioning, no RLS; the app
  role gets SELECT, INSERT, UPDATE, DELETE.
"""

from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None

APP_ROLE = "tg_myperson_app"

DDL = (
    """CREATE TABLE account_write_limits (
        account_id int PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
        enabled boolean NOT NULL DEFAULT true,
        per_minute int CHECK (per_minute >= 0),
        per_hour int CHECK (per_hour >= 0),
        per_day int CHECK (per_day >= 0),
        new_dialogs_per_day int CHECK (new_dialogs_per_day >= 0),
        min_interval_sec int CHECK (min_interval_sec >= 0),
        jitter_sec int CHECK (jitter_sec >= 0),
        actions_per_hour int CHECK (actions_per_hour >= 0),
        updated_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE write_events (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        account_id int NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        kind text NOT NULL CHECK (kind IN ('send', 'action')),
        tool text NOT NULL,
        chat_id bigint,
        new_dialog boolean NOT NULL DEFAULT false,
        at timestamptz NOT NULL)""",
    "CREATE INDEX ix_write_events_account_at ON write_events (account_id, kind, at)",
    "CREATE INDEX ix_write_events_account_chat ON write_events (account_id, chat_id) WHERE kind = 'send'",
)

GRANTS_SQL = f"""
DO $do$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON account_write_limits, write_events TO {APP_ROLE};
    GRANT USAGE, SELECT ON SEQUENCE write_events_id_seq TO {APP_ROLE};
  END IF;
END
$do$;
"""


def upgrade() -> None:
    for ddl in DDL:
        op.execute(ddl)
    op.execute(GRANTS_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS write_events")
    op.execute("DROP TABLE IF EXISTS account_write_limits")
