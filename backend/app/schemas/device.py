"""设备域请求 / 响应模型。

契约约定
--------
* 继承 :class:`app.schemas.catalog.ApiModel`：Python 侧 ``snake_case``，
  对外 JSON 为 ``camelCase``（``alias_generator=to_camel``），请求体同样接受 camelCase。
* **凭证只出掩码**：``DeviceCredentialBrief`` 只有 ``secretHint``（形如 ``ab12****``），
  与目录域的密钥处理保持一致——响应模型里根本没有明文字段，
  因此即使业务代码写错也不可能把设备密钥序列化出去。

四维状态的呈现
--------------
``DeviceResponse`` 同时给出四个**原始**维度字段与一个 ``label``
（由 :func:`app.models.enums.derive_device_label` 派生）。
前端筛选/排序用原始维度，展示用 ``label``——两者不可互相替代：
前者是业务事实，后者只是「怎么说给人听」。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from app.schemas.catalog import ApiModel


class DeviceBrief(ApiModel):
    """设备摘要（嵌在订单生成结果、批次明细等场景）。"""

    id: str
    sn: str
    network_type: str
    asset_status: str
    label: str = Field(default="", description="四维状态派生的展示标签")


class DeviceCredentialBrief(ApiModel):
    """设备凭证摘要。**只有掩码，没有明文密钥。**"""

    id: str
    credential_type: str
    secret_hint: str | None = Field(default=None, description="前 4 位 + ****")
    algorithm: str = "sha256"
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class DeviceEventResponse(ApiModel):
    """设备流转事件（设备详情时间线的数据来源）。"""

    id: str
    device_id: str
    tenant_id: str | None = None
    event_type: str
    dimension: str | None = Field(default=None, description="asset / activation / online / bind")
    from_status: str | None = None
    to_status: str | None = None
    actor_id: str | None = None
    actor_account: str | None = None
    summary: str | None = None
    detail: dict[str, Any] | None = None
    trace_id: str | None = None
    created_at: datetime


class DeviceResponse(ApiModel):
    """设备列表项。

    **刻意不含名称类字段**（``tenantName`` / ``tenantCode`` / ``clientProductName``）：
    列表页的租户与产品下拉本来就由各自的接口提供，前端已用「id → 名称」映射渲染。
    若这里再带一份名称，就会出现两套来源——下拉改名后列表还显示旧名，
    是典型的「看起来对的脏数据」。因此名称只在
    :class:`DeviceDetailResponse` 里出现（那里是单条记录，需要自解释）。

    这里给出的是**原始 id**：``tenantId`` 为空表示平台自有库存（尚未分配给租户）。
    """

    id: str
    sn: str
    imei: str | None = None
    iccid: str | None = None
    mac: str | None = None
    vendor_device_id: str | None = None

    tenant_id: str | None = Field(default=None, description="为空表示平台自有库存")
    order_id: str | None = None
    client_product_id: str | None = None
    batch_id: str | None = None
    network_type: str
    firmware_version: str | None = None

    # ---- 四维状态（ADR-03） ----
    asset_status: str
    previous_asset_status: str | None = None
    activation_status: str
    online_status: str
    bind_status: str
    label: str = Field(default="", description="四维状态派生的展示标签，仅供展示")

    generated_at: datetime | None = None
    activated_at: datetime | None = None
    bound_at: datetime | None = None
    frozen_at: datetime | None = None
    freeze_reason: str | None = None
    retired_at: datetime | None = None
    retire_reason: str | None = None
    last_heartbeat_at: datetime | None = None
    remark: str | None = None
    created_at: datetime
    updated_at: datetime


class DeviceDetailResponse(DeviceResponse):
    """设备详情：附加归属名称、凭证掩码与最近事件。

    与列表项的唯一结构性差异就是**这里带名称**
    （``tenantName`` / ``clientProductName`` / ``orderNo``）：
    详情页是单条记录，需要自解释（用户可能直接从消息里点进来），
    而列表页靠下拉映射即可——这不是遗漏，而是有意的分工。
    """

    tenant_name: str | None = None
    client_product_name: str | None = None
    order_no: str | None = None
    credentials: list[DeviceCredentialBrief] = Field(default_factory=list)
    events: list[DeviceEventResponse] = Field(default_factory=list)
    event_total: int = 0


class QrCodeItemResponse(ApiModel):
    """二维码导出项。"""

    device_id: str
    sn: str
    network_type: str
    payload: str = Field(description="二维码文本，前端据此渲染图形")
    format: str = Field(description="JX（集贤 4G）/ JD（京东 Wi-Fi）")


class DeviceFreezeRequest(ApiModel):
    """冻结设备。

    原因必填：冻结是「人为阻断一台已出货设备」的动作，
    没有原因会让后续解冻与责任追溯无从下手。
    """

    reason: str = Field(min_length=1, max_length=512)


class DeviceRetireRequest(ApiModel):
    """报废设备。"""

    reason: str = Field(min_length=1, max_length=512)


class DeviceThawRequest(ApiModel):
    """解冻设备（原因可选）。

    与冻结不同，解冻是**恢复**动作，业务上不要求说明理由；
    允许留空是为了让前端可以直接提供一个「一键解冻」按钮。
    """

    reason: str | None = Field(default=None, max_length=512)


class DeviceStatsResponse(ApiModel):
    """设备统计（供前端统计卡与工作台）。

    四个维度分开给出而不是合成一个数字：四维是正交的，
    「在线数」与「已激活数」回答的是不同问题，合成会丢失信息。
    """

    total: int = 0
    by_asset_status: dict[str, int] = Field(default_factory=dict)
    by_online_status: dict[str, int] = Field(default_factory=dict)
    by_activation_status: dict[str, int] = Field(default_factory=dict)
    by_bind_status: dict[str, int] = Field(default_factory=dict)


__all__ = [
    "DeviceBrief",
    "DeviceCredentialBrief",
    "DeviceDetailResponse",
    "DeviceEventResponse",
    "DeviceFreezeRequest",
    "DeviceResponse",
    "DeviceRetireRequest",
    "DeviceStatsResponse",
    "DeviceThawRequest",
    "QrCodeItemResponse",
]
