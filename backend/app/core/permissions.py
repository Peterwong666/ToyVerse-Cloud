"""权限码目录与内置角色的权限分配。

权限码格式：``<领域>:<资源>:<动作>``，动作为 ``read`` 或 ``write``。

* ``platform:*`` —— 平台端权限
* ``merchant:*`` —— 商户端权限（同时受租户隔离约束）
* ``factory:*``  —— 工厂端权限（同时受字段脱敏约束）

平台超级管理员使用通配符 ``*``，表示拥有全部权限。
"""

from __future__ import annotations

from app.models.enums import RoleType

#: 通配权限码
WILDCARD = "*"


# ---------------------------------------------------------------------------
# 权限码常量
# ---------------------------------------------------------------------------


class PlatformPerm:
    """平台端权限码。"""

    TENANT_READ = "platform:tenant:read"
    TENANT_WRITE = "platform:tenant:write"
    CLOUD_READ = "platform:cloud:read"
    CLOUD_WRITE = "platform:cloud:write"
    TEMPLATE_READ = "platform:template:read"
    TEMPLATE_WRITE = "platform:template:write"
    PRODUCT_READ = "platform:product:read"
    PRODUCT_WRITE = "platform:product:write"
    ORDER_READ = "platform:order:read"
    ORDER_WRITE = "platform:order:write"
    DEVICE_READ = "platform:device:read"
    DEVICE_WRITE = "platform:device:write"
    BATCH_READ = "platform:batch:read"
    BATCH_WRITE = "platform:batch:write"
    ALLOCATION_READ = "platform:allocation:read"
    ALLOCATION_WRITE = "platform:allocation:write"
    FACTORY_ORDER_READ = "platform:factory-order:read"
    FACTORY_ORDER_WRITE = "platform:factory-order:write"
    ORG_READ = "platform:org:read"
    ORG_WRITE = "platform:org:write"
    MEMBER_READ = "platform:member:read"
    MEMBER_WRITE = "platform:member:write"
    ROLE_READ = "platform:role:read"
    ROLE_WRITE = "platform:role:write"
    OTA_READ = "platform:ota:read"
    OTA_WRITE = "platform:ota:write"
    AUDIT_READ = "platform:audit:read"
    DASHBOARD_READ = "platform:dashboard:read"


class MerchantPerm:
    """商户端权限码。"""

    PRODUCT_READ = "merchant:product:read"
    PRODUCT_WRITE = "merchant:product:write"
    AI_CONFIG_READ = "merchant:ai-config:read"
    AI_CONFIG_WRITE = "merchant:ai-config:write"
    KB_READ = "merchant:kb:read"
    KB_WRITE = "merchant:kb:write"
    ORDER_READ = "merchant:order:read"
    ORDER_WRITE = "merchant:order:write"
    DEVICE_READ = "merchant:device:read"
    DEVICE_WRITE = "merchant:device:write"
    BINDING_WRITE = "merchant:binding:write"
    MINIAPP_READ = "merchant:miniapp:read"
    MINIAPP_WRITE = "merchant:miniapp:write"
    METRICS_READ = "merchant:metrics:read"
    ORG_READ = "merchant:org:read"
    ORG_WRITE = "merchant:org:write"
    MEMBER_READ = "merchant:member:read"
    MEMBER_WRITE = "merchant:member:write"
    AUDIT_READ = "merchant:audit:read"
    DASHBOARD_READ = "merchant:dashboard:read"


class FactoryPerm:
    """工厂端权限码。"""

    ORDER_READ = "factory:order:read"
    BURN_WRITE = "factory:burn:write"
    INSPECT_WRITE = "factory:inspect:write"
    FIRMWARE_READ = "factory:firmware:read"
    BATCH_READ = "factory:batch:read"
    DASHBOARD_READ = "factory:dashboard:read"


# ---------------------------------------------------------------------------
# 内置角色 → 权限
# ---------------------------------------------------------------------------

_PLATFORM_OPERATOR_PERMS: frozenset[str] = frozenset(
    {
        PlatformPerm.TENANT_READ,
        PlatformPerm.CLOUD_READ,
        PlatformPerm.TEMPLATE_READ,
        PlatformPerm.PRODUCT_READ,
        PlatformPerm.PRODUCT_WRITE,
        PlatformPerm.ORDER_READ,
        PlatformPerm.ORDER_WRITE,
        PlatformPerm.DEVICE_READ,
        PlatformPerm.DEVICE_WRITE,
        PlatformPerm.BATCH_READ,
        PlatformPerm.ALLOCATION_READ,
        PlatformPerm.ALLOCATION_WRITE,
        PlatformPerm.FACTORY_ORDER_READ,
        PlatformPerm.ORG_READ,
        PlatformPerm.MEMBER_READ,
        PlatformPerm.AUDIT_READ,
        PlatformPerm.DASHBOARD_READ,
    }
)

