"""集成测试：P6 工厂生产链路（正向闭环 + 负路径 + 跨厂隔离 + 权限 + 入库）。

被验证的业务规则
----------------
1. **两条状态链同时被工厂动作驱动**（本阶段最容易写错的地方）::

       派单       订单 IN_STOCK → PRODUCING          设备 IN_STOCK  → PRODUCING
       烧录报满   订单 PRODUCING → SHIPPED_TO_CLIENT  设备 PRODUCING → PRODUCED
       出货登记   （订单不动）                        设备 PRODUCED  → SHIPPED

   每一步都要能在**设备事件时间线**上查到（状态不是「只在内存里变过」）。
2. **每一次拒绝都必须是可执行的信息**：断言错误码 + ``details``，
   而不只是状态码（``409`` 说不出「是订单状态不对，还是工厂停用了」）。
3. **工厂两层保护**：工厂作用域（跨厂一律 404 而非 403，不泄漏存在性）
   ＋ 字段脱敏（脱敏由 ``test_factory_desensitize.py`` 单独负责）。
4. **权限边界**：商户 token 进不了工厂端，工厂 token 进不了平台端，
   ``FACTORY_OPERATOR`` 读不到固件（该权限只给 ``FACTORY_ADMIN``）。
5. **批量入库逐台语义**与**幂等**：``moved/skipped/failed`` 三个计数精确对应，
   重跑一次全部变 ``skipped`` 且**不产生新的设备事件**。

造数策略
--------
* 全链路正向用例走**真实 HTTP 链**（下单 → 审核 → 生成 → 派单…），
  因为「链路本身」就是要验证的对象；
* 负路径用例用**库级造数**直接落一条 ``IN_STOCK`` 订单与设备
  （与 ``test_order_flow.py`` 的设备生命周期用例同一取舍）：
  要验证的是「状态机与校验顺序」，让造数过程不成为变量。

测试写法对齐 ``tests/integration/test_binding.py``：模块级 helper、
camelCase 断言、中文注释说明断言依据。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext
from app.core.ids import new_id
from app.db.base import utcnow
from app.models.device import Device, DeviceEvent
from app.models.enums import (
    ActivationStatus,
    AssetStatus,
    BindStatus,
    EnableStatus,
    OnlineStatus,
    OrderStatus,
    RoleType,
)
from app.models.factory import FactoryOrder
from app.models.identity import UserAccount
from app.models.order import Order
from app.models.org import Factory
from app.services import catalog_service
from tests.conftest import API_PREFIX

pytestmark = pytest.mark.integration

PLATFORM = f"{API_PREFIX}/platform"
MERCHANT = f"{API_PREFIX}/merchant"
FACTORY = f"{API_PREFIX}/factory"

#: 测试账号口令（仅存在于测试进程）
MERCHANT_PASSWORD = "Merch4nt-Flow#2026"
FACTORY_PASSWORD = "Fact0ry-Flow#2026"

#: 工厂端错误码白名单（防止实现返回编造的错误码）
_ERROR_CODES = frozenset(
    {
        "UNAUTHENTICATED",
        "PERMISSION_DENIED",
        "VALIDATION_ERROR",
        "RESOURCE_NOT_FOUND",
        "DEVICE_NOT_FOUND",
        "DEVICE_NOT_AVAILABLE",
        "INVALID_STATE_TRANSITION",
        "FACTORY_ORDER_EXISTS",
        "BURN_COUNT_EXCEEDED",
        "IDEMPOTENCY_CONFLICT",
    }
)


# ---------------------------------------------------------------------------
# 通用辅助
# ---------------------------------------------------------------------------


def _platform_ctx() -> AuthContext:
    """平台超管上下文（通配权限），供服务层建链使用。"""
    return AuthContext(
        user_id="u-factory-flow-it",
        account="factory-flow-it",
        role_code="PLATFORM_ADMIN",
        role_type=RoleType.PLATFORM,
        tenant_id=None,
        permissions=["*"],
    )


def _assert_error(response: Any, status: int, code: str) -> dict[str, Any]:
    """断言失败响应的状态码与错误码，并顺带钉死错误码契约。"""
    assert response.status_code == status, (
        f"期望 {status} 得到 {response.status_code}：{response.text}"
    )
    body = response.json()
    assert body["code"] == code, response.text
    assert body["code"] in _ERROR_CODES, f"返回了未登记的错误码：{body['code']}"
    assert isinstance(body["message"], str) and body["message"]
    assert "traceId" in body
    return body


async def _seed_catalog(
    db: AsyncSession, tenant_id: str, *, suffix: str, model: str = "ESP32-S3"
) -> dict[str, str]:
    """建「云服务商 → 模板 → 授权 → 客户产品」最小链条（走真实服务层）。

    ``WIFI``（京东方案）：设备 SN 由平台本地生成，不依赖厂商接口与密钥。
    """
    platform = _platform_ctx()
    cloud = await catalog_service.create_cloud(
        db,
        payload={
            "code": f"CLOUD-{suffix}",
            "name": f"{suffix} 测试云",
            "vendor": "JOYINSIDE",
            "network_type": "WIFI",
        },
        actor=platform,
    )
    template = await catalog_service.create_template(
        db,
        payload={
            "code": f"TPL-{suffix}",
            "name": f"{suffix} 测试模板",
            "network_type": "WIFI",
            "cloud_provider_id": cloud.id,
            "firmware_version": "2.0.0",
            "model": model,
            "status": "ENABLED",
        },
        actor=platform,
    )
    await catalog_service.authorize_template(
        db, template_id=template.id, tenant_ids=[tenant_id], actor=platform
    )
    product = await catalog_service.create_client_product(
        db,
        payload={"tenant_id": tenant_id, "template_id": template.id, "code": f"PROD-{suffix}"},
        actor=platform,
    )
    return {"cloud_id": cloud.id, "template_id": template.id, "product_id": product.id}


async def _merchant_headers(
    auth: Any, make_user: Any, tenant_id: str, account: str
) -> dict[str, str]:
    """建一个商户管理员并登录，返回鉴权头。"""
    await make_user(
        account=account, password=MERCHANT_PASSWORD, role_code="MERCHANT_ADMIN", tenant_id=tenant_id
    )
    return auth.headers(await auth.token(account, MERCHANT_PASSWORD))


async def _make_factory(
    db: AsyncSession, *, code: str, status: str = str(EnableStatus.ENABLED)
) -> Factory:
    """直接落一家工厂。"""
    factory = Factory(
        id=new_id("factory"),
        code=code,
        name=f"{code} 智造",
        contact_name="赵厂长",
        contact_phone="13600000000",
        status=status,
        daily_capacity=1000,
        is_verified=True,
    )
    db.add(factory)
    await db.flush()
    return factory


async def _make_factory_account(
    db: AsyncSession,
    *,
    account: str,
    factory_id: str | None,
    role_code: str = "FACTORY_ADMIN",
) -> UserAccount:
    """直接落一个工厂账号（归属来自 ``user_accounts.factory_id``，P6 新增）。"""
    from app.core.security import hash_password

    user = UserAccount(
        id=new_id("user"),
        account=account,
        password_hash=hash_password(FACTORY_PASSWORD),
        nickname=account,
        role_code=role_code,
        tenant_id=None,  # 工厂跨租户
        factory_id=factory_id,
        status="ACTIVE",
    )
    db.add(user)
    await db.flush()
    return user


async def _factory_headers(
    auth: Any,
    db: AsyncSession,
    *,
    factory: Factory,
    account: str,
    role_code: str = "FACTORY_ADMIN",
) -> dict[str, str]:
    """建工厂账号并登录，返回鉴权头。"""
    await _make_factory_account(db, account=account, factory_id=factory.id, role_code=role_code)
    return auth.headers(await auth.token(account, FACTORY_PASSWORD))


async def _make_device(
    db: AsyncSession,
    *,
    sn: str,
    order_id: str | None = None,
    tenant_id: str | None = None,
    product_id: str | None = None,
    asset_status: str = str(AssetStatus.IN_STOCK),
) -> Device:
    """直接落一台设备（库级造数，避免造数过程成为变量）。"""
    device = Device(
        id=new_id("device"),
        sn=sn,
        tenant_id=tenant_id,
        order_id=order_id,
        client_product_id=product_id,
        network_type="WIFI",
        asset_status=asset_status,
        activation_status=str(ActivationStatus.NOT_ACTIVATED),
        online_status=str(OnlineStatus.NEVER_ONLINE),
        bind_status=str(BindStatus.UNBOUND),
        generated_at=utcnow(),
    )
    db.add(device)
    await db.flush()
    return device


async def _lib_stocked_order(
    db: AsyncSession,
    *,
    tenant_id: str,
    product_id: str,
    quantity: int,
    sn_prefix: str,
    with_devices: bool = True,
) -> tuple[Order, list[Device]]:
    """库级造一条 ``IN_STOCK`` 订单（可选带设备）。

    负路径用例只需要「一条已入库、可派单的订单」这个前提；
    走 HTTP 造数会让每个负路径用例多付一次完整链路的时间，
    且把跟断言无关的链路引入变量。
    """
    order = Order(
        id=new_id("order"),
        order_no=f"ORD-{uuid.uuid4().hex[:8].upper()}",
        tenant_id=tenant_id,
        client_product_id=product_id,
        quantity=quantity,
        unit_price=100.0,
        total_amount=100.0 * quantity,
        status=str(OrderStatus.IN_STOCK),
        network_type="WIFI",
        applicant_name="李经理",
        applicant_phone="13800000002",
        generated_count=quantity if with_devices else 0,
        generated_at=utcnow(),
    )
    db.add(order)
    await db.flush()

    devices: list[Device] = []
    if with_devices:
        for index in range(quantity):
            devices.append(
                await _make_device(
                    db,
                    sn=f"{sn_prefix}-{index + 1:02d}",
                    order_id=order.id,
                    tenant_id=tenant_id,
                    product_id=product_id,
                )
            )
    return order, devices


async def _dispatch(
    client: AsyncClient, platform_headers: dict[str, str], order_id: str, factory_id: str, **extra: Any
) -> Any:
    """派单（不预设状态码，便于失败用例断言 4xx）。"""
    return await client.post(
        f"{PLATFORM}/orders/{order_id}/dispatch",
        headers=platform_headers,
        json={"factoryId": factory_id, **extra},
    )


async def _dispatch_ok(
    client: AsyncClient, platform_headers: dict[str, str], order_id: str, factory_id: str, **extra: Any
) -> dict[str, Any]:
    """派单并断言 201，返回工单详情。"""
    response = await _dispatch(client, platform_headers, order_id, factory_id, **extra)
    assert response.status_code == 201, response.text
    return response.json()


async def _burn(
    client: AsyncClient, factory_headers: dict[str, str], factory_order_id: str, burned_count: int
) -> Any:
    """烧录上报（不预设状态码）。"""
    return await client.post(
        f"{FACTORY}/orders/{factory_order_id}/burn",
        headers=factory_headers,
        json={"burnedCount": burned_count},
    )


async def _ship(
    client: AsyncClient, factory_headers: dict[str, str], factory_order_id: str
) -> Any:
    return await client.post(f"{FACTORY}/orders/{factory_order_id}/ship", headers=factory_headers)


async def _inspect(
    client: AsyncClient,
    factory_headers: dict[str, str],
    factory_order_id: str,
    sn: str,
    result: str = "PASS",
    **extra: Any,
) -> Any:
    return await client.post(
        f"{FACTORY}/orders/{factory_order_id}/inspect",
        headers=factory_headers,
        json={"sn": sn, "result": result, **extra},
    )


async def _device_row(db: AsyncSession, device_id: str) -> Device:
    """直接从库读设备（响应可能被缓存 / 装配，库才是权威）。"""
    return (await db.execute(select(Device).where(Device.id == device_id))).scalar_one()


async def _order_row(db: AsyncSession, order_id: str) -> Order:
    return (await db.execute(select(Order).where(Order.id == order_id))).scalar_one()


async def _factory_order_row(db: AsyncSession, factory_order_id: str) -> FactoryOrder:
    return (
        await db.execute(select(FactoryOrder).where(FactoryOrder.id == factory_order_id))
    ).scalar_one()


async def _route_asset_events(
    client: AsyncClient, platform_headers: dict[str, str], device_id: str
) -> list[tuple[str, str | None, str | None]]:
    """读设备事件时间线里的**资产维度**事件（eventType, from, to）。"""
    response = await client.get(f"{PLATFORM}/devices/{device_id}/events", headers=platform_headers)
    assert response.status_code == 200, response.text
    return [
        (item["eventType"], item["fromStatus"], item["toStatus"])
        for item in response.json()["records"]
        if item["dimension"] == "asset"
    ]


async def _count_in_stock_events(db: AsyncSession, device_id: str) -> int:
    """某设备时间线上 ``IN_STOCK`` 事件的条数（用于入库幂等断言）。"""
    rows = (
        await db.execute(
            select(DeviceEvent).where(
                DeviceEvent.device_id == device_id, DeviceEvent.event_type == "IN_STOCK"
            )
        )
    ).scalars()
    return len(list(rows))


# ===========================================================================
# 一、全链路正向
# ===========================================================================


class TestFactoryOrderHappyPath:
    """下单 → 审核 → 生成 → 派单 → 分批烧录 → 抽检 → 出货，两条状态链逐跳核对。"""

    async def test_full_chain_drives_device_and_order_states(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ 全链路正向：每一步都同时核对工单 / 订单 / 设备 / 事件时间线。"""
        tenant = await make_tenant(code="FLOW-A", name="链路测试租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWA")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13300001001")
        platform = await auth.platform_headers()
        factory = await _make_factory(db, code="FAC-FLOWA")
        fh = await _factory_headers(auth, db, factory=factory, account="13600002001")

        # ---- ① 商户下单 → 审核 → 生成设备入库（订单落到 IN_STOCK）----
        created = await client.post(
            f"{MERCHANT}/orders",
            headers=merchant,
            json={
                "clientProductId": catalog["product_id"],
                "quantity": 3,
                "applicantName": "王经理",
                "applicantPhone": "13800000001",
            },
        )
        assert created.status_code == 201, created.text
        order_id = created.json()["id"]
        assert (await client.post(
            f"{PLATFORM}/orders/{order_id}/audit",
            headers=platform,
            json={"decision": "APPROVED"},
        )).status_code == 200
        generated = await client.post(f"{PLATFORM}/orders/{order_id}/generate", headers=platform)
        assert generated.status_code == 200, generated.text

        devices_body = await client.get(
            f"{PLATFORM}/devices", headers=platform, params={"orderId": order_id}
        )
        assert devices_body.status_code == 200, devices_body.text
        devices = devices_body.json()["records"]
        assert len(devices) == 3
        assert {d["assetStatus"] for d in devices} == {str(AssetStatus.IN_STOCK)}
        order_row = await _order_row(db, order_id)
        assert order_row.status == str(OrderStatus.IN_STOCK), "派单的前置状态"

        # ---- ② 派单：订单 IN_STOCK → PRODUCING，设备 IN_STOCK → PRODUCING，工单 PENDING ----
        dispatched = await _dispatch_ok(client, platform, order_id, factory.id)
        factory_order_id = dispatched["id"]
        assert dispatched["status"] == "PENDING"
        assert dispatched["statusLabel"] == "待生产"
        assert dispatched["quantity"] == 3
        assert dispatched["burnedCount"] == 0
        assert dispatched["remaining"] == 3
        assert dispatched["progressPercent"] == 0.0
        assert dispatched["orderStatus"] == str(OrderStatus.PRODUCING)
        assert dispatched["orderStatusLabel"] == "生产中"
        assert dispatched["factoryId"] == factory.id
        assert dispatched["factoryOrderNo"].startswith("FO-")
        assert dispatched["orderNo"] == created.json()["orderNo"], "订单号供工厂对账"

        assert (await _order_row(db, order_id)).status == str(OrderStatus.PRODUCING)
        for device in devices:
            assert (await _device_row(db, device["id"])).asset_status == str(AssetStatus.PRODUCING)
            events = await _route_asset_events(client, platform, device["id"])
            assert ("PRODUCING", str(AssetStatus.IN_STOCK), str(AssetStatus.PRODUCING)) in events, (
                f"设备 {device['sn']} 的派单迁移必须在事件时间线上留痕"
            )

        # ---- ③ 分批烧录：1 台（未满，工单 PENDING → PRODUCING）----
        partial = await _burn(client, fh, factory_order_id, 1)
        assert partial.status_code == 200, partial.text
        partial_body = partial.json()
        assert partial_body["burnedCount"] == 1
        assert partial_body["remaining"] == 2
        assert partial_body["progressPercent"] == 33.3
        assert partial_body["status"] == "PRODUCING"
        assert partial_body["statusLabel"] == "生产中"
        assert partial_body["orderStatus"] == str(OrderStatus.PRODUCING), "未报满时订单不动"
        assert (await _factory_order_row(db, factory_order_id)).burned_count == 1
        for device in devices:
            assert (await _device_row(db, device["id"])).asset_status == str(AssetStatus.PRODUCING)

        # ---- ④ 再报 1 台（累加、仍未满）----
        second = await _burn(client, fh, factory_order_id, 1)
        assert second.status_code == 200, second.text
        assert second.json()["burnedCount"] == 2
        assert second.json()["remaining"] == 1
        assert second.json()["status"] == "PRODUCING"

        # ---- ⑤ 报满：工单 → COMPLETED，设备 → PRODUCED，订单 → SHIPPED_TO_CLIENT ----
        final = await _burn(client, fh, factory_order_id, 1)
        assert final.status_code == 200, final.text
        final_body = final.json()
        assert final_body["burnedCount"] == 3
        assert final_body["remaining"] == 0
        assert final_body["progressPercent"] == 100.0
        assert final_body["status"] == "COMPLETED"
        assert final_body["statusLabel"] == "烧录完成"
        assert final_body["orderStatus"] == str(OrderStatus.SHIPPED_TO_CLIENT)
        assert final_body["orderStatusLabel"] == "已出货"
        assert len(final_body["burnReports"]) == 3, "三批上报都应留痕（可追溯每一次上报）"

        assert (await _order_row(db, order_id)).status == str(OrderStatus.SHIPPED_TO_CLIENT)
        for device in devices:
            assert (await _device_row(db, device["id"])).asset_status == str(AssetStatus.PRODUCED)
            events = await _route_asset_events(client, platform, device["id"])
            assert ("PRODUCED", str(AssetStatus.PRODUCING), str(AssetStatus.PRODUCED)) in events

        # ---- ⑥ 抽检：合格与不合格各一条（抽检**不改**工单与设备状态）----
        passed = await _inspect(client, fh, factory_order_id, devices[0]["sn"], "PASS")
        assert passed.status_code == 201, passed.text
        assert passed.json()["result"] == "PASS"
        assert passed.json()["resultLabel"] == "合格"
        assert passed.json()["inspector"] == "13600002001"

        failed = await _inspect(
            client, fh, factory_order_id, devices[1]["sn"], "FAIL", defectCode="E01", note="焊点虚接"
        )
        assert failed.status_code == 201, failed.text
        assert failed.json()["resultLabel"] == "不合格"
        assert failed.json()["defectCode"] == "E01"

        after_inspection = await _factory_order_row(db, factory_order_id)
        assert after_inspection.status == "COMPLETED", "抽检不得改动工单状态（是观测不是状态）"
        assert (await _device_row(db, devices[1]["id"])).asset_status == str(
            AssetStatus.PRODUCED
        ), "抽检不合格也不得自动改设备状态——处置是人的决定"

        detail = await client.get(f"{FACTORY}/orders/{factory_order_id}", headers=fh)
        assert detail.status_code == 200, detail.text
        assert detail.json()["inspectionSummary"] == {"passCount": 1, "failCount": 1, "total": 2}

        # ---- ⑦ 出货登记：工单 → SHIPPED，设备 PRODUCED → SHIPPED ----
        shipped = await _ship(client, fh, factory_order_id)
        assert shipped.status_code == 200, shipped.text
        ship_body = shipped.json()
        assert ship_body["status"] == "SHIPPED"
        assert ship_body["statusLabel"] == "已出货"
        assert ship_body["shippedAt"] is not None
        # 出货不动订单：订单在「烧录完成」那一步已经走到 SHIPPED_TO_CLIENT
        assert ship_body["orderStatus"] == str(OrderStatus.SHIPPED_TO_CLIENT)

        for device in devices:
            assert (await _device_row(db, device["id"])).asset_status == str(AssetStatus.SHIPPED)
            events = await _route_asset_events(client, platform, device["id"])
            assert ("SHIPPED", str(AssetStatus.PRODUCED), str(AssetStatus.SHIPPED)) in events, (
                "设备必须走到 SHIPPED 才能被分配（ALLOCATABLE_ASSET_STATUSES 含 SHIPPED）"
            )

        # ---- ⑧ 固件聚合与工作台统计（本厂口径）----
        firmwares = await client.get(f"{FACTORY}/firmwares", headers=fh)
        assert firmwares.status_code == 200, firmwares.text
        assert firmwares.json()["total"] == 1
        firmware = firmwares.json()["records"][0]
        assert firmware["firmwareVersion"] == "2.0.0"
        assert firmware["orderCount"] == 1
        assert firmware["totalQuantity"] == 3
        assert firmware["burnedCount"] == 3

        stats = await client.get(f"{FACTORY}/stats", headers=fh)
        assert stats.status_code == 200, stats.text
        stats_body = stats.json()
        assert stats_body["totalOrders"] == 1
        assert stats_body["shippedOrders"] == 1
        assert stats_body["totalQuantity"] == 3
        assert stats_body["burnedQuantity"] == 3
        assert stats_body["burnProgressPercent"] == 100.0
        assert stats_body["firmwareCount"] == 1
        assert stats_body["inspectionTotal"] == 2
        assert stats_body["inspectionPass"] == 1
        assert stats_body["inspectionFail"] == 1

        # ---- ⑨ 二维码清单：复用订单二维码（同一批设备）----
        codes = await client.get(f"{FACTORY}/orders/{factory_order_id}/qrcodes", headers=fh)
        assert codes.status_code == 200, codes.text
        assert codes.json()["total"] == 3
        assert {item["sn"] for item in codes.json()["records"]} == {d["sn"] for d in devices}


