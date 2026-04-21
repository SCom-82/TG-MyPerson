"""Fix ix_chat_members_snapshots_chat_taken: add DESC on taken_at

Revision ID: 003
Revises: 002
Create Date: 2026-04-21
"""

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chat_members_snapshots_chat_taken;")
    op.execute(
        "CREATE INDEX ix_chat_members_snapshots_chat_taken "
        "ON chat_members_snapshots (chat_id, taken_at DESC);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chat_members_snapshots_chat_taken;")
    op.execute(
        "CREATE INDEX ix_chat_members_snapshots_chat_taken "
        "ON chat_members_snapshots (chat_id, taken_at);"
    )
