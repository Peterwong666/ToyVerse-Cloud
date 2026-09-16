"""终端用户小程序域的请求 / 响应模型（P8）。

契约约定
--------
* Python 字段 ``snake_case``，对外 JSON ``camelCase``——沿用
  :class:`app.schemas.catalog.ApiModel` 的 ``alias_generator=to_camel``，
  不逐字段手写别名。
* 本文件是**前端唯一的字段来源**：小程序端页面直接按这里的字段名取值，
  因此字段名一旦发布就不可改名（与 P7 的 ``ChatChunkEvent`` 同一约定）。

为什么流式帧单独用 ``StrEnum`` 而不是 Pydantic 模型
---------------------------------------------------
WS 与 SSE 共用同一套帧，但两者的**空值策略不同**：

* ``session.ready`` 协议里明确带 ``"messageId": null``；
* 其余帧不该出现一堆无意义的 ``null`` 字段。

用 Pydantic 模型统一序列化，要么到处补 ``exclude_none``，要么每帧带上
一堆 ``null``——两种都不好。因此帧的**字段名**以
:class:`ChatFrameType` / :class:`ChatClientFrameType` 的取值 + 常量表定死
（见 :data:`SERVER_FRAME_FIELDS`），实际载荷由
:mod:`app.services.dialogue_service` 按该表逐帧构造，
WS 与 SSE 只负责序列化与发送。这样「帧协议」只有一处定义，
不需要在传输层再判断一次该带哪些字段。
"""

from __future__ import annotations

import base64
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from app.schemas.catalog import ApiModel

#: 手机号格式（中国大陆）：11 位、以 1 开头、第二位 3-9。
#:
#: 校验放在 schema 层而不是服务层，是为了让「格式不合法」与「业务拒绝」
#: 走同一条 400 ``VALIDATION_ERROR`` 通路，前端只需要处理一种错误形状。
PHONE_PATTERN = r"^1[3-9]\d{9}$"

#: 短信验证码位数（与 ``secrets.randbelow(10**6)`` 的生成口径一致）
LOGIN_CODE_LENGTH = 6


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------


class AuthCodeRequest(ApiModel):
    """请求发送登录验证码。"""

    phone: str = Field(pattern=PHONE_PATTERN, max_length=32, description="手机号（登录账号）")

    @field_validator("phone")
    @classmethod
    def _strip_phone(cls, value: str) -> str:
        """去掉首尾空白：粘贴手机号时常带空格，没必要因此报格式错误。"""
        return value.strip()


class AuthCodeResponse(ApiModel):
    """验证码发送结果。

    ``mockCode`` 只在 ``MINIAPP_SMS_PROVIDER=mock``（开发 / 验收环境）时回显，
    且 ``mock=true`` 会同时出现——**模拟结果必须显式标注**，
    否则演示数据会被误当成真实的短信下发记录（ADR-07 的精神）。
    """

    sent: bool
    mock: bool = Field(description="是否为模拟通道（未真实发送短信）")
    mock_code: str | None = Field(
        default=None, description="模拟通道下回显的验证码；真实通道恒为 null"
    )
    expires_in_seconds: int
    message: str | None = None


class AuthLoginRequest(ApiModel):
    """验证码登录。"""

    phone: str = Field(pattern=PHONE_PATTERN, max_length=32)
    code: str = Field(min_length=4, max_length=8, description="短信验证码")

    @field_validator("phone", "code")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class MiniappDeviceBrief(ApiModel):
    """设备摘要（列表 / 登录响应共用）。

    ``nickname`` 目前恒为 ``null``：``devices`` 表没有昵称列（P8 只做展示，
    重命名属 P9 的设备管理）。字段先占位，避免 P9 加上重命名时
    前端要改字段名。

    :class:`AuthLoginResponse` 会引用本模型，故它定义在认证区段之前——
    Pydantic 在类定义时即解析注解，跨区段的正向引用需要模型先存在。
    """

    id: str
    sn: str
    nickname: str | None = None
    network_type: str = Field(description="4G / WIFI")
    network_label: str | None = Field(default=None, description="联网方式的中文展示名")
    asset_status: str
    activation_status: str
    online: bool = Field(description="派生在线判据（按心跳窗口计算，非落库投影）")
    online_status: str
    last_heartbeat_at: datetime | None = None
    firmware_version: str | None = None
    bound_at: datetime | None = None