# ===========================================================================
# 二、派单的校验顺序与负路径
# ===========================================================================


class TestDispatchGuards:
    """派单的校验顺序：订单状态 → 有可生产设备 → 工厂存在且启用 → 无既有工单。"""

    async def test_dispatch_non_in_stock_order_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """订单不是 ``IN_STOCK``（这里用 ``PENDING_AUDIT``）→ 409 ``INVALID_STATE_TRANSITION``。

        业务规则：未审核/未入库的订单不该进工厂——否则会造出一张
        「工厂有活干、但订单还在等审核」的工单。
        """
        tenant = await make_tenant(code="FLOW-B1", name="派单门禁租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWB1")
        order, _ = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=2, sn_prefix="SN-B1"
        )
        order.status = str(OrderStatus.PENDING_AUDIT)
        await db.flush()
        platform = await auth.platform_headers()
        factory = await _make_factory(db, code="FAC-B1")

        response = await _dispatch(client, platform, order.id, factory.id)
        body = _assert_error(response, 409, "INVALID_STATE_TRANSITION")
        assert body["details"]["current"] == str(OrderStatus.PENDING_AUDIT)
        assert body["details"]["target"] == str(OrderStatus.PRODUCING)

        assert (await _order_row(db, order.id)).status == str(OrderStatus.PENDING_AUDIT)
        assert (
            await db.execute(select(FactoryOrder).where(FactoryOrder.order_id == order.id))
        ).scalar_one_or_none() is None, "被拒的派单不得留下工单"

    async def test_dispatch_order_without_devices_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """订单已入库但**没有 ``IN_STOCK`` 设备** → 409 ``INVALID_STATE_TRANSITION``。

        业务规则：设备全被单独冻结 / 报废时派单，会造出一张工厂无活可干的工单，
        而工厂不会收到任何「这单其实没法做」的提示。
        """
        tenant = await make_tenant(code="FLOW-B2", name="空单派单租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWB2")
        order, _ = await _lib_stocked_order(
            db,
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            quantity=2,
            sn_prefix="SN-B2",
            with_devices=False,
        )
        platform = await auth.platform_headers()
        factory = await _make_factory(db, code="FAC-B2")

        response = await _dispatch(client, platform, order.id, factory.id)
        _assert_error(response, 409, "INVALID_STATE_TRANSITION")
        assert (
            await db.execute(select(FactoryOrder).where(FactoryOrder.order_id == order.id))
        ).scalar_one_or_none() is None

    async def test_second_dispatch_returns_factory_order_exists(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """★ 同一订单再次派单 → 409 ``FACTORY_ORDER_EXISTS``（带上既有工单号）。

        这条用例记录了一次**由独立验证发现、随后修复**的校验顺序问题：

        * 修复前：``dispatch_order`` 先校验「订单必须 ``IN_STOCK``」。而首次
          派单已在同一事务里把订单推到 ``PRODUCING``，于是第二次请求在第一步
          就被拦下，永远走不到「同一订单不能有第二张工单」那条判断——
          精心设计的 ``FACTORY_ORDER_EXISTS``（含 ``existingFactoryOrderNo``）
          在真实运营里**几乎不可见**，运营只会看到笼统的「状态不允许派单」。
        * 修复后：重复派单检查排在状态校验**之前**。用户第二次点「派单」时
          最高频的误操作正是「重复派单」，返回的错误必须直接告诉他
          「你已经派过了，工单号是 X」，而不是让他去猜状态。
        """
        tenant = await make_tenant(code="FLOW-B3", name="重复派单租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWB3")
        order, _ = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=1, sn_prefix="SN-B3"
        )
        platform = await auth.platform_headers()
        factory = await _make_factory(db, code="FAC-B3")

        first = await _dispatch(client, platform, order.id, factory.id)
        assert first.status_code == 201, first.text
        first_no = first.json()["factoryOrderNo"]

        again = await _dispatch(client, platform, order.id, factory.id)
        body = _assert_error(again, 409, "FACTORY_ORDER_EXISTS")
        assert body["details"]["existingFactoryOrderNo"] == first_no, (
            "必须回显既有工单号，前端才能跳转而不用让用户反复重试"
        )

        count = len(
            list(
                (
                    await db.execute(
                        select(FactoryOrder).where(FactoryOrder.order_id == order.id)
                    )
                ).scalars()
            )
        )
        assert count == 1, "重复派单绝不能造出第二张工单"

    async def test_pre_existing_row_wins_over_state_check(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """既有工单（脏数据 / 上游回滚）也走 ``FACTORY_ORDER_EXISTS``。

        构造「订单仍 ``IN_STOCK`` 却已存在一张工单」的历史数据形态，
        验证两条规则的**优先级**：重复派单检查先于状态检查，
        因此错误码是 ``FACTORY_ORDER_EXISTS`` 而不是 ``INVALID_STATE_TRANSITION``。
        ``details.existingFactoryOrderNo`` 必须带上既有工单号（前端据此跳转）。
        """
        tenant = await make_tenant(code="FLOW-B4", name="既有工单租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWB4")
        order, _ = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=1, sn_prefix="SN-B4"
        )
        platform = await auth.platform_headers()
        factory = await _make_factory(db, code="FAC-B4")

        legacy = FactoryOrder(
            id=new_id("factory_order"),
            factory_order_no="FO-20260101-LEGACY",
            order_id=order.id,
            factory_id=factory.id,
            quantity=order.quantity,
            burned_count=0,
            status="PENDING",
            assigned_at=utcnow(),
        )
        db.add(legacy)
        await db.flush()

        response = await _dispatch(client, platform, order.id, factory.id)
        body = _assert_error(response, 409, "FACTORY_ORDER_EXISTS")
        assert body["details"]["existingFactoryOrderNo"] == "FO-20260101-LEGACY"
        # 订单仍未被改动（拒绝必须是真的没发生）
        assert (await _order_row(db, order.id)).status == str(OrderStatus.IN_STOCK)

    async def test_dispatch_to_disabled_factory_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """派给已停用工厂 → 409 ``INVALID_STATE_TRANSITION``。

        业务规则：派给停用工厂的任务会永远停在 ``PENDING`` 且没人会收到提醒。
        """
        tenant = await make_tenant(code="FLOW-B5", name="停用工厂租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWB5")
        order, _ = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=1, sn_prefix="SN-B5"
        )
        platform = await auth.platform_headers()
        factory = await _make_factory(db, code="FAC-B5", status=str(EnableStatus.DISABLED))

        body = _assert_error(
            await _dispatch(client, platform, order.id, factory.id),
            409,
            "INVALID_STATE_TRANSITION",
        )
        assert "停用" in body["message"]
        assert (await _order_row(db, order.id)).status == str(OrderStatus.IN_STOCK)

    async def test_dispatch_to_unknown_factory_is_not_found(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """工厂 ID 不存在 → 404 ``RESOURCE_NOT_FOUND``（与「停用」区分开）。"""
        tenant = await make_tenant(code="FLOW-B6", name="未知工厂租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWB6")
        order, _ = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=1, sn_prefix="SN-B6"
        )
        platform = await auth.platform_headers()

        _assert_error(
            await _dispatch(client, platform, order.id, "factory-does-not-exist"),
            404,
            "RESOURCE_NOT_FOUND",
        )


