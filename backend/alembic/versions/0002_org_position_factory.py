"""组织架构与工厂：organizations / positions / factories

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-16

* ``organizations`` —— 树形组织架构，按租户隔离
* ``positions`` —— 租户内的职位
* ``factories`` —— 烧录工厂，**跨租户**，不归属任何单一租户

工厂之所以独立建表而非复用 tenants：工厂承接多个租户的生产任务，
其数据保护手段是**字段脱敏**（客户名、金额、联系方式不下发），
而非租户行级过滤。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base  # noqa: F401 - 自定义类型 UTCDateTime 需要此导入

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """升级。"""
    # ------------------------------------------------------------
    # 组织架构
    # ------------------------------------------------------------
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("parent_id", sa.String(length=36), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["organizations.id"],
            name=op.f("fk_organizations_parent_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_organizations_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
    )
    with op.batch_alter_table("organizations", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_organizations_parent_id"), ["parent_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_organizations_tenant_id"), ["tenant_id"], unique=False)

    # ------------------------------------------------------------
    # 职位
    # ------------------------------------------------------------
    op.create_table(
        "positions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_positions_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_positions")),
    )
    with op.batch_alter_table("positions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_positions_code"), ["code"], unique=False)
        batch_op.create_index(batch_op.f("ix_positions_tenant_id"), ["tenant_id"], unique=False)

    # ------------------------------------------------------------
    # 烧录工厂
    # ------------------------------------------------------------
    op.create_table(
        "factories",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("contact_name", sa.String(length=64), nullable=True),
        sa.Column("contact_phone", sa.String(length=32), nullable=True),
        sa.Column("address", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("daily_capacity", sa.Integer(), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_factories")),
    )
    with op.batch_alter_table("factories", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_factories_code"), ["code"], unique=True)
        batch_op.create_index(batch_op.f("ix_factories_status"), ["status"], unique=False)


def downgrade() -> None:
    """回滚。"""
    with op.batch_alter_table("factories", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_factories_status"))
        batch_op.drop_index(batch_op.f("ix_factories_code"))
    op.drop_table("factories")

    with op.batch_alter_table("positions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_positions_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_positions_code"))
    op.drop_table("positions")

    with op.batch_alter_table("organizations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_organizations_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_organizations_parent_id"))
    op.drop_table("organizations")
