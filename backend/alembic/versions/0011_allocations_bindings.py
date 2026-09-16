"""分配与绑定域：allocation_orders / allocation_items / device_bindings

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-16

建表顺序
--------
1. ``allocation_orders`` —— 分配单（给谁 + 挂哪个产品 + 共几台）
2. ``allocation_items`` —— 分配明细，外键同时指向分配单与设备，
   因此必须排在两者之后
3. ``device_bindings`` —— 绑定记录（含两步流程的确认令牌摘要）

三张表承载 P5 的两条链路：``allocation_*`` 解决「平台把库存交给哪个租户」，
``device_bindings`` 解决「终端用户绑到哪台设备」。

关于确认令牌为什么复用 ``device_bindings``
------------------------------------------
``precheck`` 签发的 5 分钟令牌需要一个「用一次即销毁」的落点。
独立开一张 ``binding_confirm_tokens`` 语义上更干净，但会让
「绑定意图」与「绑定事实」分散在两处、需要在 bind 时跨表搬运状态。
本迁移选择在同一行上表达完整生命周期（``status=PENDING → BOUND``，
令牌摘要置空即销毁），并让 ``UNIQUE(device_id)`` 在**数据库层**
保证单绑约束——并发下也不依赖服务层的「先查后写」。

索引与 ORM 模型的一致性
-----------------------
P4 踩过的坑：迁移里建了而模型没声明的索引会让 ``alembic check``
报 ``remove_index`` 漂移。本迁移创建的每一个索引都在
``app/models/allocation.py`` 的 ``__table_args__`` 或 ``index=True``
里有对应声明。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base  # noqa: F401 - 自定义类型 UTCDateTime 需要此导入

revision: str = "0011"
down_revision: str | None = "0010"
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
    # 1) 分配单
    # ------------------------------------------------------------
    op.create_table(
        "allocation_orders",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("allocation_no", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("client_product_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("allocated_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("executed_by", sa.String(length=64), nullable=True),
        sa.Column("executed_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("failure_reason", sa.String(length=512), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_allocation_orders")),
        sa.ForeignKeyConstraint(
            ["client_product_id"],
            ["client_products.id"],
            name=op.f("fk_allocation_orders_client_product_id_client_products"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_allocation_orders_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
    )
    with op.batch_alter_table("allocation_orders", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_allocation_orders_allocation_no"), ["allocation_no"], unique=True
        )
        batch_op.create_index(
            batch_op.f("ix_allocation_orders_client_product_id"), ["client_product_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_allocation_orders_status"), ["status"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_allocation_orders_tenant_id"), ["tenant_id"], unique=False
        )
        batch_op.create_index(
            "idx_allocation_orders_tenant_status", ["tenant_id", "status"], unique=False
        )

    # ------------------------------------------------------------
    # 2) 分配明细
    # ------------------------------------------------------------
    op.create_table(
        "allocation_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("allocation_order_id", sa.String(length=36), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=False),
        sa.Column("device_sn", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("allocated_at", app.db.base.UTCDateTime(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_allocation_items")),
        sa.ForeignKeyConstraint(
            ["allocation_order_id"],
            ["allocation_orders.id"],
            name=op.f("fk_allocation_items_allocation_order_id_allocation_orders"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_allocation_items_device_id_devices"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "allocation_order_id", "device_id", name="uq_allocation_items_order_device"
        ),
    )
    with op.batch_alter_table("allocation_items", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_allocation_items_allocation_order_id"),
            ["allocation_order_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_allocation_items_device_id"), ["device_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_allocation_items_device_sn"), ["device_sn"], unique=False)
        batch_op.create_index(batch_op.f("ix_allocation_items_status"), ["status"], unique=False)

    # ------------------------------------------------------------
    # 3) 设备绑定（含确认令牌摘要）
    # ------------------------------------------------------------
    op.create_table(
        "device_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("client_product_id", sa.String(length=36), nullable=True),
        sa.Column("end_user_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("qr_format", sa.String(length=8), nullable=True),
        sa.Column("qr_payload_hash", sa.String(length=64), nullable=True),
        sa.Column("confirm_token_hash", sa.String(length=128), nullable=True),
        sa.Column("confirm_expires_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("confirmed_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("bound_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("bound_by", sa.String(length=64), nullable=True),
        sa.Column("unbound_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("unbind_reason", sa.String(length=512), nullable=True),
        sa.Column("unbound_by", sa.String(length=64), nullable=True),
        sa.Column("bind_count", sa.Integer(), nullable=False),
        sa.Column("remark", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_bindings")),
        sa.ForeignKeyConstraint(
            ["client_product_id"],
            ["client_products.id"],
            name=op.f("fk_device_bindings_client_product_id_client_products"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_device_bindings_device_id_devices"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_device_bindings_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
    )
    with op.batch_alter_table("device_bindings", schema=None) as batch_op:
        # ★ 唯一索引 = 单绑约束的硬保证：一台设备恒只有一行绑定记录
        batch_op.create_index(
            batch_op.f("ix_device_bindings_device_id"), ["device_id"], unique=True
        )
        batch_op.create_index(
            batch_op.f("ix_device_bindings_tenant_id"), ["tenant_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_device_bindings_client_product_id"), ["client_product_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_device_bindings_end_user_id"), ["end_user_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_device_bindings_status"), ["status"], unique=False)
        batch_op.create_index(
            "idx_device_bindings_tenant_status", ["tenant_id", "status"], unique=False
        )
        batch_op.create_index(
            "idx_device_bindings_qr_hash", ["qr_payload_hash"], unique=False
        )


def downgrade() -> None:
    """回滚（按依赖逆序）。"""
    with op.batch_alter_table("device_bindings", schema=None) as batch_op:
        batch_op.drop_index("idx_device_bindings_qr_hash")
        batch_op.drop_index("idx_device_bindings_tenant_status")
        batch_op.drop_index(batch_op.f("ix_device_bindings_status"))
        batch_op.drop_index(batch_op.f("ix_device_bindings_end_user_id"))
        batch_op.drop_index(batch_op.f("ix_device_bindings_client_product_id"))
        batch_op.drop_index(batch_op.f("ix_device_bindings_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_device_bindings_device_id"))
    op.drop_table("device_bindings")

    with op.batch_alter_table("allocation_items", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_allocation_items_status"))
        batch_op.drop_index(batch_op.f("ix_allocation_items_device_sn"))
        batch_op.drop_index(batch_op.f("ix_allocation_items_device_id"))
        batch_op.drop_index(batch_op.f("ix_allocation_items_allocation_order_id"))
    op.drop_table("allocation_items")

    with op.batch_alter_table("allocation_orders", schema=None) as batch_op:
        batch_op.drop_index("idx_allocation_orders_tenant_status")
        batch_op.drop_index(batch_op.f("ix_allocation_orders_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_allocation_orders_status"))
        batch_op.drop_index(batch_op.f("ix_allocation_orders_client_product_id"))
        batch_op.drop_index(batch_op.f("ix_allocation_orders_allocation_no"))
    op.drop_table("allocation_orders")
