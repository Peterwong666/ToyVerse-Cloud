"""绑定域的请求 / 响应模型（P5）。

两步绑定流程的字段契约
======================

``precheck`` 只负责「这台设备能不能绑、绑给谁」，并签发一张**短时确认令牌**；
``bind`` 才真正落库。拆成两步而不是一步到位，理由与线下扫码的业务现实一致：

1. 扫码结果不可信（用户可能扫了隔壁那台的码），先回显「你扫到的是
   SN-xxx / 星辰小方块」让人确认；
2. 确认令牌 5 分钟过期且**用一次即销毁**，因此「扫了码但没点确认」
   不会占住设备的绑定名额。

响应里的敏感边界
----------------
``BindingResponse`` **不含令牌明文，也不含令牌摘要**——
摘要只留在库内（``device_bindings.confirm_token_hash``），
响应模型里根本没有对应字段，因此不可能被序列化出去。
令牌明文只在 ``BindingPrecheckResponse.confirmToken`` 出现一次。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from app.schemas.catalog import ApiModel


class BindingPrecheckRequest(ApiModel):
    """扫码预检请求。"""

    qr_payload: str = Field(
        min_length=1,
        max_length=2048,
        description="二维码原始文本（JX|… 或 JD|…），由前端扫码器读取后原样提交",
    )

    @field_validator("qr_payload")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("二维码内容不能为空")
        return cleaned


class BindingPrecheckResponse(ApiModel):
    """扫码预检结果（含一次性确认令牌）。

    ``confirmToken`` 是**明文令牌，仅此一次返回**；前端应在提交 ``bind`` 后立即丢弃。
    """

    binding_id: str
    device_id: str
    sn: str
    network_type: str
    device_label: str = Field(description="四维状态派生的展示标签")
    asset_status: str
    tenant_id: str
    tenant_name: str | None = None
    client_product_id: str | None = None
    client_product_name: str | None = None
    qr_format: str = Field(description="JX（集贤 4G）/ JD（京东 Wi-Fi）")

    confirm_token: str = Field(description="一次性确认令牌，明文仅此一次返回")
    expires_at: datetime
    expires_in_seconds: int = Field(description="令牌剩余有效秒数（默认 300）")


class BindingConfirmRequest(ApiModel):
    """确认绑定请求。"""

    confirm_token: str = Field(
        min_length=8, max_length=256, description="precheck 返回的确认令牌"
    )
    end_user_id: str | None = Field(
        default=None,
        max_length=36,
        description="终端用户标识（`end_users` 属 P9，此处只存不校验）",
    )
    remark: str | None = Field(default=None, max_length=512)


class BindingUnbindRequest(ApiModel):
    """解绑请求。

    原因必填，且**空白串不算填了原因**——与 P4 订单驳回的 ``rejectReason``
    同一口径（那边由服务层拒绝 ``"   "``）。只写 ``min_length=1`` 时
    ``"   "`` 能通过校验并被原样写进绑定记录，时间线里就会出现
    「解绑设备 X：   」这种看着没有原因、无法追溯责任的记录。
    """

    reason: str = Field(min_length=1, max_length=512)

    @field_validator("reason")
    @classmethod
    def _strip_reason(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("解绑原因不能为空白")
        return cleaned


class BindingResponse(ApiModel):
    """绑定记录。

    **不含令牌字段**：摘要留在库内，明文在 ``precheck`` 后即应被前端丢弃。
    """

    id: str
    device_id: str
    sn: str | None = None
    tenant_id: str
    client_product_id: str | None = None
    end_user_id: str | None = None

    status: str = Field(description="PENDING / BOUND / UNBOUND")
    status_label: str | None = Field(default=None, description="中文状态名")

    qr_format: str | None = None
    qr_payload_hash: str | None = None

    confirmed_at: datetime | None = None
    bound_at: datetime | None = None
    bound_by: str | None = None
    unbound_at: datetime | None = None
    unbind_reason: str | None = None
    unbound_by: str | None = None
    bind_count: int = 0
    remark: str | None = None

    # ---- 设备侧快照（供列表自解释，免得前端再查一次设备） ----
    device_label: str | None = None
    asset_status: str | None = None
    bind_status: str | None = None
    network_type: str | None = None

    created_at: datetime
    updated_at: datetime


__all__ = [
    "BindingConfirmRequest",
    "BindingPrecheckRequest",
    "BindingPrecheckResponse",
    "BindingResponse",
    "BindingUnbindRequest",
]
