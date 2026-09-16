"""演示数据与基础数据初始化。

设计原则
--------
* **幂等**：可重复执行，已存在的记录不会重复插入，也不会被覆盖（除必要的权限补齐）
* **与迁移分离**：种子数据不进 Alembic 迁移脚本，
  由 ``SEED_DEMO_DATA`` 开关控制，避免演示数据流入生产环境
* **无硬编码口令**：三个管理员账号的密码一律来自环境变量，
  且服务启动时会做强度校验（见 ``Settings.validate_security``）

基础数据（角色与权限）无论是否开启演示数据都会写入——它们是系统运行所必需的。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.ids import new_id
from app.core.logging import get_logger
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import hash_password
from app.db.base import utcnow
from app.db.session import SessionLocal
from app.models.enums import BUILTIN_ROLES, TenantStatus, UserStatus
from app.models.identity import Role, RolePermission, Tenant, UserAccount

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 演示租户
# ---------------------------------------------------------------------------

DEMO_TENANTS: tuple[dict[str, Any], ...] = (
    {
        "id": "t-001",
        "code": "DEMO-BRAND",
        "name": "星辰玩具（演示租户）",
        "contact_name": "王经理",
        "contact_phone": "13800000001",
        "email": "wang@demo-brand.example.com",
        "industry": "玩具品牌商",
        "remark": "演示用品牌方租户，含完整产品与设备数据",
    },
    {
        "id": "t-002",
        "code": "DEMO-STORE",
        "name": "深圳体验店（演示租户）",
        "contact_name": "李店长",
        "contact_phone": "13800000002",
        "email": "li@demo-store.example.com",
        "industry": "零售体验店",
        "remark": "演示用零售租户，用于验证租户数据隔离",
    },
)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


async def seed_demo_data() -> None:
    """初始化基础数据与演示数据。幂等。"""
    async with SessionLocal() as session:
        await _seed_roles(session)
        await _seed_admin_users(session)

        if settings.SEED_DEMO_DATA:
            await _seed_tenants(session)
            await _seed_tenant_users(session)

        await session.commit()

    logger.info("基础数据与演示数据初始化完成")


# ---------------------------------------------------------------------------
# 角色与权限
# ---------------------------------------------------------------------------


async def _seed_roles(session: AsyncSession) -> None:
    """写入内置角色与权限。已存在的角色只补齐缺失的权限。"""
    existing_roles = {
        role.code: role for role in (await session.execute(select(Role))).scalars().all()
    }

    created = 0
    for code, (name, role_type) in BUILTIN_ROLES.items():
        role = existing_roles.get(code)
        if role is None:
            role = Role(
                id=f"r-{code.lower().replace('_', '-')}",
                code=code,
                name=name,
                role_type=str(role_type),
                description=f"内置角色：{name}",
                is_builtin=True,
            )
            session.add(role)
            await session.flush()
            existing_roles[code] = role
            created += 1

        # 补齐权限（幂等：按主键判断）
        wanted = ROLE_PERMISSIONS.get(code, frozenset())
        current = {
            row.permission
            for row in (
                await session.execute(
                    select(RolePermission).where(RolePermission.role_id == role.id)
                )
            )
            .scalars()
            .all()
        }
        for permission in wanted - current:
            session.add(RolePermission(role_id=role.id, permission=permission))

    if created:
        logger.info("已创建 %d 个内置角色", created)
    logger.info("角色权限初始化完成（共 %d 个角色）", len(existing_roles))


# ---------------------------------------------------------------------------
# 管理员账号
# ---------------------------------------------------------------------------


async def _seed_admin_users(session: AsyncSession) -> None:
    """写入三个端的管理员账号。

    密码来自环境变量，启动期已校验强度。若账号已存在则不改密码，
    避免每次重启都把线上密码重置回 `.env` 中的值。
    """
    admins: tuple[dict[str, Any], ...] = (
        {
            "account": settings.PLATFORM_ADMIN_ACCOUNT,
            "password": settings.PLATFORM_ADMIN_PASSWORD,
            "nickname": settings.PLATFORM_ADMIN_NICKNAME,
            "role_code": "PLATFORM_ADMIN",
            "tenant_id": None,  # 平台管理员是全局长，不属于任何租户
            "id": "u-platform-admin",
        },
        {
            "account": settings.MERCHANT_ADMIN_ACCOUNT,
            "password": settings.MERCHANT_ADMIN_PASSWORD,
            "nickname": settings.MERCHANT_ADMIN_NICKNAME,
            "role_code": "MERCHANT_ADMIN",
            "tenant_id": "t-001",
            "id": "u-merchant-admin",
        },
        {
            "account": settings.FACTORY_ADMIN_ACCOUNT,
            "password": settings.FACTORY_ADMIN_PASSWORD,
            "nickname": settings.FACTORY_ADMIN_NICKNAME,
            "role_code": "FACTORY_ADMIN",
            "tenant_id": None,  # 工厂跨租户，不属于任何租户
            "id": "u-factory-admin",
        },
    )

    created = 0
    for admin in admins:
        account = str(admin["account"] or "").strip()
        if not account:
            continue

        exists = (
            await session.execute(select(UserAccount).where(UserAccount.account == account))
        ).scalar_one_or_none()
        if exists is not None:
            continue

        session.add(
            UserAccount(
                id=str(admin["id"]),
                account=account,
                password_hash=hash_password(str(admin["password"])),
                nickname=str(admin["nickname"]),
                role_code=str(admin["role_code"]),
                tenant_id=admin["tenant_id"],
                status=UserStatus.ACTIVE,
                must_change_password=settings.FORCE_PASSWORD_CHANGE_ON_FIRST_LOGIN,
            )
        )
        created += 1

    if created:
        logger.info("已创建 %d 个管理员账号", created)


# ---------------------------------------------------------------------------
# 演示租户
# ---------------------------------------------------------------------------


async def _seed_tenants(session: AsyncSession) -> None:
    """写入演示租户。"""
    existing = {
        tenant.code for tenant in (await session.execute(select(Tenant))).scalars().all()
    }

    created = 0
    for spec in DEMO_TENANTS:
        if spec["code"] in existing:
            continue
        session.add(
            Tenant(
                id=str(spec["id"]),
                code=str(spec["code"]),
                name=str(spec["name"]),
                status=TenantStatus.ACTIVE,
                contact_name=spec["contact_name"],
                contact_phone=spec["contact_phone"],
                email=spec["email"],
                industry=spec["industry"],
                remark=spec["remark"],
            )
        )
        created += 1

    if created:
        logger.info("已创建 %d 个演示租户", created)


async def _seed_tenant_users(session: AsyncSession) -> None:
    """为演示租户补充一个运营账号（便于验证商户端权限差异）。"""
    account = "15555555556"
    password = settings.MERCHANT_ADMIN_PASSWORD
    if not password:
        return

    exists = (
        await session.execute(select(UserAccount).where(UserAccount.account == account))
    ).scalar_one_or_none()
    if exists is not None:
        return

    session.add(
        UserAccount(
            id=new_id("user"),
            account=account,
            password_hash=hash_password(password),
            nickname="体验店运营",
            role_code="MERCHANT_OPERATOR",
            tenant_id="t-002",
            status=UserStatus.ACTIVE,
            must_change_password=False,
        )
    )
    logger.info("已创建演示运营账号 %s", account)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


async def count_users(session: AsyncSession) -> int:
    """统计用户数量（供脚本与测试使用）。"""
    return int(
        (await session.execute(select(func.count()).select_from(UserAccount))).scalar_one()
    )


def seed_summary() -> dict[str, Any]:
    """返回当前种子数据配置摘要（供启动日志与文档使用）。"""
    return {
        "demoDataEnabled": settings.SEED_DEMO_DATA,
        "tenants": [t["code"] for t in DEMO_TENANTS] if settings.SEED_DEMO_DATA else [],
        "roles": sorted(BUILTIN_ROLES),
        "adminAccounts": [
            settings.PLATFORM_ADMIN_ACCOUNT,
            settings.MERCHANT_ADMIN_ACCOUNT,
            settings.FACTORY_ADMIN_ACCOUNT,
        ],
        "seededAt": utcnow().isoformat(),
    }
