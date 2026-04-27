"""Fix regex in drop_old_audit_partitions to handle timezone-aware bounds

The original regex in migration 005 used pattern TO ('([0-9-]+)') which
only matches date-only strings. PostgreSQL partition bounds include a full
timestamp with timezone, e.g. TO ('2026-05-01 00:00:00+00').
The [0-9-]+ pattern stops at the first space, so the closing quote never
matches and regexp_match returns NULL. upper_bound is always NULL so the
IF condition never triggers and no partitions are ever dropped.

Fix: use [^']+ to capture everything up to the closing quote, then cast
via ::timestamptz::date.

Revision ID: 006
Revises: 005
Create Date: 2026-04-27
"""

from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS drop_old_audit_partitions(int);")

    op.execute(r"""
CREATE OR REPLACE FUNCTION drop_old_audit_partitions(retention_days int DEFAULT 90)
RETURNS SETOF text LANGUAGE plpgsql AS $func$
DECLARE
    part_record record;
    cutoff      date := current_date - (retention_days || ' days')::interval;
BEGIN
    FOR part_record IN
        SELECT
            inhrelid::regclass::text AS part_name,
            (regexp_match(
                pg_get_expr(c.relpartbound, c.oid),
                $$TO \('([^']+)'\)$$
            ))[1]::timestamptz::date AS upper_bound
        FROM pg_inherits
        JOIN pg_class c ON c.oid = inhrelid
        WHERE inhparent = 'audit_logs'::regclass
    LOOP
        IF part_record.upper_bound IS NOT NULL AND part_record.upper_bound <= cutoff THEN
            EXECUTE format('DROP TABLE %I', part_record.part_name);
            RETURN NEXT part_record.part_name;
        END IF;
    END LOOP;
END;
$func$;
""")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS drop_old_audit_partitions(int);")

    # Restore the broken version from migration 005 for downgrade consistency
    op.execute(r"""
CREATE OR REPLACE FUNCTION drop_old_audit_partitions(retention_days int DEFAULT 90)
RETURNS SETOF text LANGUAGE plpgsql AS $func$
DECLARE
    part_record record;
    cutoff      date := current_date - (retention_days || ' days')::interval;
BEGIN
    FOR part_record IN
        SELECT
            inhrelid::regclass::text AS part_name,
            (regexp_match(
                pg_get_expr(c.relpartbound, c.oid),
                $$TO \('([0-9-]+)'\)$$
            ))[1]::date AS upper_bound
        FROM pg_inherits
        JOIN pg_class c ON c.oid = inhrelid
        WHERE inhparent = 'audit_logs'::regclass
    LOOP
        IF part_record.upper_bound IS NOT NULL AND part_record.upper_bound <= cutoff THEN
            EXECUTE format('DROP TABLE %I', part_record.part_name);
            RETURN NEXT part_record.part_name;
        END IF;
    END LOOP;
END;
$func$;
""")