class EndUserBrief(ApiModel):
    """终端用户摘要。"""

    id: str
    phone: str
    nickname: str | None = None


class AuthLoginResponse(ApiModel):
    """登录响应（含令牌与该用户的设备列表）。

    为什么把设备列表塞进登录响应？小程序登录后直接进首页，
    首页要展示「我的玩具」。若让前端再发一次请求，首屏会白屏一次；
    而设备数在真实场景里是个位数，一起返回的成本可以忽略。
    """

    access_token: str
    token_type: str = Field(default="Bearer", description="固定 Bearer")
    expires_in_seconds: int
    user: EndUserBrief
    devices: list[MiniappDeviceBrief] = Field(default_factory=list)


class MiniappProfileResponse(ApiModel):
    """终端用户资料。"""

    id: str
    phone: str
    nickname: str | None = None
    avatar: str | None = None
    device_count: int = Field(description="已绑定的设备数量")


# ---------------------------------------------------------------------------
# 设备
# ---------------------------------------------------------------------------


class MiniappDeviceDetail(MiniappDeviceBrief):
    """设备详情（在小程序「设备信息」页展示）。

    ``settings`` 直接回原样（含服务端维护的 ``updatedAt``），
    而不是在详情里再拆一层——小程序设置页与设备信息页读的是同一份数据。
    """

    product_name: str | None = None
    tenant_name: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    bind_count: int = 0


class ScanResolveRequest(ApiModel):
    """扫码解析请求。"""

    payload: str = Field(
        min_length=1,
        max_length=2048,
        description="二维码原始文本（JX|… 或 JD|…），由小程序扫码器读取后原样提交",
    )

    @field_validator("payload")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class ScanDeviceInfo(ApiModel):
    """扫码解析出的设备信息。"""

    id: str
    sn: str
    network_type: str
    asset_status: str
    activation_status: str
    bind_status: str
    device_label: str = Field(description="四维状态派生的展示标签")
    online: bool
    imei: str | None = None
    iccid: str | None = None
    mac: str | None = None
    firmware_version: str | None = None


class ScanProductInfo(ApiModel):
    """扫码解析出的客户产品信息。"""

    id: str
    code: str
    name: str


class ScanResolveResponse(ApiModel):
    """扫码解析结果。

    **本接口不抛业务异常**（除「二维码格式非法」与「SN 不在设备库」两种
    根本性错误）：设备不可绑是一种**正常状态**，用 ``bindable=false`` +
    中文 ``reason`` 表达，页面据此给出「为什么不能绑」而不是一个错误弹窗。
    """

    format: str = Field(description="JX（集贤 4G）/ JD（京东 Wi-Fi）")
    network_type: str = Field(description="4G / WIFI，以设备记录为准")
    bindable: bool
    reason: str | None = Field(default=None, description="不可绑原因（可绑时为 null）")
    device: ScanDeviceInfo | None = None
    product: ScanProductInfo | None = None
    tenant_name: str | None = None
    cloud_vendor: str | None = Field(default=None, description="云服务商厂商枚举值")
    cloud_vendor_label: str | None = None
    activation_status: str
    bind_status: str
    asset_status: str


class ActivateWifiRequest(ApiModel):
    """Wi-Fi（京东 JoyInside）激活上报。

    设备端配网成功后上报自己读到的 SN 与 MAC，服务端**必须**与设备记录
    逐项比对——`mac` 为空时以本次上报为准写入（设备记录 MAC 常在生产阶段
    才回填），但 ``sn`` 不一致一律判为 ``BIND_FAILED``：
    SN 是设备的唯一身份，不一致意味着「上报的不是这台」。
    """

    sn: str = Field(min_length=1, max_length=64)
    mac: str = Field(min_length=1, max_length=32)

    @field_validator("sn", "mac")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class ActivationResult(ApiModel):
    """激活结果（4G 与 Wi-Fi 共用同一形状）。"""

    device_id: str
    sn: str
    network_type: str
    activation_status: str
    bind_status: str
    activated_at: datetime | None = None
    vendor_message: str | None = Field(default=None, description="厂商回执文案（原样转述）")
    mock: bool = Field(description="是否为模拟激活结果（未调用真实厂商）")


