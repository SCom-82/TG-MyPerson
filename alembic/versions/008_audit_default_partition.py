"""Self-sufficient audit_logs partition lifecycle: DEFAULT partition + drain helper

Revision ID: 008
Revises: 007
Create Date: 2026-06-16

Implements Layer 0 (DEFAULT partition safety-net) and Layer 3 (drain-from-default)
of the self-sufficient partition rotation design — see
_system/docs/architect/2026-06-16-tg-myperson-self-sufficient-partition-lifecycle.md.

After this migration:
  - audit_logs_default catches any row whose month has no dedicated partition, so
    INSERTs into audit_logs can never fail with a partition constraint violation.
    This is the deadline-critical guarantee: it removes the 2026-07-01 silent
    audit-loss failure mode independently of any application code.
  - current + next two months are materialised immediately (create_audit_partition
    0/1/2) so prod is correct the moment the migration is applied.
  - drain_audit_default_for_month(date) moves rows that accumulated in DEFAULT for
    a given month into a freshly-created monthly partition (only needed when DEFAULT
    is non-empty; in steady state DEFAULT stays empty and this is never called).
  - ensure_audit_partition(months_ahead) is the robust entry point used by the
    in-process maintenance loop: create the month, and if DEFAULT already holds
    rows for it (which would make a plain CREATE ... PARTITION OF fail with 23514),
    drain instead.

Note: create_audit_partition (migration 005) already handles months_ahead=0 —
date_trunc('month', current_date + interval '0 months') yields the current month.
No change to that function is required.
"""

from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- Layer 0: DEFAULT partition (safety-net) ----------------------------
    op.execute(
        "CREATE TABLE IF NOT EXISTS audit_logs_default PARTITION OF audit_logs DEFAULT;"
    )

    # ---- Layer 3: drain-from-default helper ---------------------------------
    # Moves rows for [target_start, target_start + 1 month) out of the DEFAULT
    # partition into a dedicated monthly partition. DETACH the default first:
    # while it is attached, creating an overlapping partition would scan it and
    # fail if matching rows exist. After moving + deleting the rows, reattaching
    # the (now non-overlapping) default succeeds.
    op.execute(r"""
CREATE OR REPLACE FUNCTION drain_audit_default_for_month(target_start date)
RETURNS text LANGUAGE plpgsql AS $func$
DECLARE
    target_end date := (target_start + interval '1 month')::date;
    part_name  text := 'audit_logs_' || to_char(target_start, 'YYYY_MM');
BEGIN
    EXECUTE 'ALTER TABLE audit_logs DETACH PARTITION audit_logs_default';

    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF audit_logs FOR VALUES FROM (%L) TO (%L)',
        part_name, target_start, target_end
    );

    EXECUTE format(
        'INSERT INTO audit_logs SELECT * FROM audit_logs_default WHERE ts >= %L AND ts < %L',
        target_start, target_end
    );
    EXECUTE format(
        'DELETE FROM audit_logs_default WHERE ts >= %L AND ts < %L',
        target_start, target_end
    );

    EXECUTE 'ALTER TABLE audit_logs ATTACH PARTITION audit_logs_default DEFAULT';

    RETURN part_name;
END;
$func$;
""")

    # ---- Robust ensure entry point (create-or-drain) ------------------------
    # Used by the in-process maintenance loop. Idempotent.
    op.execute(r"""
CREATE OR REPLACE FUNCTION ensure_audit_partition(months_ahead int DEFAULT 1)
RETURNS text LANGUAGE plpgsql AS $func$
DECLARE
    target_start date := date_trunc('month', current_date + (months_ahead || ' months')::interval)::date;
    target_end   date := (target_start + interval '1 month')::date;
    part_name    text := 'audit_logs_' || to_char(target_start, 'YYYY_MM');
    already_exists boolean;
    default_has_rows boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM pg_inherits
        JOIN pg_class c ON c.oid = inhrelid
        WHERE inhparent = 'audit_logs'::regclass AND c.relname = part_name
    ) INTO already_exists;

    IF already_exists THEN
        RETURN part_name;
    END IF;

    EXECUTE format(
        'SELECT EXISTS (SELECT 1 FROM audit_logs_default WHERE ts >= %L AND ts < %L)',
        target_start, target_end
    ) INTO default_has_rows;

    IF default_has_rows THEN
        RETURN drain_audit_default_for_month(target_start);
    ELSE
        RETURN create_audit_partition(months_ahead);
    END IF;
END;
$func$;
""")

    # ---- Materialise current + 2 future months right now --------------------
    # DEFAULT was just created empty above, so plain create is safe here.
    op.execute("SELECT create_audit_partition(0);")
    op.execute("SELECT create_audit_partition(1);")
    op.execute("SELECT create_audit_partition(2);")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS ensure_audit_partition(int);")

    # Before dropping DEFAULT, drain any rows it holds into monthly partitions so
    # no audit data is lost. Walk each distinct month present in DEFAULT.
    op.execute(r"""
DO $do$
DECLARE
    m date;
BEGIN
    IF to_regclass('audit_logs_default') IS NULL THEN
        RETURN;
    END IF;
    FOR m IN
        SELECT DISTINCT date_trunc('month', ts)::date
        FROM audit_logs_default
        ORDER BY 1
    LOOP
        PERFORM drain_audit_default_for_month(m);
    END LOOP;
END;
$do$;
""")

    op.execute("DROP TABLE IF EXISTS audit_logs_default;")
    op.execute("DROP FUNCTION IF EXISTS drain_audit_default_for_month(date);")
