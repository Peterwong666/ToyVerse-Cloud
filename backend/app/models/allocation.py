"""分配与绑定域模型：分配单 / 分配明细 / 设备绑定。

这一层回答两个不同的问题
------------------------

``allocation_orders`` / ``allocation_items`` —— **平台把库存交给谁**
    ``devices.tenant_id`` 为空表示平台自有库存（见 :mod:`app.models.device`）。
    分配就是把设备从「平台库存」划到某个租户名下：写 ``tenant_id``、
    ``client_product_id``，并把 ``asset_status`` 由 ``IN_STOCK`` 推到
    ``ALLOCATED``。分配单只是这次批量操作的**载体与审计凭据**，
    因此明细行要逐行记录成功/失败原因——一张单里失败三台时，
    只报「执行失败」等于没说。

``device_bindings`` —— **终端用户与哪台设备绑定**
    绑定是商户侧用二维码发起的两步流程（``precheck`` → ``bind``）。
    关键约束全部落在这张表上：

    * ``device_id`` **唯一** —— 单绑约束由数据库唯一索引硬保证，
      不依赖服务层的「先查后写」（那种写法在并发下必然漏）。
    * ``confirm_token_hash`` **只存摘要**，明文只在 ``precheck`` 响应里出现一次，
      与刷新令牌 / 设备凭证同一处理方式。
    * ``status=PENDING`` 的行就是「已预检、等待确认」的绑定意图；
      ``bind`` 成功时把令牌摘要**置空**（销毁），因此同一张令牌
      不可能被用第二次——这是 :data:`app.models.enums.BindingRecordStatus`
      存在的意义。

为什么 ``end_user_id`` 不建外键
-------------------------------
终端用户表 ``end_users`` 属 P9（运营看板与终端用户域）。
在这里先建外键会强迫 P5 提前建一张自己用不到的表。
做法与 P7 的 ``dialogue_sessions.device_id`` 一致：先留字段与索引，
P9 落地时补一次轻量迁移（``项目进度.md`` 已登记该欠账）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UTCDateTime
from app.models.enums import AllocationItemStatus, AllocationStatus, BindingRecordStatus


class AllocationOrder(Base, TimestampMixin):
    """分配单（平台把一批设备分配给某个租户）。

    一次分配同时确定三件事：**给谁**（``tenant_id``）、
    **挂到哪个产品**（``client_product_id``）、**具体哪几台**（明细行）。
    这三者必须在创建时就固定下来，执行阶段只做校验与落库——
    否则「执行时再决定给谁」会让审计失去意义。
    """

    __tablename__ = "allocation_orders"
    __table_args__ = (
        # 平台端最常用的筛选组合：按租户看它的分配历史 / 按状态看待办单
        Index("idx_allocation_orders_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    allocation_no: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True, doc="人类可读单号"
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_product_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("client_products.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AllocationStatus.DRAFT, index=True
    )

    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    allocated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_by: Mapped[str | None] = mapped_column(String(64), doc="创建人账号")
    executed_by: Mapped[str | None] = mapped_column(String(64), doc="执行人账号")
    executed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    failure_reason: Mapped[str | None] = mapped_column(
        String(512), doc="整体失败原因（逐行原因在 allocation_items.error_message）"
    )
    remark: Mapped[str | None] = mapped_column(Text)

    @property
    def pending_count(self) -> int:
        """尚未成功分配的明细数量。"""
        return max(0, self.total_count - self.allocated_count)

    def __repr__(self) -> str:
        return (
            f"<AllocationOrder {self.allocation_no} status={self.status} "
            f"{self.allocated_count}/{self.total_count}>"
        )


class AllocationItem(Base, TimestampMixin):
    """分配明细行（一行一台设备）。

    与批次明细（``device_batch_lines``）同构：**预检与执行读同一份数据**，
    因此不会出现「列表说 3 台、执行说 2 台」这种两边判定不一致的情况。
    """

    __tablename__ = "allocation_items"
    __table_args__ = (
        UniqueConstraint(
            "allocation_order_id", "device_id", name="uq_allocation_items_order_device"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    allocation_order_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("allocation_orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    device_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: SN 快照：设备被删除或改写后，错误报告仍能指出是「哪一台」
    device_sn: Mapped[str | None] = mapped_column(String(64), index=True)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AllocationItemStatus.PENDING, index=True
    )
    error_message: Mapped[str | None] = mapped_column(String(512))
    allocated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    def __repr__(self) -> str:
        return f"<AllocationItem order={self.allocation_order_id} device={self.device_id} status={self.status}>"


class DeviceBinding(Base, TimestampMixin):
    """设备绑定记录（含两步流程的确认令牌）。

    生命周期::

        precheck  → status=PENDING, confirm_token_hash=<sha256>, confirm_expires_at=<now+300s>
        bind      → status=BOUND,   confirm_token_hash=NULL（销毁）, bound_at, bind_count += 1
        unbind    → status=UNBOUND, unbound_at, unbind_reason
        重新扫码  → 复用本行，重置为 PENDING 并签发新令牌

    **一台设备恒一行**（``device_id`` 唯一索引）：这既是业务上的单绑约束，
    也让「解绑后重新绑定」天然保留最后一次归属信息；
    完整的多次绑定历史由 :class:`app.models.device.DeviceEvent` 承载。
    """

    __tablename__ = "device_bindings"
    __table_args__ = (
        # 平台 / 商户端按「租户 + 绑定状态」筛绑定记录
        Index("idx_device_bindings_tenant_status", "tenant_id", "status"),
        # precheck 的「SHA-256 命中」按载荷摘要检索
        Index("idx_device_bindings_qr_hash", "qr_payload_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    #: 唯一索引 = 单绑约束的硬保证（并发下也不依赖服务层判重）
    device_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("client_products.id", ondelete="RESTRICT"), index=True
    )
    #: 终端用户（``end_users`` 属 P9，届时补外键，见模块 docstring）
    end_user_id: Mapped[str | None] = mapped_column(String(36), index=True)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=BindingRecordStatus.PENDING, index=True
    )

    # ---- 二维码 ----
    qr_format: Mapped[str | None] = mapped_column(String(8), doc="JX（集贤 4G）/ JD（京东 Wi-Fi）")
    qr_payload_hash: Mapped[str | None] = mapped_column(
        String(64), doc="二维码载荷的 SHA-256（precheck 的「命中」依据）"
    )

    # ---- 确认令牌（只存摘要，用后置空） ----
    confirm_token_hash: Mapped[str | None] = mapped_column(
        String(128), doc="SHA-256 摘要；bind 成功后置空即「销毁」"
    )
    confirm_expires_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, doc="令牌过期时间（QR_CONFIRM_TOKEN_TTL_SECONDS，默认 300 秒）"
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # ---- 绑定 / 解绑 ----
    bound_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    bound_by: Mapped[str | None] = mapped_column(String(64), doc="发起绑定的商户账号")
    unbound_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    unbind_reason: Mapped[str | None] = mapped_column(String(512))
    unbound_by: Mapped[str | None] = mapped_column(String(64))
    #: 绑定次数：解绑后重新绑定会累加，便于识别「反复解绑重绑」的异常设备
    bind_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remark: Mapped[str | None] = mapped_column(Text)

    @property
    def is_pending(self) -> bool:
        return self.status == BindingRecordStatus.PENDING

    def __repr__(self) -> str:
        return f"<DeviceBinding device={self.device_id} tenant={self.tenant_id} status={self.status}>"


__all__ = ["AllocationItem", "AllocationOrder", "DeviceBinding"]
