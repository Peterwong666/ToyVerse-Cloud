"""设备批次导入的请求 / 响应模型。

批次链路的七个环节
------------------
「上传 → SHA-256 去重 → 预检 → 导入 → 进度 → 错误报告 → 幂等重跑」

响应模型据此设计：

* ``BatchResponse`` 一次性给出 ``totalRows/validRows/invalidRows/duplicatedRows/importedRows``，
  前端不需要拼接多个接口就能画出进度条与预检结论；
* ``errorReportAvailable`` / ``importedRatio`` 是两个**派生**字段——
  让前端不必自己算（算错会导致「有错误却显示没有」这类假象）。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.catalog import ApiModel


class BatchResponse(ApiModel):
    """批次（预检 / 导入）响应。"""

    id: str
    batch_no: str
    order_id: str | None = None
    tenant_id: str | None = None
    network_type: str

    file_name: str | None = None
    file_sha256: str | None = Field(default=None, description="文件摘要，同摘要重复上传会直接复用本批次")

    total_rows: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    duplicated_rows: int = 0
    imported_rows: int = 0
    imported_ratio: float = Field(default=0.0, description="已导入 / 有效行，0~1")

    status: str
    error_report_available: bool = Field(default=False, description="是否存在逐行错误明细")
    created_by: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    remark: str | None = None
    created_at: datetime
    updated_at: datetime


class BatchLineResponse(ApiModel):
    """批次明细行。"""

    id: str
    batch_id: str
    row_no: int
    sn: str | None = None
    imei: str | None = None
    iccid: str | None = None
    mac: str | None = None
    status: str
    error_message: str | None = None
    device_id: str | None = None


__all__ = ["BatchLineResponse", "BatchResponse"]
