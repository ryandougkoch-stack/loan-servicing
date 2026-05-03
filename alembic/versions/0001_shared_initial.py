"""Initial shared schema

Revision ID: 0001_shared_initial
Revises:
Create Date: 2024-01-01 00:00:00

Creates all tables in the shared schema:
  - tenants
  - users
  - tenant_memberships
  - platform_audit_log
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_shared_initial"
down_revision = None
branch_labels = ("shared",)
depends_on = None


def upgrade() -> None:
    # Ensure shared schema exists (idempotent)
    op.execute("CREATE SCHEMA IF NOT EXISTS shared")
    op.execute('SET search_path TO shared, public')

    # ------------------------------------------------------------------
    # tenants
    # ------------------------------------------------------------------
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("plan", sa.Text(), nullable=False, server_default="standard"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
        sa.CheckConstraint(
            "status IN ('active','suspended','offboarded')",
            name="ck_tenants_status",
        ),
        schema="shared",
    )

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("mfa_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.CheckConstraint(
            "status IN ('active','locked','deactivated')",
            name="ck_users_status",
        ),
        schema="shared",
    )
    op.create_index("ix_shared_users_email_lower",
                    "users", [sa.text("LOWER(email)")], schema="shared")

    # ------------------------------------------------------------------
    # tenant_memberships
    # ------------------------------------------------------------------
    op.create_table(
        "tenant_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("portfolio_scope", postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
                  nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["tenant_id"], ["shared.tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["shared.users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "user_id"),
        sa.CheckConstraint(
            "role IN ('admin','ops','finance','compliance','reporting','api_service')",
            name="ck_memberships_role",
        ),
        schema="shared",
    )
    op.create_index("ix_shared_memberships_tenant", "tenant_memberships",
                    ["tenant_id"], schema="shared")
    op.create_index("ix_shared_memberships_user", "tenant_memberships",
                    ["user_id"], schema="shared")

    # ------------------------------------------------------------------
    # platform_audit_log
    # ------------------------------------------------------------------
    op.create_table(
        "platform_audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("event_data", postgresql.JSONB(), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["tenant_id"], ["shared.tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["shared.users.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema="shared",
    )
    op.create_index("ix_shared_audit_tenant_ts", "platform_audit_log",
                    ["tenant_id", sa.text("created_at DESC")], schema="shared")
    op.create_index("ix_shared_audit_user_ts", "platform_audit_log",
                    ["user_id", sa.text("created_at DESC")], schema="shared")


def downgrade() -> None:
    op.execute('SET search_path TO shared, public')
    op.drop_table("platform_audit_log", schema="shared")
    op.drop_table("tenant_memberships", schema="shared")
    op.drop_table("users", schema="shared")
    op.drop_table("tenants", schema="shared")