# ===========================================================================
# 三、烧录上报的负路径
# ===========================================================================


class TestBurnGuards:
    """烧录上报：数量校验、终态拦截、参数模型拦截。"""

    async def test_burn_exceeding_remaining_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """★ 超量上报 → 400 ``BURN_COUNT_EXCEEDED``，``details`` 带剩余等结构化信息。

        业务规则：这是工厂端最高频的误操作（多报一批）。只回一句
        「参数错误」会让工厂反复试；带 ``remaining`` 才能直接告诉他「最多还能报 2 台」。
        """
        tenant = await make_tenant(code="FLOW-C1", name="超量上报租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWC1")
        order, _ = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=2, sn_prefix="SN-C1"
        )
        platform = await auth.platform_headers()
        factory = await _make_factory(db, code="FAC-C1")
        fh = await _factory_headers(auth, db, factory=factory, account="13600003001")
        factory_order_id = (await _dispatch_ok(client, platform, order.id, factory.id))["id"]

        # 先报 1 台，再报 5 台（剩余 1）
        assert (await _burn(client, fh, factory_order_id, 1)).status_code == 200
        body = _assert_error(
            await _burn(client, fh, factory_order_id, 5), 400, "BURN_COUNT_EXCEEDED"
        )
        details = body["details"]
        assert details["remaining"] == 1
        assert details["quantity"] == 2
        assert details["burnedCount"] == 5
        assert details["currentBurnedCount"] == 1

        # 失败的上报不得改动累计值，也不得留下烧录记录
        row = await _factory_order_row(db, factory_order_id)
        assert row.burned_count == 1
        detail = await client.get(f"{FACTORY}/orders/{factory_order_id}", headers=fh)
        assert len(detail.json()["burnReports"]) == 1

    @pytest.mark.parametrize("burned_count", [0, -1, -100])
    async def test_burn_zero_or_negative_is_rejected_by_request_model(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, burned_count: int
    ) -> None:
        """上报 0 台或负数 → 400 ``VALIDATION_ERROR``（请求模型 ``gt=0`` 拦下）。

        业务规则：0 台的上报既不改进度也不携带信息，只会在上报历史里
        留下一串噪声记录——由**入参层**挡掉，不能靠服务层「顺手 if 一下」。
        """
        tenant = await make_tenant(code=f"FLOW-C2-{burned_count}", name="零上报租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWC2")
        order, _ = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=2, sn_prefix="SN-C2"
        )
        platform = await auth.platform_headers()
        factory = await _make_factory(db, code="FAC-C2")
        fh = await _factory_headers(auth, db, factory=factory, account="13600003002")
        factory_order_id = (await _dispatch_ok(client, platform, order.id, factory.id))["id"]

        _assert_error(
            await _burn(client, fh, factory_order_id, burned_count), 400, "VALIDATION_ERROR"
        )
        assert (await _factory_order_row(db, factory_order_id)).burned_count == 0

    async def test_burn_on_completed_order_is_rejected_before_count_check(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """★ 已烧满（``COMPLETED``）的工单再上报 → 409（**不是** ``BURN_COUNT_EXCEEDED``）。

        这两个条件在「已完成的工单又报一批」时同时不满足，但真正的原因
        是「这单已经做完了」。先报数量错误会让工厂以为是自己数字填错、
        反复重试而始终看不到「已完成」这个事实——校验顺序即提示优先级。
        """
        tenant = await make_tenant(code="FLOW-C3", name="完成后再报租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWC3")
        order, _ = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=1, sn_prefix="SN-C3"
        )
        platform = await auth.platform_headers()
        factory = await _make_factory(db, code="FAC-C3")
        fh = await _factory_headers(auth, db, factory=factory, account="13600003003")
        factory_order_id = (await _dispatch_ok(client, platform, order.id, factory.id))["id"]

        assert (await _burn(client, fh, factory_order_id, 1)).status_code == 200
        body = _assert_error(
            await _burn(client, fh, factory_order_id, 1), 409, "INVALID_STATE_TRANSITION"
        )
        assert body["details"]["current"] == "COMPLETED"
        assert body["details"]["target"] == "PRODUCING"

    async def test_burn_after_shipped_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """``SHIPPED`` 是终态：再烧录 → 409 ``INVALID_STATE_TRANSITION``。

        业务规则：出货后再报烧录意味着「已经发给客户的货还在产线上」，
        必须拦下并让人去查（而不是默默把数字加上去）。
        """
        tenant = await make_tenant(code="FLOW-C4", name="出货后上报租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWC4")
        order, _ = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=1, sn_prefix="SN-C4"
        )
        platform = await auth.platform_headers()
        factory = await _make_factory(db, code="FAC-C4")
        fh = await _factory_headers(auth, db, factory=factory, account="13600003004")
        factory_order_id = (await _dispatch_ok(client, platform, order.id, factory.id))["id"]

        assert (await _burn(client, fh, factory_order_id, 1)).status_code == 200
        assert (await _ship(client, fh, factory_order_id)).status_code == 200

        body = _assert_error(
            await _burn(client, fh, factory_order_id, 1), 409, "INVALID_STATE_TRANSITION"
        )
        assert body["details"]["current"] == "SHIPPED"
        assert (await _factory_order_row(db, factory_order_id)).burned_count == 1


