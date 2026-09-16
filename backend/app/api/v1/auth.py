"""认证端点：登录、刷新、登出、租户下拉、当前用户、修改密码。"""

from __future__ import annotations

from fastapi import APIRouter, Body, Request

from app.core.config import settings
from app.core.deps import CurrentAuth, DbSession
from app.core.errors import invalid_credentials, not_found
from app.core.logging import get_logger
from app.core.security import hash_password, validate_password_strength, verify_password
from app.models.enums import AuditAction
from app.models.identity import Tenant
from app.schemas.auth import (
    ChangePasswordRequest,
    CurrentUserResponse,
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    RefreshRequest,
    RefreshResponse,
    TenantOption,
    UserProfile,
)
from app.schemas.common import ListResult, MessageResult
from app.services import audit_service, auth_service

logger = get_logger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------


@router.post(
    "/auth/login",
    response_model=LoginResponse,
    summary="账号密码登录",
    description=(
        "账号密码登录，返回访问令牌与刷新令牌。\n\n"
        "**安全策略**：\n"
        f"- 连续 {settings.MAX_LOGIN_FAILURES} 次密码错误将锁定账号 "
        f"{settings.LOCKOUT_MINUTES} 分钟\n"
        "- 账号被禁用或所属租户被禁用时拒绝登录\n"
        "- 登录失败不区分「账号不存在」与「密码错误」，避免账号枚举"
    ),
    responses={401: {"description": "账号或密码错误"}},
)
async def login(
    request: Request,
    session: DbSession,
    payload: LoginRequest = Body(...),
) -> LoginResponse:
    """账号密码登录。"""
    issued = await auth_service.authenticate(
        session,
        account=payload.account,
        password=payload.password,
        request=request,
    )
    return LoginResponse(
        accessToken=issued.access_token,
        refreshToken=issued.refresh_token,
        expiresIn=issued.expires_in,
        mustChangePassword=issued.must_change_password,
        user=UserProfile(
            userId=issued.user_id,
            account=issued.account,
            nickname=issued.nickname,
            role=issued.role,
            roleType=_role_type_of(issued.role),
            tenantId=issued.tenant_id,
            tenantCode=issued.tenant_code,
            permissions=issued.permissions,
        ),
    )


@router.post(
    "/auth/refresh",
    response_model=RefreshResponse,
    summary="刷新访问令牌",
    description=(
        "用刷新令牌换取新的访问令牌，同时**轮换**刷新令牌。\n\n"
        "若已吊销或已轮换的令牌被再次使用，判定为令牌重放，"
        "将吊销该用户全部登录态并强制重新登录。"
    ),
    responses={401: {"description": "刷新令牌无效或已过期"}},
)
async def refresh(
    request: Request,
    session: DbSession,
    payload: RefreshRequest = Body(...),
) -> RefreshResponse:
    """刷新访问令牌。"""
    issued = await auth_service.refresh_tokens(
        session, raw_refresh_token=payload.refreshToken, request=request
    )
    return RefreshResponse(
        accessToken=issued.access_token,
        refreshToken=issued.refresh_token,
        expiresIn=issued.expires_in,
    )


@router.post(
    "/auth/logout",
    response_model=MessageResult,
    summary="登出",
    description="吊销刷新令牌。未提供 ``refreshToken`` 时吊销该用户全部登录态。",
)
async def logout(
    request: Request,
    session: DbSession,
    auth: CurrentAuth,
    payload: LogoutRequest | None = Body(default=None),
) -> MessageResult:
    """登出。"""
    await auth_service.revoke_refresh_token(
        session,
        raw_refresh_token=payload.refreshToken if payload else None,
        user_id=auth.user_id,
    )
    await audit_service.record(
        session,
        action=AuditAction.LOGOUT,
        actor=auth,
        summary="用户登出",
        request=request,
    )
    await session.commit()
    return MessageResult(message="已安全退出")


# ---------------------------------------------------------------------------
# 登录辅助
# ---------------------------------------------------------------------------


@router.get(
    "/auth/tenants",
    response_model=ListResult[TenantOption],
    summary="登录页租户下拉",
    description="仅返回启用中的租户，且**不含联系方式等敏感字段**。",
)
async def login_tenants(session: DbSession) -> ListResult[TenantOption]:
    """登录页可选租户列表。"""
    tenants = await auth_service.list_login_tenants(session)
    return ListResult[TenantOption](
        records=[
            TenantOption(id=t.id, code=t.code, name=t.name, industry=t.industry) for t in tenants
        ],
        total=len(tenants),
    )


@router.get(
    "/me",
    response_model=CurrentUserResponse,
    summary="当前登录用户",
    description="返回当前令牌对应的用户信息与权限码集合，供前端做菜单与按钮级权限控制。",
)
async def current_user(session: DbSession, auth: CurrentAuth) -> CurrentUserResponse:
    """当前登录用户信息。"""
    user = await auth_service.get_user(session, auth.user_id)
    if user is None:
        raise not_found("用户不存在")

    tenant_name: str | None = None
    if user.tenant_id is not None:
        tenant = await session.get(Tenant, user.tenant_id)
        tenant_name = tenant.name if tenant else None

    return CurrentUserResponse(
        userId=user.id,
        account=user.account,
        nickname=user.nickname,
        avatar=user.avatar,
        role=user.role_code,
        roleType=_role_type_of(user.role_code),
        tenantId=user.tenant_id,
        tenantCode=auth.tenant_code,
        tenantName=tenant_name,
        permissions=sorted(auth.permissions),
        lastLoginAt=user.last_login_at,
        mustChangePassword=user.must_change_password,
    )


@router.post(
    "/me/password",
    response_model=MessageResult,
    summary="修改密码",
    description="校验旧密码后设置新密码，并清除「首次登录需改密」标记。",
)
async def change_password(
    request: Request,
    session: DbSession,
    auth: CurrentAuth,
    payload: ChangePasswordRequest = Body(...),
) -> MessageResult:
    """修改当前用户密码。"""
    user = await auth_service.get_user(session, auth.user_id)
    if user is None:
        raise not_found("用户不存在")

    if not verify_password(payload.oldPassword, user.password_hash):
        await audit_service.record(
            session,
            action=AuditAction.UPDATE,
            actor=auth,
            resource_type="user_account",
            resource_id=user.id,
            summary="修改密码失败：旧密码不正确",
            success=False,
            request=request,
        )
        await session.commit()
        raise invalid_credentials("旧密码不正确")

    validate_password_strength(payload.newPassword)
    if payload.newPassword == payload.oldPassword:
        raise invalid_credentials("新密码不能与旧密码相同")

    user.password_hash = hash_password(payload.newPassword)
    user.must_change_password = False

    # 改密后吊销全部登录态，强制其他设备重新登录
    await auth_service.revoke_refresh_token(session, raw_refresh_token=None, user_id=user.id)

    await audit_service.record(
        session,
        action=AuditAction.UPDATE,
        actor=auth,
        resource_type="user_account",
        resource_id=user.id,
        summary="修改密码成功，已吊销全部登录态",
        request=request,
    )
    await session.commit()

    logger.info("用户 %s 修改密码成功", user.account)
    return MessageResult(message="密码修改成功，请重新登录")


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _role_type_of(role_code: str) -> str:
    """由角色编码推断角色类型。"""
    from app.core.permissions import role_type_of

    role_type = role_type_of(role_code)
    return str(role_type) if role_type else "MERCHANT"
