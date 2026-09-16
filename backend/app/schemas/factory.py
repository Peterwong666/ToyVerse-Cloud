"""工厂生产域请求 / 响应模型（P6）。

契约约定
--------
继承 :class:`app.schemas.catalog.ApiModel`：Python 侧 ``snake_case``，
对外 JSON 为 ``camelCase``（``alias_generator=to_camel``），请求体同样接受
camelCase——与 ``schemas/order.py`` / ``schemas/device.py`` 同一机制。

为什么工厂端另有一套响应模型，而不是复用 ``OrderResponse`` 再删字段
------------------------------------------------------------------
工厂是**跨租户**角色（``factories`` 独立于 ``tenants``，工厂账号
``tenant_id`` 为空），一张工单背后是某个品牌客户的订单。工厂需要知道
「做什么、做多少、烧哪个固件」，但**不需要也不应该**知道客户是谁、
单价多少、联系人电话是多少。

「复用平台端模型 + 删掉敏感字段」是一种会随时间腐化的写法：需求扩散时
（例如订单新增 ``contractNo``）只要有人忘了把它加进删除列表，敏感字段
就会静默地下发到工厂端，而且**不会报错**。本模块反过来做——**只声明
允许下发的字段**，再由 :class:`app.services.serializers.FactoryOrderSerializer`
的白名单断言与现实响应模型对齐（两者必须完全一致，有单测钉死）。

因此这里的字段是「不允许存在」而不是「忘了加」：没有 ``tenantName`` /
``unitPrice`` / ``applicantPhone``；客户名只有一个**已脱敏**的表达
``customerNameMasked``——未经脱敏的客户名在类型层面无法表示。

平台端为什么也用这套模型
------------------------
``FactoryOrderDetailResponse`` 同时服务工厂端与平台端的工单端点。平台端本可
看到真实客户名，但两侧共用同一 schema 意味着平台端也只拿到脱敏名——
这是**刻意的**：需要真名时走订单接口（``GET /platform/orders/{id}``），
那里有单独的权限码把关；工单接口因此不可能成为绕过脱敏的旁路。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from app.models.enums import InspectionResult
from app.schemas.catalog import ApiModel

# ---------------------------------------------------------------------------
# 明细响应
# ---------------------------------------------------------------------------


class BurnReportResponse(ApiModel):
    """烧录上报记录（工厂分批报「这一批烧了多少台」）。"""

    id: str
    burned_count: int
    sn_from: str | None = Field(default=None, description="本次烧录的起始 SN")
    sn_to: str | None = Field(default=None, description="本次烧录的结束 SN")
    operator: str | None = Field(default=None, description="工厂操作员账号")
    note: str | None = None
    reported_at: datetime | None = None
    created_at: datetime


class InspectionResponse(ApiModel):
    """抽检记录。

    同一 SN 允许多条记录（抽检是**观测**不是**状态**）：保留历史才能看出
    「第一次不合格、返工后合格」这样的过程。
    """

    id: str
    factory_order_id: str
    device_id: str | None = None
    sn: str
    result: str = Field(description="PASS / FAIL")
    result_label: str = Field(default="", description="中文展示名，仅供展示")
    inspector: str | None = None
    defect_code: str | None = Field(default=None, description="不合格时的不良代码")
    note: str | None = None
    inspected_at: datetime | None = None
    created_at: datetime


class InspectionSummary(ApiModel):
    """抽检汇总（合格 / 不合格 / 合计）。

    与 ``inspections`` 数组分开给出：数组有上限（详情不会一次拉回几千条），
    汇总必须是**全量**口径，否则「合格率」会在抽检变多之后悄悄失真。
    """

    pass_count: int = 0
    fail_count: int = 0
    total: int = 0


# ---------------------------------------------------------------------------
# 工单
# ---------------------------------------------------------------------------


class FactoryOrderResponse(ApiModel):
    """工厂工单列表项（**字段白名单**：只含允许下发的字段）。

    字段集合必须与 :attr:`app.services.serializers.FactoryOrderSerializer.ALLOWED_FIELDS`
    完全一致——本模型增删字段时，白名单与序列化器要同步改，单测会拦住漏改。
    """

    id: str
    factory_order_no: str
    order_no: str | None = Field(default=None, description="来源订单号，供工厂对账")
    factory_id: str
    factory_name: str | None = None
    customer_name_masked: str = Field(
        default="", description="**脱敏后**的客户名（如「中**动」）；真名永不下发"
    )
    product_model: str | None = Field(default=None, description="型号，如 ESP32-S3")
    firmware_version: str | None = None

    quantity: int = Field(description="委托生产数量（台）")
    burned_count: int = 0
    remaining: int = Field(default=0, description="还需烧录的数量")
    progress_percent: float = Field(default=0.0, description="烧录进度百分比（0–100）")

    status: str = Field(description="PENDING / PRODUCING / COMPLETED / SHIPPED")
    status_label: str = Field(default="", description="中文展示名，仅供展示")

    assigned_at: datetime | None = Field(default=None, description="派单时间")
    due_at: datetime | None = Field(default=None, description="约定交付日")
    shipped_at: datetime | None = None
    created_at: datetime


class FactoryOrderDetailResponse(FactoryOrderResponse):
    """工单详情：生产备注 + 订单侧状态 + 烧录 / 抽检明细。

    ``orderQuantity`` / ``orderStatus`` 是**订单**那侧的字段（工单数量默认
    等于订单数量，但两张单的状态各自独立）——详情页要能回答「这单在订单侧
    走到哪一步了」，否则工厂发现工单已烧完却迟迟没有下文时无从判断卡在哪。
    """

    production_note: str | None = Field(default=None, description="派单时填写的生产说明")
    order_quantity: int = 0
    order_status: str | None = None
    order_status_label: str | None = None
    burn_reports: list[BurnReportResponse] = Field(default_factory=list)
    inspections: list[InspectionResponse] = Field(default_factory=list)
    inspection_summary: InspectionSummary = Field(default_factory=InspectionSummary)


# ---------------------------------------------------------------------------
# 请求
# ---------------------------------------------------------------------------


class FactoryDispatchRequest(ApiModel):
    """派单（平台端 → 工厂）。"""

    factory_id: str = Field(min_length=1, max_length=36)
    due_at: datetime | None = Field(default=None, description="约定交付日（可选）")
    production_note: str | None = Field(
        default=None, max_length=512, description="生产说明，写入工单备注"
    )

    @field_validator("production_note")
    @classmethod
    def _strip_note(cls, value: str | None) -> str | None:
        """去首尾空白；空白串归一为 ``None``。

        与订单驳回的 ``rejectReason`` 同一口径：``max_length`` 挡不住
        ``"   "``，而工单备注会直接展示给工厂操作员——一条全空白的备注
        比没有备注更让人困惑（看起来是「有人写了点什么」）。
        """
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        return cleaned or None


class BurnReportRequest(ApiModel):
    """烧录上报。

    ``burnedCount`` 必须 ``> 0``：上报 0 台既不改工单进度，也不携带任何
    信息，只会在上报历史里留下一串噪声记录。
    """

    burned_count: int = Field(gt=0, description="本次上报的烧录数量（台）")
    sn_from: str | None = Field(default=None, max_length=64, description="本次烧录的起始 SN")
    sn_to: str | None = Field(default=None, max_length=64, description="本次烧录的结束 SN")
    note: str | None = Field(default=None, max_length=2000)


class InspectionCreateRequest(ApiModel):
    """抽检上报。

    ★ 空白 SN 必须在**入参层**挡掉：P5 的教训是 ``min_length=1`` 拦不住
    ``"   "``（它长度为 3），会一路落库并污染「抽检合格率」。
    """

    sn: str = Field(min_length=1, max_length=64)
    result: InspectionResult
    defect_code: str | None = Field(default=None, max_length=64, description="不良代码")
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("sn")
    @classmethod
    def _strip_sn(cls, value: str) -> str:
        """去空白并拒绝空白串（抽检是**针对具体 SN** 的判定动作）。"""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("SN 不能为空白")
        return cleaned


# ---------------------------------------------------------------------------
# 聚合与辅助
# ---------------------------------------------------------------------------


class FirmwareVersionResponse(ApiModel):
    """固件版本聚合项（来自本厂工单，不是固件包台账）。

    ``firmwareVersion`` 的取值来自工单上非空的 ``firmware_version`` 分组，
    因此这里出现过的版本必然有工单在烧——不会出现「有版本号但没有任务」的空项。
    """

    firmware_version: str
    order_count: int = 0
    total_quantity: int = 0
    burned_count: int = 0


class FactoryResponse(ApiModel):
    """工厂（派单下拉用）。

    含 ``status``：下拉里带上状态，前端可以把已停用的工厂置灰并说明原因，
    比后端静默隐藏更好——隐藏会让人分不清「没建这家厂」与「这家厂被停用了」。
    """

    id: str
    code: str
    name: str
    contact_name: str | None = None
    contact_phone: str | None = None
    address: str | None = None
    status: str
    daily_capacity: int = Field(default=0, description="日烧录产能（台）")
    is_verified: bool = Field(default=False, description="是否已通过资质审核")


class FactoryStatsResponse(ApiModel):
    """工厂工作台统计（**严格本厂口径**）。

    ``burnProgressPercent`` 按「累计烧录 / 累计委托」计算，而不是按工单数：
    一张 5000 台的工单与一张 10 台的工单对产能的意义完全不同，
    用工单数平均会让进度看起来比实际乐观得多。
    """

    pending_orders: int = 0
    producing_orders: int = 0
    completed_orders: int = 0
    shipped_orders: int = 0
    total_orders: int = 0
    total_quantity: int = 0
    burned_quantity: int = 0
    burn_progress_percent: float = 0.0
    firmware_count: int = Field(default=0, description="涉及的不同固件版本数")
    inspection_total: int = 0
    inspection_pass: int = 0
    inspection_fail: int = 0


__all__ = [
    "BurnReportRequest",
    "BurnReportResponse",
    "FactoryDispatchRequest",
    "FactoryOrderDetailResponse",
    "FactoryOrderResponse",
    "FactoryResponse",
    "FactoryStatsResponse",
    "FirmwareVersionResponse",
    "InspectionCreateRequest",
    "InspectionResponse",
    "InspectionSummary",
]
