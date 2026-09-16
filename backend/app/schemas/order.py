"""订单域请求 / 响应模型。

契约约定
--------
继承 :class:`app.schemas.catalog.ApiModel`（snake_case ⇄ camelCase 自动转换）。

为什么响应里既有 ``status`` 又有 ``statusLabel``
------------------------------------------------
``status`` 是**业务事实**（前端拿它做筛选、做按钮可用性判断），
``statusLabel`` 是**中文展示**（由 ``ORDER_STATUS_LABELS`` 派生）。
如果只给中文，前端就得自己维护一份「中文 → 状态码」的反查表，
两边一改就容易漂移；只给状态码又会让每张表格都要写一遍映射。
一起给出，代价是一个字段，收益是前端零映射。

金额字段的可见性
----------------
``unitPrice`` / ``totalAmount`` 只在**平台端与商户端**响应中出现；
P6 的工厂端使用独立的脱敏序列化器（白名单），不会复用本模块的模型。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from app.schemas.catalog import ApiModel
from app.schemas.device import DeviceBrief


class OrderResponse(ApiModel):
    """订单列表项。"""

    id: str
    order_no: str
    tenant_id: str
    tenant_code: str | None = None
    tenant_name: str | None = None
    client_product_id: str
    client_product_code: str | None = None
    client_product_name: str | None = None
    network_type: str

    quantity: int
    unit_price: float | None = None
    total_amount: float | None = None

    status: str
    status_label: str = Field(default="", description="中文展示名，仅供展示")

    applicant_name: str | None = None
    applicant_phone: str | None = None
    remark: str | None = None

    audited_by: str | None = None
    audited_at: datetime | None = None
    audit_remark: str | None = None
    reject_reason: str | None = None

    generated_count: int = 0
    generated_at: datetime | None = None
    device_count: int = Field(default=0, description="该订单已生成（在册）的设备数")

    created_at: datetime
    updated_at: datetime


class OrderDetailResponse(OrderResponse):
    """订单详情：附加生成摘要与设备摘要。"""

    generation_detail: dict[str, Any] | None = None
    devices: list[DeviceBrief] = Field(default_factory=list, description="最近生成的设备（最多 200 台）")


class OrderAuditRequest(ApiModel):
    """订单审核。

    ``rejectReason`` 在驳回时**必填**——驳回而不给理由，
    商户端无法整改，只能重复下单。
    """

    decision: Literal["APPROVED", "REJECTED"]
    remark: str | None = Field(default=None, max_length=512, description="审核备注")
    reject_reason: str | None = Field(default=None, max_length=512, description="驳回原因（驳回时必填）")

    @field_validator("remark", "reject_reason")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value


class GenerateResult(ApiModel):
    """设备生成结果。

    ``vendorMessage`` 承载「非致命的厂商侧信息」：例如 mock 引擎生成的
    设备会明确标注「模拟结果」，或厂商只返回了部分设备 ID。
    若厂商密钥未配置，则不会走到这里——服务层直接抛
    ``VENDOR_UNAVAILABLE``（ADR-07：安全失败，绝不伪造成功）。
    """

    order_id: str
    requested: int = Field(description="本次请求生成的设备数")
    generated: int = Field(description="实际生成成功数")
    failed: int = Field(description="失败数")
    devices: list[DeviceBrief] = Field(default_factory=list)
    vendor_message: str | None = None


class MerchantOrderCreateRequest(ApiModel):
    """商户端下单。"""

    client_product_id: str = Field(min_length=1, description="必须是本租户已启用的客户产品")
    quantity: int = Field(ge=1, le=10_000, description="下单数量（台）")
    applicant_name: str | None = Field(default=None, max_length=64)
    applicant_phone: str | None = Field(default=None, max_length=32)
    remark: str | None = Field(default=None, max_length=2000)

    @field_validator("applicant_name", "applicant_phone", "remark")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value


__all__ = [
    "GenerateResult",
    "MerchantOrderCreateRequest",
    "OrderAuditRequest",
    "OrderDetailResponse",
    "OrderResponse",
]
