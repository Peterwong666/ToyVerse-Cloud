"""AI 配置与知识库服务（P9）。

本模块承担四件事，它们共享同一个「商户端、租户级、需要落盘」的语境：

1. **AI 配置视图与部分更新**（``ai_configs``）
2. **供应商 / 角色预设 / 音色的只读清单**（供配置页下拉）
3. **知识库与文件**（``knowledge_bases`` / ``kb_files`` + 对象存储）
4. **内容库只读视图**（``content_items``）

三条必须写进文档的规则
----------------------

**① 绝不下发密钥。** 供应商列表只给 ``configured`` 布尔。密钥是平台级凭据，
商户既不需要、也不应该拿到它——这是对参考站「SecretKey 明文下发前端」的
架构性防范，与 :mod:`app.services.catalog_service` 同一红线。

**② 角色支持与否是业务规则，不是配置项的镜像。**
联网方式为 ``4G`` 时，对话角色由平台配置（``roleSupported=true``）；
为 ``WIFI``（京东 JoyInside）时，角色在厂商智能体侧配置，平台不覆盖
（``roleSupported=false`` + 固定的 ``roleUnsupportedReason``）。
判据与文案收在本模块的常量里，GET 与 PUT 共用，避免两处各写一句导致
前端拿到的「为什么不能改」与实际拒绝理由不一致。

**③ Wi-Fi 产品的角色写入是 409 而不是静默忽略。**
静默忽略会让商户以为「改成功了」，直到发现设备说话的语气毫无变化。
宁可当场拒绝并说明原因（``INVALID_STATE_TRANSITION``，``details`` 带
``networkType`` 与 ``reason``）。

为什么温度用「×100 整数」而非浮点
---------------------------------
``AiConfig.temperature`` 是整数列。SQLite 与某些驱动在 NUMERIC 上做浮点
存取会出现 ``0.7000000000000001`` 这类漂移，「1.0 与 0.9999 是否相等」
在配置校验里是真实问题。放大的整数在库内是精确值，只在出入参边界换算。

知识库的取舍
------------
* **文件重复**：同一知识库内 checksum 相同 → ``409 VALIDATION_ERROR``。
  选「报错」而不是「静默返回既有文件」，是因为响应契约 ``KbFileResponse``
  里没有「这是既有文件」的字段：静默复用会让前端拿到 200/201 却分不清
  「刚上传成功」与「早就有了」，而 ``details.existingFileId`` 让客户端可以
  直接跳到那个文件。这与项目「目录域编码重复一律 409」的口径也一致。
* **解析是诚实的**：``txt/md/csv/json`` 真的读文本、按段落切块、写回
  ``chunk_count``；``pdf/docx`` 本阶段没有解析器，直接 ``FAILED`` 并说明
  需要什么，**绝不假装解析成功**（那会让检索永远查不到却显示「已就绪」）。
* **原始文件名只入库展示**：落盘 key 由 :func:`app.core.storage.build_object_key`
  生成（只取扩展名），用户上传的名字可能含路径分隔符或控制字符，
  直接拼进路径就是路径穿越（原型 ``server.py`` 正是栽在这里）。

内容库为什么只读
----------------
内容库的运营维护涉及内容审核流程，不在 P9 范围；P9 的验收项是「内容热度榜」
（读）。因此本模块只提供列表查询，**不提供 CRUD**——写接口留到有审核流程的
阶段，比先造一个能绕过审核的写入口更安全。

租户收口的一个例外
------------------
``content_items`` 与 ``role_presets`` 的 ``tenant_id`` 可为空，代表
**平台公共内容**。因此它们不能用 :func:`app.db.scope.scoped`（那会加上
``tenant_id == 本租户``，把公共内容整批挡掉），而不能被挡掉恰恰是它们的
设计意图。这里的做法是用同样来自 :mod:`app.db.scope` 的
:func:`scoped_value` 取出本租户 ID，显式构造
``tenant_id IS NULL OR tenant_id = 本租户``——**收口点仍然是 scope 模块**，
只是这两个模型的可见性语义天然是「并集」而不是「等于」。
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import Request
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import registry
from app.core.config import settings
from app.core.deps import AuthContext
from app.core.errors import (
    AppException,
    ErrorCode,
    cascade_conflict,
    not_found,
    product_not_authorized,
    validation_error,
)
from app.core.ids import new_id
from app.core.logging import get_logger
from app.core.storage import build_object_key, get_storage, sha256_hex
from app.db.scope import assert_visible, scoped, scoped_value
from app.models.ai import (
    AiConfig,
    AiProvider,
    DialogueMessage,
    DialogueSession,
    KbFile,
    KnowledgeBase,
    RolePreset,
    VoiceProfile,
)
from app.models.catalog import ClientProduct, ProductTemplate
from app.models.device import Device
from app.models.enums import (
    ActivationStatus,
    AssetStatus,
    AuditAction,
    EnableStatus,
    KbFileStatus,
    MessageRole,
    NetworkType,
)
from app.models.ops import ContentItem
from app.schemas.ops import (
    AiConfigSummary,
    AiConfigView,
    ContentItemResponse,
    KbDetailResponse,
    KbFileResponse,
    KbResponse,
    MerchantProductOpsResponse,
    MerchantProviderItem,
    RolePresetItem,
    SafetySwitches,
    VoiceProfileItem,
)
from app.services import audit_service

logger = get_logger(__name__)

#: Wi-Fi 方案的角色不支持文案（GET 与 PUT 共用，改一处即两处生效）
WIFI_ROLE_UNSUPPORTED_REASON = "Wi-Fi 方案的对话角色由厂商智能体配置（平台不覆盖）"

#: 联网方式的中文展示名。
#:
#: 本地定义而不复用 ``miniapp_service.NETWORK_LABELS``：那个常量属于小程序端
#: 的展示语境（设备卡片），两者会独立演化（例如商户端要显示方案厂商）。
#: 跨域 import 一个「看起来一样」的展示常量，将来改一处忘一处才是隐患。
NETWORK_TYPE_LABELS: dict[str, str] = {
    str(NetworkType.FOUR_G): "4G 蜂窝网络",
    str(NetworkType.WIFI): "Wi-Fi 无线网络",
}

#: 内容类型展示名
CONTENT_TYPE_LABELS: dict[str, str] = {"STORY": "故事", "SONG": "儿歌"}

#: 知识库文件解析状态展示名
KB_FILE_STATUS_LABELS: dict[str, str] = {
    str(KbFileStatus.PENDING): "待解析",
    str(KbFileStatus.PARSING): "解析中",
    str(KbFileStatus.PARSED): "已解析",
    str(KbFileStatus.FAILED): "解析失败",
}

#: 本阶段**真的能**抽取文本的扩展名（其余白名单格式只能诚实失败）
KB_TEXT_EXTENSIONS: frozenset[str] = frozenset({"txt", "md", "csv", "json"})

#: 二进制格式未实现解析时的说明（写死一份，前端与客服照此解释）
KB_UNPARSABLE_MESSAGE = (
    "该格式的文本抽取尚未实现（需要 PDF/DOCX 解析器），请改用 txt/md/csv/json"
)

#: 空文件（或全是空白）的说明
KB_EMPTY_MESSAGE = "文件内容为空，没有可检索的文本块"


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def allowed_kb_extensions() -> set[str]:
    """解析 ``settings.KB_ALLOWED_EXTENSIONS`` 为小写扩展名集合（不含点）。"""
    return {
        item.strip().lower().lstrip(".")
        for item in settings.KB_ALLOWED_EXTENSIONS.split(",")
        if item.strip()
    }


def file_extension(filename: str) -> str:
    """取小写扩展名（不含点）；没有扩展名返回空串。"""
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].strip().lower()


def split_text_blocks(text: str) -> list[str]:
    """把文本切成块（知识库检索的基本单位）。

    切块策略：**优先按空行分段**（段落是作者提供的语义边界），
    整篇没有空行时退化为按行切——单段长文本若不切，检索会退化成
    「整篇命中或整篇不命中」，等于没有检索。
    """
    blocks = [raw.strip() for raw in re.split(r"\n\s*\n", text) if raw.strip()]
    if len(blocks) <= 1:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) > 1:
            return lines
    return blocks


def _first_line(block: str, *, limit: int = 40) -> str:
    """取块的首行作为标题（用于下拉展示与检索关键词来源）。"""
    line = block.splitlines()[0].strip() if block.splitlines() else block.strip()
    return line[:limit] if line else block.strip()[:limit]


async def get_product(session: AsyncSession, auth: AuthContext, product_id: str) -> ClientProduct:
    """取客户产品并校验归属（越权与不存在都返回 404）。

    Raises:
        AppException: 产品不存在或不属于当前租户。
    """
    product = (
        await session.execute(select(ClientProduct).where(ClientProduct.id == product_id))
    ).scalar_one_or_none()
    if product is None:
        raise not_found("客户产品不存在")
    assert_visible(product.tenant_id, auth, resource="产品")
    return product


async def get_ai_config(session: AsyncSession, product_id: str) -> AiConfig | None:
    """取客户产品的 AI 配置（未配置返回 ``None``，不报错）。"""
    return (
        await session.execute(
            select(AiConfig).where(AiConfig.client_product_id == product_id)
        )
    ).scalar_one_or_none()


async def _role_preset_name(
    session: AsyncSession, tenant_id: str | None, code: str | None
) -> str | None:
    """按编码取角色预设名（平台内置 + 本租户范围内）。"""
    if not code:
        return None
    stmt = select(RolePreset.name).where(RolePreset.code == code)
    if tenant_id is not None:
        stmt = stmt.where(
            or_(RolePreset.tenant_id.is_(None), RolePreset.tenant_id == tenant_id)
        )
    value = (await session.execute(stmt.limit(1))).scalar_one_or_none()
    return str(value) if value is not None else None


async def _label_names(
    session: AsyncSession, *, kb_id: str | None, voice_id: str | None, tenant_id: str | None
) -> tuple[str | None, str | None]:
    """批量取知识库名与音色名（两个单行查询，避免把无关表 join 进来）。"""
    kb_name: str | None = None
    if kb_id:
        stmt = select(KnowledgeBase.name).where(KnowledgeBase.id == kb_id)
        if tenant_id is not None:
            stmt = stmt.where(KnowledgeBase.tenant_id == tenant_id)
        value = (await session.execute(stmt.limit(1))).scalar_one_or_none()
        kb_name = str(value) if value is not None else None

    voice_name: str | None = None
    if voice_id:
        stmt = select(VoiceProfile.name).where(VoiceProfile.id == voice_id)
        if tenant_id is not None:
            stmt = stmt.where(VoiceProfile.tenant_id == tenant_id)
        value = (await session.execute(stmt.limit(1))).scalar_one_or_none()
        voice_name = str(value) if value is not None else None

    return kb_name, voice_name


def _provider_code_or_fail(code: str) -> None:
    """校验供应商「已注册且已配置密钥」，否则 409。

    为什么用 ``PRODUCT_NOT_AUTHORIZED`` 这个码：它的默认语义正是
    「这个组合不被许可」。相比于新开一个供应商专用码，复用它让前端的
    「不可用配置」处理分支只写一处（契约也明确要求该码）。

    Raises:
        AppException: 未注册，或未配置密钥。
    """
    registry.ensure_default_registry()
    provider = registry.get(code)
    if provider is None:
        raise product_not_authorized(
            f"供应商「{code}」未注册，无法选定",
            details={"providerCode": code, "reason": "PROVIDER_NOT_REGISTERED", "ok": False},
        )
    if not provider.is_configured:
        raise product_not_authorized(
            f"供应商「{provider.name or code}」未配置密钥，选定后对话会安全失败（ADR-07）",
            details={"providerCode": code, "configured": False, "ok": False},
        )


def _wifi_role_rejection() -> AppException:
    """Wi-Fi 产品写角色的统一拒绝（409）。

    ``details`` 固定带 ``{networkType, reason}``，与 GET 的
    ``roleUnsupportedReason`` 是**同一句文案**：前端在两种入口下拿到的解释
    必须一致，否则用户会看到「配置页说不能改，但点一下又报了另一句话」。
    """
    return AppException(
        ErrorCode.INVALID_STATE_TRANSITION,
        WIFI_ROLE_UNSUPPORTED_REASON,
        details={
            "networkType": str(NetworkType.WIFI),
            "reason": WIFI_ROLE_UNSUPPORTED_REASON,
        },
    )


async def _assert_provider_available(session: AsyncSession, product: ClientProduct) -> str:
    """校验该产品当前有可用供应商（用于 ``status=ENABLED``）。

    Returns:
        实际解析到的供应商注册键。

    Raises:
        AppException: 四层解析全部未命中，或命中的供应商未配置密钥。
    """
    registry.ensure_default_registry()
    resolved = await registry.resolve_for_product(session, client_product_id=product.id)
    if resolved is None:
        raise product_not_authorized(
            "该产品未解析到可用的 AI 供应商，无法启用 AI 对话",
            details={"reason": "NO_PROVIDER_RESOLVED", "ok": False},
        )
    if not resolved.provider.is_configured:
        raise product_not_authorized(
            f"解析到的供应商「{resolved.provider.name or resolved.provider.code}」"
            "未配置密钥，无法启用 AI 对话",
            details={
                "providerCode": resolved.provider.code,
                "configured": False,
                "ok": False,
            },
        )
    return resolved.provider.code


# ---------------------------------------------------------------------------
# 一、AI 配置视图
# ---------------------------------------------------------------------------


def _default_safety() -> SafetySwitches:
    """未配置时的安全开关默认值（与 ``AiConfig`` 的列默认值保持一致）。"""
    return SafetySwitches(enabled=True, sensitive_words=True, llm_review=False)


async def build_ai_config_view(
    session: AsyncSession, product: ClientProduct, config: AiConfig | None
) -> AiConfigView:
    """组装 AI 配置视图（``config`` 为空时返回「默认视图」而不是 404）。

    为什么「没配过」要返回默认视图：产品刚建出来时配置页总得能打开，
    返回 404 会让前端把「尚未配置」渲染成「加载失败」，商户找不到入口去配。
    """
    registry.ensure_default_registry()
    resolved = await registry.resolve_for_product(session, client_product_id=product.id)
    provider_configured = bool(resolved and resolved.provider.is_configured)

    kb_name, voice_name = await _label_names(
        session,
        kb_id=config.knowledge_base_id if config else None,
        voice_id=config.voice_profile_id if config else None,
        tenant_id=product.tenant_id,
    )
    role_name = await _role_preset_name(
        session, product.tenant_id, config.role_preset_code if config else None
    )

    network = str(product.network_type)
    role_supported = network != str(NetworkType.WIFI)

    return AiConfigView(
        product_id=product.id,
        network_type=network,
        provider_code=resolved.provider.code if resolved else None,
        provider_name=resolved.provider.name if resolved else None,
        provider_configured=provider_configured,
        # 配置推导的健康态：GET 不做出站探测（真实探测见 P7 的 /ai/health）
        provider_health=("UP" if provider_configured else "DOWN") if resolved else None,
        role_preset_code=config.role_preset_code if config else None,
        role_preset_name=role_name,
        role_supported=role_supported,
        role_unsupported_reason=None if role_supported else WIFI_ROLE_UNSUPPORTED_REASON,
        knowledge_base_id=config.knowledge_base_id if config else None,
        knowledge_base_name=kb_name,
        voice_profile_id=config.voice_profile_id if config else None,
        voice_profile_name=voice_name,
        system_prompt=config.system_prompt if config else None,
        greeting=config.greeting if config else None,
        temperature=(
            config.temperature / 100 if config and config.temperature is not None else None
        ),
        max_tokens=config.max_tokens if config else None,
        status=config.status if config else str(EnableStatus.DISABLED),
        safety=(
            SafetySwitches(
                enabled=config.safety_enabled,
                sensitive_words=config.safety_sensitive_words,
                llm_review=config.safety_llm_review,
            )
            if config
            else _default_safety()
        ),
        updated_at=config.updated_at if config else None,
    )


async def get_ai_config_view(
    session: AsyncSession, auth: AuthContext, product_id: str
) -> AiConfigView:
    """取某产品的 AI 配置视图（产品不属于本租户 → 404）。"""
    product = await get_product(session, auth, product_id)
    config = await get_ai_config(session, product.id)
    return await build_ai_config_view(session, product, config)


async def _update_ai_config(
    session: AsyncSession,
    auth: AuthContext,
    product_id: str,
    *,
    values: dict[str, Any],
    request: Request | None,
) -> AiConfigView:
    """按 ``values`` 中**实际出现的键**部分更新 AI 配置（upsert）。

    Args:
        values: 只包含调用方显式提供的字段；值为 ``None`` 表示**清空该字段**。
            未出现的键一律不动——「没传」与「传了 null」必须区分，
            否则「只想改问候语」的请求会顺手清掉角色预设。

    Raises:
        AppException: 供应商未配置密钥 / 知识库或音色不属于本租户 /
            Wi-Fi 产品写角色 / 启用时无可用供应商。
    """
    product = await get_product(session, auth, product_id)
    config = await get_ai_config(session, product.id)
    created = config is None
    if config is None:
        config = AiConfig(
            id=new_id("ai_config"),
            tenant_id=product.tenant_id,
            client_product_id=product.id,
            status=str(EnableStatus.DISABLED),
        )
        session.add(config)

    changed: list[str] = []

    if "provider_code" in values:
        code = values["provider_code"]
        if code is not None:
            _provider_code_or_fail(str(code))
        config.provider_code = str(code) if code is not None else None
        changed.append("provider_code")

    if "role_preset_code" in values:
        code = values["role_preset_code"]
        if code is not None and str(product.network_type) == str(NetworkType.WIFI):
            # 与 PUT /ai-config/role 同一判据、同一文案：规则属于「字段」而不是「端点」
            raise _wifi_role_rejection()
        config.role_preset_code = str(code) if code is not None else None
        changed.append("role_preset_code")

    if "knowledge_base_id" in values:
        kb_id = values["knowledge_base_id"]
        if kb_id is not None:
            await _get_visible_kb(session, auth, str(kb_id))
        config.knowledge_base_id = str(kb_id) if kb_id is not None else None
        changed.append("knowledge_base_id")

    if "voice_profile_id" in values:
        voice_id = values["voice_profile_id"]
        if voice_id is not None:
            await _get_visible_voice(session, auth, str(voice_id))
        config.voice_profile_id = str(voice_id) if voice_id is not None else None
        changed.append("voice_profile_id")

    for field_name in ("system_prompt", "greeting"):
        if field_name in values:
            raw = values[field_name]
            setattr(config, field_name, str(raw) if raw is not None else None)
            changed.append(field_name)

    if "temperature" in values:
        raw_temp = values["temperature"]
        config.temperature = (
            int(round(float(raw_temp) * 100)) if raw_temp is not None else None
        )
        changed.append("temperature")

    if "max_tokens" in values:
        raw_tokens = values["max_tokens"]
        config.max_tokens = int(raw_tokens) if raw_tokens is not None else None
        changed.append("max_tokens")

    if "status" in values:
        raw_status = values["status"]
        config.status = str(raw_status) if raw_status is not None else str(EnableStatus.DISABLED)
        if config.status == str(EnableStatus.ENABLED):
            await _assert_provider_available(session, product)
        # 客户产品的 ai_enabled 是「是否已配置 AI」的展示位，与配置状态保持同步
        product.ai_enabled = config.status == str(EnableStatus.ENABLED)
        changed.append("status")

    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE if created else AuditAction.UPDATE,
        actor=auth,
        tenant_id=product.tenant_id,
        resource_type="ai_config",
        resource_id=config.id,
        summary=f"{'创建' if created else '更新'}客户产品「{product.name}」的 AI 配置",
        # 审计只记「改了哪些字段」，不记任何密钥（本模块本来也拿不到密钥）
        detail={"fields": changed, "networkType": product.network_type},
        request=request,
    )
    await session.commit()
    logger.info("已%s AI 配置：产品=%s 字段=%s", "创建" if created else "更新", product.code, changed)
    return await build_ai_config_view(session, product, config)


async def update_ai_config(
    session: AsyncSession,
    auth: AuthContext,
    product_id: str,
    payload: Any,
    *,
    request: Request | None = None,
) -> AiConfigView:
    """部分更新 AI 配置（``PUT /merchant/products/{id}/ai-config``）。

    用 ``model_fields_set`` 区分「没传」与「传了 null」——这是契约要求，
    也是避免静默清空字段的唯一可靠方式。
    """
    values = {name: getattr(payload, name) for name in payload.model_fields_set}
    return await _update_ai_config(session, auth, product_id, values=values, request=request)


async def update_prompt(
    session: AsyncSession, auth: AuthContext, product_id: str, payload: Any, *, request: Request | None = None
) -> AiConfigView:
    """更新提示词相关字段。"""
    values = {name: getattr(payload, name) for name in payload.model_fields_set}
    return await _update_ai_config(session, auth, product_id, values=values, request=request)


async def update_role(
    session: AsyncSession, auth: AuthContext, product_id: str, payload: Any, *, request: Request | None = None
) -> AiConfigView:
    """更新角色预设。

    ★ Wi-Fi 产品 → 409 ``INVALID_STATE_TRANSITION``，``details`` 带
    ``{networkType, reason}``（与 GET 的 ``roleUnsupportedReason`` 同一文案）。
    显式传 ``null``（清空）在 Wi-Fi 上**允许**：清空不会让平台「覆盖」厂商角色，
    拒绝它只会让前端为了清一个本就为空的值而报错。
    """
    product = await get_product(session, auth, product_id)
    code = payload.role_preset_code
    if (
        "role_preset_code" in payload.model_fields_set
        and code is not None
        and str(product.network_type) == str(NetworkType.WIFI)
    ):
        raise _wifi_role_rejection()
    values = {name: getattr(payload, name) for name in payload.model_fields_set}
    return await _update_ai_config(session, auth, product_id, values=values, request=request)


async def update_voice(
    session: AsyncSession, auth: AuthContext, product_id: str, payload: Any, *, request: Request | None = None
) -> AiConfigView:
    """更新音色档案。"""
    values = {name: getattr(payload, name) for name in payload.model_fields_set}
    return await _update_ai_config(session, auth, product_id, values=values, request=request)


async def update_safety(
    session: AsyncSession, auth: AuthContext, product_id: str, payload: Any, *, request: Request | None = None
) -> AiConfigView:
    """更新内容安全三开关。

    映射到 ``AiConfig.safety_enabled`` / ``safety_sensitive_words`` /
    ``safety_llm_review``；P8 的对话链路在 ``assistant.delta`` 之前读这三列，
    因此改完**下一次对话就生效**，无需重启或重建索引。
    """
    product = await get_product(session, auth, product_id)
    config = await get_ai_config(session, product.id)
    created = config is None
    if config is None:
        config = AiConfig(
            id=new_id("ai_config"),
            tenant_id=product.tenant_id,
            client_product_id=product.id,
            status=str(EnableStatus.DISABLED),
        )
        session.add(config)

    changed: list[str] = []
    mapping = {
        "enabled": "safety_enabled",
        "sensitive_words": "safety_sensitive_words",
        "llm_review": "safety_llm_review",
    }
    for api_name, column in mapping.items():
        if api_name not in payload.model_fields_set:
            continue
        value = getattr(payload, api_name)
        if value is None:
            continue
        setattr(config, column, bool(value))
        changed.append(column)

    await session.flush()
    await audit_service.record(
        session,
        action=AuditAction.CREATE if created else AuditAction.UPDATE,
        actor=auth,
        tenant_id=product.tenant_id,
        resource_type="ai_config",
        resource_id=config.id,
        summary=f"更新客户产品「{product.name}」的内容安全开关",
        detail={"fields": changed},
        request=request,
    )
    await session.commit()
    return await build_ai_config_view(session, product, config)


# ---------------------------------------------------------------------------
# 二、供应商 / 角色 / 音色清单
# ---------------------------------------------------------------------------


async def list_providers(session: AsyncSession) -> list[MerchantProviderItem]:
    """已注册供应商清单（**不含任何密钥或密钥片段**）。

    ``isDefault`` 与 ``status`` 优先取 ``ai_providers`` 表的行（平台可能显式
    标记默认供应商或停用某个），表里没有该行时回退为「已注册即 ACTIVE +
    是否为 ``AI_DEFAULT_PROVIDER``」——本阶段种子数据不建 ai_providers 行，
    没有这层回退清单会整片空白。
    """
    registry.ensure_default_registry()
    rows = {
        str(row.code): row
        for row in (await session.execute(select(AiProvider))).scalars().all()
    }
    items: list[MerchantProviderItem] = []
    for descriptor in registry.describe_providers():
        row = rows.get(descriptor.code)
        items.append(
            MerchantProviderItem(
                code=descriptor.code,
                name=descriptor.name,
                kind=descriptor.kind,
                vendor=descriptor.vendor,
                is_default=(
                    bool(row.is_default)
                    if row is not None
                    else descriptor.code == registry.default_provider_code()
                ),
                configured=descriptor.configured,
                status=str(row.status) if row is not None else "ACTIVE",
            )
        )
    return items


async def list_role_presets(session: AsyncSession, auth: AuthContext) -> list[RolePresetItem]:
    """角色预设清单（平台内置 + 本租户，只列启用的）。"""
    tenant_id = scoped_value(auth)
    stmt = select(RolePreset).where(RolePreset.status == str(EnableStatus.ENABLED))
    if tenant_id is not None:
        stmt = stmt.where(
            or_(RolePreset.tenant_id.is_(None), RolePreset.tenant_id == tenant_id)
        )
    stmt = stmt.order_by(RolePreset.is_builtin.desc(), RolePreset.code.asc())
    rows = list((await session.execute(stmt)).scalars().all())
    return [
        RolePresetItem(
            code=row.code,
            name=row.name,
            persona=row.persona,
            greeting=row.greeting,
            age_group=row.age_group,
            tone=row.tone,
            is_builtin=row.is_builtin,
        )
        for row in rows
    ]


async def list_voice_profiles(session: AsyncSession, auth: AuthContext) -> list[VoiceProfileItem]:
    """本租户的音色档案清单。"""
    stmt = scoped(select(VoiceProfile), VoiceProfile, auth).order_by(VoiceProfile.name.asc())
    rows = list((await session.execute(stmt)).scalars().all())
    return [
        VoiceProfileItem(
            id=row.id,
            name=row.name,
            provider_code=row.provider_code,
            language=row.language,
            status=row.status,
            train_status=row.train_status,
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# 三、知识库
# ---------------------------------------------------------------------------


async def _kb_file_counts(
    session: AsyncSession, kb_ids: list[str]
) -> dict[str, tuple[int, int]]:
    """统计每个知识库的 ``(文件数, 已解析文件数)``（两次分组查询，避免 N+1）。

    用两条查询而不是 ``CASE WHEN`` 聚合：本阶段 SQLite 与 PostgreSQL 都要能跑，
    而「统计已解析数」在两条查询里各自都是最直白的 ``WHERE``，可读性也更好。
    """
    if not kb_ids:
        return {}
    total_stmt = (
        select(KbFile.knowledge_base_id, func.count())
        .where(KbFile.knowledge_base_id.in_(kb_ids))
        .group_by(KbFile.knowledge_base_id)
    )
    parsed_stmt = (
        select(KbFile.knowledge_base_id, func.count())
        .where(
            KbFile.knowledge_base_id.in_(kb_ids),
            KbFile.status == str(KbFileStatus.PARSED),
        )
        .group_by(KbFile.knowledge_base_id)
    )
    parsed = {
        str(row[0]): int(row[1]) for row in (await session.execute(parsed_stmt)).all()
    }
    return {
        str(row[0]): (int(row[1]), parsed.get(str(row[0]), 0))
        for row in (await session.execute(total_stmt)).all()
    }


def _kb_to_response(kb: KnowledgeBase, *, file_count: int = 0, parsed_count: int = 0) -> KbResponse:
    """ORM → 知识库响应。"""
    return KbResponse(
        id=kb.id,
        name=kb.name,
        description=kb.description,
        status=kb.status,
        doc_count=kb.doc_count,
        chunk_count=kb.chunk_count,
        client_product_id=kb.client_product_id,
        file_count=file_count,
        parsed_file_count=parsed_count,
        updated_at=kb.updated_at,
    )


def kb_file_to_response(kb_file: KbFile) -> KbFileResponse:
    """ORM → 知识库文件响应。

    ``parsedAt`` 由 ``updated_at`` 派生：``kb_files`` 表刻意没有单独的
    ``parsed_at`` 列（本阶段不再改数据层），而「解析完成时刻」与「该行最后
    更新时刻」在本阶段是同一次写入。状态不是 ``PARSED`` 时返回 ``null``，
    避免把「上次失败的时刻」当成解析成功时间展示。
    """
    parsed = kb_file.status == str(KbFileStatus.PARSED)
    return KbFileResponse(
        id=kb_file.id,
        filename=kb_file.filename,
        content_type=kb_file.content_type,
        size_bytes=kb_file.size_bytes,
        status=kb_file.status,
        status_label=KB_FILE_STATUS_LABELS.get(kb_file.status, kb_file.status),
        chunk_count=kb_file.chunk_count,
        error_message=kb_file.error_message,
        created_at=kb_file.created_at,
        parsed_at=kb_file.updated_at if parsed else None,
    )


async def _get_visible_kb(session: AsyncSession, auth: AuthContext, kb_id: str) -> KnowledgeBase:
    """取知识库并校验归属（经 ``scoped`` 收口，越权与不存在同为 404）。

    Raises:
        AppException: 知识库不存在或不属于当前租户。
    """
    # 显式标注类型：``scoped()`` 返回 ``Select[Any]``，不标注会触发 mypy 的
    # no-any-return（返回值看起来是 Any，而声明是具体模型）。
    kb: KnowledgeBase | None = (
        await session.execute(
            scoped(select(KnowledgeBase), KnowledgeBase, auth).where(KnowledgeBase.id == kb_id)
        )
    ).scalar_one_or_none()
    if kb is None:
        raise not_found("知识库不存在")
    return kb


async def _get_visible_voice(
    session: AsyncSession, auth: AuthContext, voice_id: str
) -> VoiceProfile:
    """取音色档案并校验归属。

    Raises:
        AppException: 音色不存在或不属于当前租户。
    """
    voice: VoiceProfile | None = (
        await session.execute(
            scoped(select(VoiceProfile), VoiceProfile, auth).where(VoiceProfile.id == voice_id)
        )
    ).scalar_one_or_none()
    if voice is None:
        raise not_found("音色档案不存在")
    return voice


async def list_knowledge_bases(
    session: AsyncSession,
    auth: AuthContext,
    *,
    keyword: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[KbResponse], int]:
    """分页查询本租户的知识库。"""
    conditions: list[ColumnElement[bool]] = []
    if keyword:
        pattern = f"%{keyword.strip()}%"
        conditions.append(KnowledgeBase.name.like(pattern))

    total = int(
        (
            await session.execute(
                scoped(
                    select(func.count()).select_from(KnowledgeBase), KnowledgeBase, auth
                ).where(*conditions)
            )
        ).scalar_one()
    )
    stmt = (
        scoped(select(KnowledgeBase), KnowledgeBase, auth)
        .where(*conditions)
        .order_by(KnowledgeBase.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    counts = await _kb_file_counts(session, [row.id for row in rows])
    return (
        [
            _kb_to_response(row, file_count=counts.get(row.id, (0, 0))[0], parsed_count=counts.get(row.id, (0, 0))[1])
            for row in rows
        ],
        total,
    )


async def create_knowledge_base(
    session: AsyncSession,
    auth: AuthContext,
    *,
    name: str,
    description: str | None = None,
    client_product_id: str | None = None,
    request: Request | None = None,
) -> KbResponse:
    """创建知识库（可选挂到某个本租户的客户产品）。

    Raises:
        AppException: 指定的客户产品不存在或不属于本租户。
    """
    tenant_id = auth.require_tenant_id()
    if client_product_id:
        # 复用产品可见性校验：挂到别人的产品上等于越权写
        await get_product(session, auth, client_product_id)

    kb = KnowledgeBase(
        id=new_id("knowledge_base"),
        tenant_id=tenant_id,
        client_product_id=client_product_id,
        name=name,
        description=description,
        status=str(EnableStatus.ENABLED),
    )
    session.add(kb)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        actor=auth,
        tenant_id=tenant_id,
        resource_type="knowledge_base",
        resource_id=kb.id,
        summary=f"创建知识库「{kb.name}」",
        detail={"clientProductId": client_product_id},
        request=request,
    )
    await session.commit()
    return _kb_to_response(kb)


async def get_knowledge_base_detail(
    session: AsyncSession, auth: AuthContext, kb_id: str
) -> KbDetailResponse:
    """知识库详情（含文件清单，按上传时间倒序）。"""
    kb = await _get_visible_kb(session, auth, kb_id)
    files = list(
        (
            await session.execute(
                select(KbFile)
                .where(KbFile.knowledge_base_id == kb.id)
                .order_by(KbFile.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    parsed_count = sum(1 for item in files if item.status == str(KbFileStatus.PARSED))
    base = _kb_to_response(kb, file_count=len(files), parsed_count=parsed_count)
    return KbDetailResponse(**base.model_dump(), files=[kb_file_to_response(item) for item in files])


async def update_knowledge_base(
    session: AsyncSession,
    auth: AuthContext,
    kb_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
    client_product_id: str | None = None,
    fields_set: set[str] | None = None,
    request: Request | None = None,
) -> KbResponse:
    """部分更新知识库（``fields_set`` 里没有的字段不动）。

    ``clientProductId`` 显式传 ``null`` 表示解除产品关联——这是有意允许的：
    知识库可以在产品之间复用，解除关联不应要求删库重建。
    """
    kb = await _get_visible_kb(session, auth, kb_id)
    present = fields_set if fields_set is not None else set()
    changed: list[str] = []

    if "name" in present and name is not None and kb.name != name:
        kb.name = name
        changed.append("name")
    if "description" in present:
        kb.description = description
        changed.append("description")
    if "status" in present and status is not None and kb.status != status:
        kb.status = status
        changed.append("status")
    if "client_product_id" in present:
        if client_product_id is not None:
            await get_product(session, auth, client_product_id)
        kb.client_product_id = client_product_id
        changed.append("client_product_id")

    if not changed:
        return _kb_to_response(kb)

    await audit_service.record(
        session,
        action=AuditAction.UPDATE,
        actor=auth,
        tenant_id=kb.tenant_id,
        resource_type="knowledge_base",
        resource_id=kb.id,
        summary=f"更新知识库「{kb.name}」",
        detail={"fields": changed},
        request=request,
    )
    await session.commit()
    return _kb_to_response(kb)


def _delete_storage_object(key: str | None) -> None:
    """尽力删除存储对象：删不掉只记 warning，不阻断业务删除。

    存储是**外部依赖**，它不可用时不该让「删知识库」这种本地事实失败；
    残留对象可以由对账脚本清理，而「删不掉记录」会立刻变成数据错误
    （用户看到文件还在、却打不开）。
    """
    if not key:
        return
    try:
        get_storage().delete(key)
    except AppException as exc:  # 存储不可用或 key 非法
        logger.warning("删除存储对象失败（已跳过）：key=%s err=%s", key, exc.message)


async def delete_knowledge_base(
    session: AsyncSession, auth: AuthContext, kb_id: str, *, request: Request | None = None
) -> None:
    """删除知识库（先校验引用，再级联删文件与存储对象）。

    ★ 被任何 ``ai_configs.knowledge_base_id`` 引用时返回 ``CASCADE_CONFLICT``，
    ``details`` 带 ``{products: [{id, name}]}``——直接删会让对话配置指向一个
    不存在的库（外键是 ``SET NULL``，配置会**静默**失去知识库）。
    让引用方显式处理，比让配置悄悄失效安全。

    Raises:
        AppException: 知识库不存在 / 仍被产品引用。
    """
    kb = await _get_visible_kb(session, auth, kb_id)

    referencing = list(
        (
            await session.execute(
                select(ClientProduct.id, ClientProduct.name)
                .join(AiConfig, AiConfig.client_product_id == ClientProduct.id)
                .where(AiConfig.knowledge_base_id == kb.id)
            )
        ).all()
    )
    if referencing:
        products = [{"id": str(row[0]), "name": str(row[1])} for row in referencing]
        raise cascade_conflict(
            f"知识库「{kb.name}」仍被 {len(products)} 个客户产品的 AI 配置引用，无法删除。"
            "请先在 AI 配置里解除引用。",
            details={"products": products},
        )

    files = list(
        (
            await session.execute(select(KbFile).where(KbFile.knowledge_base_id == kb.id))
        )
        .scalars()
        .all()
    )
    for item in files:
        _delete_storage_object(item.storage_path)

    await session.delete(kb)
    await audit_service.record(
        session,
        action=AuditAction.DELETE,
        actor=auth,
        tenant_id=kb.tenant_id,
        resource_type="knowledge_base",
        resource_id=kb.id,
        summary=f"删除知识库「{kb.name}」（含 {len(files)} 个文件）",
        detail={"fileCount": len(files)},
        request=request,
    )
    await session.commit()
    logger.warning("已删除知识库 %s（%d 个文件）", kb.name, len(files))


async def list_kb_files(
    session: AsyncSession,
    auth: AuthContext,
    kb_id: str,
    *,
    status: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[KbFileResponse], int]:
    """分页查询知识库文件。"""
    kb = await _get_visible_kb(session, auth, kb_id)
    conditions: list[ColumnElement[bool]] = [KbFile.knowledge_base_id == kb.id]
    if status:
        conditions.append(KbFile.status == status)

    total = int(
        (
            await session.execute(
                select(func.count()).select_from(KbFile).where(*conditions)
            )
        ).scalar_one()
    )
    rows = list(
        (
            await session.execute(
                select(KbFile)
                .where(*conditions)
                .order_by(KbFile.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [kb_file_to_response(item) for item in rows], total


async def upload_kb_file(
    session: AsyncSession,
    auth: AuthContext,
    kb_id: str,
    *,
    filename: str,
    content_type: str | None,
    content: bytes,
    request: Request | None = None,
) -> KbFileResponse:
    """上传知识库文件（落盘 + 落库，``status=PENDING``）。

    Raises:
        AppException: 扩展名不在白名单 / 超过大小上限 / 同库内 checksum 重复。
    """
    kb = await _get_visible_kb(session, auth, kb_id)

    extension = file_extension(filename)
    if extension not in allowed_kb_extensions():
        allowed = "、".join(sorted(allowed_kb_extensions())) or "（未配置）"
        raise validation_error(
            f"不支持的文件格式「.{extension or '未知'}」，允许的扩展名：{allowed}",
            details={"field": "file", "allowed": sorted(allowed_kb_extensions())},
        )
    if len(content) > settings.KB_FILE_MAX_BYTES:
        raise validation_error(
            f"文件超过大小上限（{settings.KB_FILE_MAX_BYTES // (1024 * 1024)} MB）",
            details={"field": "file", "maxBytes": settings.KB_FILE_MAX_BYTES},
        )

    checksum = sha256_hex(content)
    duplicate = (
        await session.execute(
            select(KbFile).where(
                KbFile.knowledge_base_id == kb.id, KbFile.checksum == checksum
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        # 选「显式 409」而不是「静默复用」：响应契约里没有「这是既有文件」的
        # 字段，静默复用会让前端分不清「刚传成功」与「早就有」；给出
        # existingFileId 让客户端能直接定位那个文件。
        #
        # 错误码用 VALIDATION_ERROR（项目内没有「文件已存在」专用码），但状态码
        # 显式指定 409：请求本身合法，只是与既有资源撞了去重键——属于冲突而非
        # 参数错误，契约亦明确要求 409。与 OTA 的版本重复同一口径。
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            "该文件已存在（同一知识库内内容完全相同的文件）",
            details={"existingFileId": duplicate.id, "filename": duplicate.filename},
            status_code=409,
        )

    file_id = new_id("kb_file")
    key = build_object_key(
        prefix="kb", owner=kb.tenant_id, object_id=file_id, filename=filename
    )
    stored = get_storage().save(key, content)

    kb_file = KbFile(
        id=file_id,
        knowledge_base_id=kb.id,
        tenant_id=kb.tenant_id,
        filename=filename,
        content_type=content_type,
        size_bytes=stored.size,
        storage_path=stored.key,
        checksum=stored.checksum,
        status=str(KbFileStatus.PENDING),
        chunk_count=0,
    )
    session.add(kb_file)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        actor=auth,
        tenant_id=kb.tenant_id,
        resource_type="kb_file",
        resource_id=kb_file.id,
        summary=f"上传知识库文件「{filename}」（{stored.size} 字节）",
        detail={"knowledgeBaseId": kb.id, "checksum": stored.checksum},
        request=request,
    )
    await session.commit()
    logger.info("已上传知识库文件 %s（%d 字节）→ %s", filename, stored.size, key)
    return kb_file_to_response(kb_file)


async def delete_kb_file(
    session: AsyncSession, auth: AuthContext, kb_id: str, file_id: str, *, request: Request | None = None
) -> None:
    """删除知识库文件（记录 + 存储对象；对象不存在也视为成功）。

    Raises:
        AppException: 文件不存在或不属于该知识库 / 该租户。
    """
    kb = await _get_visible_kb(session, auth, kb_id)
    kb_file = (
        await session.execute(
            select(KbFile).where(KbFile.id == file_id, KbFile.knowledge_base_id == kb.id)
        )
    ).scalar_one_or_none()
    if kb_file is None:
        raise not_found("知识库文件不存在")

    _delete_storage_object(kb_file.storage_path)
    await session.delete(kb_file)
    await _refresh_kb_summary(session, kb)
    await audit_service.record(
        session,
        action=AuditAction.DELETE,
        actor=auth,
        tenant_id=kb.tenant_id,
        resource_type="kb_file",
        resource_id=file_id,
        summary=f"删除知识库文件「{kb_file.filename}」",
        detail={"knowledgeBaseId": kb.id},
        request=request,
    )
    await session.commit()


async def _refresh_kb_summary(session: AsyncSession, kb: KnowledgeBase) -> None:
    """按**实际**已解析文件重算 ``doc_count`` / ``chunk_count``。

    为什么重算而不是自增：删除文件、重复解析、解析失败都会让「加一减一」
    的写法漂移，而这两个数字是运营判断「知识库有没有真的生效」的依据，
    漂移的数字比没有数字更糟。重算只是一次分组查询，代价可接受。
    """
    stmt = (
        select(func.count(), func.coalesce(func.sum(KbFile.chunk_count), 0))
        .select_from(KbFile)
        .where(
            KbFile.knowledge_base_id == kb.id,
            KbFile.status == str(KbFileStatus.PARSED),
        )
    )
    row = (await session.execute(stmt)).one()
    kb.doc_count = int(row[0])
    kb.chunk_count = int(row[1])


async def parse_kb_file(
    session: AsyncSession, auth: AuthContext, kb_id: str, file_id: str, *, request: Request | None = None
) -> KbFileResponse:
    """解析知识库文件（**诚实实现**：能解析就真解析，不能就明确失败）。

    * ``txt/md/csv/json`` → 从对象存储读文本、按空行/段落切块、写回
      ``chunk_count``、``status=PARSED``
    * ``pdf/docx`` → ``status=FAILED`` + 明确的「需要 PDF/DOCX 解析器」说明。
      **绝不假装解析成功**：检索查不到却显示「已就绪」是这类系统最难排查的
      沉默故障（:class:`KbFileStatus` 把 PENDING 与 PARSED 分开，正是为它留的）
    * 读取失败（对象被清理 / 存储不可用）→ ``FAILED`` + 原因，不让异常冒到接口层

    Raises:
        AppException: 文件不存在或不属于该知识库 / 该租户。
    """
    kb = await _get_visible_kb(session, auth, kb_id)
    kb_file = (
        await session.execute(
            select(KbFile).where(KbFile.id == file_id, KbFile.knowledge_base_id == kb.id)
        )
    ).scalar_one_or_none()
    if kb_file is None:
        raise not_found("知识库文件不存在")

    extension = file_extension(kb_file.filename)
    if extension not in KB_TEXT_EXTENSIONS:
        kb_file.status = str(KbFileStatus.FAILED)
        kb_file.chunk_count = 0
        kb_file.error_message = KB_UNPARSABLE_MESSAGE
    else:
        try:
            raw = get_storage().read(kb_file.storage_path or "")
        except AppException as exc:
            kb_file.status = str(KbFileStatus.FAILED)
            kb_file.chunk_count = 0
            kb_file.error_message = f"读取文件失败：{exc.message}"
        else:
            blocks = split_text_blocks(raw.decode("utf-8", errors="replace"))
            if not blocks:
                kb_file.status = str(KbFileStatus.FAILED)
                kb_file.chunk_count = 0
                kb_file.error_message = KB_EMPTY_MESSAGE
            else:
                kb_file.status = str(KbFileStatus.PARSED)
                kb_file.chunk_count = len(blocks)
                kb_file.error_message = None

    await session.flush()
    await _refresh_kb_summary(session, kb)

    await audit_service.record(
        session,
        action=AuditAction.UPDATE,
        actor=auth,
        tenant_id=kb.tenant_id,
        resource_type="kb_file",
        resource_id=kb_file.id,
        summary=f"解析知识库文件「{kb_file.filename}」：{kb_file.status}",
        detail={"chunkCount": kb_file.chunk_count, "error": kb_file.error_message},
        success=kb_file.status == str(KbFileStatus.PARSED),
        request=request,
    )
    await session.commit()
    return kb_file_to_response(kb_file)


# ---------------------------------------------------------------------------
# 四、内容库（只读）
# ---------------------------------------------------------------------------


def content_to_response(item: ContentItem) -> ContentItemResponse:
    """ORM → 内容库条目响应。"""
    return ContentItemResponse(
        id=item.id,
        type=item.type,
        type_label=CONTENT_TYPE_LABELS.get(item.type, item.type),
        title=item.title,
        description=item.description,
        age_group=item.age_group,
        duration_seconds=item.duration_seconds,
        status=item.status,
        hit_count=item.hit_count,
        scope="TENANT" if item.tenant_id else "PLATFORM",
    )


async def list_content_items(
    session: AsyncSession,
    auth: AuthContext,
    *,
    item_type: str | None = None,
    keyword: str | None = None,
    scope: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[ContentItemResponse], int]:
    """内容库列表。

    可见性走 :func:`app.db.scope.scoped_value` 构造的并集条件
    （平台公共 ``tenant_id IS NULL`` + 本租户），理由见模块 docstring：
    ``content_items`` 的 ``tenant_id`` 可空就是为了表达「公共内容」，
    用 ``scoped`` 的等值过滤会把公共内容整批挡掉。

    ``scope``（``PLATFORM`` / ``TENANT``）仅平台端使用：平台运营需要能
    分开看「平台公共内容」与「租户自建内容」。
    """
    conditions: list[ColumnElement[bool]] = []
    tenant_id = scoped_value(auth)

    if auth.is_platform:
        if scope == "PLATFORM":
            conditions.append(ContentItem.tenant_id.is_(None))
        elif scope == "TENANT":
            conditions.append(ContentItem.tenant_id.isnot(None))
    elif tenant_id is not None:
        conditions.append(
            or_(ContentItem.tenant_id.is_(None), ContentItem.tenant_id == tenant_id)
        )

    if item_type:
        conditions.append(ContentItem.type == item_type)
    if keyword:
        pattern = f"%{keyword.strip()}%"
        conditions.append(
            ContentItem.title.like(pattern) | ContentItem.description.like(pattern)
        )

    total = int(
        (
            await session.execute(
                select(func.count()).select_from(ContentItem).where(*conditions)
            )
        ).scalar_one()
    )
    rows = list(
        (
            await session.execute(
                select(ContentItem)
                .where(*conditions)
                .order_by(ContentItem.sort_order.asc(), ContentItem.title.asc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [content_to_response(item) for item in rows], total


# ---------------------------------------------------------------------------
# 五、我的产品（P9 摘要视图）
# ---------------------------------------------------------------------------


async def build_product_ops_detail(
    session: AsyncSession, auth: AuthContext, product_id: str
) -> MerchantProductOpsResponse:
    """组装商户端「我的产品」详情（设备计数 + 交互总数 + AI 配置摘要）。

    计数一律走真实 ``COUNT``（ADR/P-07 口径），不做任何系数还原。

    Raises:
        AppException: 产品不存在或不属于本租户。
    """
    product = await get_product(session, auth, product_id)

    template_name = (
        await session.execute(
            select(ProductTemplate.name).where(ProductTemplate.id == product.template_id)
        )
    ).scalar_one_or_none()

    device_conditions: list[ColumnElement[bool]] = [
        Device.client_product_id == product.id,
        Device.asset_status != str(AssetStatus.RETIRED),
    ]
    device_count = int(
        (
            await session.execute(
                scoped(select(func.count()).select_from(Device), Device, auth).where(
                    *device_conditions
                )
            )
        ).scalar_one()
    )
    activated_count = int(
        (
            await session.execute(
                scoped(select(func.count()).select_from(Device), Device, auth).where(
                    *device_conditions,
                    Device.activation_status == str(ActivationStatus.ACTIVATED),
                )
            )
        ).scalar_one()
    )

    interaction_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(DialogueMessage)
                .join(DialogueSession, DialogueMessage.session_id == DialogueSession.id)
                .where(
                    DialogueSession.client_product_id == product.id,
                    DialogueMessage.role == str(MessageRole.USER),
                )
            )
        ).scalar_one()
    )

    config = await get_ai_config(session, product.id)
    registry.ensure_default_registry()
    resolved = await registry.resolve_for_product(session, client_product_id=product.id)
    kb_name, _ = await _label_names(
        session,
        kb_id=config.knowledge_base_id if config else None,
        voice_id=None,
        tenant_id=product.tenant_id,
    )
    summary = AiConfigSummary(
        provider_code=resolved.provider.code if resolved else None,
        provider_name=resolved.provider.name if resolved else None,
        provider_configured=bool(resolved and resolved.provider.is_configured),
        safety_enabled=config.safety_enabled if config else True,
        role_preset_code=config.role_preset_code if config else None,
        knowledge_base_name=kb_name,
        status=config.status if config else str(EnableStatus.DISABLED),
    )

    return MerchantProductOpsResponse(
        id=product.id,
        tenant_id=product.tenant_id,
        code=product.code,
        name=product.name,
        network_type=product.network_type,
        network_type_label=NETWORK_TYPE_LABELS.get(
            str(product.network_type), str(product.network_type)
        ),
        template_name=str(template_name) if template_name is not None else None,
        firmware_version=product.firmware_version,
        status=product.status,
        device_count=device_count,
        activated_device_count=activated_count,
        total_interactions=interaction_count,
        ai_config_summary=summary,
        remark=product.remark,
        created_at=product.created_at,
        updated_at=product.updated_at,
    )


async def list_products_ops(
    session: AsyncSession,
    auth: AuthContext,
    *,
    offset: int = 0,
    limit: int = 20,
    keyword: str | None = None,
) -> tuple[list[MerchantProductOpsResponse], int]:
    """商户端「我的产品」**列表**（P9 版：每行都带设备计数与 AI 摘要）。

    为什么列表也要带计数，而不是让前端逐行回查详情
    ----------------------------------------------
    「我的产品」是商户最常打开的一页，一行一个产品。若列表只给基础字段、
    详情才给计数，前端要么显示空列（用户以为功能没做完），要么对每一行发一次
    详情请求——20 行就是 21 个请求，而这是个会被反复打开的页面。

    代价与取舍：计数用**批量分组查询**解决（一次 `GROUP BY client_product_id`
    拿回整页的计数），而供应商解析仍逐产品调用 ``registry.resolve_for_product``。
    后者没有批量化，因为供应商解析的**四层优先级**（客户产品 → 模板厂商 →
    平台默认 → 配置兜底）必须只有一处实现——在列表里另写一遍等价逻辑，
    迟早会与注册表分叉。解析成本是常数级小查询，而页大小有上限
    （``pagination.MAX_PAGE_SIZE``），因此这里接受这个（有界的）N 次查询。

    Args:
        session: 数据库会话。
        auth: 商户认证上下文（经 ``scoped`` 收口，天然只看本租户）。
        offset: 分页偏移。
        limit: 每页条数。
        keyword: 名称 / 编码关键字。

    Returns:
        ``(列表项, 总数)``；两者都只覆盖当前租户。
    """
    conditions: list[ColumnElement[bool]] = []
    if keyword:
        needle = f"%{keyword.strip()}%"
        conditions.append(or_(ClientProduct.name.like(needle), ClientProduct.code.like(needle)))

    total = int(
        (
            await session.execute(
                scoped(select(func.count()).select_from(ClientProduct), ClientProduct, auth).where(
                    *conditions
                )
            )
        ).scalar_one()
    )
    products = list(
        (
            await session.execute(
                scoped(select(ClientProduct), ClientProduct, auth)
                .where(*conditions)
                .order_by(ClientProduct.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not products:
        return [], total

    product_ids = [item.id for item in products]

    # ---- 批量取模板名 ----
    template_ids = {item.template_id for item in products if item.template_id}
    template_names: dict[str, str] = {}
    if template_ids:
        rows = await session.execute(
            select(ProductTemplate.id, ProductTemplate.name).where(
                ProductTemplate.id.in_(template_ids)
            )
        )
        template_names = {str(row[0]): str(row[1]) for row in rows.all()}

    # ---- 批量取设备计数（一次 GROUP BY，避免逐产品查询） ----
    device_totals: dict[str, int] = {}
    activated_totals: dict[str, int] = {}
    skipped_retired = Device.asset_status != str(AssetStatus.RETIRED)
    rows = await session.execute(
        select(Device.client_product_id, func.count())
        .where(Device.client_product_id.in_(product_ids), skipped_retired)
        .group_by(Device.client_product_id)
    )
    device_totals = {str(row[0]): int(row[1]) for row in rows.all()}
    rows = await session.execute(
        select(Device.client_product_id, func.count())
        .where(
            Device.client_product_id.in_(product_ids),
            skipped_retired,
            Device.activation_status == str(ActivationStatus.ACTIVATED),
        )
        .group_by(Device.client_product_id)
    )
    activated_totals = {str(row[0]): int(row[1]) for row in rows.all()}

    # ---- 批量取交互数（按会话所属产品分组） ----
    interaction_totals: dict[str, int] = {}
    rows = await session.execute(
        select(DialogueSession.client_product_id, func.count())
        .select_from(DialogueMessage)
        .join(DialogueSession, DialogueMessage.session_id == DialogueSession.id)
        .where(
            DialogueSession.client_product_id.in_(product_ids),
            DialogueMessage.role == str(MessageRole.USER),
        )
        .group_by(DialogueSession.client_product_id)
    )
    interaction_totals = {str(row[0]): int(row[1]) for row in rows.all() if row[0] is not None}

    # ---- 批量取 AI 配置 ----
    configs: dict[str, AiConfig] = {}
    rows = await session.execute(
        select(AiConfig).where(AiConfig.client_product_id.in_(product_ids))
    )
    configs = {item.client_product_id: item for item in rows.scalars().all()}

    # ---- 批量取知识库名 ----
    kb_ids = {cfg.knowledge_base_id for cfg in configs.values() if cfg.knowledge_base_id}
    kb_names: dict[str, str] = {}
    if kb_ids:
        rows = await session.execute(
            select(KnowledgeBase.id, KnowledgeBase.name).where(KnowledgeBase.id.in_(kb_ids))
        )
        kb_names = {str(row[0]): str(row[1]) for row in rows.all()}

    registry.ensure_default_registry()
    items: list[MerchantProductOpsResponse] = []
    for product in products:
        config = configs.get(product.id)
        resolved = await registry.resolve_for_product(session, client_product_id=product.id)
        items.append(
            MerchantProductOpsResponse(
                id=product.id,
                tenant_id=product.tenant_id,
                code=product.code,
                name=product.name,
                network_type=product.network_type,
                network_type_label=NETWORK_TYPE_LABELS.get(
                    str(product.network_type), str(product.network_type)
                ),
                template_name=template_names.get(product.template_id or ""),
                firmware_version=product.firmware_version,
                status=product.status,
                device_count=device_totals.get(product.id, 0),
                activated_device_count=activated_totals.get(product.id, 0),
                total_interactions=interaction_totals.get(product.id, 0),
                ai_config_summary=AiConfigSummary(
                    provider_code=resolved.provider.code if resolved else None,
                    provider_name=resolved.provider.name if resolved else None,
                    provider_configured=bool(resolved and resolved.provider.is_configured),
                    safety_enabled=config.safety_enabled if config else True,
                    role_preset_code=config.role_preset_code if config else None,
                    knowledge_base_name=(
                        kb_names.get(config.knowledge_base_id)
                        if config and config.knowledge_base_id
                        else None
                    ),
                    status=config.status if config else str(EnableStatus.DISABLED),
                ),
                remark=product.remark,
                created_at=product.created_at,
                updated_at=product.updated_at,
            )
        )
    return items, total


__all__ = [
    "KB_TEXT_EXTENSIONS",
    "KB_UNPARSABLE_MESSAGE",
    "NETWORK_TYPE_LABELS",
    "WIFI_ROLE_UNSUPPORTED_REASON",
    "allowed_kb_extensions",
    "build_ai_config_view",
    "list_products_ops",
    "build_product_ops_detail",
    "content_to_response",
    "create_knowledge_base",
    "delete_kb_file",
    "delete_knowledge_base",
    "file_extension",
    "get_ai_config",
    "get_ai_config_view",
    "get_knowledge_base_detail",
    "get_product",
    "kb_file_to_response",
    "list_content_items",
    "list_kb_files",
    "list_knowledge_bases",
    "list_providers",
    "list_role_presets",
    "list_voice_profiles",
    "parse_kb_file",
    "split_text_blocks",
    "update_ai_config",
    "update_knowledge_base",
    "update_prompt",
    "update_role",
    "update_safety",
    "update_voice",
    "upload_kb_file",
]
