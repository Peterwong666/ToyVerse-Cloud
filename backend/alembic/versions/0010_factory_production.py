"""工厂生产：factory_orders / burn_reports / inspections

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-16

**编号与原计划的差异**：todolist 原计划 ``0010`` 为「设备批次」，
但批次表必须在 ``devices`` 之前创建（否则设备表的外键指向不存在的表），
已并入 ``0009``；本迁移只承载工厂生产的三个表。

与 P6（烧录工厂端）的关系
------------------------
本迁移只建表与基本结构；**字段脱敏**由 P6 在序列化层实现
（客户名脱敏、金额/联系方式绝不下发）。因此这里刻意只给工单保留
「型号 + 数量」这类必要信息，不给客户身份字段——少一个字段，
就少一次脱敏疏漏的机会。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base  # noqa: F401 - 自定义类型 UTCDateTime 需要此导入

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    """创建时间 / 更新时间两列（与 db.base.TimestampMixin 保持一致）。"""
    return [
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
    ]


def upgrade() -> None:
    """升级。"""
    # ------------------------------------------------------------
    # 生产工单
    # ------------------------------------------------------------
    op.create_table(
        "factory_orders",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("factory_order_no", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=36), nullable=False),
        sa.Column("factory_id", sa.String(length=36), nullable=False),
        sa.Column("product_model", sa.String(length=64), nullable=True),
        sa.Column("firmware_version", sa.String(length=64), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("burned_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("assigned_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("due_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("shipped_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_factory_orders")),
        sa.ForeignKeyConstraint(
            ["factory_id"],
            ["factories.id"],
            name=op.f("fk_factory_orders_factory_id_factories"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_factory_orders_order_id_orders"), ondelete="CASCADE"
        ),
    )
    with op.batch_alter_table("factory_orders", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_factory_orders_factory_order_no"), ["factory_order_no"], unique=True
        )
        batch_op.create_index(batch_op.f("ix_factory_orders_factory_id"), ["factory_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_factory_orders_order_id"), ["order_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_factory_orders_status"), ["status"], unique=False)
        batch_op.create_index("idx_factory_orders_factory_status", ["factory_id", "status"], unique=False)

    # ------------------------------------------------------------
    # 烧录上报
    # ------------------------------------------------------------
    op.create_table(
        "burn_reports",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("factory_order_id", sa.String(length=36), nullable=False),
        sa.Column("burned_count", sa.Integer(), nullable=False),
        sa.Column("sn_from", sa.String(length=64), nullable=True),
        sa.Column("sn_to", sa.String(length=64), nullable=True),
        sa.Column("operator", sa.String(length=64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("reported_at", app.db.base.UTCDateTime(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_burn_reports")),
        sa.ForeignKeyConstraint(
            ["factory_order_id"],
            ["factory_orders.id"],
            name=op.f("fk_burn_reports_factory_order_id_factory_orders"),
            ondelete="CASCADE",
        ),
    )
    with op.batch_alter_table("burn_reports", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_burn_reports_factory_order_id"), ["factory_order_id"], unique=False
        )

    # ------------------------------------------------------------
    # 抽检记录
    # ------------------------------------------------------------
    op.create_table(
        "inspections",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("factory_order_id", sa.String(length=36), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=True),
        sa.Column("sn", sa.String(length=64), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("inspector", sa.String(length=64), nullable=True),
        sa.Column("defect_code", sa.String(length=64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("inspected_at", app.db.base.UTCDateTime(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inspections")),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"], name=op.f("fk_inspections_device_id_devices"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["factory_order_id"],
            ["factory_orders.id"],
            name=op.f("fk_inspections_factory_order_id_factory_orders"),
            ondelete="CASCADE",
        ),
    )
    with op.batch_alter_table("inspections", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_inspections_device_id"), ["device_id"], unique=False)
        batch_op.create_index("idx_inspections_order_sn", ["factory_order_id", "sn"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_inspections_factory_order_id"), ["factory_order_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_inspections_result"), ["result"], unique=False)
        batch_op.create_index(batch_op.f("ix_inspections_sn"), ["sn"], unique=False)


def downgrade() -> None:
    """回滚（按依赖逆序）。"""
    with op.batch_alter_table("inspections", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_inspections_sn"))
        batch_op.drop_index(batch_op.f("ix_inspections_result"))
        batch_op.drop_index(batch_op.f("ix_inspections_factory_order_id"))
        batch_op.drop_index("idx_inspections_order_sn")
        batch_op.drop_index(batch_op.f("ix_inspections_device_id"))
    op.drop_table("inspections")

    with op.batch_alter_table("burn_reports", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_burn_reports_factory_order_id"))
    op.drop_table("burn_reports")

    with op.batch_alter_table("factory_orders", schema=None) as batch_op:
        batch_op.drop_index("idx_factory_orders_factory_status")
        batch_op.drop_index(batch_op.f("ix_factory_orders_status"))
        batch_op.drop_index(batch_op.f("ix_factory_orders_order_id"))
        batch_op.drop_index(batch_op.f("ix_factory_orders_factory_id"))
        batch_op.drop_index(batch_op.f("ix_factory_orders_factory_order_no"))
    op.drop_table("factory_orders")
