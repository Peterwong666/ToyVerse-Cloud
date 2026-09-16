"""离线模拟引擎：一个不需要任何密钥就能跑通全流程的 ``AIProvider``。

定位
----
它是 ADR-04 的落地载体，也是整个项目的「可运行基线」：

* 演示与验收：``AI_DEFAULT_PROVIDER=mock`` 时，``/ai/chat`` ``/ai/asr`` ``/ai/tts``
  以及设备侧方法全部可用，前端流式渲染、会话落库、安全过滤标记都能被验证；
* 基准对比：真实厂商接入后，同一套指标（首字延迟 / 端到端延迟 / 意图命中率）
  可以先在 mock 上取到基线值，再对比真实链路（见 docs/10）。

诚实的边界
----------
模拟结果一律带 ``simulated=True``，且 ``raw`` 里写明「这是离线模拟」。
把「模拟」与「真实回执」在数据结构上区分开，是为了避免演示数据日后
被误当成真实出货 / 真实调用记录——这与 ADR-07 的精神一致：
**可以没有真实结果，但不能假装有**。
"""

from __future__ import annotations

import asyncio
import zlib
from collections.abc import AsyncIterator, Sequence

from app.ai.base import (
    ALL_CAPABILITIES,
    AIProvider,
    AsrResult,
    ChatChunk,
    ChatMessage,
    DeviceActionResult,
    DeviceProvisionResult,
    HealthCheckResult,
    TtsResult,
)
from app.ai.mock.asr import MockAsrEngine
from app.ai.mock.dialogue import MockReply, compose, iter_chunks
from app.ai.mock.scenarios import ContentSnippet
from app.ai.mock.tts import MockTtsEngine
from app.core.config import settings
from app.db.base import utcnow
from app.models.enums import AiProviderKind, ProviderHealth

#: 供应商注册键（与 ``AI_DEFAULT_PROVIDER`` 的默认值一致）
MOCK_PROVIDER_CODE = "mock"


