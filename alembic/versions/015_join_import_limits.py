"""MAX: limits of joining chats and importing contacts (plan 2026-10-19 Р4 §3 PR-R4-1, Р-39).

Revision ID: 015
Revises: 014
Create Date: 2026-10-19

- account_write_limits.chats_joined_per_day (DEFAULT 5, one per join) and
  contacts_imported_per_day (DEFAULT 50, one per number sent to MAX): DEFAULT
  fills the existing rows and those an older code would create after a rollback.
- write_events.kind gets 'chat_join' and 'contact_import'.
- No new tables or sequences: the grants of 012 cover both.
"""

from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE account_write_limits"
               " ADD COLUMN chats_joined_per_day int DEFAULT 5 CHECK (chats_joined_per_day >= 0),"
               " ADD COLUMN contacts_imported_per_day int DEFAULT 50 CHECK (contacts_imported_per_day >= 0)")
    op.execute("ALTER TABLE write_events DROP CONSTRAINT write_events_kind_check")
    op.execute("ALTER TABLE write_events ADD CONSTRAINT write_events_kind_check"
               " CHECK (kind IN ('send', 'action', 'lookup', 'group_create', 'group_invite',"
               " 'chat_join', 'contact_import'))")


def downgrade() -> None:
    op.execute("DELETE FROM write_events WHERE kind IN ('chat_join', 'contact_import')")
    op.execute("ALTER TABLE write_events DROP CONSTRAINT write_events_kind_check")
    op.execute("ALTER TABLE write_events ADD CONSTRAINT write_events_kind_check"
               " CHECK (kind IN ('send', 'action', 'lookup', 'group_create', 'group_invite'))")
    op.execute("ALTER TABLE account_write_limits DROP COLUMN chats_joined_per_day,"
               " DROP COLUMN contacts_imported_per_day")