_MERCHANT_ADMIN_PERMS: frozenset[str] = frozenset(
    {
        MerchantPerm.PRODUCT_READ,
        MerchantPerm.PRODUCT_WRITE,
        MerchantPerm.AI_CONFIG_READ,
        MerchantPerm.AI_CONFIG_WRITE,
        MerchantPerm.KB_READ,
        MerchantPerm.KB_WRITE,
        MerchantPerm.ORDER_READ,
        MerchantPerm.ORDER_WRITE,
        MerchantPerm.DEVICE_READ,
        MerchantPerm.DEVICE_WRITE,
        MerchantPerm.BINDING_WRITE,
        MerchantPerm.MINIAPP_READ,
        MerchantPerm.MINIAPP_WRITE,
        MerchantPerm.METRICS_READ,
        MerchantPerm.ORG_READ,
        MerchantPerm.ORG_WRITE,
        MerchantPerm.MEMBER_READ,
        MerchantPerm.MEMBER_WRITE,
        MerchantPerm.AUDIT_READ,
        MerchantPerm.DASHBOARD_READ,
    }
)

_MERCHANT_OPERATOR_PERMS: frozenset[str] = frozenset(
    {
        MerchantPerm.PRODUCT_READ,
        MerchantPerm.AI_CONFIG_READ,
        MerchantPerm.KB_READ,
        MerchantPerm.ORDER_READ,
        MerchantPerm.DEVICE_READ,
        MerchantPerm.BINDING_WRITE,
        MerchantPerm.MINIAPP_READ,
        MerchantPerm.METRICS_READ,
        MerchantPerm.MEMBER_READ,
        MerchantPerm.DASHBOARD_READ,
    }
)

_FACTORY_ADMIN_PERMS: frozenset[str] = frozenset(
    {
        FactoryPerm.ORDER_READ,
        FactoryPerm.BURN_WRITE,
        FactoryPerm.INSPECT_WRITE,
        FactoryPerm.FIRMWARE_READ,
        FactoryPerm.BATCH_READ,
        FactoryPerm.DASHBOARD_READ,
    }
)

_FACTORY_OPERATOR_PERMS: frozenset[str] = frozenset(
    {
        FactoryPerm.ORDER_READ,
        FactoryPerm.BURN_WRITE,
        FactoryPerm.INSPECT_WRITE,
        FactoryPerm.DASHBOARD_READ,
    }
)


#: 内置角色编码 → 权限码集合
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "PLATFORM_ADMIN": frozenset({WILDCARD}),
    "PLATFORM_OPERATOR": _PLATFORM_OPERATOR_PERMS,
    "MERCHANT_ADMIN": _MERCHANT_ADMIN_PERMS,
    "MERCHANT_OPERATOR": _MERCHANT_OPERATOR_PERMS,
    "FACTORY_ADMIN": _FACTORY_ADMIN_PERMS,
    "FACTORY_OPERATOR": _FACTORY_OPERATOR_PERMS,
}


def permissions_for_role(role_code: str) -> list[str]:
    """返回角色的权限码列表（已排序，便于断言与展示）。"""
    return sorted(ROLE_PERMISSIONS.get(role_code, frozenset()))


def role_type_of(role_code: str) -> RoleType | None:
    """由角色编码推断角色类型。"""
    prefix = role_code.split("_", 1)[0]
    mapping = {
        "PLATFORM": RoleType.PLATFORM,
        "MERCHANT": RoleType.MERCHANT,
        "FACTORY": RoleType.FACTORY,
    }
    return mapping.get(prefix)


#: 平台端的角色编码集合
PLATFORM_ROLE_CODES: frozenset[str] = frozenset({"PLATFORM_ADMIN", "PLATFORM_OPERATOR"})

#: 商户端的角色编码集合
MERCHANT_ROLE_CODES: frozenset[str] = frozenset({"MERCHANT_ADMIN", "MERCHANT_OPERATOR"})

#: 工厂端的角色编码集合
FACTORY_ROLE_CODES: frozenset[str] = frozenset({"FACTORY_ADMIN", "FACTORY_OPERATOR"})
