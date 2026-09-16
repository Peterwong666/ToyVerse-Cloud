"""目录域（一）：cloud_providers / product_templates / product_authorizations

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-16

编号说明
--------
原计划把目录域放在 ``0003`` / ``0004``，但 P1 落地时后端地基实际占用了
``0001``–``0004``（身份租户 / 组织工厂 / 用户账号 / 基础设施）。
Alembic 的 ``revision`` 必须全局唯一且线性，因此目录域顺延为
``0005`` / ``0006``，P4 及之后的编号同步顺延（见 todolist.md）。

本迁移建立「平台级」的三张表：

* ``cloud_providers`` —— 云服务商账号。``access_key_enc`` / ``secret_key_enc``
  存**密文**，``*_hint`` 存掩码提示，任何响应都不含明文密钥
* ``product_templates`` —— 产品模板（型号 / 芯片方案 / 联网方式 / 云服务商）
* ``product_authorizations`` —— 模板 × 租户 的授权关系，
  是「租户能否创建客户产品」的唯一依据
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base  # noqa: F401 - 自定义类型 UTCDateTime 需要此导入

revision: str = "0005"
down_revision: str | None = "0004"
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
    # 云服务商
    # ------------------------------------------------------------
    op.create_table(
        "cloud_providers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("vendor", sa.String(length=32), nullable=False),
        sa.Column("network_type", sa.String(length=16), nullable=False),
        sa.Column("api_base", sa.String(length=256), nullable=True),
        # 密钥密文：TEXT 而非 VARCHAR，避免不同厂商密文长度差异
        sa.Column("access_key_enc", sa.Text(), nullable=True),
        sa.Column("secret_key_enc", sa.Text(), nullable=True),
        sa.Column("access_key_hint", sa.String(length=32), nullable=True),
        sa.Column("secret_key_hint", sa.String(length=32), nullable=True),
        sa.Column("extra_config", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("ota_support", sa.String(length=32), nullable=False),
        sa.Column("last_tested_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.Column("last_test_message", sa.String(length=512), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cloud_providers")),
    )
    with op.batch_alter_table("cloud_providers", schema=None) as batch_op:
        # unique + index 组合生成的是**唯一索引**（而非唯一约束）
        batch_op.create_index(batch_op.f("ix_cloud_providers_code"), ["code"], unique=True)
        batch_op.create_index(
            batch_op.f("ix_cloud_providers_network_type"), ["network_type"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_cloud_providers_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_cloud_providers_vendor"), ["vendor"], unique=False)

    # ------------------------------------------------------------
    # 产品模板
    # ------------------------------------------------------------
    op.create_table(
        "product_templates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("chip", sa.String(length=64), nullable=True),
        sa.Column("network_type", sa.String(length=16), nullable=False),
        sa.Column("cloud_provider_id", sa.String(length=36), nullable=True),
        sa.Column("firmware_version", sa.String(length=64), nullable=True),
        sa.Column("reference_price", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("ai_features", sa.JSON(), nullable=True),
        sa.Column("specs", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_templates")),
        # RESTRICT：模板被客户产品引用时，数据库层面拒绝删除（服务层先行校验）
        sa.ForeignKeyConstraint(
            ["cloud_provider_id"],
            ["cloud_providers.id"],
            name=op.f("fk_product_templates_cloud_provider_id_cloud_providers"),
            ondelete="RESTRICT",
        ),
    )
    with op.batch_alter_table("product_templates", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_product_templates_category"), ["category"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_product_templates_cloud_provider_id"), ["cloud_provider_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_product_templates_code"), ["code"], unique=True)
        batch_op.create_index(
            batch_op.f("ix_product_templates_network_type"), ["network_type"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_product_templates_status"), ["status"], unique=False)

    # ------------------------------------------------------------
    # 产品授权（模板 × 租户）
    # ------------------------------------------------------------
    op.create_table(
        "product_authorizations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("template_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("authorized_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("authorized_by", sa.String(length=64), nullable=True),
        sa.Column("expires_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("max_devices", sa.Integer(), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_authorizations")),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["product_templates.id"],
            name=op.f("fk_product_authorizations_template_id_product_templates"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_product_authorizations_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        # 同一租户对同一模板只能有一条授权（幂等授权的基础）
        sa.UniqueConstraint(
            "tenant_id", "template_id", name="uq_product_authorizations_tenant_template"
        ),
    )
    with op.batch_alter_table("product_authorizations", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_product_authorizations_status"), ["status"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_product_authorizations_template_id"), ["template_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_product_authorizations_tenant_id"), ["tenant_id"], unique=False
        )


def downgrade() -> None:
    """回滚（按依赖逆序）。"""
    with op.batch_alter_table("product_authorizations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_product_authorizations_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_product_authorizations_template_id"))
        batch_op.drop_index(batch_op.f("ix_product_authorizations_status"))
    op.drop_table("product_authorizations")

    with op.batch_alter_table("product_templates", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_product_templates_status"))
        batch_op.drop_index(batch_op.f("ix_product_templates_network_type"))
        batch_op.drop_index(batch_op.f("ix_product_templates_code"))
        batch_op.drop_index(batch_op.f("ix_product_templates_cloud_provider_id"))
        batch_op.drop_index(batch_op.f("ix_product_templates_category"))
    op.drop_table("product_templates")

    with op.batch_alter_table("cloud_providers", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_cloud_providers_vendor"))
        batch_op.drop_index(batch_op.f("ix_cloud_providers_status"))
        batch_op.drop_index(batch_op.f("ix_cloud_providers_network_type"))
        batch_op.drop_index(batch_op.f("ix_cloud_providers_code"))
    op.drop_table("cloud_providers")
