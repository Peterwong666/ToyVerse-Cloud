"""运营域：metrics_daily / metrics_hourly / metrics_region / content_hot_ranking / content_items / ota_packages / ota_records + devices.region

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-17

编号说明
--------
todolist 原计划 P9 用 ``0012``，其后被 P6（``0012``/``0013``）与 P8
（``0014``）依次占用，按既定约定「迁移编号以落库先后顺延」落到 ``0015``。

本迁移**不再建** ``end_users`` / ``recharge_plans`` / ``recharge_orders``
---------------------------------------------------------------
这三张表由 P8 的 ``0014`` 提前落地（小程序登录与充值直接依赖它们）。
todolist 里 P9 的清单原本包含它们，这里按**已经落库的事实**剔除，
避免重复建表报「table already exists」。

``devices.region`` 为什么由本迁移新增
-------------------------------------
``metrics_region`` 要聚合「地域分布」，而此前没有任何字段承载地域。
没有真实数据源时只有两条路：编一个分布（比没有更危险——它会让人据此做出
真实的渠道投放决策），或者加一列并允许它为空（未知即未知，聚合时归入
「未知」一档）。本迁移选后者。真实部署中该值来自设备激活时的 IP 归属地
或出厂分配信息。

表之间的关系
------------
* ``content_items`` —— 内容库（``tenant_id`` 为空 = 平台公共内容）
* ``content_hot_ranking`` → ``content_items``（``SET NULL``：内容下架后排行
  仍保留，因为排行里存了标题快照，历史复盘不该因为下架而失去标题）
* ``ota_packages`` → ``product_templates``（固件绑定型号）
* ``ota_records`` → ``ota_packages`` / ``devices``（逐台设备的推送留痕）
* 四张 ``metrics_*`` / ``content_hot_ranking`` 表都带 ``client_product_id``：
  遗留缺陷 P-08「运营数据全局共享」的根因就是聚合时丢了产品维度，
  因此这里把产品维度做进**唯一键**，让「漏掉它」在结构上不成立。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base  # noqa: F401 - 自定义类型 UTCDateTime 需要此导入

revision: str = "0015"
down_revision: str | None = "0014"
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
    # 内容库（先建：content_hot_ranking 依赖它）
    # ------------------------------------------------------------
    op.create_table(
        "content_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=True),
        sa.Column("age_group", sa.String(length=32), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("audio_url", sa.String(length=512), nullable=True),
        sa.Column("cover_url", sa.String(length=512), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False),
        sa.Column("remark", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_content_items_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_items")),
    )
    with op.batch_alter_table("content_items", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_content_items_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_content_items_type"), ["type"], unique=False)
        batch_op.create_index(batch_op.f("ix_content_items_title"), ["title"], unique=False)
        batch_op.create_index(batch_op.f("ix_content_items_status"), ["status"], unique=False)
        batch_op.create_index("idx_content_items_tenant_type", ["tenant_id", "type"], unique=False)

    # ------------------------------------------------------------
    # 运营指标：日 / 小时 / 地域
    # ------------------------------------------------------------
    op.create_table(
        "metrics_daily",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("client_product_id", sa.String(length=36), nullable=False),
        sa.Column("metric_date", sa.Date(), nullable=False),
        sa.Column("new_activations", sa.Integer(), nullable=False),
        sa.Column("active_devices", sa.Integer(), nullable=False),
        sa.Column("total_interactions", sa.Integer(), nullable=False),
        sa.Column("assistant_messages", sa.Integer(), nullable=False),
        sa.Column("session_count", sa.Integer(), nullable=False),
        sa.Column("unique_end_users", sa.Integer(), nullable=False),
        sa.Column("safety_blocked", sa.Integer(), nullable=False),
        sa.Column("total_latency_ms", sa.Integer(), nullable=False),
        sa.Column("avg_latency_ms", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name=op.f("fk_metrics_daily_tenant_id_tenants"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["client_product_id"], ["client_products.id"],
            name=op.f("fk_metrics_daily_client_product_id_client_products"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metrics_daily")),
        sa.UniqueConstraint(
            "tenant_id", "client_product_id", "metric_date", name="uq_metrics_daily_tenant_product_date"
        ),
    )
    with op.batch_alter_table("metrics_daily", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_metrics_daily_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_metrics_daily_client_product_id"), ["client_product_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_metrics_daily_metric_date"), ["metric_date"], unique=False)
        batch_op.create_index(
            "idx_metrics_daily_product_date", ["client_product_id", "metric_date"], unique=False
        )

    op.create_table(
        "metrics_hourly",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("client_product_id", sa.String(length=36), nullable=False),
        sa.Column("metric_date", sa.Date(), nullable=False),
        sa.Column("hour", sa.Integer(), nullable=False),
        sa.Column("interactions", sa.Integer(), nullable=False),
        sa.Column("active_devices", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name=op.f("fk_metrics_hourly_tenant_id_tenants"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["client_product_id"], ["client_products.id"],
            name=op.f("fk_metrics_hourly_client_product_id_client_products"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metrics_hourly")),
        sa.UniqueConstraint(
            "tenant_id", "client_product_id", "metric_date", "hour",
            name="uq_metrics_hourly_tenant_product_date_hour",
        ),
    )
    with op.batch_alter_table("metrics_hourly", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_metrics_hourly_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_metrics_hourly_client_product_id"), ["client_product_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_metrics_hourly_metric_date"), ["metric_date"], unique=False)

    op.create_table(
        "metrics_region",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("client_product_id", sa.String(length=36), nullable=False),
        sa.Column("metric_date", sa.Date(), nullable=False),
        sa.Column("region", sa.String(length=64), nullable=False),
        sa.Column("device_count", sa.Integer(), nullable=False),
        sa.Column("interactions", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name=op.f("fk_metrics_region_tenant_id_tenants"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["client_product_id"], ["client_products.id"],
            name=op.f("fk_metrics_region_client_product_id_client_products"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metrics_region")),
        sa.UniqueConstraint(
            "tenant_id", "client_product_id", "metric_date", "region",
            name="uq_metrics_region_tenant_product_date_region",
        ),
    )
    with op.batch_alter_table("metrics_region", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_metrics_region_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_metrics_region_client_product_id"), ["client_product_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_metrics_region_metric_date"), ["metric_date"], unique=False)

    # ------------------------------------------------------------
    # 内容热度排行（快照）
    # ------------------------------------------------------------
    op.create_table(
        "content_hot_ranking",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("client_product_id", sa.String(length=36), nullable=False),
        sa.Column("metric_date", sa.Date(), nullable=False),
        sa.Column("content_id", sa.String(length=36), nullable=True),
        sa.Column("content_title", sa.String(length=128), nullable=False),
        sa.Column("content_type", sa.String(length=16), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name=op.f("fk_content_hot_ranking_tenant_id_tenants"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["client_product_id"], ["client_products.id"],
            name=op.f("fk_content_hot_ranking_client_product_id_client_products"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["content_id"], ["content_items.id"],
            name=op.f("fk_content_hot_ranking_content_id_content_items"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_hot_ranking")),
        sa.UniqueConstraint(
            "tenant_id", "client_product_id", "metric_date", "content_id",
            name="uq_content_hot_ranking_tenant_product_date_content",
        ),
    )
    with op.batch_alter_table("content_hot_ranking", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_content_hot_ranking_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_content_hot_ranking_client_product_id"), ["client_product_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_content_hot_ranking_metric_date"), ["metric_date"], unique=False
        )
        batch_op.create_index(
            "idx_content_hot_ranking_date_rank", ["metric_date", "rank"], unique=False
        )

    # ------------------------------------------------------------
    # OTA：固件包 + 推送记录
    # ------------------------------------------------------------
    op.create_table(
        "ota_packages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("template_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("release_notes", sa.Text(), nullable=True),
        sa.Column("file_name", sa.String(length=256), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("storage_path", sa.String(length=512), nullable=True),
        sa.Column("checksum", sa.String(length=64), nullable=True),
        sa.Column("min_version", sa.String(length=64), nullable=True),
        sa.Column("is_forced", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("published_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["template_id"], ["product_templates.id"],
            name=op.f("fk_ota_packages_template_id_product_templates"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name=op.f("fk_ota_packages_tenant_id_tenants"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ota_packages")),
        sa.UniqueConstraint("template_id", "version", name="uq_ota_packages_template_version"),
    )
    with op.batch_alter_table("ota_packages", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ota_packages_template_id"), ["template_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_ota_packages_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_ota_packages_status"), ["status"], unique=False)
        batch_op.create_index(
            "idx_ota_packages_template_status", ["template_id", "status"], unique=False
        )

    op.create_table(
        "ota_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("ota_package_id", sa.String(length=36), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("client_product_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("from_version", sa.String(length=64), nullable=True),
        sa.Column("to_version", sa.String(length=64), nullable=False),
        sa.Column("pushed_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("finished_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("vendor_message", sa.String(length=512), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["ota_package_id"], ["ota_packages.id"],
            name=op.f("fk_ota_records_ota_package_id_ota_packages"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"],
            name=op.f("fk_ota_records_device_id_devices"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name=op.f("fk_ota_records_tenant_id_tenants"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["client_product_id"], ["client_products.id"],
            name=op.f("fk_ota_records_client_product_id_client_products"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ota_records")),
    )
    with op.batch_alter_table("ota_records", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_ota_records_ota_package_id"), ["ota_package_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_ota_records_device_id"), ["device_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_ota_records_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_ota_records_client_product_id"), ["client_product_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_ota_records_status"), ["status"], unique=False)
        batch_op.create_index(
            "idx_ota_records_package_status", ["ota_package_id", "status"], unique=False
        )
        batch_op.create_index(
            "idx_ota_records_device_created", ["device_id", "created_at"], unique=False
        )

    # ------------------------------------------------------------
    # 设备地域（metrics_region 的数据源）
    # ------------------------------------------------------------
    with op.batch_alter_table("devices", schema=None) as batch_op:
        batch_op.add_column(sa.Column("region", sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f("ix_devices_region"), ["region"], unique=False)

    # ------------------------------------------------------------
    # 对话消息的内容归属（content_hot_ranking 的数据源）
    # ------------------------------------------------------------
    # 必须放在 content_items 建表之后（外键依赖）。
    with op.batch_alter_table("dialogue_messages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("content_item_id", sa.String(length=36), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_dialogue_messages_content_item_id"), ["content_item_id"], unique=False
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_dialogue_messages_content_item_id_content_items"),
            "content_items",
            ["content_item_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """回滚。"""
    with op.batch_alter_table("dialogue_messages", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_dialogue_messages_content_item_id_content_items"), type_="foreignkey"
        )
        batch_op.drop_index(batch_op.f("ix_dialogue_messages_content_item_id"))
        batch_op.drop_column("content_item_id")

    with op.batch_alter_table("devices", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_devices_region"))
        batch_op.drop_column("region")

    with op.batch_alter_table("ota_records", schema=None) as batch_op:
        batch_op.drop_index("idx_ota_records_device_created")
        batch_op.drop_index("idx_ota_records_package_status")
        batch_op.drop_index(batch_op.f("ix_ota_records_status"))
        batch_op.drop_index(batch_op.f("ix_ota_records_client_product_id"))
        batch_op.drop_index(batch_op.f("ix_ota_records_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_ota_records_device_id"))
        batch_op.drop_index(batch_op.f("ix_ota_records_ota_package_id"))
    op.drop_table("ota_records")

    with op.batch_alter_table("ota_packages", schema=None) as batch_op:
        batch_op.drop_index("idx_ota_packages_template_status")
        batch_op.drop_index(batch_op.f("ix_ota_packages_status"))
        batch_op.drop_index(batch_op.f("ix_ota_packages_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_ota_packages_template_id"))
    op.drop_table("ota_packages")

    with op.batch_alter_table("content_hot_ranking", schema=None) as batch_op:
        batch_op.drop_index("idx_content_hot_ranking_date_rank")
        batch_op.drop_index(batch_op.f("ix_content_hot_ranking_metric_date"))
        batch_op.drop_index(batch_op.f("ix_content_hot_ranking_client_product_id"))
        batch_op.drop_index(batch_op.f("ix_content_hot_ranking_tenant_id"))
    op.drop_table("content_hot_ranking")

    with op.batch_alter_table("metrics_region", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_metrics_region_metric_date"))
        batch_op.drop_index(batch_op.f("ix_metrics_region_client_product_id"))
        batch_op.drop_index(batch_op.f("ix_metrics_region_tenant_id"))
    op.drop_table("metrics_region")

    with op.batch_alter_table("metrics_hourly", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_metrics_hourly_metric_date"))
        batch_op.drop_index(batch_op.f("ix_metrics_hourly_client_product_id"))
        batch_op.drop_index(batch_op.f("ix_metrics_hourly_tenant_id"))
    op.drop_table("metrics_hourly")

    with op.batch_alter_table("metrics_daily", schema=None) as batch_op:
        batch_op.drop_index("idx_metrics_daily_product_date")
        batch_op.drop_index(batch_op.f("ix_metrics_daily_metric_date"))
        batch_op.drop_index(batch_op.f("ix_metrics_daily_client_product_id"))
        batch_op.drop_index(batch_op.f("ix_metrics_daily_tenant_id"))
    op.drop_table("metrics_daily")

    with op.batch_alter_table("content_items", schema=None) as batch_op:
        batch_op.drop_index("idx_content_items_tenant_type")
        batch_op.drop_index(batch_op.f("ix_content_items_status"))
        batch_op.drop_index(batch_op.f("ix_content_items_title"))
        batch_op.drop_index(batch_op.f("ix_content_items_type"))
        batch_op.drop_index(batch_op.f("ix_content_items_tenant_id"))
    op.drop_table("content_items")
