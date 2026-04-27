"""Audit log partition rotation helpers (SQL functions)

Creates two helper SQL functions in PostgreSQL:
  - create_audit_partition(months_ahead int): idempotent, creates next month's partition
  - drop_old_audit_partitions(retention_days int): drops partitions older than N days

These functions are called by the cron job (app/scripts/audit_partitions.py)
to automate monthly partition creation and enforce 90-day retention.

Revision ID: 005
Revises: 004
Create Date: 2026-04-27
"""

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE OR REPLACE FUNCTION create_audit_partition(months_ahead int DEFAULT 1)
RETURNS text LANGUAGE plpgsql AS $$
DECLARE
    target_start date := date_trunc('month', current_date + (months_ahead || ' months')::interval)::date;
    target_end   date := (target_start + interval '1 month')::date;
    part_name    text := 'audit_logs_' || to_char(target_start, 'YYYY_MM');
BEGIN
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF audit_logs FOR VALUES FROM (%L) TO (%L)',
        part_name, target_start, target_end
    );
    RETURN part_name;
END;
$$;
""")

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


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS drop_old_audit_partitions(int);")
    op.execute("DROP FUNCTION IF EXISTS create_audit_partition(int);")
