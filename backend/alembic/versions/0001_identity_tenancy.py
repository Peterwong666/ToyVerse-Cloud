"""身份与租户：tenants / roles / role_permissions

Revision ID: 0001
Revises:
Create Date: 2026-09-16

本迁移是整条迁移链的起点，建立多租户体系的根基：

* ``tenants`` —— 租户（客户），合并了原型的客户业务字段
* ``roles`` / ``role_permissions`` —— RBAC 角色与权限

注意：``user_accounts`` 因依赖 ``organizations`` / ``positions``，
安排在后继迁移中创建，以保持外键依赖顺序正确。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base  # noqa: F401 - 自定义类型 UTCDateTime 需要此导入

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """升级。"""
    # ------------------------------------------------------------
    # 租户
    # ------------------------------------------------------------
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("contact_name", sa.String(length=64), nullable=True),
        sa.Column("contact_phone", sa.String(length=32), nullable=True),
        sa.Column("email", sa.String(length=128), nullable=True),
        sa.Column("industry", sa.String(length=64), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
    )
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_tenants_code"), ["code"], unique=True)
        batch_op.create_index(batch_op.f("ix_tenants_status"), ["status"], unique=False)

    # ------------------------------------------------------------
    # 角色
    # ------------------------------------------------------------
    op.create_table(
        "roles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("role_type", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_builtin", sa.Boolean(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
    )
    with op.batch_alter_table("roles", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_roles_code"), ["code"], unique=True)
        batch_op.create_index(batch_op.f("ix_roles_role_type"), ["role_type"], unique=False)

    # ------------------------------------------------------------
    # 角色权限
    # ------------------------------------------------------------
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.String(length=36), nullable=False),
        sa.Column("permission", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name=op.f("fk_role_permissions_role_id_roles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("role_id", "permission", name=op.f("pk_role_permissions")),
    )


def downgrade() -> None:
    """回滚。"""
    op.drop_table("role_permissions")

    with op.batch_alter_table("roles", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_roles_role_type"))
        batch_op.drop_index(batch_op.f("ix_roles_code"))
    op.drop_table("roles")

    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_tenants_status"))
        batch_op.drop_index(batch_op.f("ix_tenants_code"))
    op.drop_table("tenants")