# ===========================================================================
# 四、出货登记的负路径
# ===========================================================================


class TestShipGuards:
    """只有 ``COMPLETED`` 的工单可以出货。"""

    async def test_ship_pending_order_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """刚派单（``PENDING``，一台未烧）就出货 → 409。

        业务规则：烧录数为 0 时出货登记等于把「没做过的活」标记成已完成。
        """
        tenant = await make_tenant(code="FLOW-D1", name="未完成出货租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWD1")
        order, _ = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=1, sn_prefix="SN-D1"
        )
        platform = await auth.platform_headers()
        factory = await _make_factory(db, code="FAC-D1")
        fh = await _factory_headers(auth, db, factory=factory, account="13600004001")
        factory_order_id = (await _dispatch_ok(client, platform, order.id, factory.id))["id"]

        body = _assert_error(await _ship(client, fh, factory_order_id), 409, "INVALID_STATE_TRANSITION")
        assert body["details"]["current"] == "PENDING"
        assert body["details"]["target"] == "SHIPPED"

    async def test_ship_partially_burned_order_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """烧了一半（``PRODUCING``）出货 → 409。

        与上一条互补：这里工单**已经开工**但未报满，拦截依据必须是
        「未 ``COMPLETED``」而不是「未开工」。
        """
        tenant = await make_tenant(code="FLOW-D2", name="半成品出货租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWD2")
        order, _ = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=3, sn_prefix="SN-D2"
        )
        platform = await auth.platform_headers()
        factory = await _make_factory(db, code="FAC-D2")
        fh = await _factory_headers(auth, db, factory=factory, account="13600004002")
        factory_order_id = (await _dispatch_ok(client, platform, order.id, factory.id))["id"]

        assert (await _burn(client, fh, factory_order_id, 2)).status_code == 200
        body = _assert_error(await _ship(client, fh, factory_order_id), 409, "INVALID_STATE_TRANSITION")
        assert body["details"]["current"] == "PRODUCING"

        # 设备仍在 PRODUCING（被拒的出货不得改动设备状态）
        row = await _factory_order_row(db, factory_order_id)
        devices = list(
            (
                await db.execute(select(Device).where(Device.order_id == row.order_id))
            ).scalars()
        )
        assert {d.asset_status for d in devices} == {str(AssetStatus.PRODUCING)}

    async def test_ship_twice_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """``SHIPPED`` 是终态：重复出货登记 → 409。

        业务规则：重复出货会让人分不清「到底发了几次货」，
        且第二次的设备迁移会被 ``ASSET_TRANSITIONS`` 拒绝（``SHIPPED`` 无出边到自身）。
        """
        tenant = await make_tenant(code="FLOW-D3", name="重复出货租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWD3")
        order, _ = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=1, sn_prefix="SN-D3"
        )
        platform = await auth.platform_headers()
        factory = await _make_factory(db, code="FAC-D3")
        fh = await _factory_headers(auth, db, factory=factory, account="13600004003")
        factory_order_id = (await _dispatch_ok(client, platform, order.id, factory.id))["id"]

        assert (await _burn(client, fh, factory_order_id, 1)).status_code == 200
        assert (await _ship(client, fh, factory_order_id)).status_code == 200

        body = _assert_error(await _ship(client, fh, factory_order_id), 409, "INVALID_STATE_TRANSITION")
        assert body["details"]["current"] == "SHIPPED"


