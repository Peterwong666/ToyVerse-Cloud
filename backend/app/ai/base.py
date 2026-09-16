"""AI 供应商抽象层：把「玩具要的 AI 能力」与「厂商怎么实现」彻底分开。

为什么这样分层
--------------
智能玩具的 AI 能力横跨三个**时间尺度完全不同**的领域，把它们塞进一个
``chat()`` 会让适配器变成一锅粥：

====================  ==========================  ==========================
能力域                典型调用时机                失败代价
====================  ==========================  ==========================
设备侧 device         出厂 / 出货 / 激活（低频）   设备变砖，必须可重试可幂等
对话侧 dialogue       每次说话（高频，长连接）     用户当场感知卡顿
语音侧 voice          每次说话前后（高频）         同上
生命周期 health_check 运维与联调                 决定「这个厂商现在能不能用」
====================  ==========================  ==========================

因此抽象基类按**能力域**切分方法，而不是按厂商切分。收益：

1. **mock 与真实厂商可互换**：``AIProvider`` 是唯一契约，
   离线模拟引擎与火山引擎在调用方眼里没有区别（ADR-04 的落地方式）。
2. **能力是可探测的**：``capabilities`` 声明支持哪些域，
   路由层据此给出「该厂商不支持 TTS」这类精确反馈，而不是笼统 500。
3. **安全失败有统一出口**：未配置密钥的厂商由
   :meth:`AIProvider.ensure_configured` 统一抛 ``VENDOR_UNAVAILABLE``，
   绝不返回伪造成果（ADR-07，项目的架构红线）。

流式为什么用 ``AsyncIterator[ChatChunk]``
-----------------------------------------
真实大模型与端到端语音都是**逐块产出**的；如果抽象层只提供
「返回整段文本」，前端就无法做逐字渲染，也无法度量首字延迟——
而首字延迟正是玩具体验的核心指标（见 docs/10）。因此 ``chat`` 从
第一天就定为异步迭代器，mock 引擎也必须真的分多块 yield。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from app.core.errors import AppException, ErrorCode
from app.db.base import utcnow
from app.models.enums import AiProviderKind, MessageContentType, MessageRole, ProviderHealth

# ---------------------------------------------------------------------------
# 能力标签
# ---------------------------------------------------------------------------

CAP_DEVICE = "device"
CAP_DIALOGUE = "dialogue"
CAP_ASR = "asr"
CAP_TTS = "tts"

#: 全部能力标签（用于解析请求里 ``capabilities`` 参数）
ALL_CAPABILITIES: frozenset[str] = frozenset({CAP_DEVICE, CAP_DIALOGUE, CAP_ASR, CAP_TTS})


def capabilities_for_kind(kind: AiProviderKind | str) -> frozenset[str]:
    """按供应商类型给出默认能力集合。

    * ``DEVICE_CLOUD``（集贤 / JoyInside）—— 设备侧能力为主，多数不带模型对话
    * ``MODEL``（火山 / 百度）—— 对话与语音能力
    * ``LOCAL``（mock）—— 四类能力全有，否则离线跑不通全流程
    """
    kind_value = str(kind)
    if kind_value == AiProviderKind.DEVICE_CLOUD:
        return frozenset({CAP_DEVICE})
    if kind_value == AiProviderKind.MODEL:
        return frozenset({CAP_DIALOGUE, CAP_ASR, CAP_TTS})
    return ALL_CAPABILITIES


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ChatMessage:
    """一条对话消息（作为 ``chat`` 的入参历史）。

    ``content_type`` 用字符串而非枚举，是为了让 DTO 不绑定 SQLAlchemy 层，
    便于在单元测试里直接构造。
    """

    role: str = str(MessageRole.USER)
    content: str = ""
    content_type: str = str(MessageContentType.TEXT)
    audio_url: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(slots=True)
class ChatChunk:
    """流式回复的一个分块。

    Attributes:
        delta: 本次新增的文本片段（**增量**，不是累积全文）。
        index: 分块序号，从 0 开始；前端据此检测丢块。
        is_final: 是否为最后一块。
        latency_ms: 该块的相对耗时（毫秒），用于度量首字延迟。
        finish_reason: 结束原因（``stop`` / ``length`` / ``safety``）。
        message_id: 厂商侧消息 ID（若有）。
    """

    delta: str
    index: int
    is_final: bool = False
    latency_ms: int | None = None
    finish_reason: str | None = None
    message_id: str | None = None


@dataclass(slots=True)
class HealthCheckResult:
    """健康探测结果。"""

    status: str
    ok: bool
    message: str
    vendor: str | None = None
    latency_ms: int | None = None
    checked_at: datetime | None = None
    detail: dict[str, Any] | None = None


@dataclass(slots=True)
class DeviceProvisionResult:
    """设备生成结果。

    ``simulated`` 标记「这是模拟结果，不是厂商真实回执」——
    mock 引擎必须诚实标注，避免演示数据被误当成真实出货记录（ADR-07 精神）。
    """

    success: bool
    message: str
    vendor_device_ids: list[str] = field(default_factory=list)
    simulated: bool = False
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class DeviceActionResult:
    """设备激活动作的结果（激活 / 停用 / OTA 共用）。"""

    success: bool
    message: str
    device_id: str | None = None
    activated: bool | None = None
    task_id: str | None = None
    simulated: bool = False
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class AsrResult:
    """语音识别结果。

    ``is_final`` 区分「中间结果」（边说边出字）与「最终结果」；
    玩具场景下中间结果用于即时反馈，最终结果才落库。
    """

    text: str
    is_final: bool = True
    confidence: float | None = None
    duration_ms: int | None = None
    simulated: bool = False
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class TtsResult:
    """语音合成结果。

    ``audio`` 为 ``None`` 表示本次未产出真实音频（如 mock 的 ``text`` 模式），
    调用方应回退为「文本播报」而不是把空音频塞给设备。
    """

    text: str
    content_type: str = "audio/mpeg"
    audio: bytes | None = None
    duration_ms: int | None = None
    simulated: bool = False
    voice_id: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class ProviderDescriptor:
    """供应商的自描述（供 ``/ai/providers`` 列表使用）。"""

    code: str
    name: str
    kind: str
    vendor: str | None
    capabilities: list[str]
    configured: bool
    requires_credentials: bool
    description: str


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------


class AIProvider(ABC):
    """AI 供应商抽象基类。

    子类只需实现自己**真正支持**的能力域，其余方法保持默认行为：
    抛 :class:`NotImplementedError`，由调用方按 ``capabilities`` 提前规避。
    """

    #: 注册键，必须全局唯一（与 ``ai_providers.code`` 对应）
    code: str = ""
    #: 中文展示名
    name: str = ""
    #: 供应商类型
    kind: AiProviderKind = AiProviderKind.MODEL
    #: 对应 ``CloudVendor``（mock 为 ``None``）
    vendor: str | None = None
    #: 是否必须配置密钥才能工作（mock 为 ``False``）
    requires_credentials: bool = True
    #: 支持的能力域
    capabilities: frozenset[str] = frozenset()
    description: str = ""

    def __init__(self, *, credentials: dict[str, str] | None = None) -> None:
        #: 厂商密钥（由注册表从 ``settings.ai_provider_credentials`` 注入）
        self.credentials: dict[str, str] = dict(credentials or {})

    # ---------------- 配置与安全失败 ----------------

    @property
    def is_configured(self) -> bool:
        """是否已具备可调用条件。

        未接入的厂商一律视为「未配置」，任何真实调用都必须安全失败（ADR-07）。
        mock 引擎 ``requires_credentials=False``，因此恒为已配置。
        """
        if not self.requires_credentials:
            return True
        return self.missing_credentials() == []

    def missing_credentials(self) -> list[str]:
        """返回缺失的必填密钥名（默认要求 ``api_base`` 与 ``api_key``）。"""
        required = ("api_base", "api_key")
        return [key for key in required if not (self.credentials.get(key) or "").strip()]

    def ensure_configured(self) -> None:
        """未配置则抛 ``VENDOR_UNAVAILABLE``。

        这是 ADR-07 的**唯一出口**：所有真实调用都先经过这里，
        因此不可能出现「密钥没配却返回成功」的情况。

        ``details`` 里固定带 ``ok=False``，让前端与自动化测试都能用一个
        布尔字段判断「这次是不是安全失败」，而不必去解析中文提示。
        """
        if self.is_configured:
            return
        missing = self.missing_credentials()
        raise AppException(
            ErrorCode.VENDOR_UNAVAILABLE,
            f"{self.name or self.code} 尚未接入（缺少 {'、'.join(missing) or '密钥'}），"
            "已按安全策略拒绝调用",
            details={
                "vendor": self.vendor or self.code,
                "ok": False,
                "missing": missing,
            },
        )

    def describe(self) -> ProviderDescriptor:
        """自描述信息。"""
        return ProviderDescriptor(
            code=self.code,
            name=self.name or self.code,
            kind=str(self.kind),
            vendor=self.vendor,
            capabilities=sorted(self.capabilities),
            configured=self.is_configured,
            requires_credentials=self.requires_credentials,
            description=self.description,
        )

    def supports(self, capability: str) -> bool:
        """是否支持某能力域。"""
        return capability in self.capabilities

    def __repr__(self) -> str:
        return f"<{type(self).__name__} code={self.code} configured={self.is_configured}>"

    # ---------------- 生命周期 ----------------

    @abstractmethod
    async def health_check(self) -> HealthCheckResult:
        """探测供应商是否可用。

        约定：**未配置密钥时必须返回 DOWN 且 ``ok=False``**，
        不允许为了「看起来正常」而返回 UP。
        """
        raise NotImplementedError

    # ---------------- 设备侧 ----------------

    async def provision_devices(
        self,
        *,
        product_code: str,
        count: int,
        metadata: dict[str, Any] | None = None,
    ) -> DeviceProvisionResult:
        """在厂商侧批量生成设备（出厂前）。"""
        raise NotImplementedError

    async def activate_device(
        self, *, device_id: str, sn: str, extra: dict[str, Any] | None = None
    ) -> DeviceActionResult:
        """激活设备（终端扫码激活 / 开机上线）。"""
        raise NotImplementedError

    async def deactivate_device(self, *, device_id: str, reason: str | None = None) -> DeviceActionResult:
        """停用设备（解绑 / 冻结 / 报废）。"""
        raise NotImplementedError

    async def push_ota(
        self, *, device_ids: Sequence[str], firmware_version: str, firmware_url: str
    ) -> DeviceActionResult:
        """推送 OTA 固件。

        注意：京东 JoyInside 方案**不支持平台侧 OTA**（仅端侧升级），
        其适配器会在 ``capabilities`` 中省略 ``device`` 之外的能力并在
        实现里直接拒绝，而不是假装成功。
        """
        raise NotImplementedError

    # ---------------- 对话侧 ----------------

    async def open_session(
        self,
        *,
        session_id: str,
        role_preset: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        """为一次对话建立厂商侧会话，返回厂商会话 ID。

        mock 引擎直接返回传入的 ``session_id``（幂等），
        真实厂商则返回其长连接 / 会话句柄。
        """
        raise NotImplementedError

    async def chat(
        self,
        *,
        session_id: str,
        message: ChatMessage,
        history: Sequence[ChatMessage] = (),
        role_preset: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> AsyncIterator[ChatChunk]:
        """流式对话。

        Yields:
            ``ChatChunk``，``is_final=True`` 的那块表示结束。
        """
        raise NotImplementedError
        yield ChatChunk(delta="", index=0, is_final=True)  # pragma: no cover

    async def close_session(self, *, session_id: str, reason: str | None = None) -> None:
        """关闭厂商侧会话并释放长连接。"""
        raise NotImplementedError

    # ---------------- 语音侧 ----------------

    async def asr(
        self,
        *,
        audio: bytes | None = None,
        audio_url: str | None = None,
        hint_text: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> AsrResult:
        """语音转文字。

        ``hint_text`` 为可选提示（mock 的 ``echo`` 模式据此复读，
        也为真实厂商的「热词 / 上下文偏置」预留）。
        """
        raise NotImplementedError

    async def tts(
        self,
        *,
        text: str,
        voice_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> TtsResult:
        """文字转语音。"""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# HTTP 适配器公共基类
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HttpRetryConfig:
    """HTTP 重试与超时参数。

    为什么把三者放一起？因为它们互相牵制：超时太长时重试会让请求方
    （玩具设备）等太久，超时太短时重试又会放大厂商侧压力。集中一处便于
    按厂商实际 SLA 调整（如火山 30s、集贤 10s，见 ``settings.*_TIMEOUT_SECONDS``）。
    """

    timeout_seconds: float = 10.0
    max_retries: int = 2
    backoff_seconds: float = 0.2
    #: 只对这些状态码重试；4xx 是「请求本身错了」，重试没有意义
    retry_statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504})


class BaseHttpProvider(AIProvider):
    """真实厂商适配器的公共基类。

    统一处理四件所有厂商都一样的事，让每个适配器只剩「字段映射」这一件差异：

    1. **安全失败**：所有出站调用先过 :meth:`AIProvider.ensure_configured`
    2. **超时与重试**：见 :class:`HttpRetryConfig`
    3. **签名**：:meth:`sign` 给出占位实现（各厂商算法不同，联调时替换）
    4. **健康探测**：未配置 → ``DOWN``；已配置但只验证了网络可达 → ``DEGRADED``

    第 4 条刻意不返回 ``UP``：网络可达 ≠ 密钥有效。宣称 ``UP`` 就等于
    替厂商做了它没做的承诺（ADR-07）。
    """

    #: 未显式传参时的默认值，子类按厂商 SLA 覆盖
    default_timeout: float = 10.0
    default_max_retries: int = 2
    #: 健康探测的超时上限（探测接口不应长时间占用请求线程）
    health_timeout: float = 5.0

    def __init__(
        self,
        *,
        credentials: dict[str, str] | None = None,
        retry: HttpRetryConfig | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        super().__init__(credentials=credentials)
        self.retry = retry or HttpRetryConfig(
            timeout_seconds=timeout if timeout is not None else self.default_timeout,
            max_retries=max_retries if max_retries is not None else self.default_max_retries,
        )

    # ---------------- 凭据与签名 ----------------

    @property
    def api_base(self) -> str:
        """厂商接口基址（去掉尾部斜杠，避免拼出 ``//``）。"""
        return (self.credentials.get("api_base") or "").strip().rstrip("/")

    def canonical_payload(self, params: dict[str, Any]) -> str:
        """把参数序列化为规范化字符串（按键名升序）。"""
        return "&".join(f"{key}={params[key]}" for key in sorted(params))

    def sign(self, params: dict[str, Any]) -> str:
        """对参数签名。

        **占位实现**：HMAC-SHA256(规范化参数, secret_key)。
        真实厂商各有算法（有的要求时间戳 + 随机串参与、有的用 RSA、
        有的把签名放在 Header 而非 Query），联调时按官方文档替换本方法，
        其余代码无需改动——签名被刻意收口在这一个方法里。
        """
        secret = self.credentials.get("secret_key", "")
        return hmac.new(
            secret.encode("utf-8"), self.canonical_payload(params).encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def signed_payload(self, params: dict[str, Any]) -> dict[str, Any]:
        """返回带签名的请求参数副本。"""
        payload = dict(params)
        payload["signature"] = self.sign(payload)
        return payload

    def build_headers(self) -> dict[str, str]:
        """构造厂商自定义请求头（子类覆盖）。"""
        return {}

    # ---------------- 出站调用 ----------------

    def _call_failed(self, message: str, *, detail: dict[str, Any] | None = None) -> AppException:
        """构造「厂商调用失败」异常。

        统一使用 ``VENDOR_UNAVAILABLE`` 错误码而非 ``INTERNAL_ERROR``：
        对前端与固件而言，两者都是「这个能力现在用不了，请稍后重试或降级」，
        合并成一个码能让降级逻辑只写一处。
        """
        merged: dict[str, Any] = {
            "vendor": self.vendor or self.code,
            "ok": False,
        }
        if detail:
            merged.update(detail)
        return AppException(ErrorCode.VENDOR_UNAVAILABLE, message, details=merged)

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        signed: bool = True,
    ) -> dict[str, Any]:
        """发起一次带重试的 JSON 请求。

        Args:
            method: HTTP 方法。
            path: 相对路径（会拼到 ``api_base`` 之后）。
            params: 查询参数；``signed=True`` 时会附加签名。
            json_body: JSON 请求体。
            signed: 是否附加签名。

        Returns:
            解析后的 JSON 对象（非对象响应会被包成 ``{"data": ...}``）。

        Raises:
            AppException: 未配置密钥，或重试耗尽后仍然失败。
        """
        self.ensure_configured()
        query = self.signed_payload(params or {}) if signed else dict(params or {})
        url = f"{self.api_base}{path}"
        headers = self.build_headers()
        last_error = "未知错误"

        for attempt in range(self.retry.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.retry.timeout_seconds) as client:
                    response = await client.request(
                        method, url, params=query, json=json_body, headers=headers
                    )
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retry.max_retries:
                    await asyncio.sleep(self.retry.backoff_seconds * (attempt + 1))
                    continue
                raise self._call_failed(
                    f"{self.name} 调用失败（已重试 {self.retry.max_retries} 次）：{last_error}"
                ) from exc

            if response.status_code in self.retry.retry_statuses and attempt < self.retry.max_retries:
                last_error = f"HTTP {response.status_code}"
                await asyncio.sleep(self.retry.backoff_seconds * (attempt + 1))
                continue

            if response.status_code >= 400:
                raise self._call_failed(
                    f"{self.name} 返回 HTTP {response.status_code}",
                    detail={"statusCode": response.status_code},
                )

            try:
                data = response.json()
            except ValueError as exc:
                raise self._call_failed(f"{self.name} 返回了非 JSON 响应") from exc
            if isinstance(data, dict):
                return data
            return {"data": data}

        raise self._call_failed(f"{self.name} 调用失败：{last_error}")

    # ---------------- 健康探测 ----------------

    async def health_check(self) -> HealthCheckResult:
        """默认健康探测实现。

        * 未配置 → ``DOWN``（``ok=False``），**且不发起任何网络请求**
        * 已配置 → 对 ``api_base`` 发一次 GET；能拿到响应即 ``DEGRADED``
          （网络可达，但密钥有效性尚未验证），连不上即 ``DOWN``
        """
        checked_at = utcnow()

        if not self.is_configured:
            missing = self.missing_credentials()
            return HealthCheckResult(
                status=str(ProviderHealth.DOWN),
                ok=False,
                message=(
                    f"{self.name or self.code} 尚未接入（缺少 {'、'.join(missing) or '密钥'}），"
                    "已按安全策略跳过真实探测"
                ),
                vendor=self.vendor or self.code,
                latency_ms=0,
                checked_at=checked_at,
                detail={"ok": False, "missing": missing, "called": False},
            )

        started = utcnow()
        try:
            async with httpx.AsyncClient(timeout=min(self.health_timeout, self.retry.timeout_seconds)) as client:
                response = await client.get(self.api_base)
        except httpx.HTTPError as exc:
            return HealthCheckResult(
                status=str(ProviderHealth.DOWN),
                ok=False,
                message=f"网络不可达：{type(exc).__name__}",
                vendor=self.vendor or self.code,
                latency_ms=int((utcnow() - started).total_seconds() * 1000),
                checked_at=checked_at,
                detail={"ok": False, "called": True},
            )

        latency = int((utcnow() - started).total_seconds() * 1000)
        reachable = response.status_code < 500
        return HealthCheckResult(
            status=str(ProviderHealth.DEGRADED if reachable else ProviderHealth.DOWN),
            ok=reachable,
            message=(
                f"网络可达（HTTP {response.status_code}），但密钥有效性需在真实业务调用时验证"
                if reachable
                else f"厂商返回 HTTP {response.status_code}"
            ),
            vendor=self.vendor or self.code,
            latency_ms=latency,
            checked_at=checked_at,
            detail={"ok": reachable, "called": True, "statusCode": response.status_code},
        )


