"""订单域模型。

订单是「客户下单 → 平台审核 → 生成设备 → 入库」这条主干的载体，
上承目录域（租户 + 客户产品），下接设备域（生成的设备）。

状态机
------
状态取值与迁移表定义在 :mod:`app.models.enums`（``OrderStatus`` /
``ORDER_TRANSITIONS``），模型层不重复定义规则，只落库当前状态；
所有迁移都经 :mod:`app.services.order_service` 校验。

金额字段的可见性
----------------
``unit_price`` / ``total_amount`` 属于**敏感信息**：P6 的工厂端响应
一律不下发金额（`FactoryOrderSerializer` 做字段白名单脱敏），因此
这些字段只出现在平台端与商户端。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, UTCDateTime
from app.models.enums import NetworkType, OrderStatus


class Order(Base, TimestampMixin):
    """订单（客户下单）。"""

    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_no: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True, doc="人类可读单号"
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("client_products.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    quantity: Mapped[int] = mapped_column(Integer, nullable=False, doc="下单数量（台）")
    unit_price: Mapped[float | None] = mapped_column(Numeric(10, 2), doc="单价（元，敏感）")
    total_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), doc="总额（元，敏感）")

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=OrderStatus.PENDING_AUDIT, index=True
    )
    #: 联网方式快照：决定生成设备时走哪个云服务商与二维码格式
    network_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=NetworkType.WIFI, index=True
    )

    # ---- 申请人信息（商户端下单时填写） ----
    applicant_name: Mapped[str | None] = mapped_column(String(64))
    applicant_phone: Mapped[str | None] = mapped_column(String(32))
    remark: Mapped[str | None] = mapped_column(Text)

    # ---- 审核 ----
    audited_by: Mapped[str | None] = mapped_column(String(64), doc="审核人账号")
    audited_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    audit_remark: Mapped[str | None] = mapped_column(String(512))
    reject_reason: Mapped[str | None] = mapped_column(String(512))

    # ---- 设备生成 ----
    generated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    generated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    generation_detail: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, doc="生成结果摘要（成功/失败数量、厂商侧批次号等）"
    )

    def __repr__(self) -> str:
        return f"<Order {self.order_no} status={self.status} qty={self.quantity}>"
