"""Add sender_chat_id column to tg_messages

Revision ID: 007
Revises: 006
Create Date: 2026-05-17

Adds a nullable BigInteger column sender_chat_id to tg_messages for storing
the channel/chat id of broadcast senders (negative sender_id from Telethon).
No FK intentionally — mirrors forward_from_chat_id pattern (models.py:79).
No data backfill: existing rows correctly remain NULL.
"""

from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tg_messages",
        sa.Column("sender_chat_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_tg_messages_sender_chat",
        "tg_messages",
        ["sender_chat_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_tg_messages_sender_chat", table_name="tg_messages")
    op.drop_column("tg_messages", "sender_chat_id")
