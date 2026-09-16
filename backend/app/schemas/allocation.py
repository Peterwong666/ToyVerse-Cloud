"""分配域的请求 / 响应模型（P5）。

分配单的字段取舍
================

* **创建时就固定「给谁 + 挂哪个产品 + 哪几台」**，执行阶段只校验与落库。
  允许「先建单、执行时再选设备」会让审计记录指向一个当时并不存在的事实。
* 明细行**逐行带 ``errorMessage``**：一张单里失败三台时只报「执行失败」
  等于没说——与 P4 批次导入的错误报告同一口径。
* 执行结果里保留 ``failures`` 数组（而非只给计数），前端因此可以
  直接把失败原因列出来，不需要再拉一次明细。

敏感字段
--------
分配单不涉及金额与联系方式（那些在订单域），因此没有脱敏要求；
但 ``tenantName`` / ``clientProductName`` 只在**详情**里出现，
与 P3/P4 的分工保持一致（列表靠下拉映射，详情自解释）。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from app.schemas.catalog import ApiModel

#: 单张分配单允许的最大设备数。
#:
#: 与批次导入的 5000 行上限同一考虑：一次请求要在一个事务里逐台校验并更新，
#: 不设上限时一张「全库分配」的单子会把请求拖到超时，
#: 且失败后无法安全重试（事务过大）。500 台足够覆盖真实的分批出货节奏。
MAX_ALLOCATION_DEVICES = 500


class AllocationItemResponse(ApiModel):
    """分配明细行。"""

    id: str
    allocation_order_id: str
    device_id: str
    device_sn: str | None = None
    status: str = Field(description="PENDING / ALLOCATED / FAILED / SKIPPED")
    error_message: str | None = None
    allocated_at: datetime | None = None
    created_at: datetime


class AllocationOrderResponse(ApiModel):
    """分配单（列表项）。"""

    id: str
    allocation_no: str
    tenant_id: str
    client_product_id: str
    status: str
    status_label: str | None = None

    total_count: int = 0
    allocated_count: int = 0
    failed_count: int = 0

    created_by: str | None = None
    executed_by: str | None = None
    executed_at: datetime | None = None
    failure_reason: str | None = None
    remark: str | None = None

    created_at: datetime
    updated_at: datetime


class AllocationOrderDetailResponse(AllocationOrderResponse):
    """分配单详情（带归属名称与明细行）。"""

    tenant_name: str | None = None
    client_product_name: str | None = None
    client_product_code: str | None = None
    network_type: str | None = None
    items: list[AllocationItemResponse] = Field(default_factory=list)
    item_total: int = 0


class AllocationOrderCreateRequest(ApiModel):
    """创建分配单。

    ``deviceIds`` 指向的设备必须处于 ``IN_STOCK``
    （未冻结、未报废、尚未属于任何租户），否则执行阶段逐行失败；
    创建阶段只做「非空 + 去重 + 数量上限」的形态校验。
    """

    tenant_id: str = Field(min_length=1, max_length=36)
    client_product_id: str = Field(min_length=1, max_length=36)
    device_ids: list[str] = Field(
        min_length=1,
        max_length=MAX_ALLOCATION_DEVICES,
        description=f"设备 ID 列表（最多 {MAX_ALLOCATION_DEVICES} 台）",
    )
    remark: str | None = Field(default=None, max_length=512)

    @field_validator("device_ids")
    @classmethod
    def _dedupe(cls, value: list[str]) -> list[str]:
        """去重并保持顺序。

        重复提交同一台设备是拼接多段名单时的常见错误；
        静默去重比让唯一约束在落库时抛错更友好，且结果与用户预期一致。
        """
        seen: set[str] = set()
        unique: list[str] = []
        for item in value:
            cleaned = item.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                unique.append(cleaned)
        if not unique:
            raise ValueError("deviceIds 不能为空")
        return unique


class AllocationFailure(ApiModel):
    """一条失败明细（执行结果里的可读原因）。"""

    device_id: str
    device_sn: str | None = None
    code: str = Field(description="错误码，便于前端分类展示")
    message: str


class AllocationExecuteResult(ApiModel):
    """分配执行结果。

    ``skipped`` 用于表达「重跑时已处于目标状态、无需再动」的明细——
    它与 ``allocated`` 分开计数，否则重跑会让成功数虚高。
    """

    allocation: AllocationOrderResponse
    allocated_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    failures: list[AllocationFailure] = Field(default_factory=list)


__all__ = [
    "MAX_ALLOCATION_DEVICES",
    "AllocationExecuteResult",
    "AllocationFailure",
    "AllocationItemResponse",
    "AllocationOrderCreateRequest",
    "AllocationOrderDetailResponse",
    "AllocationOrderResponse",
]
