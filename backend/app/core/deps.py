"""FastAPI 依赖注入：认证上下文、权限校验、租户作用域。

这是**多租户隔离的唯一收口点**（架构红线 #1）。

请求处理链::

    TraceIdMiddleware          → 注入 traceId
    get_auth_context()         → 解 JWT，产出 AuthContext
    require_perm("...")        → 校验权限码
    tenant_scope(query)        → repository 层强制注入 tenant_id 条件

禁止在路由层手写 ``WHERE tenant_id = ...``；所有租户过滤都经由此处的
``AuthContext`` 与 :mod:`app.db.scope` 完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Request, params
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import permission_denied, unauthenticated
from app.core.logging import set_tenant_id
from app.core.permissions import (
    FACTORY_ROLE_CODES,
    MERCHANT_ROLE_CODES,
    PLATFORM_ROLE_CODES,
    WILDCARD,
)
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.enums import RoleType

#: 关闭 auto_error，改用项目统一错误码返回 401
_bearer_scheme = HTTPBearer(auto_error=False, description="JWT 访问令牌")


@dataclass(slots=True)
class AuthContext:
    """已认证请求的上下文。

    由 JWT 解码得到，**不查数据库**——这是无状态鉴权的性能优势。
    需要用户详情（如昵称）时才回查。
    """

    user_id: str
    account: str
    role_code: str
    role_type: RoleType
    tenant_id: str | None = None
    tenant_code: str | None = None
    #: 工厂账号所属工厂（P6 起）。非工厂账号恒为 ``None``。
    factory_id: str | None = None
    permissions: list[str] = field(default_factory=list)
    nickname: str | None = None

    # ---- 角色判定 ----

    @property
    def is_platform(self) -> bool:
        return self.role_type is RoleType.PLATFORM

    @property
    def is_merchant(self) -> bool:
        return self.role_type is RoleType.MERCHANT

    @property
    def is_factory(self) -> bool:
        return self.role_type is RoleType.FACTORY

    # ---- 权限判定 ----

    def has_permission(self, code: str) -> bool:
        """是否拥有指定权限码。``*`` 通配表示拥有全部权限。"""
        return WILDCARD in self.permissions or code in self.permissions

    def has_any_permission(self, codes: list[str] | tuple[str, ...]) -> bool:
        """是否拥有其中任意一个权限码。"""
        return any(self.has_permission(code) for code in codes)

    def require_permission(self, code: str) -> None:
        """校验权限，不通过则抛 403。"""
        if not self.has_permission(code):
            raise permission_denied(required=code)

    # ---- 租户判定 ----

    def require_tenant_id(self) -> str:
        """返回强制存在的租户 ID。

        商户端调用此方法获取作用域；平台端调用会抛权限错误——
        这样可以在架构上防止商户接口被平台角色误用而绕过租户过滤。
        """
        if self.tenant_id is None:
            raise permission_denied("当前账号不属于任何租户，无法访问租户级资源")
        return self.tenant_id

    def require_factory_id(self) -> str:
        """返回强制存在的工厂 ID（工厂作用域的唯一来源）。

        工厂账号若没有绑定工厂，就**什么都看不到**——而不是退化成
        「看到全部工单」。后者在多工厂场景下等于 A 厂能读到 B 厂的产量与
        客户脱敏名，是数据越权。宁可在播种阶段就暴露「账号没绑工厂」，
        也不能让它在生产上默默放宽。
        """
        if self.factory_id is None:
            raise permission_denied("当前工厂账号未绑定工厂，无法访问生产工单")
        return self.factory_id

    def ensure_tenant_matches(self, resource_tenant_id: str | None) -> None:
        """校验资源归属当前租户。

        平台角色跳过校验（全局长）；商户角色必须匹配。
        """
        if self.is_platform:
            return
        if resource_tenant_id != self.tenant_id:
            from app.core.errors import device_not_in_tenant

            raise device_not_in_tenant("资源不属于当前租户")

    def __repr__(self) -> str:
        return (
            f"<AuthContext {self.account} role={self.role_code} "
            f"type={self.role_type} tenant={self.tenant_id}>"
        )


# ---------------------------------------------------------------------------
# 认证依赖
# ---------------------------------------------------------------------------


async def get_auth_context(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> AuthContext:
    """解析 ``Authorization: Bearer <token>`` 得到认证上下文。

    Raises:
        AppException: 缺少令牌、令牌无效或过期。
    """
    if credentials is None or not credentials.credentials:
        raise unauthenticated("请先登录")
    if credentials.scheme.lower() != "bearer":
        raise unauthenticated("不支持的认证方式")

    payload = decode_access_token(credentials.credentials)

    try:
        role_type = RoleType(payload.role_type)
    except ValueError as exc:
        raise unauthenticated("登录态中的角色类型无效") from exc

    context = AuthContext(
        user_id=payload.subject,
        account=payload.account,
        role_code=payload.role,
        role_type=role_type,
        tenant_id=payload.tenant_id,
        tenant_code=payload.tenant_code,
        factory_id=payload.factory_id,
        permissions=payload.permissions,
    )

    # 让日志自动带上租户标识，便于按租户检索
    set_tenant_id(context.tenant_id)
    request.state.auth = context

    return context


async def get_optional_auth_context(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> AuthContext | None:
    """可选认证：无令牌时返回 ``None`` 而非报错（用于公开但需增强的端点）。"""
    if credentials is None or not credentials.credentials:
        return None
    return await get_auth_context(request, credentials)


#: 便捷类型别名
CurrentAuth = Annotated[AuthContext, Depends(get_auth_context)]
OptionalAuth = Annotated[AuthContext | None, Depends(get_optional_auth_context)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


# ---------------------------------------------------------------------------
# 权限 / 角色依赖工厂
# ---------------------------------------------------------------------------


def require_perm(*codes: str) -> params.Depends:
    """构造一个校验权限码的依赖。

    多个权限码为**或**关系：拥有任意一个即可通过。

    Usage::

        @router.get("/devices", dependencies=[Depends(require_perm(MerchantPerm.DEVICE_READ))])
    """

    async def _checker(auth: CurrentAuth) -> AuthContext:
        if not auth.has_any_permission(codes):
            raise permission_denied(required=codes[0] if codes else None, )
        return auth

    # 显式标注局部变量：fastapi.Depends 的返回类型在存根里是 Any，
    # 直接 return 会触发 mypy strict 的 no-any-return。
    dependency: params.Depends = Depends(_checker)
    return dependency


def require_role(*role_codes: str) -> params.Depends:
    """构造一个校验角色编码的依赖。"""

    async def _checker(auth: CurrentAuth) -> AuthContext:
        if auth.role_code not in role_codes:
            raise permission_denied(f"需要以下角色之一：{'、'.join(role_codes)}")
        return auth

    dependency: params.Depends = Depends(_checker)
    return dependency


async def require_platform(auth: CurrentAuth) -> AuthContext:
    """要求平台角色（全局长）。"""
    if auth.role_code not in PLATFORM_ROLE_CODES:
        raise permission_denied("该接口仅平台端可访问")
    return auth


async def require_merchant(auth: CurrentAuth) -> AuthContext:
    """要求商户角色，并确保租户 ID 存在。"""
    if auth.role_code not in MERCHANT_ROLE_CODES:
        raise permission_denied("该接口仅商户端可访问")
    if auth.tenant_id is None:
        raise permission_denied("商户账号缺少租户归属")
    return auth


async def require_factory(auth: CurrentAuth) -> AuthContext:
    """要求工厂角色。"""
    if auth.role_code not in FACTORY_ROLE_CODES:
        raise permission_denied("该接口仅工厂端可访问")
    return auth


#: 便捷类型别名
PlatformAuth = Annotated[AuthContext, Depends(require_platform)]
MerchantAuth = Annotated[AuthContext, Depends(require_merchant)]
FactoryAuth = Annotated[AuthContext, Depends(require_factory)]
