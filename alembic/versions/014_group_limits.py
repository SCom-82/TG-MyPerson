"""MAX groups: limits per account and their counters (plan 2026-10-19 Р3 §3 PR-G1, Р-23).

Revision ID: 014
Revises: 013
Create Date: 2026-10-19

- account_write_limits.groups_created_per_day (DEFAULT 3) and
  group_invites_per_day (DEFAULT 30, one per person added): DEFAULT fills the
  existing rows and those an older code would create after a rollback.
- write_events.kind gets 'group_create' and 'group_invite'.
- No new tables or sequences: the grants of 012 cover both.
"""

from alembic import op

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE account_write_limits"
               " ADD COLUMN groups_created_per_day int DEFAULT 3 CHECK (groups_created_per_day >= 0),"
               " ADD COLUMN group_invites_per_day int DEFAULT 30 CHECK (group_invites_per_day >= 0)")
    op.execute("ALTER TABLE write_events DROP CONSTRAINT write_events_kind_check")
    op.execute("ALTER TABLE write_events ADD CONSTRAINT write_events_kind_check"
               " CHECK (kind IN ('send', 'action', 'lookup', 'group_create', 'group_invite'))")


def downgrade() -> None:
    op.execute("DELETE FROM write_events WHERE kind IN ('group_create', 'group_invite')")
    op.execute("ALTER TABLE write_events DROP CONSTRAINT write_events_kind_check")
    op.execute("ALTER TABLE write_events ADD CONSTRAINT write_events_kind_check"
               " CHECK (kind IN ('send', 'action', 'lookup'))")
    op.execute("ALTER TABLE account_write_limits DROP COLUMN groups_created_per_day,"
               " DROP COLUMN group_invites_per_day")
