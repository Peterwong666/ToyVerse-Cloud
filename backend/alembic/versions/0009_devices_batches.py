"""设备域：device_batches / devices / device_batch_lines / device_credentials / device_events

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-16

**编号与原计划的差异**：todolist 原计划把 ``device_batches`` 放在 ``0010``，
但 ``devices.batch_id`` 指向 ``device_batches``，若批次表晚于设备表创建，
外键就指向了尚不存在的表（SQLite 上只能靠「建表后再 rebuild 加约束」绕过）。
因此本迁移内的建表顺序按**依赖方向**排列：批次 → 设备 → 批次明细 →
凭证 → 事件，保证所有外键都指向已存在的表。

建表顺序
--------
1. ``device_batches`` —— 批次（CSV 导入的载体），``file_sha256`` 唯一，
   用于「同一份文件重复上传直接复用」的幂等语义
2. ``devices`` —— 设备本体，四维状态（ADR-03）；
   ``tenant_id`` 可为空 = 平台自有库存
3. ``device_batch_lines`` —— 批次明细行，逐行标记 VALID/INVALID/SKIPPED
4. ``device_credentials`` —— 设备凭证，只存摘要
5. ``device_events`` —— 设备流转时间线
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base  # noqa: F401 - 自定义类型 UTCDateTime 需要此导入

revision: str = "0009"
down_revision: str | None = "0008"
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
    # 1) 设备批次
    # ------------------------------------------------------------
    op.create_table(
        "device_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("batch_no", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=36), nullable=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("network_type", sa.String(length=16), nullable=False),
        sa.Column("file_name", sa.String(length=256), nullable=True),
        sa.Column("file_sha256", sa.String(length=64), nullable=True),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column("valid_rows", sa.Integer(), nullable=False),
        sa.Column("invalid_rows", sa.Integer(), nullable=False),
        sa.Column("duplicated_rows", sa.Integer(), nullable=False),
        sa.Column("imported_rows", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_report", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("started_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("finished_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_batches")),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_device_batches_order_id_orders"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_device_batches_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
    )
    with op.batch_alter_table("device_batches", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_device_batches_batch_no"), ["batch_no"], unique=True)
        batch_op.create_index(batch_op.f("ix_device_batches_order_id"), ["order_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_device_batches_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_device_batches_status"), ["status"], unique=False)
        # 唯一索引：同一份文件只能对应一个批次（幂等重跑的基础）
        batch_op.create_index(batch_op.f("ix_device_batches_file_sha256"), ["file_sha256"], unique=True)

    # ------------------------------------------------------------
    # 2) 设备（四维状态，ADR-03）
    # ------------------------------------------------------------
    op.create_table(
        "devices",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("order_id", sa.String(length=36), nullable=True),
        sa.Column("client_product_id", sa.String(length=36), nullable=True),
        sa.Column("batch_id", sa.String(length=36), nullable=True),
        sa.Column("cloud_provider_id", sa.String(length=36), nullable=True),
        sa.Column("sn", sa.String(length=64), nullable=False),
        sa.Column("imei", sa.String(length=32), nullable=True),
        sa.Column("iccid", sa.String(length=32), nullable=True),
        sa.Column("mac", sa.String(length=32), nullable=True),
        sa.Column("vendor_device_id", sa.String(length=128), nullable=True),
        sa.Column("network_type", sa.String(length=16), nullable=False),
        sa.Column("firmware_version", sa.String(length=64), nullable=True),
        sa.Column("asset_status", sa.String(length=32), nullable=False),
        sa.Column("previous_asset_status", sa.String(length=32), nullable=True),
        sa.Column("activation_status", sa.String(length=32), nullable=False),
        sa.Column("online_status", sa.String(length=32), nullable=False),
        sa.Column("bind_status", sa.String(length=32), nullable=False),
        sa.Column("generated_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("activated_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("bound_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("frozen_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("freeze_reason", sa.String(length=512), nullable=True),
        sa.Column("retired_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("retire_reason", sa.String(length=512), nullable=True),
        sa.Column("last_heartbeat_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_devices")),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["device_batches.id"],
            name=op.f("fk_devices_batch_id_device_batches"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["client_product_id"],
            ["client_products.id"],
            name=op.f("fk_devices_client_product_id_client_products"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["cloud_provider_id"],
            ["cloud_providers.id"],
            name=op.f("fk_devices_cloud_provider_id_cloud_providers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_devices_order_id_orders"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_devices_tenant_id_tenants"), ondelete="CASCADE"
        ),
    )
    with op.batch_alter_table("devices", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_devices_activation_status"), ["activation_status"], unique=False)
        batch_op.create_index(batch_op.f("ix_devices_asset_status"), ["asset_status"], unique=False)
        batch_op.create_index(batch_op.f("ix_devices_batch_id"), ["batch_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_devices_bind_status"), ["bind_status"], unique=False)
        batch_op.create_index(batch_op.f("ix_devices_client_product_id"), ["client_product_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_devices_cloud_provider_id"), ["cloud_provider_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_devices_imei"), ["imei"], unique=False)
        batch_op.create_index(batch_op.f("ix_devices_last_heartbeat_at"), ["last_heartbeat_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_devices_mac"), ["mac"], unique=False)
        batch_op.create_index(batch_op.f("ix_devices_network_type"), ["network_type"], unique=False)
        batch_op.create_index(batch_op.f("ix_devices_online_status"), ["online_status"], unique=False)
        batch_op.create_index(batch_op.f("ix_devices_order_id"), ["order_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_devices_sn"), ["sn"], unique=True)
        batch_op.create_index(batch_op.f("ix_devices_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_devices_vendor_device_id"), ["vendor_device_id"], unique=False)
        # 组合索引服务于最常用的筛选：租户 + 资产状态 / 租户 + 在线状态
        batch_op.create_index("idx_devices_tenant_asset", ["tenant_id", "asset_status"], unique=False)
        batch_op.create_index("idx_devices_tenant_online", ["tenant_id", "online_status"], unique=False)

    # ------------------------------------------------------------
    # 3) 批次明细行
    # ------------------------------------------------------------
    op.create_table(
        "device_batch_lines",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("sn", sa.String(length=64), nullable=True),
        sa.Column("imei", sa.String(length=32), nullable=True),
        sa.Column("iccid", sa.String(length=32), nullable=True),
        sa.Column("mac", sa.String(length=32), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("device_id", sa.String(length=36), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_batch_lines")),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["device_batches.id"],
            name=op.f("fk_device_batch_lines_batch_id_device_batches"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_device_batch_lines_device_id_devices"),
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("batch_id", "row_no", name="uq_device_batch_lines_batch_row"),
    )
    with op.batch_alter_table("device_batch_lines", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_device_batch_lines_batch_id"), ["batch_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_device_batch_lines_device_id"), ["device_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_device_batch_lines_sn"), ["sn"], unique=False)
        batch_op.create_index(batch_op.f("ix_device_batch_lines_status"), ["status"], unique=False)

    # ------------------------------------------------------------
    # 4) 设备凭证（只存摘要）
    # ------------------------------------------------------------
    op.create_table(
        "device_credentials",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=False),
        sa.Column("credential_type", sa.String(length=32), nullable=False),
        sa.Column("secret_hash", sa.String(length=128), nullable=False),
        sa.Column("secret_hint", sa.String(length=32), nullable=True),
        sa.Column("algorithm", sa.String(length=32), nullable=False),
        sa.Column("issued_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("expires_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("last_used_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("revoked_at", app.db.base.UTCDateTime(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_credentials")),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_device_credentials_device_id_devices"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("device_id", "credential_type", name="uq_device_credentials_device_type"),
    )
    with op.batch_alter_table("device_credentials", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_device_credentials_credential_type"), ["credential_type"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_device_credentials_device_id"), ["device_id"], unique=False)

    # ------------------------------------------------------------
    # 5) 设备事件（流转时间线）
    # ------------------------------------------------------------
    op.create_table(
        "device_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("dimension", sa.String(length=24), nullable=True),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=True),
        sa.Column("actor_id", sa.String(length=36), nullable=True),
        sa.Column("actor_account", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.String(length=256), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("client_ip", sa.String(length=64), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_events")),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"], name=op.f("fk_device_events_device_id_devices"), ondelete="CASCADE"
        ),
    )
    with op.batch_alter_table("device_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_device_events_device_id"), ["device_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_device_events_event_type"), ["event_type"], unique=False)
        batch_op.create_index(batch_op.f("ix_device_events_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index("idx_device_events_device_created", ["device_id", "created_at"], unique=False)


def downgrade() -> None:
    """回滚（按依赖逆序）。"""
    with op.batch_alter_table("device_events", schema=None) as batch_op:
        batch_op.drop_index("idx_device_events_device_created")
        batch_op.drop_index(batch_op.f("ix_device_events_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_device_events_event_type"))
        batch_op.drop_index(batch_op.f("ix_device_events_device_id"))
    op.drop_table("device_events")

    with op.batch_alter_table("device_credentials", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_device_credentials_device_id"))
        batch_op.drop_index(batch_op.f("ix_device_credentials_credential_type"))
    op.drop_table("device_credentials")

    with op.batch_alter_table("device_batch_lines", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_device_batch_lines_status"))
        batch_op.drop_index(batch_op.f("ix_device_batch_lines_sn"))
        batch_op.drop_index(batch_op.f("ix_device_batch_lines_device_id"))
        batch_op.drop_index(batch_op.f("ix_device_batch_lines_batch_id"))
    op.drop_table("device_batch_lines")

    with op.batch_alter_table("devices", schema=None) as batch_op:
        batch_op.drop_index("idx_devices_tenant_online")
        batch_op.drop_index("idx_devices_tenant_asset")
        batch_op.drop_index(batch_op.f("ix_devices_vendor_device_id"))
        batch_op.drop_index(batch_op.f("ix_devices_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_devices_sn"))
        batch_op.drop_index(batch_op.f("ix_devices_order_id"))
        batch_op.drop_index(batch_op.f("ix_devices_online_status"))
        batch_op.drop_index(batch_op.f("ix_devices_network_type"))
        batch_op.drop_index(batch_op.f("ix_devices_mac"))
        batch_op.drop_index(batch_op.f("ix_devices_last_heartbeat_at"))
        batch_op.drop_index(batch_op.f("ix_devices_imei"))
        batch_op.drop_index(batch_op.f("ix_devices_cloud_provider_id"))
        batch_op.drop_index(batch_op.f("ix_devices_client_product_id"))
        batch_op.drop_index(batch_op.f("ix_devices_bind_status"))
        batch_op.drop_index(batch_op.f("ix_devices_batch_id"))
        batch_op.drop_index(batch_op.f("ix_devices_asset_status"))
        batch_op.drop_index(batch_op.f("ix_devices_activation_status"))
    op.drop_table("devices")

    with op.batch_alter_table("device_batches", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_device_batches_file_sha256"))
        batch_op.drop_index(batch_op.f("ix_device_batches_status"))
        batch_op.drop_index(batch_op.f("ix_device_batches_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_device_batches_order_id"))
        batch_op.drop_index(batch_op.f("ix_device_batches_batch_no"))
    op.drop_table("device_batches")
