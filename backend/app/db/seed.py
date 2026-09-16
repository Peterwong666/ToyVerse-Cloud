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

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.ids import new_id
from app.core.logging import get_logger
from app.core.permissions import FACTORY_ROLE_CODES, ROLE_PERMISSIONS
from app.core.security import hash_password
from app.db.base import utcnow
from app.db.session import SessionLocal
from app.models.catalog import (
    ClientProduct,
    CloudProvider,
    ProductAuthorization,
    ProductTemplate,
)
from app.models.device import Device, DeviceEvent
from app.models.enums import (
    BUILTIN_ROLES,
    ActivationStatus,
    AssetStatus,
    BindStatus,
    CloudProviderStatus,
    CloudVendor,
    EnableStatus,
    NetworkType,
    OnlineStatus,
    OrderStatus,
    OtaSupport,
    TenantStatus,
    UserStatus,
)
from app.models.identity import Role, RolePermission, Tenant, UserAccount
from app.models.order import Order
from app.models.org import Factory

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
# 演示工厂
# ---------------------------------------------------------------------------

DEMO_FACTORIES: tuple[dict[str, Any], ...] = (
    {
        "id": "f-demo-01",
        "code": "FACTORY-DEMO-01",
        "name": "星辰智造（演示工厂）",
        "contact_name": "赵厂长",
        "contact_phone": "13600000000",
        "address": "广东省深圳市宝安区演示工业园 1 栋",
        "daily_capacity": 2000,
        "is_verified": True,
        "remark": "演示用烧录工厂；工厂端账号绑定本厂，仅可见本厂工单",
    },
)


DEMO_CLOUD_PROVIDERS: tuple[dict[str, Any], ...] = (
    {
        "id": "cloud-volcano-hw",
        "code": "VOLCANO-HW",
        "name": "火山引擎智能云（硬件对话智能体）",
        "vendor": CloudVendor.VOLCANO,
        "network_type": NetworkType.WIFI,
        "api_base": "https://rtc.volcengineapi.com",
        "ota_support": OtaSupport.UNSUPPORTED,
        "remark": "端到端实时语音对话；密钥请在 .env 配置 VOLCANO_ACCESS_KEY / SECRET_KEY 后由管理员录入",
    },
    {
        "id": "cloud-jixian-4g",
        "code": "JIXIAN-4G",
        "name": "集贤 4G 设备云",
        "vendor": CloudVendor.JIXIAN,
        "network_type": NetworkType.FOUR_G,
        "api_base": None,
        "ota_support": OtaSupport.SUPPORTED,
        "remark": "演示用 4G 方案（未配置接口地址与密钥，连通性检测将返回「未配置」）",
    },
)

DEMO_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "tpl-esp32s3-toy",
        "code": "TPL-ESP32S3-TOY",
        "name": "ESP32-S3 智能玩具（Wi-Fi）",
        "category": "智能玩具",
        "model": "ESP32-S3",
        "chip": "乐鑫 ESP32-S3",
        "network_type": NetworkType.WIFI,
        "cloud_provider_id": "cloud-volcano-hw",
        "firmware_version": "1.0.0",
        "reference_price": 199.00,
        "ai_features": {"chat": True, "story": True, "music": True, "vision": False},
        "specs": {"尺寸": "80×80×90mm", "材质": "ABS + 硅胶", "电池": "1200mAh"},
        "description": "接入火山引擎硬件对话智能体的 Wi-Fi 方案智能玩具",
    },
    {
        "id": "tpl-4g-storyteller",
        "code": "TPL-4G-STORY",
        "name": "4G 故事机（集贤方案）",
        "category": "故事机",
        "model": "ML307N",
        "chip": "移芯 ML307N",
        "network_type": NetworkType.FOUR_G,
        "cloud_provider_id": "cloud-jixian-4g",
        "firmware_version": "1.2.3",
        "reference_price": 259.00,
        "ai_features": {"chat": True, "story": True, "music": True, "vision": False},
        "specs": {"尺寸": "120×90×60mm", "材质": "ABS", "电池": "2000mAh"},
        "description": "插卡即用的 4G 故事机方案，平台可推送 OTA",
    },
)

#: 授权关系：(模板 ID, 租户 ID)
DEMO_AUTHORIZATIONS: tuple[tuple[str, str], ...] = (
    ("tpl-esp32s3-toy", "t-001"),
    ("tpl-esp32s3-toy", "t-002"),
    ("tpl-4g-storyteller", "t-001"),
)

