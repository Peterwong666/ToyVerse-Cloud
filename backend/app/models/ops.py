"""运营域模型（P9）：运营指标 / 内容库 / OTA。

三组表，各自的定位
------------------

    metrics_daily          按「租户 + 客户产品 + 日期」的日汇总快照
    metrics_hourly         同日的小时分布（24 点热力图的数据源）
    metrics_region         同日的地域分布
    content_hot_ranking    当日内容热度排行（含标题快照）
    content_items          内容库（故事 / 儿歌）
    ota_packages           固件包（平台级）
    ota_records            固件推送记录（逐台设备）

**快照表与实时聚合的分工**（这一点决定了本模块的正确性口径）
-------------------------------------------------------------
运营数据有两个性质不同的读取场景，混用一套实现必然在某一头出错：

* **「当前是多少」**（今日交互、今日新增激活、在线设备）→ 一律
  **实时从原始表 GROUP BY 聚合**。任何「预计算 + 系数还原」的写法都会在
  口径变化时静默失真——这正是遗留缺陷 **P-07「维度数据非真实汇总」** 的成因。
* **「过去几天怎么走的」**（日趋势 / 24 小时热力 / 地域分布 / 内容排行）
  → 读**快照表**。历史序列如果每次请求都重算全表，成本随数据量线性增长，
  且「上周的看板数字今天再看会变」——因为原始数据在变（补录、清洗）。
  快照的意义就是**把当时的结论固定下来**。

因此快照表由 :mod:`app.services.metrics_service` 的 `rebuild_*` 显式写入
（幂等 upsert），而不是靠定时任务——本阶段没有调度器，让「什么时候固化」
成为一个**可点击的动作**比假装有调度更诚实（商户端看板上有「刷新快照」按钮）。

为什么每张指标表都带 `client_product_id`
----------------------------------------
遗留缺陷 **P-08「运营数据全局共享」** 的根因就是聚合时丢了产品维度：
两个产品共用一个租户，看板把它们的交互混在一起算，于是「A 产品的活跃设备数」
包含了 B 产品的设备。因此本模块所有指标表的唯一键都从
``(tenant_id, client_product_id, ...)`` 起手——产品维度不是可选的筛选条件，
而是**表结构的一部分**，聚合时想漏掉它都做不到。

地域从哪来
----------
``metrics_region`` 的数据源是 ``devices.region``（迁移 0015 新增的列）。
真实部署里它应来自设备激活时的 IP 归属地或出厂分配信息；本阶段由种子数据
与设备端上报提供。**没有这一列就没有地域分布**——宁可加一列并让它可能为空，
也不要为了「看板好看」编一个地域出来。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
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
from app.models.enums import ContentItemType, EnableStatus, OtaPushStatus


class ContentItem(Base, TimestampMixin):
    """内容库条目（故事 / 儿歌）。

    ``tenant_id`` 可为空：为空的条目是**平台公共内容**（所有租户可用），
    非空则是租户自建内容。这样既有开箱即用的内容库，也允许品牌方上架自有内容。

    ``hit_count`` 是**累计命中次数**的近似值（由快照重建时累加）：
    真正的排行以 :class:`ContentHotRanking` 的按日快照为准——累计值无法回答
    「昨天哪首最火」，而运营要的恰恰是后者。
    """

    __tablename__ = "content_items"
    __table_args__ = (
        # 内容库按「租户 + 类型 + 状态」列表查询
        Index("idx_content_items_tenant_type", "tenant_id", "type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True, doc="为空表示平台公共内容"
    )

    type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ContentItemType.STORY, index=True
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(512))
    age_group: Mapped[str | None] = mapped_column(String(32), doc="适龄段，如 3-6 岁")
    tags: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    audio_url: Mapped[str | None] = mapped_column(String(512))
    cover_url: Mapped[str | None] = mapped_column(String(512))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    #: 内容正文（讲故事用的文本；音频未就绪时离线引擎按它播报）
    body: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EnableStatus.ENABLED, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hit_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, doc="累计命中次数（近似值，按日排行见 content_hot_ranking）"
    )
    remark: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<ContentItem {self.type} {self.title}>"


class MetricsDaily(Base, TimestampMixin):
    """日汇总快照（租户 × 客户产品 × 日期）。

    列的口径都写在这里，因为「同一个指标在不同人嘴里含义不同」是运营看板
    最常见的事故来源：

    * ``new_activations`` —— 当天 ``devices.activated_at`` 落在该日的台数
      （**激活**，不是绑定、不是入库）
    * ``active_devices`` —— 当天有过对话设备的去重台数（DAU 口径）
    * ``total_interactions`` —— 当天用户发出的消息条数（**一问一答算一次交互**，
      以 USER 消息计数，因为助手消息可能被内容安全整段替换、不代表用户行为）
    * ``unique_end_users`` —— 当天产生过交互的终端用户去重数
    * ``safety_blocked`` —— 当天被内容安全拦下的助手消息条数
    * ``avg_latency_ms`` —— 当天助手消息的端到端耗时均值（无数据时为 0）
    """

    __tablename__ = "metrics_daily"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "client_product_id", "metric_date", name="uq_metrics_daily_tenant_product_date"
        ),
        Index("idx_metrics_daily_product_date", "client_product_id", "metric_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("client_products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    metric_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    new_activations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_devices: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_interactions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    assistant_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    session_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unique_end_users: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    safety_blocked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<MetricsDaily {self.metric_date} product={self.client_product_id}>"


class MetricsHourly(Base, TimestampMixin):
    """小时分布快照（24 点热力图数据源）。"""

    __tablename__ = "metrics_hourly"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "client_product_id",
            "metric_date",
            "hour",
            name="uq_metrics_hourly_tenant_product_date_hour",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("client_products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    metric_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    #: 0–23（服务器按 UTC 归档；展示时区由前端按租户时区换算）
    hour: Mapped[int] = mapped_column(Integer, nullable=False)

    interactions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_devices: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<MetricsHourly {self.metric_date} {self.hour}:00 = {self.interactions}>"


class MetricsRegion(Base, TimestampMixin):
    """地域分布快照（数据源：``devices.region``）。"""

    __tablename__ = "metrics_region"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "client_product_id",
            "metric_date",
            "region",
            name="uq_metrics_region_tenant_product_date_region",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("client_products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    metric_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    region: Mapped[str] = mapped_column(String(64), nullable=False)

    device_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    interactions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<MetricsRegion {self.region} = {self.device_count}>"


class ContentHotRanking(Base, TimestampMixin):
    """内容热度排行（按日快照）。

    ``content_title`` / ``content_type`` 是**快照字段**而不是联表取值：
    内容被下架或改名后，历史排行仍应显示当时的名字，否则运营复盘时会看到
    「上周第一名的标题是空」。
    """

    __tablename__ = "content_hot_ranking"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "client_product_id", "metric_date", "content_id",
            name="uq_content_hot_ranking_tenant_product_date_content",
        ),
        Index("idx_content_hot_ranking_date_rank", "metric_date", "rank"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("client_products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    metric_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    content_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("content_items.id", ondelete="SET NULL")
    )
    content_title: Mapped[str] = mapped_column(String(128), nullable=False)
    content_type: Mapped[str] = mapped_column(String(16), nullable=False)

    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<ContentHotRanking #{self.rank} {self.content_title} = {self.hits}>"


class OtaPackage(Base, TimestampMixin):
    """固件包（平台级）。

    **推送能力取决于云服务商，而不是取决于本表**：``cloud_providers.ota_support``
    是权威字段（集贤 4G = ``SUPPORTED``、京东 JoyInside Wi-Fi = ``UNSUPPORTED``，
    后者只能端侧升级）。推送时按设备所属客户产品的云服务商判定，
    因此「Wi-Fi 产品推送被拒绝」是**数据驱动的**，不是写死的 if。

    ``tenant_id`` 为空表示面向所有使用该型号的租户；非空则限定租户专用固件
    （品牌定制版）。
    """

    __tablename__ = "ota_packages"
    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uq_ota_packages_template_version"),
        Index("idx_ota_packages_template_status", "template_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    template_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_templates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True, doc="为空表示全平台可用"
    )

    version: Mapped[str] = mapped_column(String(64), nullable=False)
    release_notes: Mapped[str | None] = mapped_column(Text)
    file_name: Mapped[str | None] = mapped_column(String(256))
    file_size: Mapped[int | None] = mapped_column(Integer)
    storage_path: Mapped[str | None] = mapped_column(String(512))
    checksum: Mapped[str | None] = mapped_column(String(64), doc="SHA-256，推送前校验完整性")
    min_version: Mapped[str | None] = mapped_column(String(64), doc="低于该版本才可升级（避免降级）")
    is_forced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EnableStatus.ENABLED, index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_by: Mapped[str | None] = mapped_column(String(64))
    remark: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<OtaPackage {self.version} template={self.template_id}>"


class OtaRecord(Base, TimestampMixin):
    """固件推送记录（逐台设备）。

    为什么按**设备**而不是按批次记一条：OTA 的现实是「大部分成功、少数失败」，
    只看批次状态无法回答「哪几台没升上去、为什么」。``PARTIAL_FAILED`` 这个
    状态只有在逐台留痕时才有意义。
    """

    __tablename__ = "ota_records"
    __table_args__ = (
        Index("idx_ota_records_package_status", "ota_package_id", "status"),
        Index("idx_ota_records_device_created", "device_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ota_package_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ota_packages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("client_products.id", ondelete="SET NULL"), index=True
    )

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=OtaPushStatus.PENDING, index=True
    )
    from_version: Mapped[str | None] = mapped_column(String(64))
    to_version: Mapped[str] = mapped_column(String(64), nullable=False)
    pushed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error_message: Mapped[str | None] = mapped_column(String(512))
    vendor_message: Mapped[str | None] = mapped_column(String(512), doc="厂商侧返回（脱敏后）")
    remark: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<OtaRecord device={self.device_id} {self.status} -> {self.to_version}>"