# ===========================================================================
# 五、抽检的负路径与语义
# ===========================================================================


class TestInspectionGuards:
    """抽检：SN 必须真实存在且属于本工单；抽检不改状态。"""

    async def test_inspect_unknown_sn_is_device_not_found(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """SN 不存在 → 404 ``DEVICE_NOT_FOUND``。

        业务规则：记一条没有对应设备的抽检记录会直接污染「抽检合格率」。
        """
        tenant = await make_tenant(code="FLOW-E1", name="抽检未知SN租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWE1")
        order, _ = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=1, sn_prefix="SN-E1"
        )
        platform = await auth.platform_headers()
        factory = await _make_factory(db, code="FAC-E1")
        fh = await _factory_headers(auth, db, factory=factory, account="13600005001")
        factory_order_id = (await _dispatch_ok(client, platform, order.id, factory.id))["id"]

        _assert_error(
            await _inspect(client, fh, factory_order_id, "SN-NOT-EXIST"), 404, "DEVICE_NOT_FOUND"
        )

    async def test_inspect_sn_of_another_order_is_validation_error(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """SN 属于**别的订单** → 400 ``VALIDATION_ERROR``（``details.field == sn``）。

        与上一条的区别是「设备存在但不对这单」——错误码必须不同，
        否则工厂无法区分「抄错 SN」与「拿错了另一批货」。
        """
        tenant = await make_tenant(code="FLOW-E2", name="跨单抽检租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWE2")
        order_a, _ = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=1, sn_prefix="SN-E2A"
        )
        order_b, devices_b = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=1, sn_prefix="SN-E2B"
        )
        platform = await auth.platform_headers()
        factory = await _make_factory(db, code="FAC-E2")
        fh = await _factory_headers(auth, db, factory=factory, account="13600005002")
        factory_order_id = (await _dispatch_ok(client, platform, order_a.id, factory.id))["id"]

        body = _assert_error(
            await _inspect(client, fh, factory_order_id, devices_b[0].sn), 400, "VALIDATION_ERROR"
        )
        assert body["details"]["field"] == "sn"

    @pytest.mark.parametrize("sn", ["   ", "\t", "\n  "])
    async def test_inspect_blank_sn_is_rejected_by_request_model(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, sn: str
    ) -> None:
        """★ 纯空白 SN → 400 ``VALIDATION_ERROR``（P5 的教训：``min_length=1`` 挡不住空白串）。

        业务规则：``"   "`` 长度为 3 能通过 ``min_length=1``，会一路落库并
        污染合格率；必须在**入参层**用 strip 校验器挡掉。
        """
        tenant = await make_tenant(code="FLOW-E3", name="空白SN租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWE3")
        order, _ = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=1, sn_prefix="SN-E3"
        )
        platform = await auth.platform_headers()
        factory = await _make_factory(db, code="FAC-E3")
        fh = await _factory_headers(auth, db, factory=factory, account="13600005003")
        factory_order_id = (await _dispatch_ok(client, platform, order.id, factory.id))["id"]

        _assert_error(await _inspect(client, fh, factory_order_id, sn), 400, "VALIDATION_ERROR")

    async def test_same_sn_can_be_inspected_multiple_times_with_history(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """同一 SN 可被抽检多次，历史全部保留，汇总是全量口径。

        业务规则：抽检是**观测**不是状态——保留「先不合格、返工后合格」
        这个过程，才能看出质量问题有没有真的被解决。
        """
        tenant = await make_tenant(code="FLOW-E4", name="抽检历史租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWE4")
        order, devices = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=1, sn_prefix="SN-E4"
        )
        platform = await auth.platform_headers()
        factory = await _make_factory(db, code="FAC-E4")
        fh = await _factory_headers(auth, db, factory=factory, account="13600005004")
        factory_order_id = (await _dispatch_ok(client, platform, order.id, factory.id))["id"]

        first = await _inspect(
            client, fh, factory_order_id, devices[0].sn, "FAIL", defectCode="E01"
        )
        assert first.status_code == 201, first.text
        second = await _inspect(client, fh, factory_order_id, devices[0].sn, "PASS", note="返工后复检")
        assert second.status_code == 201, second.text

        listed = await client.get(
            f"{FACTORY}/inspections", headers=fh, params={"factoryOrderId": factory_order_id}
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["total"] == 2, "同一 SN 的两条抽检记录都必须保留"

        detail = await client.get(f"{FACTORY}/orders/{factory_order_id}", headers=fh)
        summary = detail.json()["inspectionSummary"]
        assert summary == {"passCount": 1, "failCount": 1, "total": 2}

        # 抽检筛选：按 SN 精确匹配命中两条；result 筛选各命中一条
        by_sn = await client.get(f"{FACTORY}/inspections", headers=fh, params={"sn": devices[0].sn})
        assert by_sn.json()["total"] == 2
        by_result = await client.get(f"{FACTORY}/inspections", headers=fh, params={"result": "FAIL"})
        assert by_result.json()["total"] == 1
        assert by_result.json()["records"][0]["resultLabel"] == "不合格"


# ===========================================================================
# 六、★ 跨厂隔离与统计口径
# ===========================================================================


@pytest.mark.tenant_isolation
class TestCrossFactoryIsolation:
    """一家工厂只能看到、只能操作自己的工单；越权一律「不存在」。"""

    async def test_cross_factory_actions_are_all_404(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """★ A 厂的工单，B 厂的账号访问详情 / 烧录 / 抽检 / 出货 / 二维码 → 全部 404。

        为什么必须是 404 而不是 403：错误码本身不能成为「别家工厂有没有
        这张工单」的探测器。若返回 403，B 厂可以拿工单号逐个试，
        据此推断对手的在手订单规模——这恰恰是工厂作用域要保护的信息。
        """
        tenant = await make_tenant(code="FLOW-F1", name="跨厂隔离租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWF1")
        order, devices = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=2, sn_prefix="SN-F1"
        )
        platform = await auth.platform_headers()
        factory_a = await _make_factory(db, code="FAC-F1A")
        factory_b = await _make_factory(db, code="FAC-F1B")
        fh_a = await _factory_headers(auth, db, factory=factory_a, account="13600006001")
        fh_b = await _factory_headers(auth, db, factory=factory_b, account="13600006002")

        factory_order_id = (await _dispatch_ok(client, platform, order.id, factory_a.id))["id"]

        cases: dict[str, Any] = {
            "工单详情": await client.get(f"{FACTORY}/orders/{factory_order_id}", headers=fh_b),
            "烧录上报": await _burn(client, fh_b, factory_order_id, 1),
            "抽检上报": await _inspect(client, fh_b, factory_order_id, devices[0].sn),
            "出货登记": await _ship(client, fh_b, factory_order_id),
            "二维码清单": await client.get(
                f"{FACTORY}/orders/{factory_order_id}/qrcodes", headers=fh_b
            ),
        }
        for label, response in cases.items():
            # 把场景名放进断言消息：越权矩阵里失败时能一眼看出是哪一条
            assert response.status_code == 404, f"{label}：{response.text}"
            assert response.json()["code"] == "RESOURCE_NOT_FOUND", f"{label}：{response.text}"

        # 越权失败必须真的没发生：工单仍 PENDING、累计仍为 0、无抽检记录
        row = await _factory_order_row(db, factory_order_id)
        assert row.status == "PENDING"
        assert row.burned_count == 0
        assert (await client.get(f"{FACTORY}/inspections", headers=fh_b)).json()["total"] == 0

        # 列表与统计也只看得到本厂：B 厂什么都没有
        assert (await client.get(f"{FACTORY}/orders", headers=fh_b)).json()["total"] == 0
        assert (await client.get(f"{FACTORY}/stats", headers=fh_b)).json()["totalOrders"] == 0
        # A 厂看得到自己的那一张
        listed_a = await client.get(f"{FACTORY}/orders", headers=fh_a)
        assert listed_a.json()["total"] == 1
        assert listed_a.json()["records"][0]["id"] == factory_order_id
        assert listed_a.json()["records"][0]["factoryId"] == factory_a.id

    async def test_stats_are_strictly_factory_scoped(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """★ 工作台统计严格本厂口径：两家工厂各派 1 单，数量刻意不同。

        用**不同的数量**（3 与 5）而不是「各 1 单」：若实现漏了
        ``factory_scoped``，A 厂的 ``totalQuantity`` 会变成 8（跨厂汇总），
        本用例能立刻发现；而两张单数量相同时，「A 看到 2 单」也会被发现，
        但「数量被合计」这类更隐蔽的泄漏只能靠不同值暴露。
        """
        platform = await auth.platform_headers()

        tenant_a = await make_tenant(code="FLOW-F2A", name="统计口径租户A")
        catalog_a = await _seed_catalog(db, tenant_a.id, suffix="FLOWF2A")
        order_a, _ = await _lib_stocked_order(
            db, tenant_id=tenant_a.id, product_id=catalog_a["product_id"], quantity=3, sn_prefix="SN-F2A"
        )
        tenant_b = await make_tenant(code="FLOW-F2B", name="统计口径租户B")
        catalog_b = await _seed_catalog(db, tenant_b.id, suffix="FLOWF2B")
        order_b, _ = await _lib_stocked_order(
            db, tenant_id=tenant_b.id, product_id=catalog_b["product_id"], quantity=5, sn_prefix="SN-F2B"
        )

        factory_a = await _make_factory(db, code="FAC-F2A")
        factory_b = await _make_factory(db, code="FAC-F2B")
        fh_a = await _factory_headers(auth, db, factory=factory_a, account="13600007001")
        fh_b = await _factory_headers(auth, db, factory=factory_b, account="13600007002")

        fo_a = (await _dispatch_ok(client, platform, order_a.id, factory_a.id))["id"]
        fo_b = (await _dispatch_ok(client, platform, order_b.id, factory_b.id))["id"]
        # A 厂烧 2 台，B 厂烧 1 台：两边进度也不得互相污染
        assert (await _burn(client, fh_a, fo_a, 2)).status_code == 200
        assert (await _burn(client, fh_b, fo_b, 1)).status_code == 200

        stats_a = (await client.get(f"{FACTORY}/stats", headers=fh_a)).json()
        assert stats_a["totalOrders"] == 1, "A 厂只应看到自己那 1 张工单"
        assert stats_a["totalQuantity"] == 3
        assert stats_a["burnedQuantity"] == 2, "B 厂的烧录量绝不能计进 A 厂"

        stats_b = (await client.get(f"{FACTORY}/stats", headers=fh_b)).json()
        assert stats_b["totalOrders"] == 1
        assert stats_b["totalQuantity"] == 5
        assert stats_b["burnedQuantity"] == 1

        # 工单号互相不可见（同一份代码在两端都成立）
        list_a_ids = {
            item["id"] for item in (await client.get(f"{FACTORY}/orders", headers=fh_a)).json()["records"]
        }
        assert list_a_ids == {fo_a}

    async def test_unbound_factory_account_is_forbidden(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """★ 未绑定工厂的工厂账号 → 403 ``PERMISSION_DENIED``（而非「看到全部」）。

        业务规则：宁可「登录得进来看不到东西」，也不能退化成全局长——
        后者在多工厂场景下等于 A 厂读到 B 厂的产量与在手订单。
        与上一条的 404 形成对照：**作用域（别家的单）= 404**，
        **身份缺失（自己没归属）= 403**，两者语义不同，不能混用。
        """
        await _make_factory_account(db, account="13600008001", factory_id=None)
        headers = auth.headers(await auth.token("13600008001", FACTORY_PASSWORD))

        for path in ("/orders", "/stats", "/inspections", "/firmwares"):
            response = await client.get(f"{FACTORY}{path}", headers=headers)
            _assert_error(response, 403, "PERMISSION_DENIED")


# ===========================================================================
# 七、权限边界
# ===========================================================================


class TestPermissionBoundaries:
    """三端 token 不能互串；工厂操作员读不到固件。"""

    async def test_merchant_token_is_rejected_on_factory_endpoints(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """商户 token 访问任何 ``/factory/**`` → 403 ``PERMISSION_DENIED``。

        商户账号有租户归属，工厂端接口对它没有意义；更危险的是若放行，
        ``assert_factory_visible`` 会因 ``is_factory`` 为假而抛权限错误——
        但那是「碰巧」的拦截，必须在角色入口处明确拒绝。
        """
        tenant = await make_tenant(code="FLOW-G1", name="商户越权租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13300009001")

        cases: dict[str, Any] = {
            "工单列表": await client.get(f"{FACTORY}/orders", headers=merchant),
            "工作台": await client.get(f"{FACTORY}/stats", headers=merchant),
            "固件列表": await client.get(f"{FACTORY}/firmwares", headers=merchant),
            "烧录上报": await client.post(
                f"{FACTORY}/orders/fo-whatever/burn", headers=merchant, json={"burnedCount": 1}
            ),
        }
        for label, response in cases.items():
            assert response.status_code == 403, f"{label}：{response.text}"
            assert response.json()["code"] == "PERMISSION_DENIED", f"{label}：{response.text}"

    async def test_factory_token_is_rejected_on_platform_endpoints(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """工厂 token 访问任何 ``/platform/**`` → 403 ``PERMISSION_DENIED``。

        业务规则：工厂是跨租户角色，一旦能进平台端就能看到全部订单与金额——
        这是本阶段最严重的越权路径，必须由 ``require_platform`` 明确挡住。
        """
        factory = await _make_factory(db, code="FAC-G2")
        fh = await _factory_headers(auth, db, factory=factory, account="13600009001")

        cases: dict[str, Any] = {
            "工厂下拉": await client.get(f"{PLATFORM}/factories", headers=fh),
            "平台工单列表": await client.get(f"{PLATFORM}/factory-orders", headers=fh),
            "平台订单列表": await client.get(f"{PLATFORM}/orders", headers=fh),
            "批量入库": await client.post(
                f"{PLATFORM}/devices/stock-in", headers=fh, json={"deviceIds": ["d-x"]}
            ),
            "派单": await client.post(
                f"{PLATFORM}/orders/o-x/dispatch", headers=fh, json={"factoryId": factory.id}
            ),
        }
        for label, response in cases.items():
            assert response.status_code == 403, f"{label}：{response.text}"
            assert response.json()["code"] == "PERMISSION_DENIED", f"{label}：{response.text}"

    async def test_factory_operator_cannot_read_firmwares(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """``FACTORY_OPERATOR`` 读固件 → 403；读工单 → 200。

        业务规则：``factory:firmware:read`` 只给 ``FACTORY_ADMIN``
        （固件版本属于产线技术资料）。若这条被拒而工单能读，
        说明权限码确实在生效，而不是整个接口对工厂角色都不可用。
        """
        factory = await _make_factory(db, code="FAC-G3")
        fh = await _factory_headers(
            auth, db, factory=factory, account="13600009002", role_code="FACTORY_OPERATOR"
        )

        _assert_error(
            await client.get(f"{FACTORY}/firmwares", headers=fh), 403, "PERMISSION_DENIED"
        )
        assert (await client.get(f"{FACTORY}/orders", headers=fh)).status_code == 200
        assert (await client.get(f"{FACTORY}/stats", headers=fh)).status_code == 200


# ===========================================================================
# 八、设备批量入库（P5 遗留②）
# ===========================================================================


class TestStockInEndpoint:
    """``POST /platform/devices/stock-in`` 的逐行语义与幂等。"""

    async def test_mixed_batch_counts_failures_and_idempotent_replay(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """★ 混入四种输入，断言 ``moved/skipped/failed`` 与 ``failures`` 明细精确对应；
        同一批**再调一次**全部变 ``skipped``，且不产生新的设备事件。

        逐行语义（这是本端点的核心取舍：一次请求里 3 台状态不对时，
        整单回滚意味着「其余几十台白入库一遍」）：
        * 不存在 ID → ``failed``（``RESOURCE_NOT_FOUND``）；
        * 已是 ``IN_STOCK`` → ``skipped``（重复点击不报错）；
        * ``GENERATED`` → ``moved``（这才是入库的目的）；
        * 其它状态（``ALLOCATED``）→ ``failed``（``DEVICE_NOT_AVAILABLE``）。
        """
        tenant = await make_tenant(code="FLOW-H1", name="批量入库租户")
        await _seed_catalog(db, tenant.id, suffix="FLOWH1")
        platform = await auth.platform_headers()

        in_stock = await _make_device(
            db, sn="SN-H1-INSTOCK", tenant_id=tenant.id, asset_status=str(AssetStatus.IN_STOCK)
        )
        generated = await _make_device(
            db, sn="SN-H1-GEN", tenant_id=tenant.id, asset_status=str(AssetStatus.GENERATED)
        )
        allocated = await _make_device(
            db, sn="SN-H1-ALLOC", tenant_id=tenant.id, asset_status=str(AssetStatus.ALLOCATED)
        )
        device_ids = ["device-does-not-exist", in_stock.id, generated.id, allocated.id]

        first = await client.post(
            f"{PLATFORM}/devices/stock-in", headers=platform, json={"deviceIds": device_ids}
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["requested"] == 4
        assert body["moved"] == 1, "只有 GENERATED 那一台应当被推进 IN_STOCK"
        assert body["skipped"] == 1
        assert body["failed"] == 2

        failures = {item["deviceId"]: item for item in body["failures"]}
        assert set(failures) == {"device-does-not-exist", allocated.id}
        assert failures["device-does-not-exist"]["code"] == "RESOURCE_NOT_FOUND"
        assert failures["device-does-not-exist"]["sn"] is None
        assert failures[allocated.id]["code"] == "DEVICE_NOT_AVAILABLE"
        assert failures[allocated.id]["sn"] == "SN-H1-ALLOC"
        assert str(AssetStatus.ALLOCATED) in failures[allocated.id]["message"]

        # 状态与事件：GENERATED 那台变成 IN_STOCK 且留下恰好一条 IN_STOCK 事件；
        # 其余三台状态不变
        assert (await _device_row(db, generated.id)).asset_status == str(AssetStatus.IN_STOCK)
        assert await _count_in_stock_events(db, generated.id) == 1
        assert (await _device_row(db, in_stock.id)).asset_status == str(AssetStatus.IN_STOCK)
        assert (await _device_row(db, allocated.id)).asset_status == str(AssetStatus.ALLOCATED)

        # ★ 幂等：同一批再调一次
        second = await client.post(
            f"{PLATFORM}/devices/stock-in", headers=platform, json={"deviceIds": device_ids}
        )
        assert second.status_code == 200, second.text
        replay = second.json()
        assert replay["requested"] == 4
        assert replay["moved"] == 0, "重跑不得再产生一次迁移"
        assert replay["skipped"] == 2, "已是 IN_STOCK 的两台应记为 skipped"
        assert replay["failed"] == 2, "不存在与 ALLOCATED 仍然失败（失败原因不会自愈）"
        assert await _count_in_stock_events(db, generated.id) == 1, (
            "★ 幂等重跑绝不能产生新的设备事件——否则时间线会显示两次入库"
        )

    async def test_empty_device_ids_is_rejected(
        self, client: AsyncClient, auth: Any
    ) -> None:
        """``deviceIds`` 为空 → 400 ``VALIDATION_ERROR``（请求模型 ``min_length=1``）。

        业务规则：空批次的「成功」没有任何信息量，会掩盖调用方的传参 bug。
        """
        platform = await auth.platform_headers()
        response = await client.post(
            f"{PLATFORM}/devices/stock-in", headers=platform, json={"deviceIds": []}
        )
        _assert_error(response, 400, "VALIDATION_ERROR")

    async def test_stock_in_requires_device_write_permission(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """商户 token 调批量入库 → 403（该端点是平台独占 ``platform:device:write``）。"""
        tenant = await make_tenant(code="FLOW-H3", name="入库权限租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13300009003")
        device = await _make_device(db, sn="SN-H3-GEN", asset_status=str(AssetStatus.GENERATED))

        response = await client.post(
            f"{PLATFORM}/devices/stock-in",
            headers=merchant,
            json={"deviceIds": [device.id]},
        )
        _assert_error(response, 403, "PERMISSION_DENIED")
        assert (await _device_row(db, device.id)).asset_status == str(AssetStatus.GENERATED)


# ===========================================================================
# 九、平台端工厂列表（派单下拉）
# ===========================================================================


class TestFactoryDropdown:
    """``GET /platform/factories`` 含停用工厂并带状态（不静默隐藏）。"""

    async def test_disabled_factory_still_listed_with_status(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """停用工厂仍出现在下拉里，``status`` 为 ``DISABLED``。

        业务规则：若后端静默隐藏，运维看到的只是「下拉里没有这家厂」，
        无法区分「没建」与「被停用」——两者需要完全不同的处置。
        """
        platform = await auth.platform_headers()
        enabled = await _make_factory(db, code="FAC-Z1")
        disabled = await _make_factory(db, code="FAC-Z2", status=str(EnableStatus.DISABLED))

        response = await client.get(f"{PLATFORM}/factories", headers=platform)
        assert response.status_code == 200, response.text
        records = {item["id"]: item for item in response.json()["records"]}
        assert enabled.id in records
        assert disabled.id in records
        assert records[disabled.id]["status"] == str(EnableStatus.DISABLED)
        assert records[enabled.id]["status"] == str(EnableStatus.ENABLED)


# ===========================================================================
# 十、列表查询：排序字段归一、关键字、状态筛选与分状态统计
# ===========================================================================


class TestFactoryOrderQuery:
    """工厂端表格的查询契约（排序键用的是**响应里的 camelCase 字段名**）。"""

    async def test_sort_by_camel_case_is_accepted_and_unknown_field_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """★ ``sortBy=createdAt`` 必须放行，``sortBy=factoryName`` 必须 400。

        业务规则（见 ``factory_service._resolve_sort_field``）：前端表格的排序键
        取自**响应字段名**（camelCase），因此服务层先归一成 snake_case 再过白名单。
        * ``createdAt`` / ``dueAt`` / ``factoryOrderNo`` 是白名单字段，必须能用；
        * ``factoryName`` 归一后是 ``factory_name``，**不在**白名单（它是关联查询
          得来的名称，不是 ``factory_orders`` 上的列）——必须报错而不是静默退回默认排序，
          否则用户会以为排序生效了。
        """
        tenant = await make_tenant(code="FLOW-I1", name="排序归一租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWI1")
        order, _ = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=1, sn_prefix="SN-I1"
        )
        platform = await auth.platform_headers()
        factory = await _make_factory(db, code="FAC-I1")
        fh = await _factory_headers(auth, db, factory=factory, account="13600010001")
        await _dispatch_ok(client, platform, order.id, factory.id)

        for sort_by in ("createdAt", "dueAt", "factoryOrderNo", "quantity"):
            response = await client.get(
                f"{FACTORY}/orders", headers=fh, params={"sortBy": sort_by, "order": "asc"}
            )
            assert response.status_code == 200, f"sortBy={sort_by}：{response.text}"
            assert response.json()["total"] == 1

        # 白名单之外的字段名（归一后仍不在白名单）→ 400，而不是静默忽略
        _assert_error(
            await client.get(f"{FACTORY}/orders", headers=fh, params={"sortBy": "factoryName"}),
            400,
            "VALIDATION_ERROR",
        )
        # 排序方向只接受 asc / desc
        _assert_error(
            await client.get(f"{FACTORY}/orders", headers=fh, params={"order": "sideways"}),
            400,
            "VALIDATION_ERROR",
        )

    async def test_keyword_matches_both_factory_order_no_and_order_no(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """``keyword`` 同时匹配工单号与来源订单号。

        业务规则：平台派单通知里给的是**订单号**，产线看板上贴的是**工单号**，
        工厂手上拿到哪一个都要能搜到——只匹配一个会让另一条路径的搜索「看起来坏了」。
        """
        tenant = await make_tenant(code="FLOW-I2", name="关键字租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWI2")
        order, _ = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=1, sn_prefix="SN-I2"
        )
        platform = await auth.platform_headers()
        factory = await _make_factory(db, code="FAC-I2")
        fh = await _factory_headers(auth, db, factory=factory, account="13600010002")
        dispatched = await _dispatch_ok(client, platform, order.id, factory.id)

        by_factory_order_no = await client.get(
            f"{FACTORY}/orders", headers=fh, params={"keyword": dispatched["factoryOrderNo"]}
        )
        assert by_factory_order_no.json()["total"] == 1
        assert by_factory_order_no.json()["records"][0]["id"] == dispatched["id"]

        by_order_no = await client.get(
            f"{FACTORY}/orders", headers=fh, params={"keyword": order.order_no}
        )
        assert by_order_no.json()["total"] == 1
        assert by_order_no.json()["records"][0]["orderNo"] == order.order_no

        none = await client.get(f"{FACTORY}/orders", headers=fh, params={"keyword": "NO-SUCH-ORDER"})
        assert none.json()["total"] == 0
        assert none.json()["records"] == []

    async def test_status_filter_and_per_status_stats(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """同一工厂两张工单：``status`` 筛选与统计的**分状态计数**各自准确。

        业务规则：工作台卡片（待生产 / 生产中 / 烧录完成 / 已出货）与列表筛选
        必须来自同一份数据。只测「1 张工单」时 ``pending/producing/completed``
        之间无法互相印证；这里刻意造出 1 张 ``PENDING`` + 1 张 ``COMPLETED``，
        让四个计数里「应为 0」的那些也具备判别力。
        """
        tenant = await make_tenant(code="FLOW-I3", name="状态统计租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWI3")
        order_pending, _ = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=1, sn_prefix="SN-I3A"
        )
        order_done, _ = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=1, sn_prefix="SN-I3B"
        )
        platform = await auth.platform_headers()
        factory = await _make_factory(db, code="FAC-I3")
        fh = await _factory_headers(auth, db, factory=factory, account="13600010003")

        fo_pending = (await _dispatch_ok(client, platform, order_pending.id, factory.id))["id"]
        fo_done = (await _dispatch_ok(client, platform, order_done.id, factory.id))["id"]
        assert (await _burn(client, fh, fo_done, 1)).status_code == 200  # 烧满 → COMPLETED

        pending = await client.get(f"{FACTORY}/orders", headers=fh, params={"status": "PENDING"})
        assert pending.json()["total"] == 1
        assert pending.json()["records"][0]["id"] == fo_pending

        completed = await client.get(f"{FACTORY}/orders", headers=fh, params={"status": "COMPLETED"})
        assert completed.json()["total"] == 1
        assert completed.json()["records"][0]["id"] == fo_done

        producing = await client.get(f"{FACTORY}/orders", headers=fh, params={"status": "PRODUCING"})
        assert producing.json()["total"] == 0

        stats = (await client.get(f"{FACTORY}/stats", headers=fh)).json()
        assert stats["pendingOrders"] == 1
        assert stats["completedOrders"] == 1
        assert stats["producingOrders"] == 0
        assert stats["shippedOrders"] == 0
        assert stats["totalOrders"] == 2
        assert stats["totalQuantity"] == 2
        assert stats["burnedQuantity"] == 1
        assert stats["burnProgressPercent"] == 50.0, "进度按累计数量加权（1/2），不是按工单数"


# ===========================================================================
# 十一、派单遇到「订单下已冻结设备」的口径（独立验证发现 → 已修复）
# ===========================================================================


class TestFrozenDeviceDuringDispatch:
    """订单里有一台设备被单独冻结时，工单数量必须与真正被推进的设备数一致。

    这段用例记录了一次**由独立验证抓出的真缺口及其修复**：

    * 修复前：``dispatch_order`` 把 ``quantity`` 取成 ``order.quantity``。
      订单 2 台、其中 1 台被冻结 → 工单写着 2 台，却只有 1 台会流转到
      ``PRODUCING``；工厂报满 2 台后工单``COMPLETED``，而
      ``burned_count(2) > 实际出货设备数(1)``，「烧录台数」与「设备台数」
      从此永久对不上账，且那台冻结设备在订单出货后无路可走。
    * 修复后：``quantity`` 取**实际可派工（``IN_STOCK``）的设备数**，
      订单的合同数量另以 ``orderQuantity`` 下发。工单描述的是「委托工厂
      生产多少台」，订单描述的是「客户订了多少台」，两个数字都可见，
      差异因此是**可对账的**而不是被掩盖的。
    """

    async def test_quantity_matches_actually_dispatched_devices(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """★ 冻结设备被跳过，工单 ``quantity`` 随之等于实际派工台数。

        复现步骤（全部经**公开接口**，非脏数据构造）：
        1. 订单已入库，含 2 台 ``IN_STOCK`` 设备；
        2. 平台用 ``POST /platform/devices/{id}/freeze`` 冻结其中 1 台
           （``FREEZABLE_ASSET_STATUSES`` 含 ``IN_STOCK``，这条路径合法可达）；
        3. 派单 → 工单 ``quantity = 1``（实际派工台数），``orderQuantity = 2``
           （合同台数）——只有 1 台设备被推进 ``PRODUCING``
           （``_mark_order_devices`` 只挑 ``IN_STOCK``，冻结的天然被排除，
           这是有意的「冻结 = 别动它」）；
        4. 工厂报满 1 台 → 工单 ``COMPLETED``、订单 ``SHIPPED_TO_CLIENT``，
           设备走到 ``PRODUCED``/``SHIPPED``。

        关键不变量：``burned_count == quantity == 实际出货设备数``。
        这条一旦被破坏，工厂产量报表与设备台账就会开始互相矛盾。
        """
        tenant = await make_tenant(code="FLOW-J1", name="冻结设备派单租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWJ1")
        order, devices = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=2, sn_prefix="SN-J1"
        )
        platform = await auth.platform_headers()

        # ② 经公开接口冻结一台（证明这条路径真实可达）
        frozen = await client.post(
            f"{PLATFORM}/devices/{devices[1].id}/freeze",
            headers=platform,
            json={"reason": "疑似质量批次，先扣下"},
        )
        assert frozen.status_code == 200, frozen.text
        assert frozen.json()["assetStatus"] == str(AssetStatus.FROZEN)

        factory = await _make_factory(db, code="FAC-J1")
        fh = await _factory_headers(auth, db, factory=factory, account="13600011001")

        # ③ 派单：工单数量 = 实际派工台数（1），合同台数另给（2）
        dispatched = await _dispatch_ok(client, platform, order.id, factory.id)
        factory_order_id = dispatched["id"]
        assert dispatched["quantity"] == 1, "工单数量必须是实际派给工厂的台数"
        assert dispatched["orderQuantity"] == 2, "合同台数必须一并下发，差异才可对账"
        assert dispatched["remaining"] == 1
        assert (await _device_row(db, devices[0].id)).asset_status == str(AssetStatus.PRODUCING)
        assert (await _device_row(db, devices[1].id)).asset_status == str(AssetStatus.FROZEN), (
            "冻结设备必须先解冻才会进入生产（冻结语义 = 别动它）"
        )

        # ④ 超量上报会被拦下——这是「委托数与实际台数一致」带来的直接收益：
        #    修复前 remaining 是 2，工厂可以多报 1 台，账面从此对不上
        over = await _burn(client, fh, factory_order_id, 2)
        _assert_error(over, 400, "BURN_COUNT_EXCEEDED")

        # ⑤ 按实际台数报满 1 台 → 工单 COMPLETED、设备 PRODUCED
        burned = await _burn(client, fh, factory_order_id, 1)
        assert burned.status_code == 200, burned.text
        assert burned.json()["status"] == "COMPLETED"
        assert (await _device_row(db, devices[0].id)).asset_status == str(AssetStatus.PRODUCED)
        assert (await _device_row(db, devices[1].id)).asset_status == str(AssetStatus.FROZEN)

        shipped = await _ship(client, fh, factory_order_id)
        assert shipped.status_code == 200, shipped.text

        # ⑥ 修复的核心：三个数字必须互相印证
        row = await _factory_order_row(db, factory_order_id)
        assert row.burned_count == row.quantity == 1
        assert (await _device_row(db, devices[0].id)).asset_status == str(AssetStatus.SHIPPED)
        assert (await _device_row(db, devices[1].id)).asset_status == str(AssetStatus.FROZEN)
        assert (await _order_row(db, order.id)).status == str(OrderStatus.SHIPPED_TO_CLIENT)

        shipped_devices = list(
            (
                await db.execute(
                    select(Device).where(
                        Device.order_id == order.id, Device.asset_status == str(AssetStatus.SHIPPED)
                    )
                )
            ).scalars()
        )
        assert len(shipped_devices) == 1
        assert row.burned_count == len(shipped_devices), (
            "烧录台数必须等于实际出货设备数——这是修复前对不上账的那个不变量"
        )

    async def test_frozen_device_can_be_thawed_and_re_dispatched(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """反向补齐：被扣下的那台设备解冻后仍有活路。

        ``dispatch_order`` 的「一单只能有一张工单」规则意味着冻结设备
        **不能**指望「解冻后再派一次」——但它可以走**分配单**这条路：
        ``FREEZABLE_ASSET_STATUSES`` 与 ``ALLOCATABLE_ASSET_STATUSES``
        都包含 ``IN_STOCK``，解冻回来就重新是一台可分配的库存设备。
        这条用例证明「冻结造成的差额」不会变成永久孤儿资产。
        """
        tenant = await make_tenant(code="FLOW-J2", name="解冻后处置租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="FLOWJ2")
        order, devices = await _lib_stocked_order(
            db, tenant_id=tenant.id, product_id=catalog["product_id"], quantity=2, sn_prefix="SN-J2"
        )
        platform = await auth.platform_headers()

        frozen = await client.post(
            f"{PLATFORM}/devices/{devices[1].id}/freeze", headers=platform, json={"reason": "扣下"}
        )
        assert frozen.status_code == 200, frozen.text

        factory = await _make_factory(db, code="FAC-J2")
        fh = await _factory_headers(auth, db, factory=factory, account="13600011002")
        dispatched = await _dispatch_ok(client, platform, order.id, factory.id)
        assert dispatched["quantity"] == 1

        # 解冻 → 回到 IN_STOCK（原路恢复），随即成为可分配库存
        thawed = await client.post(
            f"{PLATFORM}/devices/{devices[1].id}/thaw", headers=platform, json={"reason": "复核通过"}
        )
        assert thawed.status_code == 200, thawed.text
        assert thawed.json()["assetStatus"] == str(AssetStatus.IN_STOCK)

        # 完成本次生产，释放订单到 SHIPPED_TO_CLIENT（不再占用 IN_STOCK 语义）
        assert (await _burn(client, fh, dispatched["id"], 1)).status_code == 200
        assert (await _ship(client, fh, dispatched["id"])).status_code == 200
        assert (await _device_row(db, devices[1].id)).asset_status == str(AssetStatus.IN_STOCK)
