"""租户服务：客户开通、资料维护、启停、账号与密码管理。

为什么单列一个服务
------------------
租户是「数据可见范围」的根：产品授权、客户产品、订单、设备全部挂在它下面。
把租户的写路径集中在此，可以保证：

* 编码唯一性只在一个地方判断（``TENANT_CODE_EXISTS``）
* **删除前的关联校验只在一个地方判断**（修复 P-06：有关联时返回
  ``CASCADE_CONFLICT``，而不是静默级联删掉客户数据）
* 租户账号的密码策略只在一个地方落地（初始密码随机 + 强制改密）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Request
from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    cascade_conflict,
    not_found,
    tenant_code_exists,
    validation_error,
)
from app.core.ids import new_id
from app.core.logging import get_logger
from app.core.security import generate_password, hash_password, validate_password_strength
from app.db.base import utcnow
from app.models.catalog import ClientProduct, ProductAuthorization
from app.models.enums import AuditAction, TenantStatus, UserStatus
from app.models.identity import RefreshToken, Tenant, UserAccount
from app.services import audit_service

logger = get_logger(__name__)

#: 允许在租户下创建的账号角色（工厂与平台角色不属于任何租户）
TENANT_ROLE_CODES: frozenset[str] = frozenset({"MERCHANT_ADMIN", "MERCHANT_OPERATOR"})


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


async def get_tenant(session: AsyncSession, tenant_id: str) -> Tenant:
    """按 ID 取租户。

    Raises:
        AppException: 租户不存在。
    """
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant is None:
        raise not_found("租户不存在")
    return tenant


async def list_tenants(
    session: AsyncSession,
    *,
    offset: int = 0,
    limit: int = 20,
    sort_by: str | None = None,
    order: str = "desc",
    keyword: str | None = None,
    status: str | None = None,
    industry: str | None = None,
) -> tuple[list[Tenant], int]:
    """分页查询租户。

    Returns:
        ``(当前页记录, 总条数)``
    """
    conditions: list[ColumnElement[bool]] = []
    if keyword:
        pattern = f"%{keyword.strip()}%"
        conditions.append(
            Tenant.name.like(pattern)
            | Tenant.code.like(pattern)
            | Tenant.contact_name.like(pattern)
            | Tenant.contact_phone.like(pattern)
        )
    if status:
        conditions.append(Tenant.status == status)
    if industry:
        conditions.append(Tenant.industry == industry)

    total = int(
        (
            await session.execute(select(func.count()).select_from(Tenant).where(*conditions))
        ).scalar_one()
    )

    stmt = select(Tenant).where(*conditions)
    column = getattr(Tenant, sort_by, None) if sort_by else None
    if column is None or not hasattr(column, "asc"):
        column = Tenant.created_at
    stmt = stmt.order_by(column.desc() if order == "desc" else column.asc())
    stmt = stmt.offset(offset).limit(limit)

    return list((await session.execute(stmt)).scalars().all()), total


async def list_industries(session: AsyncSession) -> list[str]:
    """已存在的行业取值（供前端筛选下拉）。"""
    stmt = (
        select(Tenant.industry)
        .where(Tenant.industry.is_not(None))
        .distinct()
        .order_by(Tenant.industry)
    )
    return [row for row in (await session.execute(stmt)).scalars().all() if row]


async def list_accounts(session: AsyncSession, tenant_id: str) -> list[UserAccount]:
    """租户下的全部账号。"""
    stmt = (
        select(UserAccount)
        .where(UserAccount.tenant_id == tenant_id)
        .order_by(UserAccount.created_at)
    )
    return list((await session.execute(stmt)).scalars().all())


@dataclass(slots=True)
class TenantCounts:
    """租户关联数据计数。"""

    authorizations: int = 0
    client_products: int = 0
    users: int = 0


async def collect_counts(session: AsyncSession, tenant_id: str) -> TenantCounts:
    """统计租户的关联数据量（供详情页与删除校验共用）。"""

    async def _count(model: Any) -> int:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
                )
            ).scalar_one()
        )

    return TenantCounts(
        authorizations=await _count(ProductAuthorization),
        client_products=await _count(ClientProduct),
        users=await _count(UserAccount),
    )


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


async def create_tenant(
    session: AsyncSession,
    *,
    payload: dict[str, Any],
    actor: Any = None,
    request: Request | None = None,
) -> Tenant:
    """开通租户（客户开通）。

    Raises:
        AppException: 租户编码已存在。
    """
    code = str(payload["code"])
    exists = (
        await session.execute(select(Tenant).where(Tenant.code == code))
    ).scalar_one_or_none()
    if exists is not None:
        raise tenant_code_exists(f"租户编码「{code}」已存在")

    tenant = Tenant(
        id=new_id("tenant"),
        code=code,
        name=payload["name"],
        status=str(payload.get("status") or TenantStatus.ACTIVE),
        timezone=payload.get("timezone") or "Asia/Shanghai",
        contact_name=payload.get("contact_name"),
        contact_phone=payload.get("contact_phone"),
        email=payload.get("email"),
        industry=payload.get("industry"),
        remark=payload.get("remark"),
    )
    session.add(tenant)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        actor=actor,
        tenant_id=tenant.id,
        resource_type="tenant",
        resource_id=tenant.id,
        summary=f"开通租户「{tenant.name}」（{tenant.code}）",
        detail={"code": tenant.code, "industry": tenant.industry},
        request=request,
    )
    await session.commit()

    logger.info("已开通租户 %s（%s）", tenant.code, tenant.name)
    return tenant


async def update_tenant(
    session: AsyncSession,
    *,
    tenant_id: str,
    payload: dict[str, Any],
    actor: Any = None,
    request: Request | None = None,
) -> Tenant:
    """更新租户资料（编码与状态不在此修改）。"""
    tenant = await get_tenant(session, tenant_id)

    changed: dict[str, Any] = {}
    for field_name, value in payload.items():
        if value is None:
            continue
        if getattr(tenant, field_name) != value:
            setattr(tenant, field_name, value)
            changed[field_name] = value

    if not changed:
        return tenant

    await audit_service.record(
        session,
        action=AuditAction.UPDATE,
        actor=actor,
        tenant_id=tenant.id,
        resource_type="tenant",
        resource_id=tenant.id,
        summary=f"更新租户「{tenant.name}」资料",
        detail={"fields": sorted(changed)},
        request=request,
    )
    await session.commit()
    return tenant


async def set_tenant_status(
    session: AsyncSession,
    *,
    tenant_id: str,
    status: str,
    reason: str | None = None,
    actor: Any = None,
    request: Request | None = None,
) -> Tenant:
    """启用 / 禁用租户。

    禁用后该租户下所有账号将无法登录（登录流程校验租户状态，见 P-04）。
    账号本身的状态不变——这样「解禁」无需逐个恢复账号。
    """
    tenant = await get_tenant(session, tenant_id)
    if tenant.status == status:
        return tenant

    tenant.status = status
    await audit_service.record(
        session,
        action=AuditAction.UPDATE,
        actor=actor,
        tenant_id=tenant.id,
        resource_type="tenant",
        resource_id=tenant.id,
        summary=f"{'启用' if status == TenantStatus.ACTIVE else '禁用'}租户「{tenant.name}」",
        detail={"status": status, "reason": reason},
        request=request,
    )
    await session.commit()

    logger.info("租户 %s 状态变更为 %s（原因：%s）", tenant.code, status, reason or "未填写")
    return tenant


async def delete_tenant(
    session: AsyncSession,
    *,
    tenant_id: str,
    actor: Any = None,
    request: Request | None = None,
) -> None:
    """删除租户。

    ★ 修复 P-06：删除前校验关联数据（账号 / 授权 / 客户产品），
    有任一项即返回 ``CASCADE_CONFLICT`` 并**列出具体数量**，
    让运维知道该先处理什么，而不是抛一个数据库外键错误。

    Raises:
        AppException: 存在关联数据，或租户不存在。
    """
    tenant = await get_tenant(session, tenant_id)
    counts = await collect_counts(session, tenant.id)

    blockers: list[str] = []
    if counts.users:
        blockers.append(f"{counts.users} 个登录账号")
    if counts.authorizations:
        blockers.append(f"{counts.authorizations} 条产品授权")
    if counts.client_products:
        blockers.append(f"{counts.client_products} 个客户产品")

    if blockers:
        raise cascade_conflict(
            f"租户「{tenant.name}」下仍有 {'、'.join(blockers)}，无法删除。"
            "请先解除关联或改为「禁用」。",
            details={
                "users": counts.users,
                "authorizations": counts.authorizations,
                "clientProducts": counts.client_products,
            },
        )

    await session.delete(tenant)
    await audit_service.record(
        session,
        action=AuditAction.DELETE,
        actor=actor,
        resource_type="tenant",
        resource_id=tenant.id,
        summary=f"删除租户「{tenant.name}」（{tenant.code}）",
        request=request,
    )
    await session.commit()
    logger.warning("已删除租户 %s（%s）", tenant.code, tenant.name)


# ---------------------------------------------------------------------------
# 账号与密码
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class IssuedAccount:
    """新建账号 / 重置密码的结果。

    ``password`` 仅在服务端**自动生成**密码时返回明文（只返回一次）；
    调用方自行指定密码时为 ``None``（服务端不回显调用方提供的密码）。
    """

    account: UserAccount
    password: str | None = None


async def add_tenant_account(
    session: AsyncSession,
    *,
    tenant_id: str,
    account: str,
    nickname: str | None = None,
    role_code: str = "MERCHANT_ADMIN",
    password: str | None = None,
    actor: Any = None,
    request: Request | None = None,
) -> IssuedAccount:
    """为租户创建登录账号。

    未提供密码时自动生成强随机密码，并把账号标记为「下次登录必须改密」。

    Raises:
        AppException: 账号已存在 / 角色不允许 / 密码强度不足 / 租户不存在。
    """
    tenant = await get_tenant(session, tenant_id)

    if role_code not in TENANT_ROLE_CODES:
        raise validation_error(
            f"租户账号的角色只能是 {'、'.join(sorted(TENANT_ROLE_CODES))}",
            details={"field": "roleCode", "message": "角色不合法"},
        )

    exists = (
        await session.execute(select(UserAccount).where(UserAccount.account == account))
    ).scalar_one_or_none()
    if exists is not None:
        raise validation_error(
            f"账号「{account}」已存在",
            details={"field": "account", "message": "该账号已被占用"},
        )

    generated = not password
    raw_password = password or generate_password()
    validate_password_strength(raw_password)

    user = UserAccount(
        id=new_id("user"),
        account=account,
        password_hash=hash_password(raw_password),
        nickname=nickname or f"{tenant.name}管理员",
        role_code=role_code,
        tenant_id=tenant.id,
        status=UserStatus.ACTIVE,
        must_change_password=True,
    )
    session.add(user)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        actor=actor,
        tenant_id=tenant.id,
        resource_type="user_account",
        resource_id=user.id,
        summary=f"为租户「{tenant.name}」创建账号 {account}（{role_code}）",
        detail={"generatedPassword": generated},
        request=request,
    )
    await session.commit()

    return IssuedAccount(account=user, password=raw_password if generated else None)


async def reset_account_password(
    session: AsyncSession,
    *,
    tenant_id: str,
    account: str,
    new_password: str | None = None,
    actor: Any = None,
    request: Request | None = None,
) -> IssuedAccount:
    """重置租户账号密码。

    重置后：
    * 账号被置为「下次登录必须改密」
    * 清空登录失败计数与锁定状态（否则刚重置又被锁着）
    * 吊销该账号全部刷新令牌（在线的旧登录态立即失效）

    Raises:
        AppException: 账号不存在或不属于该租户 / 密码强度不足。
    """
    tenant = await get_tenant(session, tenant_id)

    user = (
        await session.execute(
            select(UserAccount).where(
                UserAccount.account == account, UserAccount.tenant_id == tenant.id
            )
        )
    ).scalar_one_or_none()
    if user is None:
        raise not_found("该租户下不存在此账号")

    generated = not new_password
    raw_password = new_password or generate_password()
    validate_password_strength(raw_password)

    user.password_hash = hash_password(raw_password)
    user.must_change_password = True
    user.login_failures = 0
    user.locked_until = None

    # 强制下线：旧登录态不应在改密后继续可用
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )

    await audit_service.record(
        session,
        action=AuditAction.UPDATE,
        actor=actor,
        tenant_id=tenant.id,
        resource_type="user_account",
        resource_id=user.id,
        summary=f"重置账号 {account} 的密码，并吊销其全部登录态",
        detail={"generatedPassword": generated},
        request=request,
    )
    await session.commit()

    logger.warning("已重置账号 %s 的密码（租户 %s）", account, tenant.code)
    return IssuedAccount(account=user, password=raw_password if generated else None)


__all__ = [
    "TENANT_ROLE_CODES",
    "IssuedAccount",
    "TenantCounts",
    "add_tenant_account",
    "collect_counts",
    "create_tenant",
    "delete_tenant",
    "get_tenant",
    "list_accounts",
    "list_industries",
    "list_tenants",
    "reset_account_password",
    "set_tenant_status",
    "update_tenant",
]