def chunk_text(text: str, *, chunk_size: int = 24) -> list[str]:
    """把整段文本切成流式分块（真实适配器用）。

    与 mock 的分块相比 ``chunk_size`` 更大：真实链路受网络开销主导，
    块太小反而增加延迟；mock 用小分块是为了让前端逐字效果肉眼可见。
    """
    if not text:
        return []
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


# ---------------------------------------------------------------------------
# 助手
# ---------------------------------------------------------------------------


def provider_unavailable_error(provider: AIProvider, capability: str) -> AppException:
    """构造「该供应商不支持此能力」的错误。

    与「未配置密钥」区分开：前者是能力缺口（400 语义），
    后者是安全失败（503 语义）。混用会让前端无法给出正确提示。
    """
    return AppException(
        ErrorCode.VENDOR_UNAVAILABLE,
        f"{provider.name or provider.code} 不支持 {capability} 能力",
        details={"vendor": provider.vendor or provider.code, "ok": False, "capability": capability},
    )


__all__ = [
    "ALL_CAPABILITIES",
    "CAP_ASR",
    "CAP_DEVICE",
    "CAP_DIALOGUE",
    "CAP_TTS",
    "AIProvider",
    "AsrResult",
    "BaseHttpProvider",
    "ChatChunk",
    "ChatMessage",
    "DeviceActionResult",
    "DeviceProvisionResult",
    "HealthCheckResult",
    "HttpRetryConfig",
    "ProviderDescriptor",
    "TtsResult",
    "capabilities_for_kind",
    "chunk_text",
    "provider_unavailable_error",
]
