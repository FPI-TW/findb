"""unified credentials and admin identity

Revision ID: 8ad9e0f1a2b3
Revises: 7fc8d9e0f1a2
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8ad9e0f1a2b3"
down_revision: Union[str, Sequence[str], None] = "7fc8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("api_key", sa.Column("fingerprint", sa.String(24), nullable=True))
    op.add_column(
        "api_key", sa.Column("kind", sa.String(20), nullable=False, server_default="serve")
    )
    op.add_column("api_key", sa.Column("name", sa.String(100), nullable=True))
    op.add_column("api_key", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("api_key", sa.Column("role", sa.String(20), nullable=True))
    op.add_column("api_key", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "api_key", sa.Column("rotated_from_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_api_key_rotated_from_id_api_key",
        "api_key",
        "api_key",
        ["rotated_from_id"],
        ["key_id"],
    )
    op.create_check_constraint("api_key_kind_valid", "api_key", "kind IN ('serve', 'admin')")
    op.create_check_constraint(
        "api_key_role_valid", "api_key", "role IS NULL OR role IN ('owner', 'operator', 'viewer')"
    )
    op.execute(
        "UPDATE api_key SET fingerprint = left(key_hash, 16), name = owner "
        "WHERE fingerprint IS NULL OR name IS NULL"
    )

    for name, column_type in (
        ("owner", sa.String(100)),
        ("description", sa.Text()),
        ("fingerprint", sa.String(24)),
        ("expires_at", sa.DateTime(timezone=True)),
        ("rotated_from_id", postgresql.UUID(as_uuid=True)),
        ("last_used_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("source_client", sa.Column(name, column_type, nullable=True))
    op.add_column(
        "source_client",
        sa.Column("usage_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_foreign_key(
        "fk_source_client_rotated_from_id_source_client",
        "source_client",
        "source_client",
        ["rotated_from_id"],
        ["client_id"],
    )
    op.execute(
        "UPDATE source_client SET fingerprint = left(key_hash, 16), owner = name "
        "WHERE fingerprint IS NULL OR owner IS NULL"
    )

    op.create_table(
        "admin_user",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('owner', 'operator', 'viewer')", name="admin_user_role_valid"),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("username", name="uq_admin_user_username"),
    )
    op.create_index("idx_admin_user_role_active", "admin_user", ["role", "is_active"])

    op.create_table(
        "admin_session",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["admin_user.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id"),
        sa.UniqueConstraint("token_hash", name="uq_admin_session_token_hash"),
    )
    op.create_index("idx_admin_session_user_active", "admin_session", ["user_id", "revoked_at"])
    op.create_index("idx_admin_session_expires", "admin_session", ["expires_at"])

    op.create_table(
        "credential_usage_rollup",
        sa.Column("usage_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("credential_kind", sa.String(20), nullable=False),
        sa.Column("credential_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_endpoint", sa.String(255), nullable=True),
        sa.Column("last_status", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("usage_id"),
        sa.UniqueConstraint("credential_kind", "credential_id", name="uq_credential_usage_ref"),
    )
    op.create_table(
        "admin_audit_event",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_type", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=True),
        sa.Column("actor_display_name", sa.String(100), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("resource_type", sa.String(30), nullable=False),
        sa.Column("resource_id", sa.String(100), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("idx_admin_audit_event_created", "admin_audit_event", ["created_at"])
    op.create_index(
        "idx_admin_audit_event_resource",
        "admin_audit_event",
        ["resource_type", "resource_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_admin_audit_event_resource", table_name="admin_audit_event")
    op.drop_index("idx_admin_audit_event_created", table_name="admin_audit_event")
    op.drop_table("admin_audit_event")
    op.drop_table("credential_usage_rollup")
    op.drop_index("idx_admin_session_expires", table_name="admin_session")
    op.drop_index("idx_admin_session_user_active", table_name="admin_session")
    op.drop_table("admin_session")
    op.drop_index("idx_admin_user_role_active", table_name="admin_user")
    op.drop_table("admin_user")

    op.drop_constraint(
        "fk_source_client_rotated_from_id_source_client", "source_client", type_="foreignkey"
    )
    for name in (
        "last_used_at",
        "usage_count",
        "rotated_from_id",
        "expires_at",
        "fingerprint",
        "description",
        "owner",
    ):
        op.drop_column("source_client", name)

    op.drop_constraint("api_key_role_valid", "api_key", type_="check")
    op.drop_constraint("api_key_kind_valid", "api_key", type_="check")
    op.drop_constraint("fk_api_key_rotated_from_id_api_key", "api_key", type_="foreignkey")
    for name in (
        "rotated_from_id",
        "expires_at",
        "role",
        "description",
        "name",
        "kind",
        "fingerprint",
    ):
        op.drop_column("api_key", name)
