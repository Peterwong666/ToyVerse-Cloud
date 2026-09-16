"""单元测试：供应商注册表、解析顺序与 mock 引擎的可复现性。

这些都是**纯逻辑**验证，不依赖数据库与 HTTP：

* 解析顺序的四层优先级（``client_product → template.vendor → provider_default
  → platform_default``）各拍一个用例，并用带随机性的 mock 引擎验证
  「同种子必然同结果」——可复现是离线引擎能当 benchmark 的前提。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.ai import registry, volcano
from app.ai.base import (
    ALL_CAPABILITIES,
    CAP_ASR,
    CAP_DEVICE,
    CAP_DIALOGUE,
    CAP_TTS,
    AIProvider,
    HealthCheckResult,
    capabilities_for_kind,
)
from app.ai.mock import build_mock_provider
from app.ai.mock.dialogue import iter_chunks
from app.ai.mock.tts import MockTtsEngine
from app.core.errors import AppException, ErrorCode
from app.models.enums import AiProviderKind, ProviderHealth

pytestmark = pytest.mark.unit


class FakeProvider(AIProvider):
    """测试替身：只需要能被注册与解析，不需要真实能力。"""

    kind = AiProviderKind.MODEL
    requires_credentials = False
    capabilities = frozenset({CAP_DIALOGUE})

    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code
        self.name = f"替身-{code}"

    async def health_check(self) -> HealthCheckResult:
        return HealthCheckResult(status=str(ProviderHealth.UP), ok=True, message="ok")


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """每个用例前后都清空注册表，避免全局状态串台。"""
    registry.reset_registry()
    yield
    registry.reset_registry()


# ---------------------------------------------------------------------------
# 注册表基础行为
# ---------------------------------------------------------------------------


def test_register_and_lookup() -> None:
    """注册后可按键取回，``available_providers`` 输出稳定排序。"""
    registry.register(FakeProvider("beta"))
    registry.register(FakeProvider("alpha"))

    assert registry.get("alpha") is not None
    assert registry.get("不存在") is None
    assert registry.get(None) is None
    assert registry.provider_codes() == ["alpha", "beta"]


def test_register_rejects_empty_code() -> None:
    """没有 code 的供应商无法注册——否则解析时无法被寻址。"""
    with pytest.raises(ValueError, match="code"):
        registry.register(FakeProvider(""))


def test_register_replace_flag() -> None:
    """``replace=False`` 时重复注册报错，默认允许覆盖（应用重启场景）。"""
    registry.register(FakeProvider("alpha"))
    registry.register(FakeProvider("alpha"))

    with pytest.raises(ValueError, match="已注册"):
        registry.register(FakeProvider("alpha"), replace=False)


def test_require_unknown_code_raises_vendor_unavailable() -> None:
    """取不存在的供应商必须安全失败，且 ``details.ok=False`` 可被前端断言。"""
    with pytest.raises(AppException) as excinfo:
        registry.require("nope")

    assert excinfo.value.code == ErrorCode.VENDOR_UNAVAILABLE
    assert excinfo.value.details is not None
    assert excinfo.value.details["ok"] is False
    assert excinfo.value.status_code == 503


# ---------------------------------------------------------------------------
# 解析顺序（四层优先级）
# ---------------------------------------------------------------------------


def test_request_code_wins_over_everything() -> None:
    """① 请求显式指定优先级最高（调试台 / 运维临时切换）。"""
    for code in ("alpha", "beta", "gamma", "delta"):
        registry.register(FakeProvider(code))

    resolved = registry.resolve_provider(
        request_code="alpha",
        client_product_code="beta",
        vendor="GAMMA",
        provider_default_code="delta",
        platform_default_code="mock",
    )

    assert resolved is not None
    assert resolved.provider.code == "alpha"
    assert resolved.layer == registry.LAYER_REQUEST


def test_client_product_wins_over_template_vendor() -> None:
    """② 产品级 AI 配置优先于模板绑定的云厂商。"""
    registry.register(FakeProvider("beta"))
    registry.register(FakeProvider("gamma"))

    resolved = registry.resolve_provider(
        client_product_code="beta",
        vendor="JOYINSIDE",
        provider_default_code="gamma",
        platform_default_code="mock",
    )

    assert resolved is not None
    assert resolved.provider.code == "beta"
    assert resolved.layer == registry.LAYER_CLIENT_PRODUCT


def test_template_vendor_wins_over_defaults() -> None:
    """③ 模板厂商优先于两个默认层（但低于产品级覆盖）。"""
    registry.register(FakeProvider("volcano"))
    registry.register(FakeProvider("mock"))

    resolved = registry.resolve_provider(
        vendor="VOLCANO",
        provider_default_code="mock",
        platform_default_code="mock",
    )

    assert resolved is not None
    assert resolved.provider.code == "volcano"
    assert resolved.layer == registry.LAYER_TEMPLATE_VENDOR


def test_provider_default_wins_over_platform_default() -> None:
    """④ 平台配置的默认供应商优先于 ``AI_DEFAULT_PROVIDER`` 兜底。"""
    registry.register(FakeProvider("gamma"))
    registry.register(FakeProvider("mock"))

    resolved = registry.resolve_provider(
        provider_default_code="gamma", platform_default_code="mock"
    )

    assert resolved is not None
    assert resolved.provider.code == "gamma"
    assert resolved.layer == registry.LAYER_PROVIDER_DEFAULT


def test_platform_default_is_the_fallback() -> None:
    """⑤ 前三层皆空时落到 ``AI_DEFAULT_PROVIDER``。"""
    registry.register(FakeProvider("mock"))

    resolved = registry.resolve_provider(platform_default_code="mock")

    assert resolved is not None
    assert resolved.provider.code == "mock"
    assert resolved.layer == registry.LAYER_PLATFORM_DEFAULT


def test_unregistered_code_falls_through_instead_of_failing() -> None:
    """未注册的候选值**不应**直接失败，而要继续下探。

    为什么要这样设计：把租户配置里的 ``provider_code`` 拼错一个字母，
    不应该让该产品的全部对话立刻挂掉——应继续用平台默认供应商，
    同时把拼错的值记录在 ``candidates`` 里供排查。
    """
    registry.register(FakeProvider("mock"))

    resolved = registry.resolve_provider(
        request_code="vo1cano",  # 手误
        client_product_code="also-missing",
        platform_default_code="mock",
    )

    assert resolved is not None
    assert resolved.provider.code == "mock"
    assert resolved.layer == registry.LAYER_PLATFORM_DEFAULT
    assert resolved.candidates == ["vo1cano", "also-missing", "mock"]


def test_all_layers_miss_returns_none() -> None:
    """四层全部未命中已注册供应商时返回 ``None``，由调用方决定如何失败。"""
    assert registry.resolve_provider(request_code="a", platform_default_code="b") is None


def test_vendor_code_mapping() -> None:
    """厂商 → 注册键的映射要显式，避免变成隐式约定。"""
    assert registry.provider_code_for_vendor("VOLCANO") == "volcano"
    assert registry.provider_code_for_vendor(" joyinside ") == "joyinside"
    assert registry.provider_code_for_vendor("BAIDU") is None
    assert registry.provider_code_for_vendor(None) is None


# ---------------------------------------------------------------------------
# 能力声明与安全失败
# ---------------------------------------------------------------------------


def test_capabilities_for_kind() -> None:
    """设备云只管设备；模型类只管对话与语音；本地引擎四类全有。"""
    assert capabilities_for_kind(AiProviderKind.DEVICE_CLOUD) == frozenset({CAP_DEVICE})
    assert capabilities_for_kind(AiProviderKind.MODEL) == frozenset({CAP_DIALOGUE, CAP_ASR, CAP_TTS})
    assert capabilities_for_kind(AiProviderKind.LOCAL) == ALL_CAPABILITIES


def test_default_registry_contains_mock_and_four_vendors() -> None:
    """默认注册表包含 mock 与四个真实厂商骨架，且 mock 恒为已配置。"""
    codes = registry.build_default_registry(force=True)

    assert codes == ["baidu", "jixian", "joyinside", "mock", "volcano"]
    assert registry.require("mock").is_configured is True
    # 未接入的真实厂商必须报出缺失的密钥名，而不是静默「可用」
    assert registry.require("volcano").is_configured is False
    assert registry.require("volcano").missing_credentials() == ["access_key", "secret_key"]


def test_ensure_configured_raises_with_ok_false() -> None:
    """未接入厂商的统一安全失败出口：``VENDOR_UNAVAILABLE`` + ``ok=False``。"""
    registry.build_default_registry(force=True)
    provider = registry.require("volcano")

    with pytest.raises(AppException) as excinfo:
        provider.ensure_configured()

    assert excinfo.value.code == ErrorCode.VENDOR_UNAVAILABLE
    assert excinfo.value.details is not None
    assert excinfo.value.details["ok"] is False
    assert excinfo.value.details["vendor"] == "VOLCANO"


def test_describe_providers_shape() -> None:
    """自描述信息要能让前端区分「没实现」与「没配置」。"""
    registry.build_default_registry(force=True)
    described = {item.code: item for item in registry.describe_providers()}

    assert described["mock"].configured is True
    assert described["mock"].requires_credentials is False
    assert described["volcano"].configured is False
    assert sorted(described["volcano"].capabilities) == ["asr", "device", "dialogue", "tts"]


# ---------------------------------------------------------------------------
# mock 引擎：可复现性
# ---------------------------------------------------------------------------


def test_mock_reply_is_reproducible_for_same_seed() -> None:
    """同种子 + 同输入 → 同结果（两个独立实例也必须一致）。"""
    first = build_mock_provider(seed=1234, latency_ms=0).compose_reply("我想听故事")
    second = build_mock_provider(seed=1234, latency_ms=0).compose_reply("我想听故事")

    assert first.text == second.text
    assert first.intent == second.intent
    assert first.snippet_title == second.snippet_title


def test_mock_seed_for_is_text_sensitive_and_stable() -> None:
    """种子派生必须是纯函数（跨进程稳定），且不同文本得到不同种子。

    这条断言防的是「用内置 ``hash()`` 派生种子」这个坑：
    字符串哈希默认带随机盐，跨进程会变，可复现性会静默失效。
    """
    provider = build_mock_provider(seed=1000, latency_ms=0)

    assert provider.seed_for("故事") == build_mock_provider(seed=1000).seed_for("故事")
    assert provider.seed_for("故事") != provider.seed_for("儿歌")


def test_mock_stream_is_deterministic_and_multi_chunk() -> None:
    """流式分块必须**真的分批**（否则前端逐字渲染无法验证），且可复现。"""
    text = "姐姐轻声说：好呀，我给你讲《小狐狸的月亮灯》。从前有只小狐狸，住在山脚下的树洞里。"
    chunks = list(iter_chunks(text))

    assert len(chunks) > 1
    assert "".join(chunks) == text
    assert list(iter_chunks(text)) == chunks


def test_iter_chunks_handles_short_and_empty_text() -> None:
    """边界：空文本不产出分块；短文本只产出一块。"""
    assert list(iter_chunks("")) == []
    assert list(iter_chunks("你好")) == ["你好"]


def test_mock_intent_rules_are_explainable() -> None:
    """规则对话的命中必须可解释：意图与命中的关键词都要回传。"""
    provider = build_mock_provider(seed=20260916, latency_ms=0)

    story = provider.compose_reply("给我讲个故事吧")
    song = provider.compose_reply("唱首歌吧")
    weather = provider.compose_reply("今天天气怎么样")
    safety = provider.compose_reply("我们聊点暴力的话题")

    assert story.intent == "story"
    assert story.snippet_title is not None
    assert song.intent == "song"
    assert weather.intent == "weather"
    assert safety.safety_flag == "暴力"


def test_mock_knowledge_influences_reply() -> None:
    """知识库命中要真的影响回复（引用素材标题），而不是只走兜底话术。"""
    from app.ai.mock.scenarios import ContentSnippet

    provider = build_mock_provider(seed=7, latency_ms=0)
    knowledge = (
        ContentSnippet(
            title="宇航员小象",
            keywords=("月亮", "太空"),
            body="小象坐上火箭去了太空。",
            kind="story",
        ),
    )

    reply = provider.compose_reply("月亮上有什么，为什么呢", knowledge=knowledge)

    assert reply.snippet_title == "宇航员小象"
    assert "宇航员小象" in reply.text


def test_mock_tts_wav_mode_produces_valid_riff_header() -> None:
    """``wav`` 模式产出结构合法的 WAV（前端与固件才能真的走播放链路）。"""
    result = MockTtsEngine(mode="wav").synthesize("你好呀小玩具")

    assert result.audio is not None
    assert result.audio[:4] == b"RIFF"
    assert result.content_type == "audio/wav"
    assert result.duration_ms is not None and result.duration_ms > 0
    # 同一文本必然得到同样的字节数（可复现）
    assert result.audio == MockTtsEngine(mode="wav").synthesize("你好呀小玩具").audio


def test_mock_tts_text_mode_is_honest() -> None:
    """``text`` 模式必须诚实返回「无音频」，而不是塞一段静音假音频。"""
    result = MockTtsEngine(mode="text").synthesize("你好")

    assert result.audio is None
    assert result.simulated is True


# ---------------------------------------------------------------------------
# 火山引擎适配器：按官方文档核实的调用形态
# ---------------------------------------------------------------------------


def test_volcano_call_shape_matches_official_docs() -> None:
    """基址、版本与 Query 形态必须与官方一致（Action + Version）。

    这条断言防的是「基址或版本被随手改掉」——那会让所有 Action 静默 404，
    而 404 在联调时最容易被误判成「密钥没配好」。
    """
    provider = volcano.VolcanoProvider(credentials={})

    assert volcano.DEFAULT_API_BASE == "https://rtc.volcengineapi.com"
    assert volcano.API_VERSION == "2025-08-01"
    assert provider.api_base == volcano.DEFAULT_API_BASE
    # 所有 Action 共用一个地址，仅靠 Query 区分
    assert volcano.API_PATH == "/"
    assert provider._action_params(volcano.ACTION_AIBOT_CREATE) == {
        "Action": "AibotCreate",
        "Version": "2025-08-01",
    }


def test_volcano_api_base_fallback_keeps_adr07_semantics() -> None:
    """回落基址**不能**把「未接入」变成「已接入」（ADR-07 不能被削弱）。

    官方基址是固定值，所以缺基址不算缺配置；但缺密钥仍然必须视为未接入。
    """
    provider = volcano.VolcanoProvider(credentials={})

    assert provider.api_base == volcano.DEFAULT_API_BASE
    assert provider.is_configured is False
    assert provider.missing_credentials() == ["access_key", "secret_key"]


def test_volcano_create_body_follows_verified_structure() -> None:
    """``AibotCreate`` 的 ``Name`` / ``AccessType`` / ``Config.ASRConfig`` 层级已核实。"""
    body = volcano.VolcanoProvider.build_create_body(
        name="小狐狸助手",
        system_prompt="你是温柔的睡前故事姐姐",
        greeting="你好呀，我们来讲故事吧",
    )

    assert body["Name"] == "小狐狸助手"
    assert body["AccessType"] == volcano.ACCESS_TYPE_PRIVATE

    config = body["Config"]
    # ASRConfig.Provider 的取值官方规定固定为 volcano
    assert config["ASRConfig"]["Provider"] == volcano.ASR_PROVIDER_VOLCANO == "volcano"
    assert (
        config["ASRConfig"]["ProviderParams"]["ApiResourceId"]
        == "volc.bigasr.sauc.duration"
    )
    # TTS 模型取官方示例值（字符版）
    assert config["TTSConfig"]["ProviderParams"]["ApiResourceId"] == "volc.service_type.10029"

    # 角色预设通过 LLMConfig.UserPrompts 的 system 提示承载
    prompts = config["LLMConfig"]["UserPrompts"]
    assert prompts[0]["Role"] == "system"
    assert "你是温柔的睡前故事姐姐" in prompts[0]["Content"]
    assert "你好呀，我们来讲故事吧" in prompts[0]["Content"]


def test_volcano_create_body_omits_empty_llm_model() -> None:
    """未选模型时**不提交** ``ModelName``，而不是塞空串。

    提交空字符串会让厂商校验报「参数非法」，反而掩盖了「还没选模型」这个真实原因。
    """
    body = volcano.VolcanoProvider.build_create_body(name="x")

    assert "ModelName" not in body["Config"]["LLMConfig"]


def test_volcano_default_access_type_is_private() -> None:
    """控制台只能建平台智能体，多租户人设必须走后端 ``private``。

    这是 P9「每个商户 / 每台设备专属人设」的唯一实现路径，默认值一旦被改成
    ``public``，那条业务能力就会静默失效，故在此钉住。
    """
    default_provider = volcano.VolcanoProvider(credentials={})
    assert default_provider.access_type == volcano.ACCESS_TYPE_PRIVATE
    assert volcano.VolcanoProvider.build_create_body(name="x")["AccessType"] == "private"

    # 平台级人设仍需可显式建为 public
    public_body = volcano.VolcanoProvider.build_create_body(
        name="x", access_type=volcano.ACCESS_TYPE_PUBLIC
    )
    assert public_body["AccessType"] == "public"


async def test_volcano_rejects_ota_instead_of_faking_success() -> None:
    """火山没有平台侧 OTA：必须明确拒绝，不能返回成功。"""
    provider = volcano.VolcanoProvider(credentials={})

    with pytest.raises(AppException) as excinfo:
        await provider.push_ota(
            device_ids=["d-1"], firmware_version="1.2.0", firmware_url="https://example.com/fw.bin"
        )

    assert excinfo.value.code == ErrorCode.VENDOR_UNAVAILABLE
    assert excinfo.value.details is not None
    assert excinfo.value.details["reason"] == "OTA_NOT_SUPPORTED"
    assert excinfo.value.details["ok"] is False


async def test_volcano_bind_requires_a_target() -> None:
    """``AibotBind`` 至少要给 ProductId 或 DeviceId，否则是调用方错误。"""
    provider = volcano.VolcanoProvider(credentials={"access_key": "ak", "secret_key": "sk"})

    with pytest.raises(AppException) as excinfo:
        await provider.aibot_bind(aibot_id="ab-1")

    assert excinfo.value.details is not None
    assert excinfo.value.details["reason"] == "MISSING_TARGET"

