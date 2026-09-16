"""工厂域模型：生产工单 / 烧录上报 / 抽检记录。

烧录工厂是**跨租户**的（ADR-02：独立 `factories` 表，账号 `tenant_id = None`），
因此这里的字段设计围绕「工厂能干活、但看不到客户是谁」展开：

* 工单只带**产品型号与数量**，不带客户名、联系方式、金额
* 客户名的脱敏（`中国移动 → 中**动`）由 P6 的
  ``app/services/serializers.py::FactoryOrderSerializer`` 在**序列化层**
  做字段白名单，而不是靠「少查几个字段」——后者容易随需求扩散而漏
* 烧录上报必须满足 ``burned_count ≤ quantity``，否则工单数据失去意义
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UTCDateTime
from app.models.enums import FactoryOrderStatus, InspectionResult


class FactoryOrder(Base, TimestampMixin):
    """生产工单（平台把订单拆成工单发给工厂）。

    ``quantity`` 来自订单，``burned_count`` 由工厂分批上报累加。
    """

    __tablename__ = "factory_orders"
    __table_args__ = (Index("idx_factory_orders_factory_status", "factory_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    factory_order_no: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    factory_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("factories.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    #: 工单只承载「做什么、做多少」，客户身份与金额一律不下发（P6 脱敏由序列化层保证）
    product_model: Mapped[str | None] = mapped_column(String(64), doc="型号，如 ESP32-S3")
    firmware_version: Mapped[str | None] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    burned_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=FactoryOrderStatus.PENDING, index=True
    )
    assigned_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime, doc="约定交付日")
    shipped_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    remark: Mapped[str | None] = mapped_column(Text)

    @property
    def remaining(self) -> int:
        """还需烧录的数量。"""
        return max(0, self.quantity - self.burned_count)

    def __repr__(self) -> str:
        return (
            f"<FactoryOrder {self.factory_order_no} status={self.status} "
            f"{self.burned_count}/{self.quantity}>"
        )


class BurnReport(Base, TimestampMixin):
    """烧录上报（工厂分批报「这一批烧了多少台」）。"""

    __tablename__ = "burn_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    factory_order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("factory_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    burned_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 本次上报对应的 SN 区间，便于与设备表核对
    sn_from: Mapped[str | None] = mapped_column(String(64))
    sn_to: Mapped[str | None] = mapped_column(String(64))
    operator: Mapped[str | None] = mapped_column(String(64), doc="工厂操作员账号")
    note: Mapped[str | None] = mapped_column(Text)
    reported_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    def __repr__(self) -> str:
        return f"<BurnReport order={self.factory_order_id} count={self.burned_count}>"


class Inspection(Base, TimestampMixin):
    """抽检记录。

    抽检针对**具体 SN**：SN 不存在时必须报错，而不是记一条空记录
    （否则「抽检通过率」这类指标会被无意义数据污染）。
    """

    __tablename__ = "inspections"
    __table_args__ = (
        # 抽检记录按「工单 + SN」查询，是最高频的访问路径
        Index("idx_inspections_order_sn", "factory_order_id", "sn"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    factory_order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("factory_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("devices.id", ondelete="SET NULL"), index=True
    )
    sn: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    result: Mapped[str] = mapped_column(
        String(16), nullable=False, default=InspectionResult.PASS, index=True
    )
    inspector: Mapped[str | None] = mapped_column(String(64))
    defect_code: Mapped[str | None] = mapped_column(String(64), doc="不良代码（来自工厂抽检口径）")
    note: Mapped[str | None] = mapped_column(Text)
    inspected_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    def __repr__(self) -> str:
        return f"<Inspection sn={self.sn} result={self.result}>"
