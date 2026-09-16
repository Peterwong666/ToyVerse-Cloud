"""订单域：orders

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-16

编号说明：P3 目录域占用 ``0005``/``0006``、P7 占用 ``0007``，故 P4 从
``0008`` 起（原计划的 0005–0007 已被占用，todolist 已同步）。

订单是业务主干「客户下单 → 平台审核 → 生成设备 → 入库」的载体：
* ``tenant_id`` / ``client_product_id`` 都是 ``NOT NULL``——订单必须能回答
  「谁的、哪个产品的」
* ``network_type`` 是**下单时的快照**：生成设备时据此选择云服务商与
  二维码格式，产品后续改配置不影响存量订单
* ``unit_price`` / ``total_amount`` 为敏感字段，工厂端一律不下发（P6）
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base  # noqa: F401 - 自定义类型 UTCDateTime 需要此导入

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """升级。"""
    op.create_table(
        "orders",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("order_no", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("client_product_id", sa.String(length=36), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("total_amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("network_type", sa.String(length=16), nullable=False),
        sa.Column("applicant_name", sa.String(length=64), nullable=True),
        sa.Column("applicant_phone", sa.String(length=32), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("audited_by", sa.String(length=64), nullable=True),
        sa.Column("audited_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("audit_remark", sa.String(length=512), nullable=True),
        sa.Column("reject_reason", sa.String(length=512), nullable=True),
        sa.Column("generated_count", sa.Integer(), nullable=False),
        sa.Column("generated_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("generation_detail", sa.JSON(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
        sa.ForeignKeyConstraint(
            ["client_product_id"],
            ["client_products.id"],
            name=op.f("fk_orders_client_product_id_client_products"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_orders_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
    )
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_orders_client_product_id"), ["client_product_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_orders_network_type"), ["network_type"], unique=False)
        batch_op.create_index(batch_op.f("ix_orders_order_no"), ["order_no"], unique=True)
        batch_op.create_index(batch_op.f("ix_orders_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_orders_tenant_id"), ["tenant_id"], unique=False)


def downgrade() -> None:
    """回滚。"""
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_orders_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_orders_status"))
        batch_op.drop_index(batch_op.f("ix_orders_order_no"))
        batch_op.drop_index(batch_op.f("ix_orders_network_type"))
        batch_op.drop_index(batch_op.f("ix_orders_client_product_id"))
    op.drop_table("orders")
