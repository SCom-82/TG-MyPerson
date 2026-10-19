"""Phone lookups: a limit per account and their counter (plan 2026-10-19 §4 PR-D1, Р-14).

Revision ID: 013
Revises: 012
Create Date: 2026-10-19

- account_write_limits.phone_lookups_per_day: lookups by phone in 24 h
  (contact_by_phone). DEFAULT 20 fills the existing rows and the rows an older
  code would create after a rollback — none of them switches lookups off.
- write_events.kind gets 'lookup': one row per lookup that reached the
  messenger.
- No new tables or sequences: the grants of 012 cover both.
"""

from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE account_write_limits ADD COLUMN phone_lookups_per_day int DEFAULT 20"
               " CHECK (phone_lookups_per_day >= 0)")
    op.execute("ALTER TABLE write_events DROP CONSTRAINT write_events_kind_check")
    op.execute("ALTER TABLE write_events ADD CONSTRAINT write_events_kind_check"
               " CHECK (kind IN ('send', 'action', 'lookup'))")


def downgrade() -> None:
    op.execute("DELETE FROM write_events WHERE kind = 'lookup'")
    op.execute("ALTER TABLE write_events DROP CONSTRAINT write_events_kind_check")
    op.execute("ALTER TABLE write_events ADD CONSTRAINT write_events_kind_check CHECK (kind IN ('send', 'action'))")
    op.execute("ALTER TABLE account_write_limits DROP COLUMN phone_lookups_per_day")
