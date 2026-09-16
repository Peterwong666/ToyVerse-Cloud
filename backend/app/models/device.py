"""设备域模型：设备 / 设备凭证 / 设备事件 / 批次与批次明细。

四维状态是本项目的**权威状态模型**（ADR-03）
------------------------------------------------
参考实现用四维表达设备状态，原型把它压成 9 态枚举做展示。
本项目以四维为权威，9 态由 :func:`app.models.enums.derive_device_label`
派生（仅用于前端展示，不参与任何业务判断）：

===============  ==========================================================
维度             取值与迁移
===============  ==========================================================
``asset_status`` 物理流转：``PENDING_GEN → GENERATED → IN_STOCK → PRODUCING
                 → PRODUCED → SHIPPED → ALLOCATED → BOUND → RETIRED``，
                 另有 ``IN_STOCK ⇄ FROZEN``（冻结时把原状态记入
                 ``previous_asset_status``，解冻时恢复）
``activation_status``  ``NOT_ACTIVATED → ACTIVATING → ACTIVATED | BIND_FAILED``
``online_status``      ``NEVER_ONLINE ⇄ ONLINE ⇄ OFFLINE``（180 秒在线窗口）
``bind_status``        ``UNBOUND ⇄ BOUND``
===============  ==========================================================

归属与可见性
------------
``tenant_id`` 可为空：**为空表示平台自有库存**（尚未分配给任何租户）。
因此商户端的租户作用域过滤（ADR-08）天然不会看到平台库存，
不需要额外的「是否已分配」判断。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, UTCDateTime
from app.models.enums import (
    ActivationStatus,
    AssetStatus,
    BatchLineStatus,
    BatchStatus,
    BindStatus,
    NetworkType,
    OnlineStatus,
)


class Device(Base, TimestampMixin):
    """设备（一台实体玩具）。"""

    __tablename__ = "devices"
    __table_args__ = (
        # 商户端按「租户 + 资产状态」筛设备，是最常用的组合
        Index("idx_devices_tenant_asset", "tenant_id", "asset_status"),
        # 在线率统计按「租户 + 在线状态」
        Index("idx_devices_tenant_online", "tenant_id", "online_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    #: 空 = 平台自有库存，尚未分配给租户
    tenant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    order_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("orders.id", ondelete="SET NULL"), index=True
    )
    client_product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("client_products.id", ondelete="RESTRICT"), index=True
    )
    batch_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("device_batches.id", ondelete="SET NULL"), index=True
    )
    #: 生产工单归属（P6 起）。派单时一次写入，此后「这张工单负责哪几台设备」
    #: 以**本列**为准，而不是「订单下状态为 X 的设备」——后者在派单那一刻正确，
    #: 之后就会漂移（被冻结/已先分配的设备不该算进工单，二维码清单会多打标签，
    #: 抽检也无法判断「SN 是否属于本工单」）。语义是事实，不能靠状态推断。
    factory_order_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("factory_orders.id", ondelete="SET NULL"), index=True
    )
    cloud_provider_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("cloud_providers.id", ondelete="RESTRICT"), index=True
    )

    # ---- 标识 ----
    sn: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    imei: Mapped[str | None] = mapped_column(String(32), index=True, doc="4G 方案使用")
    iccid: Mapped[str | None] = mapped_column(String(32), doc="4G 卡号")
    mac: Mapped[str | None] = mapped_column(String(32), index=True, doc="Wi-Fi 方案使用")
    #: 厂商侧设备 ID（火山引擎/集贤/京东返回的 device id）
    vendor_device_id: Mapped[str | None] = mapped_column(String(128), index=True)

    network_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=NetworkType.WIFI, index=True
    )
    firmware_version: Mapped[str | None] = mapped_column(String(64))

    # ---- 四维状态（ADR-03） ----
    asset_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AssetStatus.PENDING_GEN, index=True
    )
    previous_asset_status: Mapped[str | None] = mapped_column(
        String(32), doc="冻结前的资产状态，解冻时恢复"
    )
    activation_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ActivationStatus.NOT_ACTIVATED, index=True
    )
    online_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=OnlineStatus.NEVER_ONLINE, index=True
    )
    bind_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=BindStatus.UNBOUND, index=True
    )

    # ---- 生命周期时间戳 ----
    generated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    bound_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    frozen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    freeze_reason: Mapped[str | None] = mapped_column(String(512))
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    retire_reason: Mapped[str | None] = mapped_column(String(512))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, index=True, doc="最近心跳；与 ONLINE_WINDOW_SECONDS 比较判定在线"
    )

    #: 终端用户可调的设备设置（P8）：音量、儿童模式、唤醒词等。
    #:
    #: 为什么用 JSON 而不是给每一项开一列：设置项会随设备型号与固件版本变化
    #: （4G 机器有流量提醒、Wi-Fi 机器有配网信息），逐项开列意味着每加一个
    #: 设置就要一次迁移。这里只在**服务层**维护一份键白名单
    #: （见 ``miniapp_service.DEVICE_SETTING_KEYS``）——白名单收口同样能防止
    #: 「客户端塞任意键进库」，且不需要为每次产品迭代改表结构。
    settings: Mapped[dict[str, Any] | None] = mapped_column(JSON, doc="设备设置（键白名单由服务层维护）")

    remark: Mapped[str | None] = mapped_column(Text)

    @property
    def is_frozen(self) -> bool:
        return self.asset_status == AssetStatus.FROZEN

    @property
    def in_platform_stock(self) -> bool:
        """是否仍属平台自有库存（未分配给任何租户）。"""
        return self.tenant_id is None

    def __repr__(self) -> str:
        return (
            f"<Device {self.sn} asset={self.asset_status} "
            f"act={self.activation_status} tenant={self.tenant_id}>"
        )


class DeviceCredential(Base, TimestampMixin):
    """设备凭证。

    只存**摘要**（SHA-256），不存明文——与刷新令牌同一处理方式。
    设备侧持有的明文密钥由厂商/工厂一次性下发（``secret_hint`` 留作运维核对）。
    """

    __tablename__ = "device_credentials"
    __table_args__ = (
        UniqueConstraint("device_id", "credential_type", name="uq_device_credentials_device_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: DEVICE_SECRET / PRODUCT_SECRET / VENDOR_TOKEN
    credential_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    secret_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    secret_hint: Mapped[str | None] = mapped_column(String(32), doc="前 4 位 + ****，便于核对")
    algorithm: Mapped[str] = mapped_column(String(32), nullable=False, default="sha256")

    issued_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    def __repr__(self) -> str:
        return f"<DeviceCredential device={self.device_id} type={self.credential_type}>"


class DeviceEvent(Base, TimestampMixin):
    """设备流转事件（时间线）。

    每一次状态变更、绑定、心跳异常都落一条，构成设备的完整生命周期审计。
    前端「设备详情 → 时间线」直接读这张表。
    """

    __tablename__ = "device_events"
    __table_args__ = (Index("idx_device_events_device_created", "device_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str | None] = mapped_column(String(36), index=True)

    #: GENERATED / ALLOCATED / BOUND / UNBOUND / ACTIVATED / FROZEN / THAWED /
    #: RETIRED / HEARTBEAT / BIND_FAILED …
    event_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    dimension: Mapped[str | None] = mapped_column(
        String(24), doc="变更的维度：asset / activation / online / bind"
    )
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))

    actor_id: Mapped[str | None] = mapped_column(String(36))
    actor_account: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str | None] = mapped_column(String(256))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    client_ip: Mapped[str | None] = mapped_column(String(64))

    def __repr__(self) -> str:
        return f"<DeviceEvent {self.event_type} {self.from_status}->{self.to_status}>"


class DeviceBatch(Base, TimestampMixin):
    """设备批次（主要来源是工厂/供应商的 CSV 导入）。

    设计目标（对应 todolist 的批次导入要求）：
    「上传 → SHA-256 去重 → 预检 → 导入 → 进度 → 错误报告 → 幂等重跑」。

    * ``file_sha256`` 唯一：同一份文件重复上传时直接复用原批次，
      避免重复导入（幂等的基础）。
    * ``error_report`` 保留逐行错误，供前端直接下载。
    """

    __tablename__ = "device_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    order_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("orders.id", ondelete="SET NULL"), index=True
    )
    tenant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    network_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=NetworkType.FOUR_G
    )

    file_name: Mapped[str | None] = mapped_column(String(256))
    file_sha256: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True, doc="文件摘要，用于幂等重跑"
    )

    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalid_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicated_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    imported_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=BatchStatus.UPLOADED, index=True
    )
    error_report: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, doc="逐行错误明细，可直接给前端下载"
    )
    created_by: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    remark: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<DeviceBatch {self.batch_no} status={self.status} {self.imported_rows}/{self.total_rows}>"


class DeviceBatchLine(Base, TimestampMixin):
    """批次明细行（一行一台设备）。

    导入前先写明细并逐行标记 ``VALID`` / ``INVALID`` / ``SKIPPED``，
    这样「预检」与「导入」是同一份数据，不会出现两边判定不一致。
    """

    __tablename__ = "device_batch_lines"
    __table_args__ = (
        UniqueConstraint("batch_id", "row_no", name="uq_device_batch_lines_batch_row"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("device_batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)

    sn: Mapped[str | None] = mapped_column(String(64), index=True)
    imei: Mapped[str | None] = mapped_column(String(32))
    iccid: Mapped[str | None] = mapped_column(String(32))
    mac: Mapped[str | None] = mapped_column(String(32))
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, doc="原始行内容，便于核查")

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=BatchLineStatus.VALID, index=True
    )
    error_message: Mapped[str | None] = mapped_column(String(512))
    device_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("devices.id", ondelete="SET NULL"), index=True
    )

    def __repr__(self) -> str:
        return f"<DeviceBatchLine batch={self.batch_id} row={self.row_no} status={self.status}>"
