"""目录域（二）：client_products / miniapp_configs

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-16

这两张表是**租户级**数据，也是商户端「我的产品」与终端小程序的数据来源。

* ``client_products`` —— 客户产品。``tenant_id`` 为 ``NOT NULL`` 且必须已获得
  对应模板授权，从数据层面修正 P-03（原型「新建产品未绑定客户」）。
  同时把模板的 ``network_type`` / ``cloud_provider_id`` **快照**进来，
  模板后续变更不追溯影响已交付产品。
* ``miniapp_configs`` —— 小程序配置，与客户产品 1:1。
  ``app_secret_enc`` 与云服务商密钥同等待遇：加密落库、响应只回掩码。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base  # noqa: F401 - 自定义类型 UTCDateTime 需要此导入

revision: str = "0006"
down_revision: str | None = "0005"
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
    # 客户产品
    # ------------------------------------------------------------
    op.create_table(
        "client_products",
        sa.Column("id", sa.String(length=36), nullable=False),
        # NOT NULL 是 P-03 的架构性修复：产品必须有归属租户
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("template_id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("network_type", sa.String(length=16), nullable=False),
        sa.Column("cloud_provider_id", sa.String(length=36), nullable=True),
        sa.Column("firmware_version", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("ai_enabled", sa.Boolean(), nullable=False),
        sa.Column("remark", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_client_products")),
        sa.ForeignKeyConstraint(
            ["cloud_provider_id"],
            ["cloud_providers.id"],
            name=op.f("fk_client_products_cloud_provider_id_cloud_providers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["product_templates.id"],
            name=op.f("fk_client_products_template_id_product_templates"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_client_products_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
    )
    with op.batch_alter_table("client_products", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_client_products_cloud_provider_id"), ["cloud_provider_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_client_products_code"), ["code"], unique=True)
        batch_op.create_index(batch_op.f("ix_client_products_network_type"), ["network_type"], unique=False)
        batch_op.create_index(batch_op.f("ix_client_products_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_client_products_template_id"), ["template_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_client_products_tenant_id"), ["tenant_id"], unique=False)

    # ------------------------------------------------------------
    # 小程序配置（与客户产品 1:1）
    # ------------------------------------------------------------
    op.create_table(
        "miniapp_configs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("client_product_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("app_name", sa.String(length=128), nullable=False),
        sa.Column("app_id", sa.String(length=64), nullable=True),
        sa.Column("app_secret_enc", sa.Text(), nullable=True),
        sa.Column("app_secret_hint", sa.String(length=32), nullable=True),
        sa.Column("original_id", sa.String(length=64), nullable=True),
        sa.Column("theme_color", sa.String(length=16), nullable=True),
        sa.Column("logo_url", sa.String(length=512), nullable=True),
        sa.Column("share_title", sa.String(length=128), nullable=True),
        sa.Column("share_desc", sa.String(length=256), nullable=True),
        sa.Column("service_phone", sa.String(length=32), nullable=True),
        sa.Column("service_qr_url", sa.String(length=512), nullable=True),
        sa.Column("version", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("published_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_miniapp_configs")),
        sa.ForeignKeyConstraint(
            ["client_product_id"],
            ["client_products.id"],
            name=op.f("fk_miniapp_configs_client_product_id_client_products"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_miniapp_configs_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
    )
    with op.batch_alter_table("miniapp_configs", schema=None) as batch_op:
        # unique + index 组合 → 唯一索引，保证「一个客户产品只有一份小程序配置」
        batch_op.create_index(
            batch_op.f("ix_miniapp_configs_client_product_id"), ["client_product_id"], unique=True
        )
        batch_op.create_index(batch_op.f("ix_miniapp_configs_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_miniapp_configs_tenant_id"), ["tenant_id"], unique=False)


def downgrade() -> None:
    """回滚（按依赖逆序）。"""
    with op.batch_alter_table("miniapp_configs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_miniapp_configs_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_miniapp_configs_status"))
        batch_op.drop_index(batch_op.f("ix_miniapp_configs_client_product_id"))
    op.drop_table("miniapp_configs")

    with op.batch_alter_table("client_products", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_client_products_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_client_products_template_id"))
        batch_op.drop_index(batch_op.f("ix_client_products_status"))
        batch_op.drop_index(batch_op.f("ix_client_products_network_type"))
        batch_op.drop_index(batch_op.f("ix_client_products_code"))
        batch_op.drop_index(batch_op.f("ix_client_products_cloud_provider_id"))
    op.drop_table("client_products")