DEMO_CLIENT_PRODUCTS: tuple[dict[str, Any], ...] = (
    {
        "id": "prod-t001-cube",
        "tenant_id": "t-001",
        "template_id": "tpl-esp32s3-toy",
        "code": "CP-T001-CUBE",
        "name": "星辰小方块（Wi-Fi 智能故事机）",
        "remark": "演示：租户 t-001 基于 ESP32-S3 模板创建的自有产品",
    },
    {
        "id": "prod-t002-demo",
        "tenant_id": "t-002",
        "template_id": "tpl-esp32s3-toy",
        "code": "CP-T002-DEMO",
        "name": "体验店演示样机",
        "remark": "演示：用于验证租户间产品隔离（t-002 看不到 t-001 的产品）",
    },
)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


async def seed_demo_data() -> None:
    """初始化基础数据与演示数据。幂等。"""
    async with SessionLocal() as session:
        await _seed_roles(session)
        # 工厂必须先于工厂账号落库：``user_accounts.factory_id`` 是指向
        # ``factories`` 的外键，FK 在 SQLite 上也已开启（见 db/session.py）。
        await _seed_factories(session)
        await _seed_admin_users(session)

        if settings.SEED_DEMO_DATA:
            await _seed_tenants(session)
            await _seed_tenant_users(session)
            await _seed_catalog(session)
            await _seed_orders_devices(session)

        await session.commit()

    logger.info("基础数据与演示数据初始化完成")


# ---------------------------------------------------------------------------
# 角色与权限
# ---------------------------------------------------------------------------


