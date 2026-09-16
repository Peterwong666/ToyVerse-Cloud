"""AI 域请求 / 响应模型（P7）。

契约约定
--------
* Python 字段 ``snake_case``，对外 JSON ``camelCase``——沿用
  :class:`app.schemas.catalog.ApiModel` 的 ``alias_generator=to_camel``，
  不手写别名（前端无需做字段名转换）。
* **流式事件用扁平结构**：``ChatChunkEvent`` 把 ``type`` 放在顶层，
  前端可只解析 ``type`` + ``delta`` 两个字段就完成逐字渲染。

无密钥信息泄漏
--------------
本文件中不存在任何用于回显密钥的字段——``/ai/providers`` 只回
``configured`` 布尔与健康状态，密钥明文与密文都不出现在响应契约里。
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.schemas.catalog import ApiModel

# ---------------------------------------------------------------------------
# 供应商
# ---------------------------------------------------------------------------


class ProviderInfo(ApiModel):
    """供应商信息（注册表视角 + 平台配置视角的合并）。

    ``registered`` 与 ``configured`` 是两个不同的事实：
    前者表示「代码里有这个适配器」，后者表示「平台已录入密钥」。
    分开呈现才能让运维区分「没实现」与「没配置」。
    """

    code: str
    name: str
    kind: str
    vendor: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    configured: bool = False
    registered: bool = True
    requires_credentials: bool = True
    description: str = ""
    health: str | None = Field(default=None, description="最近一次探测状态（未探测为 null）")


class ProviderListResult(ApiModel):
    """供应商列表。"""

    records: list[ProviderInfo] = Field(default_factory=list)
    total: int = 0
    default_code: str = Field(description="平台默认供应商注册键（AI_DEFAULT_PROVIDER）")


class HealthResponse(ApiModel):
    """健康探测结果。

    未配置密钥时 ``status=DOWN``、``ok=false``——**不伪造成功**（ADR-07）。
    """

    code: str
    name: str
    status: str
    ok: bool
    message: str
    vendor: str | None = None
    latency_ms: int | None = None
    checked_at: datetime | None = None
    detail: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# 对话
# ---------------------------------------------------------------------------


class ChatRequest(ApiModel):
    """发起一次对话。

    ``stream=True`` 时返回 NDJSON 流；``False`` 时返回整段响应，便于调试台
    与自动化测试直接断言文本。
    """

    message: str = Field(min_length=1, max_length=2000, description="用户输入文本")
    session_id: str | None = Field(default=None, description="续聊时传入；为空则新建会话")
    client_product_id: str | None = Field(
        default=None, description="客户产品 ID；供应商解析顺序第 1 层的依据"
    )
    device_id: str | None = Field(default=None, description="设备 ID（P8 联动用）")
    end_user_id: str | None = Field(default=None, description="终端用户 ID（P9 联动用）")
    provider_code: str | None = Field(default=None, description="显式指定供应商（调试用）")
    role_preset: str | None = Field(default=None, description="角色预设编码")
    stream: bool = Field(default=True, description="是否流式返回")

    @field_validator("message")
    @classmethod
    def _strip_message(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("对话内容不能为空")
        return cleaned


class ChatChunkEvent(ApiModel):
    """流式事件（NDJSON 每行一个）。

    选择 NDJSON 而非 SSE 的理由见 ``app.api.v1.ai`` 模块 docstring。
    """

    type: str = Field(description="session / delta / done / error")
    session_id: str | None = None
    message_id: str | None = None
    delta: str = ""
    index: int = 0
    is_final: bool = False
    finish_reason: str | None = None
    provider: str | None = None
    provider_layer: str | None = None
    latency_ms: int | None = Field(default=None, description="首字延迟（毫秒），仅首个 delta 携带")
    total_latency_ms: int | None = None
    safety_flag: str | None = Field(default=None, description="命中内容安全过滤时的标记")
    chunk_count: int = Field(default=0, description="累计分块数，仅 done 事件携带")
    error_code: str | None = None
    error_message: str | None = None


class ChatResponse(ApiModel):
    """非流式对话响应。"""

    session_id: str
    message_id: str
    reply: str
    chunks: int = Field(description="分块数（验证「真的分多块产出」）")
    latency_ms: int
    provider: str
    provider_layer: str
    role_preset: str | None = None
    safety_flag: str | None = None
    simulated: bool = False


# ---------------------------------------------------------------------------
# 语音
# ---------------------------------------------------------------------------


class AsrRequest(ApiModel):
    """语音识别请求。

    ``audio_base64`` 承载真实音频；离线环境下可只传 ``hint_text``，
    由 mock 的 ``echo`` 模式复读——这样语音链路在无音频素材时也能联调。
    """

    audio_base64: str | None = Field(default=None, description="音频 base64（可省略）")
    hint_text: str | None = Field(default=None, max_length=500, description="模拟输入（echo 模式复读）")
    format: str = Field(default="pcm", max_length=16)
    sample_rate: int = Field(default=16000, ge=8000, le=48000)
    provider_code: str | None = None
    client_product_id: str | None = None


class AsrResponse(ApiModel):
    """语音识别结果。"""

    text: str
    is_final: bool = True
    confidence: float | None = None
    duration_ms: int | None = None
    provider: str
    provider_layer: str
    simulated: bool = False


class TtsRequest(ApiModel):
    """语音合成请求。"""

    text: str = Field(min_length=1, max_length=2000)
    voice_id: str | None = Field(default=None, max_length=128)
    provider_code: str | None = None
    client_product_id: str | None = None


class TtsResponse(ApiModel):
    """语音合成结果。

    ``audio_base64`` 为空时表示本次未产出音频，调用方应回退为文本播报
    （mock 的 ``text`` 模式的正常行为，不是错误）。
    """

    text: str
    content_type: str
    audio_base64: str | None = Field(default=None, description="音频 base64；为空表示未产出音频")
    duration_ms: int | None = None
    voice_id: str | None = None
    provider: str
    provider_layer: str
    simulated: bool = False


# ---------------------------------------------------------------------------
# 模拟引擎素材（调试台）
# ---------------------------------------------------------------------------


class RolePresetItem(ApiModel):
    """内置角色预设。"""

    code: str
    name: str
    greeting: str


class ScenarioItem(ApiModel):
    """一条内置素材。"""

    kind: str
    title: str
    keywords: list[str] = Field(default_factory=list)
    preview: str = Field(description="正文前 40 字，用于列表展示")


class ScenarioListResponse(ApiModel):
    """``GET /ai/mock/scenarios`` 的响应：把离线引擎的「能力边界」摊开给调试台。"""

    stories: list[ScenarioItem] = Field(default_factory=list)
    songs: list[ScenarioItem] = Field(default_factory=list)
    intent_keywords: dict[str, list[str]] = Field(default_factory=dict)
    weather_replies: list[str] = Field(default_factory=list)
    fallback_replies: list[str] = Field(default_factory=list)
    safety_keywords: list[str] = Field(default_factory=list)
    role_presets: list[RolePresetItem] = Field(default_factory=list)


def encode_audio(payload: bytes | None) -> str | None:
    """把音频字节编码为 base64（``None`` 原样透传）。"""
    if payload is None:
        return None
    return base64.b64encode(payload).decode("ascii")


def decode_audio(value: str | None) -> bytes | None:
    """把 base64 解码为字节；非法 base64 返回 ``None``（由调用方决定是否报错）。"""
    if not value:
        return None
    try:
        return base64.b64decode(value, validate=False)
    except (ValueError, TypeError):
        return None


__all__ = [
    "AsrRequest",
    "AsrResponse",
    "ChatChunkEvent",
    "ChatRequest",
    "ChatResponse",
    "HealthResponse",
    "ProviderInfo",
    "ProviderListResult",
    "RolePresetItem",
    "ScenarioItem",
    "ScenarioListResponse",
    "TtsRequest",
    "TtsResponse",
    "decode_audio",
    "encode_audio",
]
