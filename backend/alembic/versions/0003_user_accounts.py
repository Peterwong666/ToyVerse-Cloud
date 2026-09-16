"""用户账号与刷新令牌：user_accounts / refresh_tokens

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-16

* ``user_accounts`` —— 覆盖平台端 / 商户端 / 工厂端三类使用者的登录账号
  - ``tenant_id`` 为 ``NULL`` 表示平台或工厂账号（全局长 / 跨租户）
  - 含登录保护字段：``login_failures`` / ``locked_until``
    （连续失败 5 次锁定 15 分钟，阈值由配置控制）
* ``refresh_tokens`` —— 只存 ``token_hash``（SHA-256），不存明文；
  支持轮换（``rotated_to``）与吊销（``revoked_at``），可检测令牌重放

本迁移依赖 0002 创建的 ``organizations`` / ``positions``。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base  # noqa: F401 - 自定义类型 UTCDateTime 需要此导入

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """升级。"""
    # ------------------------------------------------------------
    # 用户账号
    # ------------------------------------------------------------
    op.create_table(
        "user_accounts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("nickname", sa.String(length=128), nullable=True),
        sa.Column("avatar", sa.String(length=512), nullable=True),
        sa.Column("role_code", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("position_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("login_failures", sa.Integer(), nullable=False),
        sa.Column("locked_until", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("last_login_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("last_login_ip", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            app.db.base.UTCDateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            app.db.base.UTCDateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_user_accounts_organization_id_organizations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["positions.id"],
            name=op.f("fk_user_accounts_position_id_positions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_user_accounts_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_accounts")),
        sa.UniqueConstraint("account", name="uq_user_accounts_account"),
    )
    with op.batch_alter_table("user_accounts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_user_accounts_account"), ["account"], unique=False)
        batch_op.create_index(batch_op.f("ix_user_accounts_role_code"), ["role_code"], unique=False)
        batch_op.create_index(batch_op.f("ix_user_accounts_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_user_accounts_tenant_id"), ["tenant_id"], unique=False)

    # ------------------------------------------------------------
    # 刷新令牌
    # ------------------------------------------------------------
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("revoked_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("rotated_to", sa.String(length=36), nullable=True),
        sa.Column("user_agent", sa.String(length=256), nullable=True),
        sa.Column("client_ip", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            app.db.base.UTCDateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            app.db.base.UTCDateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_accounts.id"],
            name=op.f("fk_refresh_tokens_user_id_user_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
    )
    with op.batch_alter_table("refresh_tokens", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_refresh_tokens_expires_at"), ["expires_at"], unique=False
        )
        # token_hash 既是索引也需唯一，防止摘要碰撞导致串号
        batch_op.create_index(
            batch_op.f("ix_refresh_tokens_token_hash"), ["token_hash"], unique=True
        )
        batch_op.create_index(batch_op.f("ix_refresh_tokens_user_id"), ["user_id"], unique=False)


def downgrade() -> None:
    """回滚。"""
    with op.batch_alter_table("refresh_tokens", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_refresh_tokens_user_id"))
        batch_op.drop_index(batch_op.f("ix_refresh_tokens_token_hash"))
        batch_op.drop_index(batch_op.f("ix_refresh_tokens_expires_at"))
    op.drop_table("refresh_tokens")

    with op.batch_alter_table("user_accounts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_accounts_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_user_accounts_status"))
        batch_op.drop_index(batch_op.f("ix_user_accounts_role_code"))
        batch_op.drop_index(batch_op.f("ix_user_accounts_account"))
    op.drop_table("user_accounts")
