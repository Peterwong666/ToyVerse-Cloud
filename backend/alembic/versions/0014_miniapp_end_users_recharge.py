"""终端用户与充值：end_users / recharge_plans / recharge_orders / devices.settings

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-17

编号说明
--------
``end_users`` / ``recharge_plans`` / ``recharge_orders`` 原计划属 P9 的
``0012_ops_metrics_ota``。但 P8 的交付物（小程序登录、4G 流量充值）**直接依赖**
它们——没有 `end_users` 就无法区分「谁绑了这台玩具」（P5 的
``device_bindings.end_user_id`` 一直是「只存不建外键」的松散引用，等的就是这张表），
没有套餐表则「充值」只能在前端硬编码假数据（等于伪造一个不存在的能力）。
按既定约定「迁移编号以落库先后顺延」，P8 占用 ``0014``，
P9 顺延为 ``0015_ops_metrics_ota``。

本迁移还包含 ``devices.settings``
-------------------------------
终端用户可调的音量 / 儿童模式需要一个落点。用 JSON 列而不是逐项开列：
设置项会随设备型号与固件版本变化，逐项开列意味着每加一个设置就要一次迁移。
**键白名单由服务层维护**（``miniapp_service.DEVICE_SETTING_KEYS``），
「客户端塞任意键进库」仍然被挡住，只是把约束从表结构移到了服务层——
代价是白名单不再由数据库强制，这一点在服务层 docstring 里写明。

不外键化的地方
--------------
``devices.settings`` 是 JSON、无外键可言；``end_users`` 刻意**不带**
``tenant_id``（一个家长可能买过两个品牌的玩具，把账号绑到某个租户上会
立刻产生「同一部手机在不同品牌下是两个账号」的荒谬结果）。
终端用户的可见范围由**设备**决定，收口点是绑定关系而不是租户。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base  # noqa: F401 - 自定义类型 UTCDateTime 需要此导入

revision: str = "0014"
down_revision: str | None = "0013"
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
    # 终端用户
    # ------------------------------------------------------------
    op.create_table(
        "end_users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("nickname", sa.String(length=64), nullable=True),
        sa.Column("avatar", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("login_code_hash", sa.String(length=128), nullable=True),
        sa.Column("login_code_expires_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("login_code_attempts", sa.Integer(), nullable=False),
        sa.Column("last_login_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("last_login_ip", sa.String(length=64), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_end_users")),
    )
    with op.batch_alter_table("end_users", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_end_users_phone"), ["phone"], unique=True)
        batch_op.create_index(batch_op.f("ix_end_users_status"), ["status"], unique=False)

    # ------------------------------------------------------------
    # 流量套餐
    # ------------------------------------------------------------
    op.create_table(
        "recharge_plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=256), nullable=True),
        sa.Column("data_mb", sa.Integer(), nullable=False),
        sa.Column("valid_days", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_recommended", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("remark", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_recharge_plans_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recharge_plans")),
        sa.UniqueConstraint("tenant_id", "code", name="uq_recharge_plans_tenant_code"),
    )
    with op.batch_alter_table("recharge_plans", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_recharge_plans_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_recharge_plans_status"), ["status"], unique=False)
        batch_op.create_index(
            "idx_recharge_plans_tenant_status", ["tenant_id", "status"], unique=False
        )

    # ------------------------------------------------------------
    # 充值订单
    # ------------------------------------------------------------
    op.create_table(
        "recharge_orders",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("order_no", sa.String(length=64), nullable=False),
        sa.Column("end_user_id", sa.String(length=36), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("amount", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("data_mb", sa.Integer(), nullable=False),
        sa.Column("valid_days", sa.Integer(), nullable=False),
        sa.Column("plan_name", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("paid_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("failed_reason", sa.String(length=512), nullable=True),
        sa.Column("refunded_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["end_user_id"],
            ["end_users.id"],
            name=op.f("fk_recharge_orders_end_user_id_end_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_recharge_orders_device_id_devices"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["recharge_plans.id"],
            name=op.f("fk_recharge_orders_plan_id_recharge_plans"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_recharge_orders_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recharge_orders")),
    )
    with op.batch_alter_table("recharge_orders", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_recharge_orders_order_no"), ["order_no"], unique=True)
        batch_op.create_index(
            batch_op.f("ix_recharge_orders_end_user_id"), ["end_user_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_recharge_orders_device_id"), ["device_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_recharge_orders_plan_id"), ["plan_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_recharge_orders_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_recharge_orders_status"), ["status"], unique=False)
        batch_op.create_index(
            "idx_recharge_orders_user_created", ["end_user_id", "created_at"], unique=False
        )

    # ------------------------------------------------------------
    # 设备设置（终端用户可调项）
    # ------------------------------------------------------------
    with op.batch_alter_table("devices", schema=None) as batch_op:
        batch_op.add_column(sa.Column("settings", sa.JSON(), nullable=True))


def downgrade() -> None:
    """回滚。"""
    with op.batch_alter_table("devices", schema=None) as batch_op:
        batch_op.drop_column("settings")

    with op.batch_alter_table("recharge_orders", schema=None) as batch_op:
        batch_op.drop_index("idx_recharge_orders_user_created")
        batch_op.drop_index(batch_op.f("ix_recharge_orders_status"))
        batch_op.drop_index(batch_op.f("ix_recharge_orders_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_recharge_orders_plan_id"))
        batch_op.drop_index(batch_op.f("ix_recharge_orders_device_id"))
        batch_op.drop_index(batch_op.f("ix_recharge_orders_end_user_id"))
        batch_op.drop_index(batch_op.f("ix_recharge_orders_order_no"))
    op.drop_table("recharge_orders")

    with op.batch_alter_table("recharge_plans", schema=None) as batch_op:
        batch_op.drop_index("idx_recharge_plans_tenant_status")
        batch_op.drop_index(batch_op.f("ix_recharge_plans_status"))
        batch_op.drop_index(batch_op.f("ix_recharge_plans_tenant_id"))
    op.drop_table("recharge_plans")

    with op.batch_alter_table("end_users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_end_users_status"))
        batch_op.drop_index(batch_op.f("ix_end_users_phone"))
    op.drop_table("end_users")
