"""Drop legacy tg_session table (Phase 4 multi-account cleanup)

tg_session was the original singleton session store. Migration 002 backfilled
its data into accounts + account_sessions. This migration removes the table
entirely, enforcing that at least one 'work' account exists before dropping.

Revision ID: 004
Revises: 003
Create Date: 2026-04-21
"""

from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # Safety-check: ensure at least one active 'work' account exists.
    # If not, abort with a clear message so operator knows what to do.
    result = bind.execute(
        sa.text("SELECT count(*) FROM accounts WHERE alias = 'work' AND is_enabled = true")
    )
    row_count = result.scalar()
    if not row_count:
        raise RuntimeError(
            "Cannot drop tg_session: no 'work' account registered. "
            "Run migration 002 backfill or manually create account first: "
            "INSERT INTO accounts (alias, phone, mode, is_enabled) "
            "VALUES ('work', '<phone>', 'rw', true);"
        )

    bind.execute(sa.text("DROP TABLE IF EXISTS tg_session CASCADE"))


def downgrade() -> None:
    # Recreate tg_session in exact form from 001_initial.py.
    # Data is NOT restored — this is a structural rollback only.
    op.create_table(
        "tg_session",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("session_name", sa.String(100), unique=True, nullable=False),
        sa.Column("session_string", sa.Text, nullable=False),
        sa.Column("phone_number", sa.String(50), nullable=True),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