class MockProvider(AIProvider):
    """离线模拟供应商（规则引擎 + 静音音频）。"""

    code = MOCK_PROVIDER_CODE
    name = "离线模拟引擎"
    kind = AiProviderKind.LOCAL
    vendor = None
    #: mock 不需要任何密钥，因此恒为「已配置」——这是离线可跑通的前提
    requires_credentials = False
    capabilities = ALL_CAPABILITIES
    description = "无需密钥的规则对话 + 语音模拟引擎，用于演示、验收与基准对比"

    def __init__(
        self,
        *,
        credentials: dict[str, str] | None = None,
        seed: int | None = None,
        latency_ms: int | None = None,
        asr_mode: str | None = None,
        tts_mode: str | None = None,
    ) -> None:
        super().__init__(credentials=credentials)
        #: 固定随机种子：可复现性的来源（见 :meth:`seed_for`）
        self.seed = settings.MOCK_RANDOM_SEED if seed is None else seed
        #: 模拟延迟；测试里设为 0，演示环境保留少量延迟以体现流式效果
        self.latency_ms = settings.MOCK_LATENCY_MS if latency_ms is None else latency_ms
        self.asr_engine = MockAsrEngine(mode=asr_mode)
        self.tts_engine = MockTtsEngine(mode=tts_mode)

    # ---------------- 可复现性 ----------------

    def seed_for(self, text: str) -> int:
        """为一段文本派生稳定的随机种子。

        为什么不用 Python 内置 ``hash()``？因为字符串哈希默认带随机盐
        （``PYTHONHASHSEED``），跨进程结果会变，可复现性就没了。
        ``zlib.crc32`` 是纯函数，跨进程、跨平台稳定。
        """
        return (self.seed + zlib.crc32(text.encode("utf-8"))) % (2**31)

    def compose_reply(
        self,
        text: str,
        *,
        role_preset: str | None = None,
        knowledge: Sequence[ContentSnippet] = (),
    ) -> MockReply:
        """合成规则回复（纯函数，便于 API 层落库相同结果）。"""
        return compose(
            text,
            seed=self.seed_for(text),
            role_preset=role_preset,
            knowledge=tuple(knowledge),
        )

    # ---------------- 生命周期 ----------------

    async def health_check(self) -> HealthCheckResult:
        """模拟引擎恒为可用——这正是它能当「基线」的原因。"""
        return HealthCheckResult(
            status=str(ProviderHealth.UP),
            ok=True,
            message=f"离线模拟引擎就绪（ASR={self.asr_engine.mode}，TTS={self.tts_engine.mode}）",
            vendor=None,
            latency_ms=0,
            checked_at=utcnow(),
            detail={"simulated": True, "seed": self.seed},
        )

    # ---------------- 设备侧 ----------------

    async def provision_devices(
        self,
        *,
        product_code: str,
        count: int,
        metadata: dict[str, object] | None = None,
    ) -> DeviceProvisionResult:
        """生成确定性设备 ID：同一产品 + 同一序号必然得到同一 ID。"""
        prefix = (product_code or "MOCK").strip().upper()
        ids = [f"MOCK-{prefix}-{index:04d}" for index in range(1, max(0, count) + 1)]
        return DeviceProvisionResult(
            success=True,
            message=f"模拟生成 {len(ids)} 台设备（未调用任何厂商接口）",
            vendor_device_ids=ids,
            simulated=True,
            raw={"productCode": prefix, "count": len(ids), "metadata": metadata or {}},
        )

    async def activate_device(
        self, *, device_id: str, sn: str, extra: dict[str, object] | None = None
    ) -> DeviceActionResult:
        """模拟激活：直接成功，并在 ``raw`` 中标注模拟。"""
        return DeviceActionResult(
            success=True,
            message=f"模拟激活成功（设备 {device_id}）",
            device_id=device_id,
            activated=True,
            simulated=True,
            raw={"sn": sn, "extra": extra or {}, "note": "离线模拟：未调用厂商激活接口"},
        )

    async def deactivate_device(self, *, device_id: str, reason: str | None = None) -> DeviceActionResult:
        """模拟停用。"""
        return DeviceActionResult(
            success=True,
            message=f"模拟停用成功（设备 {device_id}）",
            device_id=device_id,
            activated=False,
            simulated=True,
            raw={"reason": reason, "note": "离线模拟：未调用厂商停用接口"},
        )

    async def push_ota(
        self, *, device_ids: Sequence[str], firmware_version: str, firmware_url: str
    ) -> DeviceActionResult:
        """模拟 OTA 推送。"""
        return DeviceActionResult(
            success=True,
            message=f"模拟推送固件 {firmware_version} 至 {len(device_ids)} 台设备",
            task_id=f"MOCK-OTA-{firmware_version}",
            simulated=True,
            raw={
                "deviceIds": list(device_ids),
                "firmwareUrl": firmware_url,
                "note": "离线模拟：未调用厂商 OTA 接口",
            },
        )

    # ---------------- 对话侧 ----------------

    async def open_session(
        self,
        *,
        session_id: str,
        role_preset: str | None = None,
        context: dict[str, object] | None = None,
    ) -> str:
        """mock 无服务端会话状态，直接回显会话 ID（幂等）。"""
        return session_id

    async def chat(
        self,
        *,
        session_id: str,
        message: ChatMessage,
        history: Sequence[ChatMessage] = (),
        role_preset: str | None = None,
        context: dict[str, object] | None = None,
    ) -> AsyncIterator[ChatChunk]:
        """流式对话：把规则回复切成多块 yield。

        ``history`` 与 ``context`` 在 mock 里只作为「知识库素材」的来源：
        ``context['knowledge']`` 可传入 ``list[ContentSnippet]``，
        从而验证「知识库关键词检索影响回复」这条链路。
        """
        knowledge = _extract_knowledge(context)
        reply = self.compose_reply(message.content, role_preset=role_preset, knowledge=knowledge)
        chunks = list(iter_chunks(reply.text))
        finish_reason = "safety" if reply.safety_flag else "stop"

        # 延迟按块均摊：既体现「首字延迟」，也让流式节奏接近真实链路
        per_chunk_seconds = (self.latency_ms / 1000 / max(1, len(chunks))) if chunks else 0.0

        for index, delta in enumerate(chunks):
            if per_chunk_seconds:
                await asyncio.sleep(per_chunk_seconds)
            is_final = index == len(chunks) - 1
            yield ChatChunk(
                delta=delta,
                index=index,
                is_final=is_final,
                latency_ms=int(per_chunk_seconds * 1000) if per_chunk_seconds else 0,
                finish_reason=finish_reason if is_final else None,
            )

    async def close_session(self, *, session_id: str, reason: str | None = None) -> None:
        """mock 无连接可释放，空实现。"""
        return None

    # ---------------- 语音侧 ----------------

    async def asr(
        self,
        *,
        audio: bytes | None = None,
        audio_url: str | None = None,
        hint_text: str | None = None,
        context: dict[str, object] | None = None,
    ) -> AsrResult:
        """委托 :class:`~app.ai.mock.asr.MockAsrEngine`。"""
        return self.asr_engine.transcribe(hint_text=hint_text, audio_size=len(audio or b""))

    async def tts(
        self,
        *,
        text: str,
        voice_id: str | None = None,
        context: dict[str, object] | None = None,
    ) -> TtsResult:
        """委托 :class:`~app.ai.mock.tts.MockTtsEngine`。"""
        return self.tts_engine.synthesize(text, voice_id=voice_id)


def _extract_knowledge(context: dict[str, object] | None) -> tuple[ContentSnippet, ...]:
    """从 ``context`` 中提取知识库素材（类型不安全，故在此收口并校验）。"""
    if not context:
        return ()
    raw = context.get("knowledge")
    if not isinstance(raw, list | tuple):
        return ()
    return tuple(item for item in raw if isinstance(item, ContentSnippet))


def build_mock_provider(
    *, seed: int | None = None, latency_ms: int | None = None
) -> MockProvider:
    """构造 mock 供应商（供注册表与测试使用）。"""
    return MockProvider(seed=seed, latency_ms=latency_ms)


__all__ = ["MOCK_PROVIDER_CODE", "MockProvider", "build_mock_provider"]
