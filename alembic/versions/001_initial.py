"""Initial schema: tg_users, tg_chats, tg_messages, tg_media, tg_session, tg_sync_state

Revision ID: 001
Revises:
Create Date: 2026-03-18
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # tg_users
    op.create_table(
        "tg_users",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("first_name", sa.String(255), nullable=True),
        sa.Column("last_name", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("is_bot", sa.Boolean, default=False),
        sa.Column("is_self", sa.Boolean, default=False),
        sa.Column("raw_data", JSONB, nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_tg_users_username", "tg_users", ["username"])

    # tg_chats
    op.create_table(
        "tg_chats",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("chat_type", sa.String(20), nullable=False),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("members_count", sa.Integer, nullable=True),
        sa.Column("is_monitored", sa.Boolean, default=True),
        sa.Column("last_message_id", sa.BigInteger, nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_data", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_tg_chats_chat_type", "tg_chats", ["chat_type"])
    op.create_index("ix_tg_chats_username", "tg_chats", ["username"])
    op.create_index("ix_tg_chats_is_monitored", "tg_chats", ["is_monitored"])

    # tg_messages
    op.create_table(
        "tg_messages",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.BigInteger, nullable=False),
        sa.Column("chat_id", sa.BigInteger, sa.ForeignKey("tg_chats.id"), nullable=False),
        sa.Column("from_user_id", sa.BigInteger, sa.ForeignKey("tg_users.id"), nullable=True),
        sa.Column("reply_to_message_id", sa.BigInteger, nullable=True),
        sa.Column("forward_from_chat_id", sa.BigInteger, nullable=True),
        sa.Column("forward_from_message_id", sa.BigInteger, nullable=True),
        sa.Column("message_type", sa.String(30), default="text"),
        sa.Column("text", sa.Text, nullable=True),
        sa.Column("text_html", sa.Text, nullable=True),
        sa.Column("tg_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_outgoing", sa.Boolean, default=False),
        sa.Column("is_edited", sa.Boolean, default=False),
        sa.Column("edit_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("views", sa.Integer, nullable=True),
        sa.Column("raw_data", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_unique_constraint("uq_tg_messages_msg_chat", "tg_messages", ["message_id", "chat_id"])
    op.create_index("ix_tg_messages_chat_date", "tg_messages", ["chat_id", "tg_date"])
    op.create_index("ix_tg_messages_from_user", "tg_messages", ["from_user_id"])
    op.create_index("ix_tg_messages_type", "tg_messages", ["message_type"])

    # tg_media
    op.create_table(
        "tg_media",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("message_pk", sa.BigInteger, sa.ForeignKey("tg_messages.id"), nullable=False),
        sa.Column("file_id", sa.String(255), nullable=True),
        sa.Column("file_unique_id", sa.String(255), nullable=True),
        sa.Column("file_type", sa.String(30), nullable=False),
        sa.Column("file_name", sa.String(500), nullable=True),
        sa.Column("file_size", sa.BigInteger, nullable=True),
        sa.Column("mime_type", sa.String(100), nullable=True),
        sa.Column("local_path", sa.String(1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # tg_session
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

    # tg_sync_state
    op.create_table(
        "tg_sync_state",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger, sa.ForeignKey("tg_chats.id"), unique=True, nullable=False),
        sa.Column("oldest_message_id", sa.BigInteger, nullable=True),
        sa.Column("newest_message_id", sa.BigInteger, nullable=True),
        sa.Column("is_fully_synced", sa.Boolean, default=False),
        sa.Column("total_messages_synced", sa.Integer, default=0),
        sa.Column("last_backfill_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("tg_sync_state")
    op.drop_table("tg_session")
    op.drop_table("tg_media")
    op.drop_table("tg_messages")
    op.drop_table("tg_chats")
    op.drop_table("tg_users")
