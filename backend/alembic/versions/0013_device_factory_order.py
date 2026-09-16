"""设备与工单的归属：devices.factory_order_id

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-17

为什么需要这一列
----------------
P6 之前，设备与生产工单之间**没有**直接关联：工单只有 ``order_id``，
要问「这张工单负责哪几台设备」只能退化成「这个订单下状态为 X 的设备」。
这种「靠状态推断归属」的写法在派单那一刻是对的，随后就会出错：

* 工单只委托了 3 台（订单里另 1 台被冻结、1 台已先分配给客户），
  但「订单下全部设备」在出货后是 5 台，工厂端导出二维码清单会多打 2 张标签；
* 抽检只能校验到「SN 属于本订单」这一层，无法回答「SN 属于本工单」——
  于是一台**没被派工**的设备也能被这张工单抽检并计入合格率。

根因是把「归属」当成了「状态的函数」，而归属是**事实**，必须落库。
本迁移给 ``devices`` 增加 ``factory_order_id``，在派单时一次写入，
之后所有工单维度的查询（二维码清单 / 烧录联动 / 抽检校验）都以它为准。

为什么不回填历史数据
--------------------
回填需要「猜测」：只能按（订单 + 状态）反推，而那个推断正是本迁移要消除的
不可靠来源。本项目尚无生产数据（仅有可重建的演示库），
因此**不回填**——历史行的 ``factory_order_id`` 保持 ``NULL``，
语义是「这台上古设备不属于任何工单」，其抽检会被拒绝。
``make reset-db && make seed`` 即可回到一致状态。

索引说明
--------
``ix_devices_factory_order_id`` 同时服务于「本工单的设备清单」与
「本工单各状态的设备数」两类高频查询，必须建；按项目约定，
迁移里建的每个索引都已在 ORM 模型上声明（否则 ``alembic check`` 会报漂移）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """升级：给 devices 增加 factory_order_id 与索引。"""
    # SQLite 不支持 ALTER TABLE ADD CONSTRAINT，必须走 batch_alter_table。
    # 注意 devices 表上有 DeviceBatch/Order 等外键，batch 模式会重建整表，
    # 因此这里**不**用 create_foreign_key 之外的额外约束变更。
    with op.batch_alter_table("devices", schema=None) as batch_op:
        batch_op.add_column(sa.Column("factory_order_id", sa.String(length=36), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_devices_factory_order_id"), ["factory_order_id"], unique=False
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_devices_factory_order_id_factory_orders"),
            "factory_orders",
            ["factory_order_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """回滚：删除外键、索引与列。"""
    with op.batch_alter_table("devices", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_devices_factory_order_id_factory_orders"), type_="foreignkey"
        )
        batch_op.drop_index(batch_op.f("ix_devices_factory_order_id"))
        batch_op.drop_column("factory_order_id")