class UnbindRequest(ApiModel):
    """解绑请求。

    与商户端的 ``BindingUnbindRequest``（原因**必填**）刻意不同：
    小程序端是终端用户自己操作，多数人不会写原因，强制填写只会催生
    「111」「。。。」这类无意义内容。省略时服务层写入「终端用户主动解绑」，
    仍能保证时间线里有可读的原因。
    """

    reason: str | None = Field(default=None, max_length=512)

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class UnbindResult(ApiModel):
    """解绑结果。"""

    device_id: str
    sn: str
    bind_status: str
    asset_status: str
    activation_status: str = Field(description="解绑**不改**激活状态（已激活的设备仍是已激活）")
    unbound_at: datetime | None = None
    message: str = "已解除绑定"


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------


class DeviceSettingsResponse(ApiModel):
    """设备设置。

    读取时若 ``devices.settings`` 为空，返回 :data:`app.services.miniapp_service.DEFAULT_DEVICE_SETTINGS`
    的默认值（音量 60 / 儿童模式关 / 唤醒词「你好小伙伴」）——
    「没设置过」与「设置为默认值」对用户是同一件事，返回 null 只会让前端
    多写一份兜底默认值。
    """

    volume: int
    child_mode: bool
    wake_word: str
    updated_at: datetime | None = Field(
        default=None, description="设置最近一次写入时间；从未写入过为 null"
    )


class DeviceSettingsUpdateRequest(ApiModel):
    """更新设备设置（局部更新，未传的字段保持原值）。

    字段范围校验（``volume`` 0–100）放在 schema 层：
    `volume=999` 是**请求本身不合法**（400），而不是「设备不支持」，
    两者对前端的处理方式不同。
    """

    volume: int | None = Field(default=None, ge=0, le=100)
    child_mode: bool | None = None
    wake_word: str | None = Field(default=None, min_length=1, max_length=32)


# ---------------------------------------------------------------------------
# 充值
# ---------------------------------------------------------------------------


class RechargePlanResponse(ApiModel):
    """流量套餐（小程序端可见字段）。

    只下发终端用户需要的字段：**不含** ``status`` / ``sortOrder`` 这类
    运营字段，也不含 ``tenantId``——套餐已经按设备所属租户筛过，
    再回一个租户 ID 只会让前端多一个不该关心的字段。
    """

    id: str
    code: str
    name: str
    description: str | None = None
    data_mb: int
    valid_days: int
    price: float
    is_recommended: bool = False


class RechargePlanListResult(ApiModel):
    """套餐列表。

    ``supported=false`` 是**正常状态**而非错误：Wi-Fi 设备通过家庭网络联网，
    根本不需要流量充值。若这里返回 4xx，前端就只能把「不支持」写成
    异常处理分支，反而埋没了真实错误。
    """

    supported: bool
    reason: str | None = Field(default=None, description="不支持时的说明文案")
    records: list[RechargePlanResponse] = Field(default_factory=list)


class RechargeOrderCreateRequest(ApiModel):
    """充值下单。"""

    device_id: str = Field(max_length=36)
    plan_id: str = Field(max_length=36)


class RechargeOrderResponse(ApiModel):
    """充值订单。

    ``amount`` / ``data_mb`` / ``valid_days`` / ``plan_name`` 是**下单时的快照**：
    套餐后续改价不得改写历史订单，否则对账时「实付金额」会跟着变。
    """

    id: str
    order_no: str
    device_id: str
    plan_id: str
    plan_name: str | None = None
    amount: float
    data_mb: int
    valid_days: int
    status: str
    paid_at: datetime | None = None
    failed_reason: str | None = None
    refunded_at: datetime | None = None
    created_at: datetime


class RechargePayResult(ApiModel):
    """支付结果。"""

    order: RechargeOrderResponse
    mock: bool = Field(description="是否为模拟支付（未走真实支付通道）")


# ---------------------------------------------------------------------------
# 流式对话
# ---------------------------------------------------------------------------