async def _seed_factories(session: AsyncSession) -> None:
    """写入演示工厂，并把未绑定工厂的工厂账号挂到该厂（幂等）。

    为什么这一份**不受** ``SEED_DEMO_DATA`` 开关控制
    ------------------------------------------------
    工厂行不是「演示数据」，而是工厂端角色能存在的前提：没有工厂，
    工厂账号的 ``factory_id`` 只能为 ``NULL``，而工厂作用域过滤
    （:func:`app.core.deps.AuthContext.require_factory_id`）会直接 403
    ——工厂端变成「登录得进去、什么都点不开」，且原因完全不可见。
    与管理员账号同理（它们同样不受该开关控制），属**启动必需的最小数据**。

    为什么要补绑已有账号
    --------------------
    ``_seed_admin_users`` 是幂等的：账号已存在就跳过。因此在 P6 之前
    建好的库上，``u-factory-admin`` 早就存在、``factory_id`` 仍是 ``NULL``。
    只播种工厂而不回填，老库的工厂端会一直 403，运维从日志里看不出原因。
    这里用一条 ``UPDATE`` 把「工厂角色 + 未绑定工厂」的账号补齐归属，
    本身也是幂等的（第二次执行匹配 0 行）。
    """
    existing = set((await session.execute(select(Factory.code))).scalars().all())

    created = 0
    for spec in DEMO_FACTORIES:
        if str(spec["code"]) in existing:
            continue
        session.add(
            Factory(
                id=str(spec["id"]),
                code=str(spec["code"]),
                name=str(spec["name"]),
                contact_name=spec["contact_name"],
                contact_phone=spec["contact_phone"],
                address=spec["address"],
                status=EnableStatus.ENABLED,
                daily_capacity=int(spec["daily_capacity"]),
                is_verified=bool(spec["is_verified"]),
                remark=spec["remark"],
            )
        )
        created += 1

    if created:
        logger.info("已创建 %d 个演示工厂", created)

    # 先 flush，确保下一条 UPDATE 之前工厂行已经存在（FK 校验）
    await session.flush()

    default_factory_id = str(DEMO_FACTORIES[0]["id"])
    result = await session.execute(
        update(UserAccount)
        .where(
            UserAccount.role_code.in_(sorted(FACTORY_ROLE_CODES)),
            UserAccount.factory_id.is_(None),
        )
        .values(factory_id=default_factory_id)
    )
    if result.rowcount:  # type: ignore[attr-defined]
        logger.info("已为 %d 个工厂账号补齐工厂归属", result.rowcount)  # type: ignore[attr-defined]


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
    """写入各端的初始账号（平台超管 / 平台运营 / 商户管理员 / 工厂管理员）。

    密码来自环境变量，启动期已校验强度。若账号已存在则不改密码，
    避免每次重启都把线上密码重置回 `.env` 中的值。

    平台运营账号是**可选**的：未在环境变量里配置账号时跳过，
    这样「克隆后最快跑起来」的路径不需要多填一个口令。
    """
    admins: list[dict[str, Any]] = [
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
            # 工厂账号的作用域来自 factory_id（工厂跨租户，tenant_id 管不了它）。
            # 指向 _seed_factories 写入的演示工厂；两者在同一次事务里落库。
            "factory_id": str(DEMO_FACTORIES[0]["id"]),
            "id": "u-factory-admin",
        },
    ]

    if settings.PLATFORM_OPERATOR_ACCOUNT.strip():
        admins.append(
            {
                "account": settings.PLATFORM_OPERATOR_ACCOUNT,
                "password": settings.PLATFORM_OPERATOR_PASSWORD,
                "nickname": settings.PLATFORM_OPERATOR_NICKNAME,
                "role_code": "PLATFORM_OPERATOR",
                "tenant_id": None,
                "id": "u-platform-operator",
            }
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
                factory_id=admin.get("factory_id"),
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
# 目录域：云服务商 / 产品模板 / 授权 / 客户产品 / 小程序配置
# ---------------------------------------------------------------------------


async def _seed_catalog(session: AsyncSession) -> None:
    """写入目录域演示数据（幂等）。

    设计要点
    --------
    * 云服务商**不写入任何密钥**——演示数据里出现假密钥会让人误以为「已接入」，
      而 ADR-07 要求未配置密钥的能力必须安全失败。
      因此连通性检测会如实返回「未配置」，这本身就是一个可演示的安全特性。
    * 授权关系与客户产品成对写入，用于验证：
      ① 授权是创建客户产品的前置条件；② 租户只能看到自己的产品（ADR-08）。
    """
    existing_clouds = set(
        (await session.execute(select(CloudProvider.code))).scalars().all()
    )
    for spec in DEMO_CLOUD_PROVIDERS:
        if str(spec["code"]) in existing_clouds:
            continue
        session.add(
            CloudProvider(
                id=str(spec["id"]),
                code=str(spec["code"]),
                name=str(spec["name"]),
                vendor=str(spec["vendor"]),
                network_type=str(spec["network_type"]),
                api_base=spec["api_base"],
                ota_support=str(spec["ota_support"]),
                status=CloudProviderStatus.NOT_CONNECTED,
                remark=spec["remark"],
            )
        )
    await session.flush()

    existing_templates = set(
        (await session.execute(select(ProductTemplate.code))).scalars().all()
    )
    for spec in DEMO_TEMPLATES:
        if str(spec["code"]) in existing_templates:
            continue
        session.add(
            ProductTemplate(
                id=str(spec["id"]),
                code=str(spec["code"]),
                name=str(spec["name"]),
                category=spec["category"],
                model=spec["model"],
                chip=spec["chip"],
                network_type=str(spec["network_type"]),
                cloud_provider_id=spec["cloud_provider_id"],
                firmware_version=spec["firmware_version"],
                reference_price=spec["reference_price"],
                ai_features=spec["ai_features"],
                specs=spec["specs"],
                status=EnableStatus.ENABLED,
                description=spec["description"],
            )
        )
    await session.flush()

    existing_auth = {
        (row[0], row[1])
        for row in (
            await session.execute(
                select(ProductAuthorization.tenant_id, ProductAuthorization.template_id)
            )
        ).all()
    }
    for template_id, tenant_id in DEMO_AUTHORIZATIONS:
        if (tenant_id, template_id) in existing_auth:
            continue
        session.add(
            ProductAuthorization(
                id=new_id("product_authorization"),
                tenant_id=tenant_id,
                template_id=template_id,
                status=EnableStatus.ENABLED,
                authorized_at=utcnow(),
                authorized_by="seed",
                remark="演示数据：随种子数据写入",
            )
        )
    await session.flush()

    existing_products = set(
        (await session.execute(select(ClientProduct.code))).scalars().all()
    )
    for spec in DEMO_CLIENT_PRODUCTS:
        if str(spec["code"]) in existing_products:
            continue
        template = (
            await session.execute(
                select(ProductTemplate).where(ProductTemplate.id == spec["template_id"])
            )
        ).scalar_one()
        session.add(
            ClientProduct(
                id=str(spec["id"]),
                tenant_id=str(spec["tenant_id"]),
                template_id=template.id,
                code=str(spec["code"]),
                name=str(spec["name"]),
                # 与创建接口一致：联网方式 / 云服务商 / 固件版本从模板快照
                network_type=template.network_type,
                cloud_provider_id=template.cloud_provider_id,
                firmware_version=template.firmware_version,
                status=EnableStatus.ENABLED,
                ai_enabled=False,
                remark=spec["remark"],
            )
        )
    await session.flush()
    logger.info("目录域演示数据初始化完成")


# ---------------------------------------------------------------------------
# 订单与设备（P4 演示数据）
# ---------------------------------------------------------------------------

#: 平台自有库存设备数量（P5「分配」链路的输入，见 ``_seed_orders_devices`` ③）
PLATFORM_STOCK_COUNT = 4


async def _seed_orders_devices(session: AsyncSession) -> None:
    """写入 P4 演示订单与设备（幂等）。

    设计要点
    --------
    * 两个订单覆盖两种**典型时序**：一个已入库（含设备，用于演示订单详情 /
      设备列表 / 二维码导出），一个待审核（用于演示审核流程）。
      待审核单刻意没有设备——真实业务里设备是审核通过之后才生成的。
    * 设备 SN 用**固定值**而不是随机生成：演示数据必须可重现。
      用随机 SN 的话每次启动都会新增一批设备，「幂等」就名存实亡了。
    * 设备状态刻意做成混合态（已入库 / 已冻结 / 已分配），否则四维状态
      在界面上看不出差别，演示价值会打折扣。
    * 第三块补一批 **平台自有库存**（``tenant_id`` 为空、``IN_STOCK``），
      作为 P5 分配链路的输入——订单生成的设备已经挂在租户名下，
      没有这批数据就演示不了「平台把库存划给租户」。
    * 事件的 ``actor_account`` 记为 ``seed``，与真实操作用户可区分。
    """
    product = (
        await session.execute(select(ClientProduct).where(ClientProduct.id == "prod-t001-cube"))
    ).scalar_one_or_none()
    if product is None:
        logger.warning("演示客户产品不存在，跳过订单与设备种子数据")
        return

    existing_orders = set((await session.execute(select(Order.order_no))).scalars().all())

    # ---- ① 已入库订单（含设备） ----
    if "ORD-20260101-DEMO" not in existing_orders:
        order = Order(
            id="o-demo-stocked",
            order_no="ORD-20260101-DEMO",
            tenant_id="t-001",
            client_product_id=product.id,
            quantity=5,
            status=str(OrderStatus.IN_STOCK),
            network_type=product.network_type,
            applicant_name="王经理",
            applicant_phone="13800000001",
            remark="演示订单：已审核、已生成设备并入库",
            audited_by="admin",
            audited_at=utcnow(),
            audit_remark="演示数据自动审核通过",
            generated_count=5,
            generated_at=utcnow(),
            generation_detail={
                "ok": True,
                "requested": 5,
                "generated": 5,
                "failed": 0,
                # 演示产品的联网方式是 WIFI → 京东 JD 格式本地生成
                "format": "JD",
                "simulated": True,
                "vendor": "种子数据：本地生成，未调用厂商接口",
            },
        )
        session.add(order)
        await session.flush()

        # (资产状态, 激活状态, 在线状态, 绑定状态, 是否追加冻结/分配事件)
        layout: tuple[tuple[str, str, str, str], ...] = (
            (AssetStatus.IN_STOCK, ActivationStatus.NOT_ACTIVATED, OnlineStatus.NEVER_ONLINE, BindStatus.UNBOUND),
            (AssetStatus.IN_STOCK, ActivationStatus.NOT_ACTIVATED, OnlineStatus.NEVER_ONLINE, BindStatus.UNBOUND),
            (AssetStatus.IN_STOCK, ActivationStatus.NOT_ACTIVATED, OnlineStatus.NEVER_ONLINE, BindStatus.UNBOUND),
            (AssetStatus.FROZEN, ActivationStatus.NOT_ACTIVATED, OnlineStatus.NEVER_ONLINE, BindStatus.UNBOUND),
            (AssetStatus.ALLOCATED, ActivationStatus.NOT_ACTIVATED, OnlineStatus.NEVER_ONLINE, BindStatus.UNBOUND),
        )
        for index, (asset, activation, online, bind) in enumerate(layout, start=1):
            frozen = asset == AssetStatus.FROZEN
            device = Device(
                id=f"d-demo-{index:02d}",
                tenant_id="t-001",
                order_id=order.id,
                client_product_id=product.id,
                sn=f"SN-20260101-DEMO{index:02d}",
                mac=f"AA:BB:CC:00:00:{index:02X}",
                network_type=product.network_type,
                firmware_version=product.firmware_version,
                asset_status=str(asset),
                previous_asset_status=str(AssetStatus.IN_STOCK) if frozen else None,
                activation_status=str(activation),
                online_status=str(online),
                bind_status=str(bind),
                generated_at=utcnow(),
                frozen_at=utcnow() if frozen else None,
                freeze_reason="演示：人为冻结设备以展示冻结态" if frozen else None,
                remark=f"订单 {order.order_no} 演示设备",
            )
            session.add(device)
            await session.flush()

            chain: list[tuple[str, str | None, str | None]] = [
                ("GENERATED", str(AssetStatus.PENDING_GEN), str(AssetStatus.GENERATED)),
                ("IN_STOCK", str(AssetStatus.GENERATED), str(AssetStatus.IN_STOCK)),
            ]
            if frozen:
                chain.append(("FROZEN", str(AssetStatus.IN_STOCK), str(AssetStatus.FROZEN)))
            if asset == AssetStatus.ALLOCATED:
                chain.append(("ALLOCATED", str(AssetStatus.IN_STOCK), str(AssetStatus.ALLOCATED)))

            for event_type, from_status, to_status in chain:
                session.add(
                    DeviceEvent(
                        id=new_id("device_event"),
                        device_id=device.id,
                        tenant_id=device.tenant_id,
                        event_type=event_type,
                        dimension="asset",
                        from_status=from_status,
                        to_status=to_status,
                        actor_account="seed",
                        summary="演示数据：随种子写入的设备流转事件",
                    )
                )

    # ---- ② 待审核订单（无设备，演示审核流程） ----
    if "ORD-20260102-DEMO" not in existing_orders:
        session.add(
            Order(
                id="o-demo-pending",
                order_no="ORD-20260102-DEMO",
                tenant_id="t-001",
                client_product_id=product.id,
                quantity=3,
                status=str(OrderStatus.PENDING_AUDIT),
                network_type=product.network_type,
                applicant_name="李经理",
                applicant_phone="13800000002",
                remark="演示订单：待平台审核（用于演示审核与驳回流程）",
            )
        )

    # ---- ③ 平台库存设备（P5：分配链路的输入） ----
    #
    # 为什么需要单独造一批「平台库存」设备？
    # 订单生成的设备在建时就带了 `tenant_id`（属于下单的那个租户），
    # 而分配单的语义是「把**平台自有**库存划给租户」，只接受
    # `tenant_id` 为空且 `asset_status=IN_STOCK` 的设备
    # （见 `ALLOCATABLE_ASSET_STATUSES` 与 `ALLOCATABLE` 的校验）。
    # 没有这批数据时，分配页的「选择设备」永远是空的——
    # 功能可用但演示不可达，等于没有交付。
    #
    # 刻意不设 `client_product_id`：平台库存设备尚未决定卖给哪个产品，
    # 产品是在分配时由分配单指定的（这正是分配单要带 clientProductId 的原因）。
    existing_device_ids = set(
        (await session.execute(select(Device.id).where(Device.id.like("d-stock-%")))).scalars().all()
    )
    if len(existing_device_ids) < PLATFORM_STOCK_COUNT:
        for index in range(1, PLATFORM_STOCK_COUNT + 1):
            device_id = f"d-stock-{index:02d}"
            if device_id in existing_device_ids:
                continue
            device = Device(
                id=device_id,
                tenant_id=None,  # 空 = 平台自有库存，尚未分配给任何租户
                order_id=None,
                client_product_id=None,  # 由分配单在执行时指定
                sn=f"SN-20260101-STOCK{index:02d}",
                mac=f"AA:BB:CC:11:00:{index:02X}",
                network_type=str(NetworkType.WIFI),
                asset_status=str(AssetStatus.IN_STOCK),
                activation_status=str(ActivationStatus.NOT_ACTIVATED),
                online_status=str(OnlineStatus.NEVER_ONLINE),
                bind_status=str(BindStatus.UNBOUND),
                generated_at=utcnow(),
                remark="演示数据：平台自有库存，用于演示「分配」链路",
            )
            session.add(device)
            await session.flush()
            for event_type, from_status, to_status in (
                ("GENERATED", str(AssetStatus.PENDING_GEN), str(AssetStatus.GENERATED)),
                ("IN_STOCK", str(AssetStatus.GENERATED), str(AssetStatus.IN_STOCK)),
            ):
                session.add(
                    DeviceEvent(
                        id=new_id("device_event"),
                        device_id=device.id,
                        tenant_id=None,
                        event_type=event_type,
                        dimension="asset",
                        from_status=from_status,
                        to_status=to_status,
                        actor_account="seed",
                        summary="演示数据：平台库存设备入库",
                    )
                )

    await session.flush()
    logger.info("订单与设备演示数据初始化完成")


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
            account
            for account in (
                settings.PLATFORM_ADMIN_ACCOUNT,
                settings.PLATFORM_OPERATOR_ACCOUNT,
                settings.MERCHANT_ADMIN_ACCOUNT,
                settings.FACTORY_ADMIN_ACCOUNT,
            )
            if account
        ],
        "seededAt": utcnow().isoformat(),
    }
