"""认证服务：登录、刷新、登出。

安全要点
--------
* 密码使用 bcrypt 校验，**失败次数累计**，达到阈值锁定账号（默认 5 次 / 15 分钟）
* 登录时校验**租户状态**——修复遗留原型缺陷 P-04（禁用客户仍可登录）
* 刷新令牌只存摘要，支持**轮换**：旧令牌被使用后立即吊销，
  若同一旧令牌被再次使用，说明发生了重放，此时吊销该用户全部刷新令牌
* 登录失败不区分「账号不存在」与「密码错误」，避免账号枚举
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from fastapi import Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import (
    account_disabled,
    account_locked,
    invalid_credentials,
    tenant_disabled,
    unauthenticated,
)
from app.core.ids import new_id
from app.core.logging import get_logger
from app.core.permissions import permissions_for_role, role_type_of
from app.core.security import (
    hash_token,
    issue_access_token,
    issue_refresh_token,
    verify_password,
)
from app.db.base import utcnow
from app.models.enums import AuditAction, RoleType, TenantStatus, UserStatus
from app.models.identity import RefreshToken, Tenant, UserAccount
from app.services import audit_service

logger = get_logger(__name__)


@dataclass(slots=True)
class IssuedTokens:
    """签发结果。"""

    access_token: str
    refresh_token: str
    expires_in: int  # 秒
    role: str
    tenant_id: str | None
    tenant_code: str | None
    user_id: str
    account: str
    nickname: str | None
    permissions: list[str]
    must_change_password: bool


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------


async def authenticate(
    session: AsyncSession,
    *,
    account: str,
    password: str,
    request: Request | None = None,
) -> IssuedTokens:
    """账号密码登录。

    Raises:
        AppException: 账号或密码错误 / 账号被禁用 / 账号被锁定 / 租户被禁用。
    """
    account = account.strip()

    stmt = select(UserAccount).where(UserAccount.account == account)
    user = (await session.execute(stmt)).scalar_one_or_none()

    # 账号不存在：不泄漏细节，但审计中如实记录
    if user is None:
        await audit_service.record(
            session,
            action=AuditAction.LOGIN_FAILED,
            actor_account=account,
            summary="登录失败：账号不存在",
            success=False,
            request=request,
        )
        await session.commit()
        raise invalid_credentials()

    # 账号被禁用
    if user.status != UserStatus.ACTIVE:
        await audit_service.record(
            session,
            action=AuditAction.LOGIN_FAILED,
            actor_account=account,
            tenant_id=user.tenant_id,
            summary="登录失败：账号已禁用",
            success=False,
            request=request,
        )
        await session.commit()
        raise account_disabled()

    # 锁定期内
    if user.is_locked:
        assert user.locked_until is not None
        remaining = max(1, int((user.locked_until - utcnow()).total_seconds() // 60) + 1)
        await audit_service.record(
            session,
            action=AuditAction.LOGIN_FAILED,
            actor_account=account,
            tenant_id=user.tenant_id,
            summary=f"登录失败：账号锁定中，剩余约 {remaining} 分钟",
            success=False,
            request=request,
        )
        await session.commit()
        raise account_locked(f"账号已被锁定，请约 {remaining} 分钟后再试")

    # 密码校验
    if not verify_password(password, user.password_hash):
        await _on_login_failure(session, user, request=request)
        raise invalid_credentials()

    # ---- 校验通过 ----

    # 租户状态校验（修复 P-04）
    tenant: Tenant | None = None
    if user.tenant_id is not None:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == user.tenant_id))
        ).scalar_one_or_none()
        if tenant is None:
            await audit_service.record(
                session,
                action=AuditAction.LOGIN_FAILED,
                actor_account=account,
                summary="登录失败：所属租户不存在",
                success=False,
                request=request,
            )
            await session.commit()
            raise invalid_credentials()
        if tenant.status != TenantStatus.ACTIVE:
            await audit_service.record(
                session,
                action=AuditAction.LOGIN_FAILED,
                actor_account=account,
                tenant_id=tenant.id,
                summary=f"登录失败：租户「{tenant.name}」已被禁用",
                success=False,
                request=request,
            )
            await session.commit()
            raise tenant_disabled()

    # 重置失败计数并记录登录信息
    user.login_failures = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    user.last_login_ip = _client_ip(request)

    tokens, _ = await _issue_tokens(session, user, tenant, request=request)

    await audit_service.record(
        session,
        action=AuditAction.LOGIN,
        tenant_id=user.tenant_id,
        actor_account=user.account,
        actor_role=user.role_code,
        summary=f"登录成功（{user.role_code}）",
        request=request,
    )
    await session.commit()

    logger.info("用户 %s 登录成功（角色 %s）", user.account, user.role_code)
    return tokens


async def _on_login_failure(
    session: AsyncSession, user: UserAccount, *, request: Request | None = None
) -> None:
    """处理一次密码错误：累加失败次数，达到阈值则锁定账号。"""
    user.login_failures += 1
    locked = False
    if user.login_failures >= settings.MAX_LOGIN_FAILURES:
        user.locked_until = utcnow() + timedelta(minutes=settings.LOCKOUT_MINUTES)
        locked = True

    summary = (
        f"登录失败：密码错误，累计 {user.login_failures} 次"
        + (f"，账号已锁定 {settings.LOCKOUT_MINUTES} 分钟" if locked else "")
    )
    await audit_service.record(
        session,
        action=AuditAction.LOGIN_FAILED,
        actor_account=user.account,
        actor_role=user.role_code,
        tenant_id=user.tenant_id,
        summary=summary,
        detail={"failures": user.login_failures, "locked": locked},
        success=False,
        request=request,
    )
    await session.commit()

    if locked:
        logger.warning(
            "账号 %s 连续 %d 次登录失败，已锁定 %d 分钟",
            user.account,
            user.login_failures,
            settings.LOCKOUT_MINUTES,
        )


async def _issue_tokens(
    session: AsyncSession,
    user: UserAccount,
    tenant: Tenant | None,
    *,
    request: Request | None = None,
) -> tuple[IssuedTokens, str]:
    """签发访问令牌与刷新令牌。

    Returns:
        ``(签发结果, 刷新令牌记录 ID)``——返回记录 ID 是为了让刷新流程
        能把旧令牌通过 ``rotated_to`` 指向新令牌。
    """
    permissions = permissions_for_role(user.role_code)

    access_token, _ = issue_access_token(
        user_id=user.id,
        account=user.account,
        role=user.role_code,
        role_type=role_type_of(user.role_code) or RoleType.MERCHANT,
        tenant_id=user.tenant_id,
        tenant_code=tenant.code if tenant else None,
        permissions=permissions,
        # 工厂账号的归属进 claims：工厂端每个请求都要按它过滤工单，
        # 回查 user_accounts 会把「无状态鉴权」这个前提悄悄丢掉。
        factory_id=user.factory_id,
    )

    raw_refresh, refresh_expires, refresh_hash = issue_refresh_token()
    refresh_record = RefreshToken(
        id=new_id("refresh_token"),
        user_id=user.id,
        token_hash=refresh_hash,
        expires_at=refresh_expires,
        user_agent=(request.headers.get("user-agent", "")[:256] if request else None),
        client_ip=_client_ip(request),
    )
    session.add(refresh_record)
    await session.flush()

    issued = IssuedTokens(
        access_token=access_token,
        refresh_token=raw_refresh,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        role=user.role_code,
        tenant_id=user.tenant_id,
        tenant_code=tenant.code if tenant else None,
        user_id=user.id,
        account=user.account,
        nickname=user.nickname,
        permissions=permissions,
        must_change_password=user.must_change_password,
    )
    return issued, refresh_record.id


# ---------------------------------------------------------------------------
# 刷新
# ---------------------------------------------------------------------------


async def refresh_tokens(
    session: AsyncSession, *, raw_refresh_token: str, request: Request | None = None
) -> IssuedTokens:
    """用刷新令牌换取新的访问令牌（并轮换刷新令牌）。

    轮换策略：旧令牌立即吊销并指向新令牌。若已吊销的令牌被再次使用，
    判定为令牌重放，吊销该用户**全部**刷新令牌并强制重新登录。

    Raises:
        AppException: 刷新令牌无效、过期或已被吊销。
    """
    token_hash = hash_token(raw_refresh_token)
    stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    record = (await session.execute(stmt)).scalar_one_or_none()

    if record is None:
        raise unauthenticated("刷新令牌无效，请重新登录")

    # 重放检测：已吊销或已轮换的令牌被再次使用
    if record.revoked_at is not None or record.rotated_to is not None:
        logger.warning("检测到刷新令牌重放，吊销用户 %s 的全部刷新令牌", record.user_id)
        await _revoke_all_for_user(session, record.user_id)
        await audit_service.record(
            session,
            action=AuditAction.LOGIN_FAILED,
            actor_account=None,
            tenant_id=None,
            resource_type="refresh_token",
            resource_id=record.id,
            summary="检测到刷新令牌重放，已吊销该用户全部登录态",
            success=False,
            request=request,
        )
        await session.commit()
        raise unauthenticated("登录态异常，已强制退出，请重新登录")

    if record.expires_at <= utcnow():
        raise unauthenticated("刷新令牌已过期，请重新登录")

    user = (
        await session.execute(select(UserAccount).where(UserAccount.id == record.user_id))
    ).scalar_one_or_none()
    if user is None or user.status != UserStatus.ACTIVE:
        raise unauthenticated("账号不可用，请重新登录")

    tenant: Tenant | None = None
    if user.tenant_id is not None:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == user.tenant_id))
        ).scalar_one_or_none()
        if tenant is None or tenant.status != TenantStatus.ACTIVE:
            raise tenant_disabled()

    tokens, new_refresh_id = await _issue_tokens(session, user, tenant, request=request)

    # 吊销旧令牌并指向新令牌（用于重放检测）
    record.revoked_at = utcnow()
    record.rotated_to = new_refresh_id

    await session.commit()
    return tokens


# ---------------------------------------------------------------------------
# 登出
# ---------------------------------------------------------------------------


async def revoke_refresh_token(
    session: AsyncSession, *, raw_refresh_token: str | None, user_id: str | None = None
) -> None:
    """吊销刷新令牌。未提供令牌时按用户吊销全部。"""
    if raw_refresh_token:
        token_hash = hash_token(raw_refresh_token)
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == token_hash, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
        await session.commit()
        return

    if user_id:
        await _revoke_all_for_user(session, user_id)
        await session.commit()


async def _revoke_all_for_user(session: AsyncSession, user_id: str) -> None:
    """吊销指定用户的全部有效刷新令牌。"""
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


async def get_user(session: AsyncSession, user_id: str) -> UserAccount | None:
    """按 ID 取用户。"""
    return (
        await session.execute(select(UserAccount).where(UserAccount.id == user_id))
    ).scalar_one_or_none()


async def list_login_tenants(session: AsyncSession) -> list[Tenant]:
    """登录页的租户下拉列表（仅返回启用中的租户）。"""
    stmt = (
        select(Tenant)
        .where(Tenant.status == TenantStatus.ACTIVE)
        .order_by(Tenant.name)
    )
    return list((await session.execute(stmt)).scalars().all())


def _client_ip(request: Request | None) -> str | None:
    """提取客户端 IP。"""
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None
