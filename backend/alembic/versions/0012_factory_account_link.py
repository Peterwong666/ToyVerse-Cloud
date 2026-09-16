"""工厂账号归属：user_accounts.factory_id

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-16

为什么需要这条迁移
------------------
``factories`` 表在 ``0002`` 就已建好，``factory_orders.factory_id``
也是 NOT NULL 外键，但 ``user_accounts`` 上没有任何字段指向工厂。
结果是工厂账号（``role_type=FACTORY``、``tenant_id=NULL``）**不知道自己
属于哪家工厂**：

* 若按「工厂端看全部工单」实现，多工厂场景下 A 厂能看到 B 厂的工单；
* 若按「工厂端看不到工单」实现，工厂端等于不可用。

租户隔离靠 ``tenant_id``，工厂跨租户，所以必须有独立的一列来承载归属。
本迁移只加列与索引，**不改任何既有数据语义**：

* 平台 / 商户账号的 ``factory_id`` 恒为 ``NULL``（它们不经过工厂作用域）；
* 工厂账号的归属由种子数据（``app/db/seed.py``）写入，属演示数据，
  不放迁移——与 ``V2__seed_dev_data`` 曾把开发种子混进生产迁移的教训一致。

编号说明
--------
``0012`` 原在计划里预留给 P9 的 ``ops_metrics_ota``，但 P6 先落库，
按「落库先后顺延」的既定约定占用 ``0012``，P9 顺延为 ``0013``。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """升级：给 user_accounts 增加 factory_id 与索引。"""
    # SQLite 不支持 ALTER TABLE ADD CONSTRAINT，必须走 batch_alter_table
    # （Alembic 会以「建新表 + 拷数据 + 改名」的方式模拟）。
    with op.batch_alter_table("user_accounts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("factory_id", sa.String(length=36), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_user_accounts_factory_id"), ["factory_id"], unique=False
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_user_accounts_factory_id_factories"),
            "factories",
            ["factory_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """回滚：删除外键、索引与列。"""
    with op.batch_alter_table("user_accounts", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_user_accounts_factory_id_factories"), type_="foreignkey"
        )
        batch_op.drop_index(batch_op.f("ix_user_accounts_factory_id"))
        batch_op.drop_column("factory_id")
