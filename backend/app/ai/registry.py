"""供应商注册表与解析顺序。

解析顺序（四层，先命中先返回）
------------------------------

    ① client_product   ``ai_configs.provider_code``     产品级显式指定（最具体）
    ② template_vendor  ``template.cloud_provider.vendor`` 模板绑定的云厂商
    ③ provider_default ``ai_providers.is_default``      平台标记的默认供应商
    ④ platform_default ``AI_DEFAULT_PROVIDER``          配置文件兜底（最泛化）

为什么是这个顺序
----------------
「最具体的声明优先」是配置系统的通行原则，落到本项目的具体理由是：

* 同一租户可能同时卖多款玩具，其中一款要用百度（客户指定），
  其余用平台默认——若租户级覆盖优先，就没法只改一款产品；
* 模板绑定了云厂商（如 JoyInside Wi-Fi），说明该产品**在设计上**就走这条链路，
  因此它的优先级高于平台默认供应商，但低于管理员对该产品的显式覆盖。

无数据库也能工作
----------------
:func:`resolve_provider` 是**纯函数**：四层候选值由调用方传入，
不碰数据库。值 → 候选值的读取（SQL）收在 :func:`resolve_for_product` 里，
因此单元测试可以只验证顺序本身，不必搭数据库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.ai.base import AIProvider, ProviderDescriptor
from app.ai.mock import build_mock_provider
from app.core.config import settings
from app.core.errors import AppException, ErrorCode
from app.core.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 解析层级常量
# ---------------------------------------------------------------------------

LAYER_REQUEST = "request"
LAYER_CLIENT_PRODUCT = "client_product"
LAYER_TEMPLATE_VENDOR = "template_vendor"
LAYER_PROVIDER_DEFAULT = "provider_default"
LAYER_PLATFORM_DEFAULT = "platform_default"
LAYER_NONE = "none"

#: 云服务商厂商 → 供应商注册键。
#: 为什么需要这层映射？``CloudVendor`` 是「设备云厂商」的词汇表，
#: 而注册键是「AI 供应商」的词汇表，两者**大部分重合但不完全相同**
#: （如百度不在 ``CloudVendor`` 内）。显式映射让这种不一致不会变成隐式约定。
VENDOR_PROVIDER_CODES: dict[str, str] = {
    "JIXIAN": "jixian",
    "JOYINSIDE": "joyinside",
    "VOLCANO": "volcano",
}


def provider_code_for_vendor(vendor: str | None) -> str | None:
    """把云服务商厂商映射为供应商注册键；无对应则返回 ``None``。"""
    if not vendor:
        return None
    return VENDOR_PROVIDER_CODES.get(vendor.strip().upper())


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, AIProvider] = {}


def register(provider: AIProvider, *, replace: bool = True) -> None:
    """注册一个供应商。

    Args:
        provider: 供应商实例。
        replace: 已存在同 ``code`` 时是否覆盖。默认 ``True``，
            便于应用重启 / 测试重复注册时不报错。

    Raises:
        ValueError: ``provider.code`` 为空或 ``replace=False`` 且已存在。
    """
    if not provider.code:
        raise ValueError("供应商必须声明非空的 code 才能注册")
    if provider.code in _REGISTRY and not replace:
        raise ValueError(f"供应商 {provider.code} 已注册")
    _REGISTRY[provider.code] = provider


def register_many(providers: list[AIProvider] | tuple[AIProvider, ...]) -> None:
    """批量注册。"""
    for provider in providers:
        register(provider)


def unregister(code: str) -> None:
    """移除注册（不存在时静默返回，便于测试清理）。"""
    _REGISTRY.pop(code, None)


def reset_registry() -> None:
    """清空注册表并重置「已注册过」标记（仅供测试使用）。

    为什么必须**同时**重置标记？:func:`build_default_registry` 用该标记做幂等
    短路：若只清空字典而不重置标记，后续 ``ensure_default_registry()`` 会误判为
    「已经注册过」直接返回，注册表就永久为空，表现为「解析不到任何供应商」。
    这是一个只在**测试执行顺序**变化时才暴露的坑，故在此一并修掉。
    """
    global _default_registered
    _REGISTRY.clear()
    _default_registered = False


def get(code: str | None) -> AIProvider | None:
    """按注册键取供应商。"""
    if not code:
        return None
    return _REGISTRY.get(code)


def require(code: str) -> AIProvider:
    """按注册键取供应商，不存在则抛 ``VENDOR_UNAVAILABLE``。"""
    provider = get(code)
    if provider is None:
        raise AppException(
            ErrorCode.VENDOR_UNAVAILABLE,
            f"供应商 {code} 未注册，无法调用",
            details={"vendor": code, "ok": False, "reason": "PROVIDER_NOT_REGISTERED"},
        )
    return provider


def available_providers() -> list[AIProvider]:
    """返回全部已注册供应商（按注册键排序，输出稳定）。"""
    return [_REGISTRY[code] for code in sorted(_REGISTRY)]


def provider_codes() -> list[str]:
    """返回全部已注册的注册键。"""
    return sorted(_REGISTRY)


def describe_providers() -> list[ProviderDescriptor]:
    """返回全部供应商的自描述信息（供 ``GET /ai/providers``）。"""
    return [provider.describe() for provider in available_providers()]


def default_provider_code() -> str:
    """平台默认供应商注册键（来自 ``AI_DEFAULT_PROVIDER``）。"""
    return settings.AI_DEFAULT_PROVIDER


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ResolvedProvider:
    """解析结果。

    ``layer`` 记录**是哪一层命中的**。把「结果」与「原因」一起返回，
    是为了让运维能回答「为什么这台设备走了火山而不是平台默认的 mock」——
    没有这一层信息，多级配置的排查成本会非常高。
    """

    provider: AIProvider
    layer: str
    candidates: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"<ResolvedProvider code={self.provider.code} layer={self.layer}>"


def resolve_provider(
    *,
    request_code: str | None = None,
    client_product_code: str | None = None,
    vendor: str | None = None,
    provider_default_code: str | None = None,
    platform_default_code: str | None = None,
) -> ResolvedProvider | None:
    """按四层优先级解析供应商（纯函数，无数据库）。

    优先级：``request`` > ``client_product`` > ``template_vendor``
    > ``provider_default`` > ``platform_default``。

    Args:
        request_code: 本次请求显式指定的供应商（调试台 / 运维指定）。
        client_product_code: 客户产品的 AI 配置里指定的供应商。
        vendor: 模板绑定的云服务商厂商（``CloudVendor`` 取值）。
        provider_default_code: ``ai_providers`` 表中标记为默认的供应商。
        platform_default_code: ``AI_DEFAULT_PROVIDER`` 配置值。

    Returns:
        :class:`ResolvedProvider`；四层**全部未命中已注册供应商**时返回 ``None``，
        由调用方决定是回退 mock（演示模式）还是安全失败（生产模式）。
    """
    candidates: list[str] = []

    chain: tuple[tuple[str, str | None], ...] = (
        (LAYER_REQUEST, request_code),
        (LAYER_CLIENT_PRODUCT, client_product_code),
        (LAYER_TEMPLATE_VENDOR, provider_code_for_vendor(vendor)),
        (LAYER_PROVIDER_DEFAULT, provider_default_code),
        (LAYER_PLATFORM_DEFAULT, platform_default_code or settings.AI_DEFAULT_PROVIDER),
    )

    for layer, code in chain:
        if not code:
            continue
        candidates.append(code)
        provider = get(code)
        if provider is not None:
            return ResolvedProvider(provider=provider, layer=layer, candidates=candidates)
        # 未注册的候选值不直接失败：继续下探，让「配了一个拼错的 code」
        # 不至于把整个产品打挂；最终若全不命中，candidates 会完整暴露拼错的值
        logger.warning("供应商 %s（来自 %s 层）未注册，继续下探下一层", code, layer)

    return None


async def resolve_for_product(
    session: Any,
    *,
    client_product_id: str | None = None,
    request_code: str | None = None,
) -> ResolvedProvider | None:
    """从数据库读出四层候选值并解析。

    本函数把「读配置」（SQL）与「定顺序」（纯函数）分开，
    因此 :func:`resolve_provider` 可以在没有数据库的单元测试里被完整覆盖。

    Args:
        session: 异步数据库会话。
        client_product_id: 客户产品 ID；为空则跳过第 ①②层。
        request_code: 本次请求显式指定的供应商。

    Returns:
        :class:`ResolvedProvider`；未命中任何已注册供应商时返回 ``None``。
    """
    # 延迟导入：让本模块在「无数据库」的纯单元测试里也能被导入
    from sqlalchemy import select

    from app.models.ai import AiConfig, AiProvider as AiProviderRow
    from app.models.catalog import ClientProduct, CloudProvider, ProductTemplate

    client_product_code: str | None = None
    vendor: str | None = None

    if client_product_id:
        config = (
            await session.execute(
                select(AiConfig).where(AiConfig.client_product_id == client_product_id)
            )
        ).scalar_one_or_none()
        if config is not None:
            client_product_code = config.provider_code

        # 模板厂商：client_products → product_templates → cloud_providers
        # 注意用 outer join：云服务商可为空（未绑定设备云的纯模型产品）
        row = (
            await session.execute(
                select(CloudProvider.vendor)
                .select_from(ClientProduct)
                .join(ProductTemplate, ClientProduct.template_id == ProductTemplate.id)
                .outerjoin(CloudProvider, ProductTemplate.cloud_provider_id == CloudProvider.id)
                .where(ClientProduct.id == client_product_id)
            )
        ).scalar_one_or_none()
        if row:
            vendor = str(row)

    default_row = (
        await session.execute(
            select(AiProviderRow.code)
            .where(AiProviderRow.is_default.is_(True))
            .where(AiProviderRow.status == "ACTIVE")
            .order_by(AiProviderRow.code)
            .limit(1)
        )
    ).scalar_one_or_none()

    return resolve_provider(
        request_code=request_code,
        client_product_code=client_product_code,
        vendor=vendor,
        provider_default_code=str(default_row) if default_row else None,
        platform_default_code=settings.AI_DEFAULT_PROVIDER,
    )


# ---------------------------------------------------------------------------
# 默认注册表
# ---------------------------------------------------------------------------

#: 是否已经完成默认注册（避免每个请求重复构造适配器）
_default_registered = False


def build_default_registry(*, force: bool = False) -> list[str]:
    """注册内置供应商（mock + 四个真实厂商骨架）。

    Args:
        force: 为 ``True`` 时即使已注册过也重新构造（测试用）。

    Returns:
        本次注册后的全部注册键。
    """
    global _default_registered
    if _default_registered and not force:
        return provider_codes()

    # 延迟导入：适配器模块会 import httpx，让不需要真实厂商的单元测试
    # 保持极低的导入成本
    from app.ai import baidu, jixian, joyinside, volcano

    register(build_mock_provider())
    register(jixian.build_provider())
    register(joyinside.build_provider())
    register(volcano.build_provider())
    register(baidu.build_provider())

    _default_registered = True
    logger.info("AI 供应商注册表已就绪：%s", "、".join(provider_codes()))
    return provider_codes()


def ensure_default_registry() -> None:
    """确保默认注册表已建立（幂等，供 API 层调用）。"""
    build_default_registry()


__all__ = [
    "LAYER_CLIENT_PRODUCT",
    "LAYER_NONE",
    "LAYER_PLATFORM_DEFAULT",
    "LAYER_PROVIDER_DEFAULT",
    "LAYER_REQUEST",
    "LAYER_TEMPLATE_VENDOR",
    "VENDOR_PROVIDER_CODES",
    "ResolvedProvider",
    "available_providers",
    "build_default_registry",
    "default_provider_code",
    "describe_providers",
    "ensure_default_registry",
    "get",
    "provider_code_for_vendor",
    "provider_codes",
    "register",
    "register_many",
    "require",
    "reset_registry",
    "resolve_for_product",
    "resolve_provider",
    "unregister",
]
