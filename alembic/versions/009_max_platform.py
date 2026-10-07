"""MAX platform: accounts.platform + max_* tables

Revision ID: 009
Revises: 008
Create Date: 2026-10-07

Additive only — see _system/docs/architect/2026-10-07-tg-myperson-max-adr.md §2.A.

  - accounts: platform (DEFAULT 'telegram', CHECK telegram|max), platform_user_id,
    write_chat_ids, write_rate_per_hour. Existing rows get platform='telegram'
    from the column default; the phase-2 write columns stay NULL (TG unchanged).
  - max_users / max_chats / max_messages / max_media / max_sync_state mirror the
    tg_* tables column-for-column (including the tg_date name), so the existing
    Pydantic response schemas validate MAX rows without mapping. MAX-specific
    additions: max_messages.deleted_at, max_media.attach_index,
    max_sync_state.{oldest_time_ms, newest_time_ms, last_catchup_at}.
  - max_raw_events: raw frame journal for reprocessing (retention is in-process).

tg_*, chat_access and the earlier migrations are not touched.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # accounts — platform + phase-2 write guards
    # ------------------------------------------------------------------
    op.add_column(
        "accounts",
        sa.Column("platform", sa.Text, server_default=sa.text("'telegram'"), nullable=False),
    )
    op.execute(
        "ALTER TABLE accounts ADD CONSTRAINT ck_accounts_platform "
        "CHECK (platform IN ('telegram', 'max'))"
    )
    op.add_column("accounts", sa.Column("platform_user_id", sa.BigInteger, nullable=True))
    op.add_column("accounts", sa.Column("write_chat_ids", ARRAY(sa.BigInteger), nullable=True))
    op.add_column("accounts", sa.Column("write_rate_per_hour", sa.Integer, nullable=True))

    # ------------------------------------------------------------------
    # max_users — shape of tg_users
    # ------------------------------------------------------------------
    op.create_table(
        "max_users",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("first_name", sa.String(255), nullable=True),
        sa.Column("last_name", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("is_bot", sa.Boolean, server_default=sa.false()),
        sa.Column("is_self", sa.Boolean, server_default=sa.false()),
        sa.Column("raw_data", JSONB, nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_max_users_username", "max_users", ["username"])

    # ------------------------------------------------------------------
    # max_chats — shape of tg_chats
    # ------------------------------------------------------------------
    op.create_table(
        "max_chats",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("chat_type", sa.String(20), nullable=False),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("members_count", sa.Integer, nullable=True),
        sa.Column("is_monitored", sa.Boolean, server_default=sa.true()),
        sa.Column("last_message_id", sa.BigInteger, nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_data", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_max_chats_chat_type", "max_chats", ["chat_type"])
    op.create_index("ix_max_chats_username", "max_chats", ["username"])
    op.create_index("ix_max_chats_is_monitored", "max_chats", ["is_monitored"])

    # ------------------------------------------------------------------
    # max_messages — shape of tg_messages + deleted_at (ADR §2.G)
    # ------------------------------------------------------------------
    op.create_table(
        "max_messages",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.BigInteger, nullable=False),
        sa.Column("chat_id", sa.BigInteger, sa.ForeignKey("max_chats.id"), nullable=False),
        sa.Column("from_user_id", sa.BigInteger, sa.ForeignKey("max_users.id"), nullable=True),
        sa.Column("sender_chat_id", sa.BigInteger, nullable=True),
        sa.Column("reply_to_message_id", sa.BigInteger, nullable=True),
        sa.Column("forward_from_chat_id", sa.BigInteger, nullable=True),
        sa.Column("forward_from_message_id", sa.BigInteger, nullable=True),
        sa.Column("message_type", sa.String(30), server_default=sa.text("'text'")),
        sa.Column("text", sa.Text, nullable=True),
        sa.Column("text_html", sa.Text, nullable=True),
        sa.Column("tg_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_outgoing", sa.Boolean, server_default=sa.false()),
        sa.Column("is_edited", sa.Boolean, server_default=sa.false()),
        sa.Column("edit_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("views", sa.Integer, nullable=True),
        sa.Column("raw_data", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_max_messages_msg_chat", "max_messages", ["message_id", "chat_id"])
    op.create_index("ix_max_messages_chat_date", "max_messages", ["chat_id", "tg_date"])
    op.create_index("ix_max_messages_from_user", "max_messages", ["from_user_id"])
    op.create_index("ix_max_messages_type", "max_messages", ["message_type"])
    op.create_index("ix_max_messages_sender_chat", "max_messages", ["sender_chat_id"])

    # ------------------------------------------------------------------
    # max_media — shape of tg_media + attach_index (several attachments per message)
    # ------------------------------------------------------------------
    op.create_table(
        "max_media",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("message_pk", sa.BigInteger, sa.ForeignKey("max_messages.id"), nullable=False),
        sa.Column("file_id", sa.String(255), nullable=True),
        sa.Column("file_unique_id", sa.String(255), nullable=True),
        sa.Column("file_type", sa.String(30), nullable=False),
        sa.Column("file_name", sa.String(500), nullable=True),
        sa.Column("file_size", sa.BigInteger, nullable=True),
        sa.Column("mime_type", sa.String(100), nullable=True),
        sa.Column("local_path", sa.String(1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("attach_index", sa.SmallInteger, server_default=sa.text("0"), nullable=False),
    )
    op.create_unique_constraint(
        "uq_max_media_message_attach", "max_media", ["message_pk", "attach_index"]
    )

    # ------------------------------------------------------------------
    # max_sync_state — shape of tg_sync_state + time cursors (ADR §2.J)
    # ------------------------------------------------------------------
    op.create_table(
        "max_sync_state",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger, sa.ForeignKey("max_chats.id"), unique=True, nullable=False),
        sa.Column("oldest_message_id", sa.BigInteger, nullable=True),
        sa.Column("newest_message_id", sa.BigInteger, nullable=True),
        sa.Column("oldest_time_ms", sa.BigInteger, nullable=True),
        sa.Column("newest_time_ms", sa.BigInteger, nullable=True),
        sa.Column("is_fully_synced", sa.Boolean, server_default=sa.false()),
        sa.Column("total_messages_synced", sa.Integer, server_default=sa.text("0")),
        sa.Column("last_backfill_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_catchup_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ------------------------------------------------------------------
    # max_raw_events — raw frame journal for reprocessing (ADR §2.E)
    # ------------------------------------------------------------------
    op.create_table(
        "max_raw_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id"), nullable=True),
        sa.Column("opcode", sa.Integer, nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("payload", JSONB, nullable=True),
        sa.Column("normalized", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column("error", sa.Text, nullable=True),
    )
    op.create_index("ix_max_raw_events_received_at", "max_raw_events", ["received_at"])
    op.create_index(
        "ix_max_raw_events_not_normalized",
        "max_raw_events",
        ["normalized"],
        postgresql_where=sa.text("normalized = false"),
    )


def downgrade() -> None:
    op.drop_table("max_raw_events")
    op.drop_table("max_sync_state")
    op.drop_table("max_media")
    op.drop_table("max_messages")
    op.drop_table("max_chats")
    op.drop_table("max_users")

    op.drop_column("accounts", "write_rate_per_hour")
    op.drop_column("accounts", "write_chat_ids")
    op.drop_column("accounts", "platform_user_id")
    op.execute("ALTER TABLE accounts DROP CONSTRAINT ck_accounts_platform")
    op.drop_column("accounts", "platform")
