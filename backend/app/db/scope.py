"""租户作用域收口（架构红线 #1）。

**所有**商户端的租户过滤都必须经过此模块，禁止在路由层手写
``WHERE tenant_id = ...``。集中收口的价值：

* 新增端点时不会因为忘记加过滤而越权
* 越权行为可被单元测试统一覆盖（见 ``tests/integration/test_tenant_isolation.py``）
* 平台角色与工厂角色的差异在此一处表达

三类角色的数据可见范围
----------------------
============  ==========================  ============================
角色          可见范围                    保护手段
============  ==========================  ============================
平台 PLATFORM 全部租户                    鉴权（RBAC）
商户 MERCHANT 仅自身租户                  租户作用域过滤
工厂 FACTORY  仅本厂工单（仍跨租户）       工厂作用域过滤 + 字段脱敏
============  ==========================  ============================

工厂的双重保护
--------------
工厂角色需要**两层**保护，缺一不可：

1. **工厂作用域过滤**（:func:`factory_scoped` / :func:`assert_factory_visible`）
   —— 决定「能看到哪些工单」。一家工厂只应看到派给自己的工单；这层保护的是
   *工单量、产能、其他工厂的存在* 等信息。
2. **字段白名单脱敏**（``app/services/serializers.py``）
   —— 决定「看到工单的哪些字段」。工单跨租户，客户名 / 金额 / 联系方式
   必须逐字段剔除；这层保护的是 *客户身份* 信息。

P6 之前只有第 2 层，是因为当时工厂账号根本没有工厂归属（见迁移 ``0012``）。
只做脱敏不做作用域，等于「把 A 厂的数据也发给 B 厂，只是把客户名打了码」。
"""

from __future__ import annotations

from typing import Any, TypeVar

from sqlalchemy import Select

from app.core.deps import AuthContext
from app.core.errors import permission_denied

#: 带 tenant_id 字段的 ORM 模型
TenantScopedModel = TypeVar("TenantScopedModel")


def scoped(stmt: Select[Any], model: Any, auth: AuthContext) -> Select[Any]:
    """为查询语句注入租户过滤条件。

    * 平台角色：不加过滤（全局长）
    * 商户角色：强制 ``model.tenant_id == auth.tenant_id``
    * 工厂角色：不加租户过滤——工厂端的数据保护由**字段脱敏**负责，
      调用方必须使用脱敏序列化器输出

    Args:
        stmt: 原始查询语句。
        model: ORM 模型类（必须含 ``tenant_id`` 字段）。
        auth: 认证上下文。

    Returns:
        注入过滤后的查询语句。

    Raises:
        AppException: 模型缺少 ``tenant_id`` 字段。
    """
    if not hasattr(model, "tenant_id"):
        raise RuntimeError(
            f"{model.__name__} 不含 tenant_id 字段，不能用于租户作用域查询"
        )

    if auth.is_platform or auth.is_factory:
        return stmt

    return stmt.where(model.tenant_id == auth.require_tenant_id())


def scoped_value(auth: AuthContext) -> str | None:
    """返回应当用于过滤的租户 ID。

    * 平台 / 工厂角色：``None``（不过滤）
    * 商户角色：强制返回租户 ID
    """
    if auth.is_platform or auth.is_factory:
        return None
    return auth.require_tenant_id()


def assert_visible(tenant_id_of_resource: str | None, auth: AuthContext, *, resource: str = "资源") -> None:
    """断言资源对当前认证上下文可见，不可见时抛 404 语义的权限错误。

    刻意返回「不存在」而非「无权限」，避免通过错误信息探测其他租户的资源是否存在。

    Raises:
        AppException: 商户角色访问其他租户的资源。
    """
    if auth.is_platform or auth.is_factory:
        return

    if tenant_id_of_resource != auth.require_tenant_id():
        from app.core.errors import not_found

        raise not_found(f"{resource}不存在")


def assert_tenant_writable(tenant_id: str | None, auth: AuthContext) -> None:
    """断言当前上下文有权写入指定租户的数据。"""
    if auth.is_platform:
        return
    if tenant_id != auth.tenant_id:
        raise permission_denied("无权操作其他租户的数据")


# ---------------------------------------------------------------------------
# 工厂作用域（P6）
# ---------------------------------------------------------------------------


def factory_scoped(stmt: Select[Any], model: Any, auth: AuthContext) -> Select[Any]:
    """为查询语句注入**工厂**过滤条件。

    与 :func:`scoped` 的分工：

    * ``scoped`` 管 ``tenant_id``（商户 → 自身租户）；
    * 本函数管 ``factory_id``（工厂 → 自身工厂）。

    工厂跨租户，所以两个作用域在语义上正交，不能互相替代：
    ``factory_orders`` 上没有可用的 ``tenant_id``（它挂在 ``orders`` 上），
    拿 :func:`scoped` 过滤它只会得到「不过滤」。

    * 平台角色：不加过滤（全局长，可查所有工厂的工单）
    * 工厂角色：强制 ``model.factory_id == auth.factory_id``，
      且账号未绑定工厂时直接拒绝（而非退化为「看全部」）

    Args:
        stmt: 原始查询语句。
        model: ORM 模型类（必须含 ``factory_id`` 字段）。
        auth: 认证上下文。

    Returns:
        注入过滤后的查询语句。

    Raises:
        AppException: 模型缺少 ``factory_id`` 字段，或工厂账号未绑定工厂。
    """
    if not hasattr(model, "factory_id"):
        raise RuntimeError(
            f"{model.__name__} 不含 factory_id 字段，不能用于工厂作用域查询"
        )

    if auth.is_platform:
        return stmt
    if auth.is_factory:
        return stmt.where(model.factory_id == auth.require_factory_id())

    # 商户角色不该出现在工厂端接口上；真出现了也必须什么都查不到。
    from app.core.errors import permission_denied

    raise permission_denied("该接口仅工厂端可访问")


def assert_factory_visible(
    factory_id_of_resource: str | None, auth: AuthContext, *, resource: str = "工单"
) -> None:
    """断言资源对当前认证上下文可见（工厂维度）。

    越权访问刻意返回「不存在」而非「无权限」——与
    :func:`assert_visible` 同一口径：不让调用方通过错误码差异
    探测「这个工单号是否存在于别的工厂」。

    Raises:
        AppException: 工厂角色访问其他工厂的工单。
    """
    if auth.is_platform:
        return
    if not auth.is_factory:
        from app.core.errors import permission_denied

        raise permission_denied("该接口仅工厂端可访问")

    if auth.factory_id is None or factory_id_of_resource != auth.factory_id:
        from app.core.errors import not_found

        raise not_found(f"{resource}不存在")