class ChatStreamRequest(ApiModel):
    """SSE 降级流请求（与 WS 的 ``user.text`` 帧等价）。

    ``sessionId`` 为空表示新开一次会话；续聊时传入 WS/上次 SSE 返回的会话 ID。
    """

    device_id: str = Field(max_length=36)
    text: str = Field(min_length=1, max_length=2000, description="用户输入文本")
    session_id: str | None = Field(default=None, max_length=36)

    @field_validator("text")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("对话内容不能为空")
        return cleaned


class ChatClientFrameType(StrEnum):
    """客户端 → 服务端的帧类型。"""

    SESSION_OPEN = "session.open"
    USER_TEXT = "user.text"
    USER_AUDIO = "user.audio"
    SESSION_CLOSE = "session.close"


class ChatFrameType(StrEnum):
    """服务端 → 客户端的帧类型。"""

    SESSION_READY = "session.ready"
    ASR_PARTIAL = "asr.partial"
    ASSISTANT_DELTA = "assistant.delta"
    ASSISTANT_AUDIO = "assistant.audio"
    ASSISTANT_DONE = "assistant.done"
    ERROR = "error"


#: 帧类型 → 该帧**必然出现**的字段（值为 ``None`` 时表示「按需携带」）。
#:
#: 这张表是帧协议的单一事实来源：:mod:`app.services.dialogue_service`
#: 按它构造载荷，据此可校验「有没有漏字段 / 多字段」，
#: 前端也可以直接读它生成类型定义，避免两端各写一份而漂移。
SERVER_FRAME_FIELDS: dict[ChatFrameType, tuple[str, ...]] = {
    ChatFrameType.SESSION_READY: ("type", "sessionId", "messageId"),
    ChatFrameType.ASR_PARTIAL: ("type", "text"),
    ChatFrameType.ASSISTANT_DELTA: ("type", "delta", "index"),
    ChatFrameType.ASSISTANT_AUDIO: ("type", "data", "mimeType"),
    ChatFrameType.ASSISTANT_DONE: ("type", "messageId", "latencyMs", "sessionId"),
    ChatFrameType.ERROR: ("type", "code", "message", "traceId"),
}

#: 帧里的内容安全标记：命中敏感词时写入 ``DialogueMessage.safety_flag``。
#:
#: 只记「命中类型」而不记具体敏感词——审计与日志里的敏感词本身就是风险源
#: （与 :data:`app.api.v1.ai.SAFETY_FLAG_KEYWORD` 同一口径与同一取值）。
SAFETY_FLAG_KEYWORD = "KEYWORD"


def encode_frame_audio(payload: bytes | None) -> str | None:
    """把回复音频编码为 base64（``None`` 原样透传，表示本次无音频）。

    与 :func:`app.schemas.ai.encode_audio` 同一实现。这里重复定义是因为
    *帧协议* 的编码口径应由帧协议自己持有：P7 的 ``/ai/tts`` 若哪天改成
    流式分片，本项目的 ``assistant.audio`` 帧不应跟着变。
    """
    if payload is None:
        return None
    return base64.b64encode(payload).decode("ascii")


__all__ = [
    "LOGIN_CODE_LENGTH",
    "PHONE_PATTERN",
    "SAFETY_FLAG_KEYWORD",
    "SERVER_FRAME_FIELDS",
    "ActivateWifiRequest",
    "ActivationResult",
    "AuthCodeRequest",
    "AuthCodeResponse",
    "AuthLoginRequest",
    "AuthLoginResponse",
    "ChatClientFrameType",
    "ChatFrameType",
    "ChatStreamRequest",
    "DeviceSettingsResponse",
    "DeviceSettingsUpdateRequest",
    "EndUserBrief",
    "MiniappDeviceBrief",
    "MiniappDeviceDetail",
    "MiniappProfileResponse",
    "RechargeOrderCreateRequest",
    "RechargeOrderResponse",
    "RechargePayResult",
    "RechargePlanListResult",
    "RechargePlanResponse",
    "ScanDeviceInfo",
    "ScanProductInfo",
    "ScanResolveRequest",
    "ScanResolveResponse",
    "UnbindRequest",
    "UnbindResult",
    "encode_frame_audio",
]
