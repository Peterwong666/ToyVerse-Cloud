"""ai_config_service 单元测试。

覆盖口径（对应 T3 覆盖率目标）：
- 纯函数：allowed_kb_extensions / file_extension / split_text_blocks / _first_line。
- 产品与配置查询：get_product / get_ai_config / 视图组装 build_ai_config_view。
- 供应商解析：_provider_code_or_fail / _assert_provider_available 的 409 分支。
- 配置写入：update_ai_config（创建/部分更新/显式清空/Wi-Fi 角色限制/启用校验与审计）、
  update_prompt / update_role / update_voice / update_safety。
- 选项清单：list_providers / list_role_presets / list_voice_profiles 的可见性与排序。
- 知识库：CRUD、文件上传（扩展名/大小/checksum 重复）、解析状态机、存储对象清理、审计。
- 内容库：scope 过滤、类型/关键字过滤、排序与分页。
- 运营视图：build_product_ops_detail / list_products_ops 的批量计数与摘要。

外部依赖全部 hermetic：供应商走内存注册表替身，文件存储走 tmp_path LocalStorage，
不需要任何真实 API Key 或网络访问。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.base import CAP_DIALOGUE, AIProvider, HealthCheckResult
from app.ai.registry import register, reset_registry
from app.core.config import settings
from app.core.deps import AuthContext
from app.core.errors import AppException, ErrorCode
from app.core.ids import new_id
from app.core.storage import LocalStorage, sha256_hex
from app.db.base import utcnow
from app.models.ai import (
    AiConfig,
    AiProvider as AiProviderRow,
    DialogueMessage,
    DialogueSession,
    KbFile,
    KnowledgeBase,
    RolePreset,
    VoiceProfile,
)
from app.models.audit import AuditLog
from app.models.catalog import ClientProduct, ProductTemplate
from app.models.device import Device
from app.models.enums import (
    ActivationStatus,
    AiProviderKind,
    AiProviderStatus,
    AssetStatus,
    ContentItemType,
    DialogueSessionStatus,
    EnableStatus,
    KbFileStatus,
    MessageContentType,
    MessageRole,
    NetworkType,
    ProviderHealth,
    RoleType,
)
from app.models.identity import Tenant
from app.models.ops import ContentItem
from app.schemas.ops import (
    AiConfigUpdateRequest,
    AiPromptUpdateRequest,
    AiRoleUpdateRequest,
    AiSafetyUpdateRequest,
    AiVoiceUpdateRequest,
)
from app.services import ai_config_service

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """每个用例前后清空供应商注册表，避免用例间互相污染。"""
    reset_registry()
    yield
    reset_registry()


@pytest.fixture(autouse=True)
def _tmp_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """把全局存储钉到用例临时目录，避免污染真实 STORAGE_LOCAL_ROOT。"""
    monkeypatch.setattr(ai_config_service, "get_storage", lambda: LocalStorage(root=tmp_path))
    return tmp_path


class _FakeModelProvider(AIProvider):
    """确定性替身：configured=False 时 is_configured 必然为 False。"""

    kind = AiProviderKind.MODEL
    vendor = None
    capabilities = frozenset({CAP_DIALOGUE})
    description = "测试替身供应商"

    def __init__(self, code: str = "fake-model", *, configured: bool = True) -> None:
        super().__init__()
        self.code = code
        self.name = f"替身-{code}"
        self.requires_credentials = not configured

    async def health_check(self) -> HealthCheckResult:
        if self.is_configured:
            return HealthCheckResult(status=str(ProviderHealth.UP), ok=True, message="ok")
        return HealthCheckResult(status=str(ProviderHealth.DOWN), ok=False, message="未配置凭证")


def _platform_auth() -> AuthContext:
    return AuthContext(
        user_id="u-platform",
        account="admin",
        role_code="PLATFORM_ADMIN",
        role_type=RoleType.PLATFORM,
    )


def _merchant_auth(tenant: Tenant) -> AuthContext:
    return AuthContext(
        user_id="u-merchant",
        account="13812345679",
        role_code="MERCHANT_ADMIN",
        role_type=RoleType.MERCHANT,
        tenant_id=tenant.id,
        tenant_code=tenant.code,
    )


async def _make_tenant(db: AsyncSession, *, code: str = "T001") -> Tenant:
    tenant = Tenant(id=new_id("tenant"), code=code, name=f"租户{code}", status="ACTIVE")
    db.add(tenant)
    await db.flush()
    return tenant


async def _make_product(
    db: AsyncSession,
    tenant: Tenant,
    *,
    code: str = "prod-1",
    name: str = "故事机",
    network_type: NetworkType = NetworkType.FOUR_G,
) -> ClientProduct:
    template = ProductTemplate(
        id=new_id("product_template"),
        code=f"tpl-{code}-{network_type.value}",
        name=f"{network_type.value} 模板",
        network_type=str(network_type),
    )
    db.add(template)
    product = ClientProduct(
        id=new_id("client_product"),
        tenant_id=tenant.id,
        template_id=template.id,
        code=code,
        name=name,
        network_type=template.network_type,
        cloud_provider_id=None,
    )
    db.add(product)
    await db.flush()
    return product


async def _make_device(
    db: AsyncSession,
    product: ClientProduct,
    *,
    activation_status: ActivationStatus = ActivationStatus.ACTIVATED,
    asset_status: AssetStatus = AssetStatus.GENERATED,
) -> Device:
    device = Device(
        id=new_id("device"),
        tenant_id=product.tenant_id,
        client_product_id=product.id,
        sn=f"SN-{new_id('device')}",
        network_type=product.network_type,
        activation_status=str(activation_status),
        asset_status=str(asset_status),
    )
    db.add(device)
    await db.flush()
    return device


async def _make_dialogue(
    db: AsyncSession,
    product: ClientProduct,
    *,
    user_messages: int = 1,
    assistant_messages: int = 0,
) -> DialogueSession:
    session = DialogueSession(
        id=new_id("dialogue_session"),
        tenant_id=product.tenant_id,
        client_product_id=product.id,
        provider_code="mock",
        status=str(DialogueSessionStatus.ACTIVE),
    )
    db.add(session)
    await db.flush()
    seq = 1
    for _ in range(user_messages):
        db.add(
            DialogueMessage(
                id=new_id("dialogue_message"),
                session_id=session.id,
                tenant_id=product.tenant_id,
                seq=seq,
                role=str(MessageRole.USER),
                content_type=str(MessageContentType.TEXT),
                content="你好",
            )
        )
        seq += 1
    for _ in range(assistant_messages):
        db.add(
            DialogueMessage(
                id=new_id("dialogue_message"),
                session_id=session.id,
                tenant_id=product.tenant_id,
                seq=seq,
                role=str(MessageRole.ASSISTANT),
                content_type=str(MessageContentType.TEXT),
                content="你好呀",
            )
        )
        seq += 1
    await db.flush()
    return session


async def _make_role_preset(
    db: AsyncSession,
    *,
    code: str,
    tenant_id: str | None = None,
    is_builtin: bool = True,
    status: EnableStatus = EnableStatus.ENABLED,
) -> RolePreset:
    preset = RolePreset(
        id=new_id("role_preset"),
        tenant_id=tenant_id,
        code=code,
        name=f"角色-{code}",
        persona="耐心",
        system_prompt="你是玩具伙伴",
        greeting="你好",
        age_group="3-6",
        tone="温柔",
        keywords=[],
        is_builtin=is_builtin,
        status=str(status),
    )
    db.add(preset)
    await db.flush()
    return preset


async def _make_kb(
    db: AsyncSession,
    tenant: Tenant,
    *,
    name: str = "安全知识库",
    client_product_id: str | None = None,
    description: str | None = "默认知识库",
) -> KnowledgeBase:
    kb = KnowledgeBase(
        id=new_id("knowledge_base"),
        tenant_id=tenant.id,
        client_product_id=client_product_id,
        name=name,
        description=description,
        status=str(EnableStatus.ENABLED),
    )
    db.add(kb)
    await db.flush()
    return kb


async def _make_kb_file(
    db: AsyncSession,
    kb: KnowledgeBase,
    *,
    filename: str = "faq.txt",
    content: bytes = b"block one\n\nblock two",
    status: KbFileStatus = KbFileStatus.PENDING,
    storage_path: str | None = None,
    chunk_count: int = 0,
    created_at: Any = None,
) -> KbFile:
    file_id = new_id("kb_file")
    kb_file = KbFile(
        id=file_id,
        knowledge_base_id=kb.id,
        tenant_id=kb.tenant_id,
        filename=filename,
        content_type="text/plain",
        size_bytes=len(content),
        storage_path=storage_path if storage_path is not None else f"kb/{kb.tenant_id}/{file_id}.txt",
        checksum=sha256_hex(content),
        status=str(status),
        chunk_count=chunk_count,
    )
    if created_at is not None:
        kb_file.created_at = created_at
    db.add(kb_file)
    await db.flush()
    return kb_file


def _write_storage(tmp_path: Path, kb_file: KbFile, content: bytes) -> None:
    target = tmp_path / (kb_file.storage_path or "")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


async def _make_voice(
    db: AsyncSession,
    tenant: Tenant,
    *,
    name: str = "温柔姐姐",
    client_product_id: str | None = None,
) -> VoiceProfile:
    voice = VoiceProfile(
        id=new_id("voice_profile"),
        tenant_id=tenant.id,
        client_product_id=client_product_id,
        name=name,
        provider_code="mock",
        external_voice_id="v-1",
        status=str(EnableStatus.ENABLED),
    )
    db.add(voice)
    await db.flush()
    return voice


async def _make_ai_config(
    db: AsyncSession,
    tenant: Tenant,
    product: ClientProduct,
    **overrides: Any,
) -> AiConfig:
    config = AiConfig(
        id=new_id("ai_config"),
        tenant_id=tenant.id,
        client_product_id=product.id,
        **overrides,
    )
    db.add(config)
    await db.flush()
    return config


async def _make_content(
    db: AsyncSession,
    *,
    title: str,
    tenant_id: str | None = None,
    item_type: ContentItemType = ContentItemType.STORY,
    description: str | None = None,
    sort_order: int = 0,
) -> ContentItem:
    item = ContentItem(
        id=new_id("content_item"),
        tenant_id=tenant_id,
        type=str(item_type),
        title=title,
        description=description,
        status=str(EnableStatus.ENABLED),
        sort_order=sort_order,
    )
    db.add(item)
    await db.flush()
    return item


async def _audit_rows(db: AsyncSession, action: str) -> list[AuditLog]:
    result = await db.execute(select(AuditLog).where(AuditLog.action == action))
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_allowed_kb_extensions() -> None:
    exts = ai_config_service.allowed_kb_extensions()
    assert {"txt", "md", "csv", "json", "pdf", "docx"} == exts


def test_allowed_kb_extensions_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "KB_ALLOWED_EXTENSIONS", " TXT , .PDF,,MD ")
    assert ai_config_service.allowed_kb_extensions() == {"txt", "pdf", "md"}


def test_file_extension() -> None:
    assert ai_config_service.file_extension("readme") == ""
    assert ai_config_service.file_extension("FAQ.MD") == "md"
    assert ai_config_service.file_extension("a.b.CSV") == "csv"


def test_split_text_blocks() -> None:
    text = "第一段\n\n\n  \n第二段\n继续\n\n第三段"
    assert ai_config_service.split_text_blocks(text) == ["第一段", "第二段\n继续", "第三段"]


def test_split_text_blocks_2() -> None:
    assert ai_config_service.split_text_blocks("甲\n乙\n丙") == ["甲", "乙", "丙"]


def test_split_text_blocks_3() -> None:
    assert ai_config_service.split_text_blocks("只有一行") == ["只有一行"]
    assert ai_config_service.split_text_blocks("   \n  ") == []


def test_first_line() -> None:
    assert ai_config_service._first_line("  标题行 \n第二行") == "标题行"
    assert ai_config_service._first_line("\n\n   ", limit=5) == ""
    long_block = "\n\n" + "长" * 50
    assert ai_config_service._first_line(long_block, limit=10) == "长" * 10


# ---------------------------------------------------------------------------
# 产品 / 配置查询
# ---------------------------------------------------------------------------


async def test_get_product_404(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    with pytest.raises(AppException) as excinfo:
        await ai_config_service.get_product(db, _merchant_auth(tenant), new_id("client_product"))
    assert excinfo.value.code == ErrorCode.RESOURCE_NOT_FOUND
    assert excinfo.value.status_code == 404
    assert excinfo.value.message == "客户产品不存在"


async def test_get_product(db: AsyncSession) -> None:
    tenant_a = await _make_tenant(db, code="T001")
    tenant_b = await _make_tenant(db, code="T002")
    product = await _make_product(db, tenant_a)
    with pytest.raises(AppException) as excinfo:
        await ai_config_service.get_product(db, _merchant_auth(tenant_b), product.id)
    assert excinfo.value.code == ErrorCode.RESOURCE_NOT_FOUND


async def test_get_product_2(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    found = await ai_config_service.get_product(db, _platform_auth(), product.id)
    assert found.id == product.id


async def test_get_ai_config_none(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    assert await ai_config_service.get_ai_config(db, product.id) is None


# ---------------------------------------------------------------------------
# 供应商解析私有函数
# ---------------------------------------------------------------------------


def test_provider_code_or_fail() -> None:
    with pytest.raises(AppException) as excinfo:
        ai_config_service._provider_code_or_fail("ghost-provider")
    assert excinfo.value.code == ErrorCode.PRODUCT_NOT_AUTHORIZED
    assert excinfo.value.status_code == 409
    assert excinfo.value.details == {
        "providerCode": "ghost-provider",
        "reason": "PROVIDER_NOT_REGISTERED",
        "ok": False,
    }


def test_provider_code_or_fail_2() -> None:
    register(_FakeModelProvider("fake-unconfigured", configured=False))
    with pytest.raises(AppException) as excinfo:
        ai_config_service._provider_code_or_fail("fake-unconfigured")
    assert excinfo.value.details == {
        "providerCode": "fake-unconfigured",
        "configured": False,
        "ok": False,
    }


def test_provider_code_or_fail_3() -> None:
    register(_FakeModelProvider("fake-ok"))
    ai_config_service._provider_code_or_fail("fake-ok")


async def test_assert_provider_available(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    monkeypatch.setattr(settings, "AI_DEFAULT_PROVIDER", "ghost")
    with pytest.raises(AppException) as excinfo:
        await ai_config_service._assert_provider_available(db, product)
    assert excinfo.value.code == ErrorCode.PRODUCT_NOT_AUTHORIZED
    assert excinfo.value.details == {"reason": "NO_PROVIDER_RESOLVED", "ok": False}


async def test_assert_provider_available_2(db: AsyncSession) -> None:
    register(_FakeModelProvider("fake-down", configured=False))
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    await _make_ai_config(db, tenant, product, provider_code="fake-down")
    with pytest.raises(AppException) as excinfo:
        await ai_config_service._assert_provider_available(db, product)
    assert excinfo.value.details == {
        "providerCode": "fake-down",
        "configured": False,
        "ok": False,
    }


async def test_assert_provider_available_3(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    # 默认注册表中 mock 供应商 configured=True（conftest 把 AI_DEFAULT_PROVIDER 钉为 mock）
    assert await ai_config_service._assert_provider_available(db, product) == "mock"


# ---------------------------------------------------------------------------
# 配置视图
# ---------------------------------------------------------------------------


async def test_build_ai_config_view(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    view = await ai_config_service.build_ai_config_view(db, product, None)
    assert view.status == str(EnableStatus.DISABLED)
    assert view.provider_code == "mock"
    assert view.provider_health == "UP"
    assert view.provider_configured is True
    assert view.role_supported is True
    assert view.role_unsupported_reason is None
    assert view.safety.enabled is True and view.safety.sensitive_words is True
    assert view.safety.llm_review is False
    assert view.updated_at is None
    assert view.temperature is None


async def test_build_ai_config_view_none(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    monkeypatch.setattr(settings, "AI_DEFAULT_PROVIDER", "ghost")
    view = await ai_config_service.build_ai_config_view(db, product, None)
    assert view.provider_code is None
    assert view.provider_health is None


async def test_build_ai_config_view_down(db: AsyncSession) -> None:
    register(_FakeModelProvider("fake-down", configured=False))
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    config = await _make_ai_config(db, tenant, product, provider_code="fake-down")
    view = await ai_config_service.build_ai_config_view(db, product, config)
    assert view.provider_code == "fake-down"
    assert view.provider_health == "DOWN"
    assert view.provider_configured is False


async def test_build_ai_config_view_wifi(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant, network_type=NetworkType.WIFI)
    preset = await _make_role_preset(db, code="story-teller")
    kb = await _make_kb(db, tenant)
    voice = await _make_voice(db, tenant)
    config = await _make_ai_config(
        db,
        tenant,
        product,
        provider_code="mock",
        role_preset_code="story-teller",
        knowledge_base_id=kb.id,
        voice_profile_id=voice.id,
        temperature=123,
        max_tokens=512,
        status=str(EnableStatus.ENABLED),
        safety_enabled=False,
        safety_llm_review=True,
    )
    view = await ai_config_service.build_ai_config_view(db, product, config)
    assert view.network_type == str(NetworkType.WIFI)
    assert view.role_supported is False
    assert view.role_unsupported_reason == ai_config_service.WIFI_ROLE_UNSUPPORTED_REASON
    assert view.role_preset_name == preset.name
    assert view.knowledge_base_name == kb.name
    assert view.voice_profile_name == voice.name
    assert view.temperature == pytest.approx(1.23)
    assert view.max_tokens == 512
    assert view.safety.enabled is False
    assert view.safety.llm_review is True
    assert view.status == str(EnableStatus.ENABLED)
    assert view.updated_at is not None


async def test_role_preset_name_code(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    assert await ai_config_service._role_preset_name(db, tenant.id, None) is None
    preset = await _make_role_preset(db, code="global-role", tenant_id=None)
    assert await ai_config_service._role_preset_name(db, None, "global-role") == preset.name


async def test_label_names(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    kb = await _make_kb(db, tenant)
    voice = await _make_voice(db, tenant)
    kb_name, voice_name = await ai_config_service._label_names(
        db, kb_id=kb.id, voice_id=voice.id, tenant_id=None
    )
    assert (kb_name, voice_name) == (kb.name, voice.name)


async def test_get_ai_config_view_404(db: AsyncSession) -> None:
    tenant_a = await _make_tenant(db, code="T001")
    tenant_b = await _make_tenant(db, code="T002")
    product = await _make_product(db, tenant_a)
    with pytest.raises(AppException) as excinfo:
        await ai_config_service.get_ai_config_view(db, _merchant_auth(tenant_b), product.id)
    assert excinfo.value.code == ErrorCode.RESOURCE_NOT_FOUND


# ---------------------------------------------------------------------------
# 配置写入
# ---------------------------------------------------------------------------


async def test_update_ai_config(db: AsyncSession) -> None:
    register(_FakeModelProvider("fake-full"))
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    product = await _make_product(db, tenant)
    preset = await _make_role_preset(db, code="guardian")
    kb = await _make_kb(db, tenant)
    voice = await _make_voice(db, tenant)
    payload = AiConfigUpdateRequest(
        provider_code="fake-full",
        role_preset_code="guardian",
        knowledge_base_id=kb.id,
        voice_profile_id=voice.id,
        system_prompt="你是安全守卫",
        greeting="嗨，小朋友",
        temperature=0.7,
        max_tokens=256,
        status=EnableStatus.ENABLED,
    )
    view = await ai_config_service.update_ai_config(db, auth, product.id, payload)
    assert view.provider_code == "fake-full"
    assert view.status == str(EnableStatus.ENABLED)
    assert view.temperature == pytest.approx(0.7)
    assert view.role_preset_name == preset.name

    config = await ai_config_service.get_ai_config(db, product.id)
    assert config is not None
    assert config.temperature == 70
    assert config.max_tokens == 256
    assert config.system_prompt == "你是安全守卫"

    await db.refresh(product)
    assert product.ai_enabled is True

    audits = await _audit_rows(db, "CREATE")
    assert len(audits) == 1
    assert audits[0].resource_type == "ai_config"
    assert audits[0].resource_id == config.id
    assert audits[0].detail["fields"] == [
        "provider_code",
        "role_preset_code",
        "knowledge_base_id",
        "voice_profile_id",
        "system_prompt",
        "greeting",
        "temperature",
        "max_tokens",
        "status",
    ]
    assert audits[0].detail["networkType"] == product.network_type


async def test_update_ai_config_2(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    product = await _make_product(db, tenant)
    await _make_ai_config(db, tenant, product, greeting="旧问候")
    view = await ai_config_service.update_ai_config(
        db, auth, product.id, AiConfigUpdateRequest(greeting="新问候")
    )
    assert view.greeting == "新问候"
    audits = await _audit_rows(db, "UPDATE")
    assert len(audits) == 1
    assert audits[0].detail["fields"] == ["greeting"]


async def test_update_ai_config_none(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    product = await _make_product(db, tenant)
    await _make_ai_config(db, tenant, product, system_prompt="旧提示词")
    view = await ai_config_service.update_ai_config(
        db, auth, product.id, AiConfigUpdateRequest(system_prompt=None)
    )
    assert view.system_prompt is None
    config = await ai_config_service.get_ai_config(db, product.id)
    assert config is not None and config.system_prompt is None


async def test_update_ai_config_409(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    product = await _make_product(db, tenant)
    with pytest.raises(AppException) as excinfo:
        await ai_config_service.update_ai_config(
            db, auth, product.id, AiConfigUpdateRequest(provider_code="ghost")
        )
    assert excinfo.value.code == ErrorCode.PRODUCT_NOT_AUTHORIZED
    assert excinfo.value.status_code == 409
    assert excinfo.value.details["reason"] == "PROVIDER_NOT_REGISTERED"


async def test_update_ai_config_409_2(db: AsyncSession) -> None:
    register(_FakeModelProvider("fake-raw", configured=False))
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    product = await _make_product(db, tenant)
    with pytest.raises(AppException) as excinfo:
        await ai_config_service.update_ai_config(
            db, auth, product.id, AiConfigUpdateRequest(provider_code="fake-raw")
        )
    assert excinfo.value.details == {
        "providerCode": "fake-raw",
        "configured": False,
        "ok": False,
    }


async def test_update_ai_config_wifi_409(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    product = await _make_product(db, tenant, network_type=NetworkType.WIFI)
    with pytest.raises(AppException) as excinfo:
        await ai_config_service.update_ai_config(
            db, auth, product.id, AiConfigUpdateRequest(role_preset_code="any-role")
        )
    assert excinfo.value.code == ErrorCode.INVALID_STATE_TRANSITION
    assert excinfo.value.status_code == 409
    assert excinfo.value.details == {
        "networkType": "WIFI",
        "reason": ai_config_service.WIFI_ROLE_UNSUPPORTED_REASON,
    }


async def test_update_ai_config_wifi(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    product = await _make_product(db, tenant, network_type=NetworkType.WIFI)
    await _make_ai_config(db, tenant, product, role_preset_code="legacy")
    view = await ai_config_service.update_ai_config(
        db, auth, product.id, AiConfigUpdateRequest(role_preset_code=None)
    )
    assert view.role_preset_code is None


async def test_update_ai_config_404(db: AsyncSession) -> None:
    tenant_a = await _make_tenant(db, code="T001")
    tenant_b = await _make_tenant(db, code="T002")
    product = await _make_product(db, tenant_a)
    foreign_kb = await _make_kb(db, tenant_b)
    with pytest.raises(AppException) as excinfo:
        await ai_config_service.update_ai_config(
            db, _merchant_auth(tenant_a), product.id,
            AiConfigUpdateRequest(knowledge_base_id=foreign_kb.id),
        )
    assert excinfo.value.code == ErrorCode.RESOURCE_NOT_FOUND
    assert excinfo.value.message == "知识库不存在"


async def test_update_ai_config_404_2(db: AsyncSession) -> None:
    tenant_a = await _make_tenant(db, code="T001")
    tenant_b = await _make_tenant(db, code="T002")
    product = await _make_product(db, tenant_a)
    foreign_voice = await _make_voice(db, tenant_b)
    with pytest.raises(AppException) as excinfo:
        await ai_config_service.update_ai_config(
            db, _merchant_auth(tenant_a), product.id,
            AiConfigUpdateRequest(voice_profile_id=foreign_voice.id),
        )
    assert excinfo.value.code == ErrorCode.RESOURCE_NOT_FOUND
    assert excinfo.value.message == "音色档案不存在"


async def test_update_ai_config_409_3(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    product = await _make_product(db, tenant)
    monkeypatch.setattr(settings, "AI_DEFAULT_PROVIDER", "ghost")
    with pytest.raises(AppException) as excinfo:
        await ai_config_service.update_ai_config(
            db, auth, product.id, AiConfigUpdateRequest(status=EnableStatus.ENABLED)
        )
    assert excinfo.value.details == {"reason": "NO_PROVIDER_RESOLVED", "ok": False}


async def test_update_ai_config_3(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    product = await _make_product(db, tenant)
    await ai_config_service.update_ai_config(
        db, auth, product.id, AiConfigUpdateRequest(status=EnableStatus.ENABLED)
    )
    await db.refresh(product)
    assert product.ai_enabled is True
    view = await ai_config_service.update_ai_config(
        db, auth, product.id, AiConfigUpdateRequest(status=EnableStatus.DISABLED)
    )
    assert view.status == str(EnableStatus.DISABLED)
    await db.refresh(product)
    assert product.ai_enabled is False


async def test_update_prompt(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    product = await _make_product(db, tenant)
    view = await ai_config_service.update_prompt(
        db,
        auth,
        product.id,
        AiPromptUpdateRequest(
            system_prompt="提示词", greeting="问候", temperature=0.75, max_tokens=128
        ),
    )
    assert view.temperature == pytest.approx(0.75)
    config = await ai_config_service.get_ai_config(db, product.id)
    assert config is not None
    assert config.temperature == 75
    assert config.max_tokens == 128


async def test_update_role_wifi_409(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    product = await _make_product(db, tenant, network_type=NetworkType.WIFI)
    with pytest.raises(AppException) as excinfo:
        await ai_config_service.update_role(
            db, auth, product.id, AiRoleUpdateRequest(role_preset_code="role-x")
        )
    assert excinfo.value.code == ErrorCode.INVALID_STATE_TRANSITION
    assert excinfo.value.details["networkType"] == "WIFI"

    await _make_ai_config(db, tenant, product, role_preset_code="legacy")
    view = await ai_config_service.update_role(
        db, auth, product.id, AiRoleUpdateRequest(role_preset_code=None)
    )
    assert view.role_preset_code is None


async def test_update_voice(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    product = await _make_product(db, tenant)
    voice = await _make_voice(db, tenant)
    view = await ai_config_service.update_voice(
        db, auth, product.id, AiVoiceUpdateRequest(voice_profile_id=voice.id)
    )
    assert view.voice_profile_id == voice.id
    assert view.voice_profile_name == voice.name


async def test_update_safety(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    product = await _make_product(db, tenant)
    view = await ai_config_service.update_safety(
        db, auth, product.id, AiSafetyUpdateRequest(enabled=False, llm_review=True)
    )
    assert view.safety.enabled is False
    assert view.safety.sensitive_words is True
    assert view.safety.llm_review is True
    audits = await _audit_rows(db, "CREATE")
    assert len(audits) == 1
    assert audits[0].detail["fields"] == ["safety_enabled", "safety_llm_review"]


async def test_update_safety_2(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    product = await _make_product(db, tenant)
    await _make_ai_config(
        db,
        tenant,
        product,
        safety_enabled=True,
        safety_sensitive_words=True,
        safety_llm_review=False,
    )
    view = await ai_config_service.update_safety(
        db, auth, product.id, AiSafetyUpdateRequest(sensitive_words=False)
    )
    assert view.safety.sensitive_words is False
    assert view.safety.enabled is True

    # 全部字段为 None：不产生字段变更，但仍写一条空 fields 审计。
    view2 = await ai_config_service.update_safety(db, auth, product.id, AiSafetyUpdateRequest())
    assert view2.safety.sensitive_words is False
    audits = await _audit_rows(db, "UPDATE")
    assert len(audits) == 2
    assert audits[0].detail["fields"] == ["safety_sensitive_words"]
    assert audits[1].detail["fields"] == []


# ---------------------------------------------------------------------------
# 选项清单
# ---------------------------------------------------------------------------


async def test_list_providers(db: AsyncSession) -> None:
    items = await ai_config_service.list_providers(db)
    by_code = {item.code: item for item in items}
    assert by_code["mock"].is_default is True
    assert by_code["mock"].configured is True
    assert by_code["mock"].status == "ACTIVE"
    assert by_code["volcano"].configured is False
    assert by_code["jixian"].is_default is False


async def test_list_providers_2(db: AsyncSession) -> None:
    db.add(
        AiProviderRow(
            id=new_id("ai_provider"),
            code="volcano",
            name="火山引擎（已开通）",
            kind=str(AiProviderKind.MODEL),
            vendor="volcano",
            status=str(AiProviderStatus.DISABLED),
            is_default=True,
        )
    )
    await db.flush()
    items = await ai_config_service.list_providers(db)
    by_code = {item.code: item for item in items}
    # 有行：以行为准
    assert by_code["volcano"].is_default is True
    assert by_code["volcano"].status == "DISABLED"
    # 无行：回退「注册键 == AI_DEFAULT_PROVIDER」
    assert by_code["mock"].is_default is True
    assert by_code["mock"].status == "ACTIVE"


async def test_list_role_presets(db: AsyncSession) -> None:
    tenant_a = await _make_tenant(db, code="T001")
    tenant_b = await _make_tenant(db, code="T002")
    await _make_role_preset(db, code="builtin-b", tenant_id=None, is_builtin=True)
    await _make_role_preset(db, code="builtin-a", tenant_id=None, is_builtin=True)
    await _make_role_preset(db, code="mine", tenant_id=tenant_a.id, is_builtin=False)
    await _make_role_preset(db, code="mine-off", tenant_id=tenant_a.id, status=EnableStatus.DISABLED)
    await _make_role_preset(db, code="theirs", tenant_id=tenant_b.id, is_builtin=False)

    items = await ai_config_service.list_role_presets(db, _merchant_auth(tenant_a))
    codes = [item.code for item in items]
    assert codes == ["builtin-a", "builtin-b", "mine"]

    platform_items = await ai_config_service.list_role_presets(db, _platform_auth())
    platform_codes = [item.code for item in platform_items]
    assert "theirs" in platform_codes
    assert "mine" in platform_codes


async def test_list_voice_profiles(db: AsyncSession) -> None:
    tenant_a = await _make_tenant(db, code="T001")
    tenant_b = await _make_tenant(db, code="T002")
    await _make_voice(db, tenant_a, name="乙声音")
    await _make_voice(db, tenant_a, name="甲声音")
    await _make_voice(db, tenant_b, name="别家声音")
    items = await ai_config_service.list_voice_profiles(db, _merchant_auth(tenant_a))
    # SQLite BINARY 排序按 Unicode 码位：乙(U+4E59) 在 甲(U+7532) 之前。
    assert [item.name for item in items] == ["乙声音", "甲声音"]


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------


async def test_create_knowledge_base(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    product = await _make_product(db, tenant)
    kb = await ai_config_service.create_knowledge_base(
        db, auth, name="产品知识库", description="说明", client_product_id=product.id
    )
    assert kb.name == "产品知识库"
    assert kb.client_product_id == product.id
    audits = await _audit_rows(db, "CREATE")
    assert len(audits) == 1
    assert audits[0].resource_type == "knowledge_base"
    assert audits[0].detail == {"clientProductId": product.id}


async def test_create_knowledge_base_404(db: AsyncSession) -> None:
    tenant_a = await _make_tenant(db, code="T001")
    tenant_b = await _make_tenant(db, code="T002")
    product_b = await _make_product(db, tenant_b, code="prod-b")
    with pytest.raises(AppException) as excinfo:
        await ai_config_service.create_knowledge_base(
            db, _merchant_auth(tenant_a), name="kb", client_product_id=product_b.id
        )
    assert excinfo.value.code == ErrorCode.RESOURCE_NOT_FOUND


async def test_create_knowledge_base_403(db: AsyncSession) -> None:
    with pytest.raises(AppException) as excinfo:
        await ai_config_service.create_knowledge_base(db, _platform_auth(), name="kb")
    assert excinfo.value.code == ErrorCode.PERMISSION_DENIED


async def test_list_knowledge_bases(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    now = utcnow()
    kb1 = await _make_kb(db, tenant, name="安全知识库")
    kb2 = await _make_kb(db, tenant, name="百科知识库")
    kb3 = await _make_kb(db, tenant, name="操作手册")
    for idx, kb in enumerate((kb1, kb2, kb3)):
        kb.created_at = now + timedelta(microseconds=idx)
    await db.flush()
    await _make_kb_file(db, kb1, filename="a.txt", status=KbFileStatus.PARSED, chunk_count=3)
    await _make_kb_file(db, kb1, filename="b.txt", status=KbFileStatus.PENDING)

    items, total = await ai_config_service.list_knowledge_bases(db, auth, keyword="知识库")
    assert total == 2
    assert [item.name for item in items] == ["百科知识库", "安全知识库"]
    target = next(item for item in items if item.id == kb1.id)
    assert target.file_count == 2
    assert target.parsed_file_count == 1

    # 无命中关键字：空列表 + total=0
    items2, total2 = await ai_config_service.list_knowledge_bases(db, auth, keyword="不存在")
    assert (items2, total2) == ([], 0)

    # 分页裁剪（created_at 倒序：操作手册 → 百科 → 安全）
    items3, total3 = await ai_config_service.list_knowledge_bases(db, auth, offset=2, limit=2)
    assert total3 == 3
    assert [item.name for item in items3] == ["安全知识库"]


async def test_get_knowledge_base_detail(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    kb = await _make_kb(db, tenant)
    now = utcnow()
    old = await _make_kb_file(
        db, kb, filename="old.md", status=KbFileStatus.PARSED, chunk_count=2, created_at=now
    )
    new = await _make_kb_file(
        db,
        kb,
        filename="new.md",
        status=KbFileStatus.PENDING,
        created_at=now + timedelta(microseconds=1),
    )
    detail = await ai_config_service.get_knowledge_base_detail(db, auth, kb.id)
    assert detail.file_count == 2
    assert detail.parsed_file_count == 1
    assert [f.filename for f in detail.files] == ["new.md", "old.md"]
    old_resp = next(f for f in detail.files if f.id == old.id)
    assert old_resp.parsed_at == old.updated_at
    new_resp = next(f for f in detail.files if f.id == new.id)
    assert new_resp.parsed_at is None


async def test_get_knowledge_base_detail_404(db: AsyncSession) -> None:
    tenant_a = await _make_tenant(db, code="T001")
    tenant_b = await _make_tenant(db, code="T002")
    kb = await _make_kb(db, tenant_a)
    with pytest.raises(AppException) as excinfo:
        await ai_config_service.get_knowledge_base_detail(db, _merchant_auth(tenant_b), kb.id)
    assert excinfo.value.code == ErrorCode.RESOURCE_NOT_FOUND
    assert excinfo.value.message == "知识库不存在"


async def test_update_knowledge_base(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    kb = await _make_kb(db, tenant)
    view = await ai_config_service.update_knowledge_base(db, auth, kb.id)
    assert view.name == "安全知识库"
    assert await _audit_rows(db, "UPDATE") == []


async def test_update_knowledge_base_2(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    kb = await _make_kb(db, tenant, name="知识库A", description="旧描述")
    view = await ai_config_service.update_knowledge_base(
        db, auth, kb.id,
        name="知识库A",
        description=None,
        fields_set={"name", "description"},
    )
    assert view.description is None
    audits = await _audit_rows(db, "UPDATE")
    assert len(audits) == 1
    # name 值未变不计入 changed；description 显式 null 是「清空」语义
    assert audits[0].detail["fields"] == ["description"]


async def test_update_knowledge_base_3(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    product = await _make_product(db, tenant)
    kb = await _make_kb(db, tenant)
    view = await ai_config_service.update_knowledge_base(
        db, auth, kb.id, client_product_id=product.id, fields_set={"client_product_id"}
    )
    assert view.client_product_id == product.id
    view2 = await ai_config_service.update_knowledge_base(
        db, auth, kb.id, client_product_id=None, fields_set={"client_product_id"}
    )
    assert view2.client_product_id is None


async def test_update_knowledge_base_404(db: AsyncSession) -> None:
    tenant_a = await _make_tenant(db, code="T001")
    tenant_b = await _make_tenant(db, code="T002")
    kb = await _make_kb(db, tenant_a)
    product_b = await _make_product(db, tenant_b, code="prod-b")
    with pytest.raises(AppException) as excinfo:
        await ai_config_service.update_knowledge_base(
            db, _merchant_auth(tenant_a), kb.id,
            client_product_id=product_b.id,
            fields_set={"client_product_id"},
        )
    assert excinfo.value.code == ErrorCode.RESOURCE_NOT_FOUND


async def test_delete_knowledge_base_409(db: AsyncSession, tmp_path: Path) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    product = await _make_product(db, tenant, name="故事机X")
    kb = await _make_kb(db, tenant)
    kb_file = await _make_kb_file(db, kb, content=b"to-be-deleted")
    _write_storage(tmp_path, kb_file, b"to-be-deleted")
    await _make_ai_config(db, tenant, product, knowledge_base_id=kb.id)

    with pytest.raises(AppException) as excinfo:
        await ai_config_service.delete_knowledge_base(db, auth, kb.id)
    assert excinfo.value.code == ErrorCode.CASCADE_CONFLICT
    assert excinfo.value.status_code == 409
    assert excinfo.value.details == {"products": [{"id": product.id, "name": "故事机X"}]}

    # 引用方是 ai_config.knowledge_base_id，需通过更新 AI 配置显式解绑。
    await ai_config_service.update_ai_config(
        db, auth, product.id, AiConfigUpdateRequest(knowledge_base_id=None)
    )
    await ai_config_service.delete_knowledge_base(db, auth, kb.id)
    assert await db.get(KnowledgeBase, kb.id) is None
    assert await db.get(KbFile, kb_file.id) is None
    assert not (tmp_path / (kb_file.storage_path or "")).exists()
    deletes = await _audit_rows(db, "DELETE")
    assert len(deletes) == 1
    assert deletes[0].resource_type == "knowledge_base"
    assert deletes[0].detail == {"fileCount": 1}


async def test_delete_knowledge_base(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    kb = await _make_kb(db, tenant)
    await _make_kb_file(db, kb, storage_path=None)
    await ai_config_service.delete_knowledge_base(db, auth, kb.id)
    assert await db.get(KnowledgeBase, kb.id) is None


async def test_upload_kb_file(db: AsyncSession, tmp_path: Path) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    kb = await _make_kb(db, tenant)
    content = b"hello kb"
    resp = await ai_config_service.upload_kb_file(
        db, auth, kb.id, filename="FAQ.TXT", content_type="text/plain", content=content
    )
    assert resp.status == str(KbFileStatus.PENDING)
    assert resp.chunk_count == 0
    assert resp.size_bytes == len(content)
    assert resp.status_label == ai_config_service.KB_FILE_STATUS_LABELS["PENDING"]

    row = await db.get(KbFile, resp.id)
    assert row is not None
    assert row.checksum == sha256_hex(content)
    assert row.storage_path is not None and row.storage_path.endswith(".txt")
    assert (tmp_path / row.storage_path).read_bytes() == content

    audits = await _audit_rows(db, "CREATE")
    assert len(audits) == 1
    assert audits[0].resource_type == "kb_file"
    assert audits[0].detail == {"knowledgeBaseId": kb.id, "checksum": row.checksum}


async def test_upload_kb_file_400(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    kb = await _make_kb(db, tenant)
    with pytest.raises(AppException) as excinfo:
        await ai_config_service.upload_kb_file(
            db, auth, kb.id, filename="evil.exe", content_type=None, content=b"x"
        )
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
    assert excinfo.value.status_code == 400
    assert excinfo.value.details["field"] == "file"
    assert excinfo.value.details["allowed"] == sorted(ai_config_service.allowed_kb_extensions())


async def test_upload_kb_file_400_2(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    kb = await _make_kb(db, tenant)
    monkeypatch.setattr(settings, "KB_FILE_MAX_BYTES", 4)
    with pytest.raises(AppException) as excinfo:
        await ai_config_service.upload_kb_file(
            db, auth, kb.id, filename="big.txt", content_type=None, content=b"12345"
        )
    assert excinfo.value.status_code == 400
    assert excinfo.value.details == {"field": "file", "maxBytes": 4}


async def test_upload_kb_file_checksum_409(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    kb = await _make_kb(db, tenant)
    content = b"duplicate-content"
    first = await ai_config_service.upload_kb_file(
        db, auth, kb.id, filename="first.txt", content_type=None, content=content
    )
    with pytest.raises(AppException) as excinfo:
        await ai_config_service.upload_kb_file(
            db, auth, kb.id, filename="second.txt", content_type=None, content=content
        )
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
    assert excinfo.value.status_code == 409
    # details 里的 filename 是已存在的那个文件，而非本次上传的文件名。
    assert excinfo.value.details == {"existingFileId": first.id, "filename": "first.txt"}


async def test_list_kb_files(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    kb = await _make_kb(db, tenant)
    now = utcnow()
    for idx in range(3):
        await _make_kb_file(
            db,
            kb,
            filename=f"f{idx}.txt",
            status=KbFileStatus.PARSED if idx < 2 else KbFileStatus.PENDING,
            created_at=now + timedelta(microseconds=idx),
        )
    items, total = await ai_config_service.list_kb_files(
        db, auth, kb.id, status=str(KbFileStatus.PARSED)
    )
    assert total == 2
    assert [item.filename for item in items] == ["f1.txt", "f0.txt"]

    items2, total2 = await ai_config_service.list_kb_files(db, auth, kb.id, offset=2, limit=2)
    assert total2 == 3
    assert [item.filename for item in items2] == ["f0.txt"]


async def test_delete_kb_file(db: AsyncSession, tmp_path: Path) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    kb = await _make_kb(db, tenant)
    kb_file = await _make_kb_file(
        db, kb, content=b"parsed-content", status=KbFileStatus.PARSED, chunk_count=5
    )
    _write_storage(tmp_path, kb_file, b"parsed-content")
    await ai_config_service._refresh_kb_summary(db, kb)
    # _refresh_kb_summary 只改内存对象不 flush，直接断言内存值。
    assert kb.doc_count == 1 and kb.chunk_count == 5

    await ai_config_service.delete_kb_file(db, auth, kb.id, kb_file.id)
    assert await db.get(KbFile, kb_file.id) is None
    assert not (tmp_path / (kb_file.storage_path or "")).exists()
    await db.refresh(kb)
    assert kb.doc_count == 0 and kb.chunk_count == 0
    deletes = await _audit_rows(db, "DELETE")
    assert len(deletes) == 1
    assert deletes[0].resource_type == "kb_file"


async def test_delete_kb_file_404(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    kb = await _make_kb(db, tenant)
    with pytest.raises(AppException) as excinfo:
        await ai_config_service.delete_kb_file(db, auth, kb.id, new_id("kb_file"))
    assert excinfo.value.code == ErrorCode.RESOURCE_NOT_FOUND
    assert excinfo.value.message == "知识库文件不存在"


async def test_parse_kb_file_txt(db: AsyncSession, tmp_path: Path) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    kb = await _make_kb(db, tenant)
    content = "第一段\n\n第二段\n\n第三段".encode()
    kb_file = await _make_kb_file(db, kb, content=content)
    _write_storage(tmp_path, kb_file, content)

    resp = await ai_config_service.parse_kb_file(db, auth, kb.id, kb_file.id)
    assert resp.status == str(KbFileStatus.PARSED)
    assert resp.chunk_count == 3
    assert resp.error_message is None
    assert resp.parsed_at is not None

    await db.refresh(kb)
    assert kb.doc_count == 1
    assert kb.chunk_count == 3

    audits = await _audit_rows(db, "UPDATE")
    assert len(audits) == 1
    assert audits[0].success is True
    assert audits[0].detail == {"chunkCount": 3, "error": None}


async def test_parse_kb_file(db: AsyncSession, tmp_path: Path) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    kb = await _make_kb(db, tenant)
    content = b"%PDF-fake"
    kb_file = await _make_kb_file(db, kb, filename="doc.pdf", content=content)
    _write_storage(tmp_path, kb_file, content)

    resp = await ai_config_service.parse_kb_file(db, auth, kb.id, kb_file.id)
    assert resp.status == str(KbFileStatus.FAILED)
    assert resp.error_message == ai_config_service.KB_UNPARSABLE_MESSAGE
    assert resp.parsed_at is None
    audits = await _audit_rows(db, "UPDATE")
    assert audits[0].success is False


async def test_parse_kb_file_2(db: AsyncSession, tmp_path: Path) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    kb = await _make_kb(db, tenant)
    content = b"   \n\n  \n"
    kb_file = await _make_kb_file(db, kb, content=content)
    _write_storage(tmp_path, kb_file, content)

    resp = await ai_config_service.parse_kb_file(db, auth, kb.id, kb_file.id)
    assert resp.status == str(KbFileStatus.FAILED)
    assert resp.error_message == ai_config_service.KB_EMPTY_MESSAGE


async def test_parse_kb_file_3(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    kb = await _make_kb(db, tenant)
    kb_file = await _make_kb_file(db, kb, content=b"ghost")  # 不落盘，模拟对象被外部清理
    resp = await ai_config_service.parse_kb_file(db, auth, kb.id, kb_file.id)
    assert resp.status == str(KbFileStatus.FAILED)
    assert resp.error_message is not None
    assert resp.error_message.startswith("读取文件失败：")


async def test_parse_kb_file_404(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    kb = await _make_kb(db, tenant)
    with pytest.raises(AppException) as excinfo:
        await ai_config_service.parse_kb_file(db, auth, kb.id, new_id("kb_file"))
    assert excinfo.value.code == ErrorCode.RESOURCE_NOT_FOUND


def test_kb_file_to_response(db: AsyncSession) -> None:
    kb_file = KbFile(
        id=new_id("kb_file"),
        knowledge_base_id=new_id("knowledge_base"),
        tenant_id=new_id("tenant"),
        filename="x.txt",
        content_type="text/plain",
        size_bytes=1,
        storage_path="kb/t/x.txt",
        checksum="c" * 64,
        status="ARCHIVED",
        chunk_count=0,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    resp = ai_config_service.kb_file_to_response(kb_file)
    assert resp.status_label == "ARCHIVED"
    assert resp.parsed_at is None


# ---------------------------------------------------------------------------
# 内容库
# ---------------------------------------------------------------------------


async def test_content_to_response_scope(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    platform_item = await _make_content(db, title="平台故事", item_type=ContentItemType.STORY)
    tenant_item = await _make_content(
        db, title="租户儿歌", tenant_id=tenant.id, item_type=ContentItemType.SONG
    )
    assert ai_config_service.content_to_response(platform_item).scope == "PLATFORM"
    assert ai_config_service.content_to_response(platform_item).type_label == "故事"
    assert ai_config_service.content_to_response(tenant_item).scope == "TENANT"
    assert ai_config_service.content_to_response(tenant_item).type_label == "儿歌"

    weird = await _make_content(db, title="未知类型")
    weird.type = "POEM"
    assert ai_config_service.content_to_response(weird).type_label == "POEM"


async def test_list_content_items(db: AsyncSession) -> None:
    tenant_a = await _make_tenant(db, code="T001")
    tenant_b = await _make_tenant(db, code="T002")
    await _make_content(db, title="公共内容")
    await _make_content(db, title="我家内容", tenant_id=tenant_a.id)
    await _make_content(db, title="别家内容", tenant_id=tenant_b.id)
    items, total = await ai_config_service.list_content_items(db, _merchant_auth(tenant_a))
    assert total == 2
    assert {item.title for item in items} == {"公共内容", "我家内容"}


async def test_list_content_items_scope(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    await _make_content(db, title="平台条目")
    await _make_content(db, title="租户条目", tenant_id=tenant.id)
    auth = _platform_auth()

    platform_items, total_p = await ai_config_service.list_content_items(
        db, auth, scope="PLATFORM"
    )
    assert total_p == 1 and platform_items[0].title == "平台条目"

    tenant_items, total_t = await ai_config_service.list_content_items(db, auth, scope="TENANT")
    assert total_t == 1 and tenant_items[0].title == "租户条目"

    all_items, total_all = await ai_config_service.list_content_items(db, auth)
    assert total_all == 2 and len(all_items) == 2


async def test_list_content_items_2(db: AsyncSession) -> None:
    await _make_content(db, title="小兔子乖乖", item_type=ContentItemType.SONG, sort_order=2)
    await _make_content(db, title="小星星", item_type=ContentItemType.SONG, sort_order=1)
    await _make_content(
        db, title="小熊历险记", item_type=ContentItemType.STORY, description="森林故事"
    )
    auth = _platform_auth()

    songs, total = await ai_config_service.list_content_items(
        db, auth, item_type=str(ContentItemType.SONG)
    )
    assert total == 2
    assert [item.title for item in songs] == ["小星星", "小兔子乖乖"]

    # keyword 命中 title 与 description 两个分支
    by_title, total_t = await ai_config_service.list_content_items(db, auth, keyword="小兔子")
    assert total_t == 1 and by_title[0].title == "小兔子乖乖"
    by_desc, total_d = await ai_config_service.list_content_items(db, auth, keyword="森林")
    assert total_d == 1 and by_desc[0].title == "小熊历险记"

    page2, total_p2 = await ai_config_service.list_content_items(db, auth, offset=2, limit=2)
    assert total_p2 == 3 and len(page2) == 1


# ---------------------------------------------------------------------------
# 运营视图
# ---------------------------------------------------------------------------


async def test_build_product_ops_detail(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    product = await _make_product(db, tenant)
    await _make_device(db, product, activation_status=ActivationStatus.ACTIVATED)
    await _make_device(db, product, activation_status=ActivationStatus.NOT_ACTIVATED)
    await _make_device(db, product, asset_status=AssetStatus.RETIRED)  # 退役不计入
    await _make_dialogue(db, product, user_messages=2, assistant_messages=1)
    kb = await _make_kb(db, tenant)
    await _make_ai_config(
        db,
        tenant,
        product,
        provider_code="mock",
        status=str(EnableStatus.ENABLED),
        knowledge_base_id=kb.id,
    )

    detail = await ai_config_service.build_product_ops_detail(db, auth, product.id)
    assert detail.device_count == 2
    assert detail.activated_device_count == 1
    assert detail.total_interactions == 2  # 只统计 USER 消息
    assert detail.template_name is not None
    assert detail.network_type_label == ai_config_service.NETWORK_TYPE_LABELS["4G"]
    summary = detail.ai_config_summary
    assert summary.status == str(EnableStatus.ENABLED)
    assert summary.provider_code == "mock"
    assert summary.knowledge_base_name == kb.name
    assert summary.safety_enabled is True


async def test_build_product_ops_detail_2(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    detail = await ai_config_service.build_product_ops_detail(db, _platform_auth(), product.id)
    assert detail.device_count == 0
    assert detail.total_interactions == 0
    summary = detail.ai_config_summary
    assert summary.status == str(EnableStatus.DISABLED)
    assert summary.provider_code == "mock"
    assert summary.safety_enabled is True
    assert summary.knowledge_base_name is None


async def test_list_products_ops(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    auth = _merchant_auth(tenant)
    now = utcnow()
    p1 = await _make_product(db, tenant, code="prod-alpha", name="阿尔法故事机")
    p2 = await _make_product(db, tenant, code="prod-beta", name="贝塔学习机")
    for idx, product in enumerate((p1, p2)):
        product.created_at = now + timedelta(microseconds=idx)
    await db.flush()
    await _make_device(db, p1, activation_status=ActivationStatus.ACTIVATED)
    await _make_device(db, p1, activation_status=ActivationStatus.ACTIVATING)
    await _make_dialogue(db, p1, user_messages=3)
    await _make_ai_config(
        db, tenant, p1, provider_code="mock", status=str(EnableStatus.ENABLED)
    )

    items, total = await ai_config_service.list_products_ops(db, auth)
    assert total == 2
    assert [item.name for item in items] == ["贝塔学习机", "阿尔法故事机"]
    first = items[1]
    assert first.device_count == 2
    assert first.activated_device_count == 1
    assert first.total_interactions == 3
    assert first.ai_config_summary.status == str(EnableStatus.ENABLED)
    # 无配置产品也给默认摘要
    assert items[0].ai_config_summary.status == str(EnableStatus.DISABLED)

    # 按编码关键字过滤
    by_code, total_c = await ai_config_service.list_products_ops(db, auth, keyword="prod-beta")
    assert total_c == 1 and by_code[0].id == p2.id

    # 无命中关键字：空列表 + total=0
    empty, total_e = await ai_config_service.list_products_ops(db, auth, keyword="不存在的产品")
    assert (empty, total_e) == ([], 0)

    # 分页裁剪
    page2, total_p2 = await ai_config_service.list_products_ops(db, auth, offset=1, limit=1)
    assert total_p2 == 2 and len(page2) == 1 and page2[0].id == p1.id


# ---------------------------------------------------------------------------
# 依赖收口语义（deps / scope 的直接断言）
# ---------------------------------------------------------------------------


def test_require_tenant_id() -> None:
    with pytest.raises(AppException) as excinfo:
        _platform_auth().require_tenant_id()
    assert excinfo.value.code == ErrorCode.PERMISSION_DENIED


async def test_require_tenant_id_2(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    assert _merchant_auth(tenant).require_tenant_id() == tenant.id
