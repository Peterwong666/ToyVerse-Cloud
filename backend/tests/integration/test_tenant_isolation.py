"""集成测试：★ 租户隔离参数化矩阵（ADR-08 的对外可见面）。

这是 P5 todolist 明确要求、但当时**未交付**的那个文件。目标只有一个：
**每一个商户端 / 租户级端点都至少有一条「A 租户 token 访问 B 租户资源必须失败」
的参数化用例**，而不是靠主会话里几条定向断言。

矩阵的三条口径
--------------
1. **详情 / 写操作**：期望 404 ``RESOURCE_NOT_FOUND``（不泄漏资源是否存在）。
2. **列表**：期望只返回本租户记录——断言记录集合的 ``tenantId`` 唯一值等于 A，
   且 B 记录一条都不出现。
3. **平台独占端点**：商户 token → 403 ``PERMISSION_DENIED``（是「无权限」，
   不是「不存在」——这类端点的存在性本身不是秘密）。

另外三条守卫
------------
* **ADR-08 收口不变式**：库级直接构造跨租户数据（绕过所有服务层校验），
  再通过 HTTP 列表接口确认看不到。即使实现忘了调 ``scoped()``，只要
  写了 keyword 筛选也能被抓到。
* **平台 token 访问商户端端点的真实行为**（任务假设是「平台是全局长，
  不应报错」，本文件用实测把真实行为钉死并登记在报告里）。
* **矩阵自身不腐化**：读取 FastAPI 的 ``app.routes``，列出全部
  ``/api/v1/merchant/**`` 路径，断言参数化覆盖清单既无遗漏、也无过期条目。
  否则将来新增商户端点时，矩阵会**静默失效**。

关于 ``GET /merchant/audits``：**该端点不存在**（商户端目前只有
``merchant.py`` 一个模块，路由面共 12 条）。本文件的元测试会持续盯住这一点：
一旦审计端点落地，元测试立刻会因「清单遗漏」变红。
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext
from app.core.ids import new_id
from app.db.base import utcnow
from app.main import app as fastapi_app
from app.models.allocation import AllocationItem, AllocationOrder, DeviceBinding
from app.models.device import Device
from app.models.enums import (
    ActivationStatus,
    AllocationItemStatus,
    AllocationStatus,
    AssetStatus,
    BindingRecordStatus,
    BindStatus,
    OnlineStatus,
    OrderStatus,
    RoleType,
)
from app.models.order import Order
from app.services import catalog_service, qrcode_service
from tests.conftest import API_PREFIX

pytestmark = pytest.mark.integration

MERCHANT = f"{API_PREFIX}/merchant"
PLATFORM = f"{API_PREFIX}/platform"

#: 商户测试账号口令（仅存在于测试进程）
MERCHANT_PASSWORD = "Merch4nt-Iso#2026"

#: 项目统一错误码集合（用于钉死「不返回编造的错误码」）
_ERROR_CODES = {
    "UNAUTHENTICATED",
    "PERMISSION_DENIED",
    "TENANT_DISABLED",
    "VALIDATION_ERROR",
    "RESOURCE_NOT_FOUND",
    "IDEMPOTENCY_CONFLICT",
    "PRODUCT_NOT_AUTHORIZED",
    "DEVICE_NOT_AVAILABLE",
    "DEVICE_NOT_FOUND",
    "DEVICE_FROZEN",
    "DEVICE_NOT_IN_TENANT",
    "INVALID_STATE_TRANSITION",
    "QR_INVALID",
    "QR_EXPIRED",
}


# ---------------------------------------------------------------------------
# 覆盖清单（元测试的「声明侧」）
# ---------------------------------------------------------------------------
#
# 键 = ``(HTTP 方法, 完整路径模板)``；值 = 覆盖它的用例（写成人能读的说明）。
# 新增商户端端点时必须在此登记，否则 TestRouteMatrixIsComplete 会红灯。
MERCHANT_ROUTE_COVERAGE: dict[tuple[str, str], str] = {
    ("GET", f"{MERCHANT}/products"): "TestMerchantListIsolation（列表只返回本租户）",
    ("GET", f"{MERCHANT}/products/{{product_id}}"): "TestCrossTenantReadIs404（B 的产品 → 404）",
    ("GET", f"{MERCHANT}/orders"): "TestMerchantListIsolation（列表只返回本租户）",
    ("POST", f"{MERCHANT}/orders"): "test_cross_tenant_create_order_is_404（B 的产品下单 → 404）",
    ("GET", f"{MERCHANT}/orders/{{order_id}}"): "TestCrossTenantReadIs404（B 的订单 → 404）",
    ("GET", f"{MERCHANT}/devices"): "TestMerchantListIsolation（列表只返回本租户）",
    ("GET", f"{MERCHANT}/devices/{{device_id}}"): "TestCrossTenantReadIs404（B 的设备 → 404）",
    ("GET", f"{MERCHANT}/devices/{{device_id}}/events"): "TestCrossTenantReadIs404（B 的事件 → 404）",
    ("POST", f"{MERCHANT}/devices/bind/precheck"): "test_cross_tenant_precheck_is_device_not_in_tenant",
    ("POST", f"{MERCHANT}/devices/{{device_id}}/bind"): "TestCrossTenantReadIs404（B 的设备确认绑定 → 404）",
    ("POST", f"{MERCHANT}/devices/{{device_id}}/unbind"): "TestCrossTenantReadIs404（B 的设备解绑 → 404）",
    ("GET", f"{MERCHANT}/bindings"): "TestMerchantListIsolation（列表只返回本租户）",
}


# ---------------------------------------------------------------------------
# 模块级辅助
# ---------------------------------------------------------------------------


def _platform_ctx() -> AuthContext:
    """平台超管上下文（通配权限），供服务层建链使用。"""
    return AuthContext(
        user_id="u-iso-it",
        account="iso-it",
        role_code="PLATFORM_ADMIN",
        role_type=RoleType.PLATFORM,
        tenant_id=None,
        permissions=["*"],
    )


async def _seed_catalog(db: AsyncSession, tenant_id: str, *, suffix: str) -> dict[str, str]:
    """建「云服务商 → 模板 → 授权 → 客户产品」最小链条（走真实服务层）。"""
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
            "firmware_version": "1.0.0",
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


async def _make_device(
    db: AsyncSession,
    *,
    sn: str,
    tenant_id: str | None = None,
    product_id: str | None = None,
    asset_status: str = str(AssetStatus.ALLOCATED),
    bind_status: str = str(BindStatus.UNBOUND),
) -> Device:
    """库级造一台设备（绕过 HTTP 与服务层，确保造数不成为断言变量）。"""
    device = Device(
        id=new_id("device"),
        sn=sn,
        tenant_id=tenant_id,
        client_product_id=product_id,
        network_type="WIFI",
        asset_status=asset_status,
        activation_status=str(ActivationStatus.NOT_ACTIVATED),
        online_status=str(OnlineStatus.NEVER_ONLINE),
        bind_status=bind_status,
        generated_at=utcnow(),
    )
    db.add(device)
    await db.flush()
    return device


async def _make_order(
    db: AsyncSession, *, tenant_id: str, product_id: str, order_no: str
) -> Order:
    """库级造一张订单（商户端「我的订单」的隔离对象）。"""
    order = Order(
        id=new_id("order"),
        order_no=order_no,
        tenant_id=tenant_id,
        client_product_id=product_id,
        quantity=3,
        status=str(OrderStatus.PENDING_AUDIT),
        network_type="WIFI",
    )
    db.add(order)
    await db.flush()
    return order


async def _make_allocation(
    db: AsyncSession, *, tenant_id: str, product_id: str, device: Device, no_suffix: str
) -> AllocationOrder:
    """库级造一张分配单（含一行明细）。

    分配单没有商户端端点（平台独占），但它是租户级资源，这里一并造出来，
    供「平台独占端点」矩阵与「库级跨租户数据」不变式使用。
    """
    order = AllocationOrder(
        id=new_id("allocation_order"),
        allocation_no=f"AL-20260101-{no_suffix}",
        tenant_id=tenant_id,
        client_product_id=product_id,
        status=str(AllocationStatus.COMPLETED),
        total_count=1,
        allocated_count=1,
        failed_count=0,
        created_by="iso-it",
        executed_by="iso-it",
        executed_at=utcnow(),
    )
    db.add(order)
    await db.flush()
    db.add(
        AllocationItem(
            id=new_id("allocation_item"),
            allocation_order_id=order.id,
            device_id=device.id,
            device_sn=device.sn,
            status=str(AllocationItemStatus.ALLOCATED),
            allocated_at=utcnow(),
        )
    )
    await db.flush()
    return order


async def _make_binding(
    db: AsyncSession,
    *,
    device: Device,
    tenant_id: str,
    status: str,
    product_id: str | None = None,
) -> DeviceBinding:
    """库级造一条绑定记录（``device_id`` 唯一，一台设备至多一行）。"""
    binding = DeviceBinding(
        id=new_id("device_binding"),
        device_id=device.id,
        tenant_id=tenant_id,
        client_product_id=product_id,
        status=status,
        bind_count=1 if status in {str(BindingRecordStatus.BOUND), str(BindingRecordStatus.UNBOUND)} else 0,
        bound_at=utcnow() if status == str(BindingRecordStatus.BOUND) else None,
        bound_by="iso-it" if status == str(BindingRecordStatus.BOUND) else None,
    )
    db.add(binding)
    await db.flush()
    return binding


async def _merchant_headers(
    auth: Any, make_user: Any, tenant_id: str, account: str
) -> dict[str, str]:
    """建一个商户管理员并登录，返回鉴权头。"""
    await make_user(
        account=account,
        password=MERCHANT_PASSWORD,
        role_code="MERCHANT_ADMIN",
        tenant_id=tenant_id,
    )
    return auth.headers(await auth.token(account, MERCHANT_PASSWORD))


def _assert_error(response: Any, status: int, code: str) -> None:
    """断言失败响应的状态码与错误码，并顺带钉死错误码契约。"""
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code, response.text
    assert body["code"] in _ERROR_CODES, f"返回了未登记的错误码：{body['code']}"
    assert isinstance(body["message"], str) and body["message"]
    assert "traceId" in body


async def _page(
    client: AsyncClient, headers: dict[str, str], path: str, **params: Any
) -> dict[str, Any]:
    """请求一个分页列表端点并返回响应体（顺带校验 200）。"""
    response = await client.get(path, headers=headers, params=params)
    assert response.status_code == 200, f"GET {path} {params}：{response.text}"
    return response.json()


# ---------------------------------------------------------------------------
# 世界：两个租户各有产品 / 设备 / 订单 / 分配单 / 绑定记录
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def world(
    client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
) -> dict[str, Any]:
    """造出 A / B 两个租户的完整同构数据，并各自登录一个商户管理员。

    刻意用**库级造数**而不是 HTTP：这样隔离断言的对象就是数据本身，
    不会因为「造数过程走了哪条服务路径」而产生额外变量。
    """
    tenant_a = await make_tenant(code="ISO-A", name="隔离租户A")
    tenant_b = await make_tenant(code="ISO-B", name="隔离租户B")
    catalog_a = await _seed_catalog(db, tenant_a.id, suffix="ISOA")
    catalog_b = await _seed_catalog(db, tenant_b.id, suffix="ISOB")

    device_a = await _make_device(
        db,
        sn="SN-ISO-A-01",
        tenant_id=tenant_a.id,
        product_id=catalog_a["product_id"],
        asset_status=str(AssetStatus.ALLOCATED),
    )
    # B 的设备做成「已绑定」：这样跨租户解绑若被放行，会真的改坏数据，
    # 404 才是唯一正确的答案（设备状态本身是合法的可解绑状态）。
    device_b = await _make_device(
        db,
        sn="SN-ISO-B-01",
        tenant_id=tenant_b.id,
        product_id=catalog_b["product_id"],
        asset_status=str(AssetStatus.BOUND),
        bind_status=str(BindStatus.BOUND),
    )

    order_a = await _make_order(
        db, tenant_id=tenant_a.id, product_id=catalog_a["product_id"], order_no="ORD-ISO-A-01"
    )
    order_b = await _make_order(
        db, tenant_id=tenant_b.id, product_id=catalog_b["product_id"], order_no="ORD-ISO-B-01"
    )
    allocation_a = await _make_allocation(
        db, tenant_id=tenant_a.id, product_id=catalog_a["product_id"], device=device_a, no_suffix="AAAA"
    )
    allocation_b = await _make_allocation(
        db, tenant_id=tenant_b.id, product_id=catalog_b["product_id"], device=device_b, no_suffix="BBBB"
    )
    binding_a = await _make_binding(
        db, device=device_a, tenant_id=tenant_a.id, status=str(BindingRecordStatus.PENDING)
    )
    binding_b = await _make_binding(
        db,
        device=device_b,
        tenant_id=tenant_b.id,
        status=str(BindingRecordStatus.BOUND),
        product_id=catalog_b["product_id"],
    )

    merchant_a = await _merchant_headers(auth, make_user, tenant_a.id, "13220000001")
    merchant_b = await _merchant_headers(auth, make_user, tenant_b.id, "13220000002")

    return {
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "catalog_a": catalog_a,
        "catalog_b": catalog_b,
        "device_a": device_a,
        "device_b": device_b,
        "order_a": order_a,
        "order_b": order_b,
        "allocation_a": allocation_a,
        "allocation_b": allocation_b,
        "binding_a": binding_a,
        "binding_b": binding_b,
        "merchant_a": merchant_a,
        "merchant_b": merchant_b,
    }


# ===========================================================================
# 一、★ 跨租户详情 / 写操作：一律 404
# ===========================================================================


#: (用例说明, 方法, 路径模板, world 里的资源键, 请求体)
_CROSS_TENANT_404_CASES: list[tuple[str, str, str, str, dict[str, Any] | None]] = [
    ("产品详情", "GET", f"{MERCHANT}/products/{{resource}}", "product_b", None),
    ("订单详情", "GET", f"{MERCHANT}/orders/{{resource}}", "order_b", None),
    ("设备详情", "GET", f"{MERCHANT}/devices/{{resource}}", "device_b", None),
    ("设备事件", "GET", f"{MERCHANT}/devices/{{resource}}/events", "device_b", None),
    (
        "确认绑定",
        "POST",
        f"{MERCHANT}/devices/{{resource}}/bind",
        "device_b",
        {"confirmToken": "dummy-confirm-token"},
    ),
    (
        "解绑设备",
        "POST",
        f"{MERCHANT}/devices/{{resource}}/unbind",
        "device_b",
        {"reason": "越权解绑尝试"},
    ),
]


class TestCrossTenantReadIs404:
    """A 的 token 带上 B 的资源 ID → 404（「不存在」与「无权限」不可区分）。"""

    @pytest.mark.parametrize(
        ("case", "method", "path_template", "resource_key", "body"),
        _CROSS_TENANT_404_CASES,
        ids=[case[0] for case in _CROSS_TENANT_404_CASES],
    )
    async def test_cross_tenant_resource_is_404(
        self,
        client: AsyncClient,
        world: dict[str, Any],
        case: str,
        method: str,
        path_template: str,
        resource_key: str,
        body: dict[str, Any] | None,
    ) -> None:
        """★ 这里坏了说明：越权访问返回了 403 或 200。

        403 会让人靠错误码差异**探测**「这个 ID 是否存在」；200 则是直接的
        数据越权。两种都算破坏 ADR-08 的对外承诺。
        """
        resource_id = _resolve_resource_id(world, resource_key)
        response = await client.request(
            method,
            path_template.format(resource=resource_id),
            headers=world["merchant_a"],
            json=body,
        )
        _assert_error(response, 404, "RESOURCE_NOT_FOUND")

    async def test_blocked_unbind_did_not_touch_foreign_binding(
        self, client: AsyncClient, world: dict[str, Any], db: AsyncSession
    ) -> None:
        """越权失败必须**真的没发生**：B 的绑定记录仍是 ``BOUND``。

        只看 HTTP 404 不够——实现可能在返回 404 之前就已经改了库
        （例如先写后校验），那样用户看到「失败」但数据已经被破坏。
        """
        response = await client.post(
            f"{MERCHANT}/devices/{world['device_b'].id}/unbind",
            headers=world["merchant_a"],
            json={"reason": "越权解绑尝试"},
        )
        _assert_error(response, 404, "RESOURCE_NOT_FOUND")

        stored = await db.get(DeviceBinding, world["binding_b"].id)
        assert stored is not None
        assert stored.status == str(BindingRecordStatus.BOUND)
        assert stored.unbound_at is None

    async def test_cross_tenant_create_order_is_404(
        self, client: AsyncClient, world: dict[str, Any], db: AsyncSession
    ) -> None:
        """★ 写操作的隔离：A 用 **B 的客户产品** 下单 → 404，且 B 的订单数不变。

        这里坏了说明：商户可以把自己（或别人）的订单挂到其他租户的产品上，
        属于跨租户数据写入。
        """
        before = await _order_count(db, world["tenant_b"].id)
        response = await client.post(
            f"{MERCHANT}/orders",
            headers=world["merchant_a"],
            json={"clientProductId": world["catalog_b"]["product_id"], "quantity": 2},
        )
        _assert_error(response, 404, "RESOURCE_NOT_FOUND")
        assert await _order_count(db, world["tenant_b"].id) == before, "越权下单不得落库"

    async def test_own_order_can_be_created_and_read(
        self, client: AsyncClient, world: dict[str, Any]
    ) -> None:
        """对抗性反证：A 用**自己的**产品下单与读详情都能成功。

        没有这条，上面那些 404 可能只是「这个端点对谁都 404」造成的假绿。
        """
        created = await client.post(
            f"{MERCHANT}/orders",
            headers=world["merchant_a"],
            json={"clientProductId": world["catalog_a"]["product_id"], "quantity": 2},
        )
        assert created.status_code == 201, created.text
        assert created.json()["tenantId"] == world["tenant_a"].id

        detail = await client.get(
            f"{MERCHANT}/orders/{created.json()['id']}", headers=world["merchant_a"]
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["id"] == created.json()["id"]

    async def test_cross_tenant_precheck_is_device_not_in_tenant(
        self, client: AsyncClient, world: dict[str, Any]
    ) -> None:
        """★ 跨租户扫码预检 → 403 ``DEVICE_NOT_IN_TENANT``（**不是** 404）。

        这条刻意与上面的 404 口径不同，且不是缺陷：``precheck`` 的入参是
        **二维码载荷**而不是资源 ID，载荷本身已明确写出「这是 B 的设备」，
        不存在「探测 B 有哪些设备」的信息泄漏；此处的语义是「你扫的不是
        你的货」，用 403 直说更有助于现场排障。
        """
        payload = qrcode_service.build_jd_payload(
            world["tenant_b"].id, world["catalog_b"]["product_id"], world["device_b"].sn
        )
        response = await client.post(
            f"{MERCHANT}/devices/bind/precheck",
            headers=world["merchant_a"],
            json={"qrPayload": payload},
        )
        _assert_error(response, 403, "DEVICE_NOT_IN_TENANT")


def _resolve_resource_id(world: dict[str, Any], key: str) -> str:
    """把清单里的资源键解析成真实 ID。"""
    mapping = {
        "product_b": world["catalog_b"]["product_id"],
        "order_b": world["order_b"].id,
        "device_b": world["device_b"].id,
        "allocation_b": world["allocation_b"].id,
    }
    assert key in mapping, f"未登记的资源键：{key}"
    return str(mapping[key])


async def _order_count(db: AsyncSession, tenant_id: str) -> int:
    """某租户的订单数（用于「越权写不得落库」的断言）。"""
    return int(
        (
            await db.execute(
                select(func.count()).select_from(Order).where(Order.tenant_id == tenant_id)
            )
        ).scalar_one()
    )


# ===========================================================================
# 二、★ 列表：只返回本租户记录
# ===========================================================================


#: (用例说明, 路径, 响应体里「本租户」的判定键)
_LIST_CASES: list[tuple[str, str, str]] = [
    ("产品列表", f"{MERCHANT}/products", "tenantId"),
    ("订单列表", f"{MERCHANT}/orders", "tenantId"),
    ("设备列表", f"{MERCHANT}/devices", "tenantId"),
    ("绑定列表", f"{MERCHANT}/bindings", "tenantId"),
]


class TestMerchantListIsolation:
    """列表口径：记录集合的 ``tenantId`` 唯一值等于 A，B 的记录一条都不出现。"""

    @pytest.mark.parametrize(
        ("case", "path", "tenant_key"),
        _LIST_CASES,
        ids=[case[0] for case in _LIST_CASES],
    )
    async def test_list_returns_only_own_tenant(
        self, client: AsyncClient, world: dict[str, Any], case: str, path: str, tenant_key: str
    ) -> None:
        """★ 这里坏了说明：``scoped()`` 没被调用（或调用错了模型），
        商户列表把别的租户的记录也发了出去——最直接的数据泄漏。
        """
        body = await _page(client, world["merchant_a"], path)

        records = body["records"]
        assert records, f"{case} 应当至少返回本租户的一条记录（否则断言无判别力）"
        assert body["total"] == len(records)

        tenants_seen = {record[tenant_key] for record in records}
        assert tenants_seen == {world["tenant_a"].id}, (
            f"{case} 出现了非本租户记录：{tenants_seen}"
        )

    @pytest.mark.parametrize(
        ("case", "path", "foreign_key"),
        [
            ("产品列表不含 B 的产品", f"{MERCHANT}/products", "product_b"),
            ("订单列表不含 B 的订单", f"{MERCHANT}/orders", "order_b"),
            ("设备列表不含 B 的设备", f"{MERCHANT}/devices", "device_b"),
            ("绑定列表不含 B 的绑定", f"{MERCHANT}/bindings", "binding_b"),
        ],
        ids=["产品", "订单", "设备", "绑定"],
    )
    async def test_list_does_not_leak_specific_foreign_record(
        self,
        client: AsyncClient,
        world: dict[str, Any],
        case: str,
        path: str,
        foreign_key: str,
    ) -> None:
        """★ 逐条点名：B 的**具体那条记录 ID** 不得出现在 A 的列表里。

        「tenantId 集合正确」与「某条 B 记录确实没被返回」不是同一句话：
        前者防不住「额外多返回了一条 tenantId 被改写成 A 的脏数据」。
        """
        body = await _page(client, world["merchant_a"], path)
        ids = {record["id"] for record in body["records"]}

        foreign_id = {
            "product_b": world["catalog_b"]["product_id"],
            "order_b": world["order_b"].id,
            "device_b": world["device_b"].id,
            "binding_b": world["binding_b"].id,
        }[foreign_key]

        assert foreign_id not in ids, f"{case}：泄漏了 B 的记录 {foreign_id}"

    async def test_list_includes_own_records(
        self, client: AsyncClient, world: dict[str, Any]
    ) -> None:
        """对抗性反证：A 自己的记录必须**在**列表里。

        没有这条，一个「永远返回空列表」的实现也能让上面全部通过。
        """
        products = await _page(client, world["merchant_a"], f"{MERCHANT}/products")
        assert world["catalog_a"]["product_id"] in {
            record["id"] for record in products["records"]
        }
        orders = await _page(client, world["merchant_a"], f"{MERCHANT}/orders")
        assert world["order_a"].id in {record["id"] for record in orders["records"]}
        devices = await _page(client, world["merchant_a"], f"{MERCHANT}/devices")
        assert world["device_a"].id in {record["id"] for record in devices["records"]}
        bindings = await _page(client, world["merchant_a"], f"{MERCHANT}/bindings")
        assert world["binding_a"].id in {record["id"] for record in bindings["records"]}


# ===========================================================================
# 三、★ ADR-08 收口不变式：库级跨租户数据在 HTTP 层不可见
# ===========================================================================


class TestAdr08ScopeInvariant:
    """即使实现忘了调 ``scoped()``，只要它还在用 keyword 筛选，就会被抓到。"""

    async def test_direct_db_foreign_rows_are_invisible_over_http(
        self, client: AsyncClient, world: dict[str, Any], db: AsyncSession
    ) -> None:
        """★ 库级直接插入 B 的记录（绕过所有服务层），用 keyword 精确检索 → 0 条。

        做法上的讲究：keyword 是**能命中该记录**的精确值
        （如 B 设备的 SN）。若实现只做了 keyword 过滤而漏了租户过滤，
        这里就会返回 1 条 —— 这正是「忘了加 scoped() 也能抓到」的含义。
        """
        tenant_b = world["tenant_b"]
        leak_device = await _make_device(
            db,
            sn="SN-ISO-LEAK-B",
            tenant_id=tenant_b.id,
            product_id=world["catalog_b"]["product_id"],
        )
        leak_order = await _make_order(
            db,
            tenant_id=tenant_b.id,
            product_id=world["catalog_b"]["product_id"],
            order_no="ORD-ISO-LEAK-B",
        )
        await _make_binding(
            db,
            device=leak_device,
            tenant_id=tenant_b.id,
            status=str(BindingRecordStatus.PENDING),
        )
        await db.flush()

        # ① 设备：按 B 的 SN 精确搜 → 0
        devices = await _page(
            client, world["merchant_a"], f"{MERCHANT}/devices", keyword="SN-ISO-LEAK-B"
        )
        assert devices["total"] == 0, f"A 用 B 的 SN 搜到了别人的设备：{devices['records']}"

        # ② 订单：按 B 的单号精确搜 → 0
        orders = await _page(
            client, world["merchant_a"], f"{MERCHANT}/orders", keyword="ORD-ISO-LEAK-B"
        )
        assert orders["total"] == 0, f"A 用 B 的单号搜到了别人的订单：{orders['records']}"

        # ③ 绑定：按 B 的设备 SN 搜 → 0
        bindings = await _page(
            client, world["merchant_a"], f"{MERCHANT}/bindings", keyword="SN-ISO-LEAK-B"
        )
        assert bindings["total"] == 0, f"A 按 SN 搜到了别人的绑定：{bindings['records']}"

        # ④ 产品：按 B 的产品编码搜 → 0
        products = await _page(
            client, world["merchant_a"], f"{MERCHANT}/products", keyword="PROD-ISOB"
        )
        assert products["total"] == 0, f"A 用 B 的编码搜到了别人的产品：{products['records']}"

        # ⑤ 反向控制：同样的筛选用在 A 自己的数据上必须命中 1 条，
        #    否则「0 条」可能只是 keyword 拼错导致的假绿。
        own_devices = await _page(
            client, world["merchant_a"], f"{MERCHANT}/devices", keyword=world["device_a"].sn
        )
        assert own_devices["total"] == 1
        assert own_devices["records"][0]["id"] == world["device_a"].id

        own_orders = await _page(
            client, world["merchant_a"], f"{MERCHANT}/orders", keyword=world["order_a"].order_no
        )
        assert own_orders["total"] == 1
        assert own_orders["records"][0]["id"] == world["order_a"].id

        # 库里确实存在这些跨租户数据（证明 0 条来自过滤，而不是数据没插进去）
        assert (await db.get(Device, leak_device.id)) is not None
        assert (await db.get(Order, leak_order.id)) is not None


# ===========================================================================
# 四、平台独占端点：商户 token → 403
# ===========================================================================


class TestPlatformOnlyEndpointsRejectMerchant:
    """平台独占端点对商户是「无权限」（403），不是「不存在」（404）。"""

    @pytest.mark.parametrize(
        ("method", "path_template", "body_factory"),
        [
            ("GET", f"{PLATFORM}/tenants", None),
            ("GET", f"{PLATFORM}/orders", None),
            ("GET", f"{PLATFORM}/devices", None),
            ("GET", f"{PLATFORM}/batches", None),
            ("GET", f"{PLATFORM}/allocations", None),
            ("POST", f"{PLATFORM}/allocations", "create_allocation"),
            ("POST", f"{PLATFORM}/allocations/{{resource}}/execute", None),
            ("GET", f"{PLATFORM}/allocations/{{resource}}", None),
        ],
        ids=[
            "租户列表",
            "订单列表",
            "设备列表",
            "批次列表",
            "分配单列表",
            "创建分配单",
            "执行分配单",
            "分配单详情",
        ],
    )
    async def test_merchant_gets_permission_denied(
        self,
        client: AsyncClient,
        world: dict[str, Any],
        method: str,
        path_template: str,
        body_factory: str | None,
    ) -> None:
        """★ 这里坏了说明：商户拿到了平台独占能力（可读全平台 / 可分设备）。

        注意断言 ``403 PERMISSION_DENIED``（而不是 404）：平台端点的**存在性**
        不是秘密（前端本来就要按角色渲染菜单），此处要表达的是「你不该来」。
        """
        path = path_template.format(resource=world["allocation_a"].id)
        body: dict[str, Any] | None = None
        if body_factory == "create_allocation":
            body = {
                "tenantId": world["tenant_a"].id,
                "clientProductId": world["catalog_a"]["product_id"],
                "deviceIds": [world["device_a"].id],
            }
        response = await client.request(
            method, path, headers=world["merchant_a"], json=body
        )
        _assert_error(response, 403, "PERMISSION_DENIED")
        details = response.json().get("details") or {}
        assert str(details.get("requiredPermission", "")).startswith("platform:"), (
            "权限错误必须回显缺失的权限码，便于前端与排障定位"
        )


# ===========================================================================
# 五、平台 token 访问商户端端点的**真实行为**
# ===========================================================================


class TestPlatformTokenOnMerchantEndpoints:
    """任务假设「平台是全局长，不应报错」——这里用实测把真实行为钉死。"""

    @pytest.mark.parametrize(
        ("method", "path_template"),
        [
            ("GET", f"{MERCHANT}/products"),
            ("GET", f"{MERCHANT}/orders"),
            ("GET", f"{MERCHANT}/devices"),
            ("GET", f"{MERCHANT}/bindings"),
            ("GET", f"{MERCHANT}/devices/{{resource}}"),
        ],
        ids=["产品列表", "订单列表", "设备列表", "绑定列表", "设备详情"],
    )
    async def test_actual_behavior_platform_token_is_rejected_by_role_guard(
        self,
        client: AsyncClient,
        auth: Any,
        world: dict[str, Any],
        method: str,
        path_template: str,
    ) -> None:
        """★ 实测结果：**平台 token 访问商户端端点会被角色守卫拒绝（403）**。

        与「平台是全局长」并不矛盾，但必须说清是哪一层在拒绝：

        * ``scoped()`` / ``assert_visible()`` 的**数据作用域**对平台确实不过滤
          （``AuthContext.is_platform`` 直接放行）；
        * 但商户端路由额外挂了 ``MerchantAuth``（``require_merchant``），
          它只认 ``MERCHANT_ADMIN`` / ``MERCHANT_OPERATOR`` 两个角色码，
          平台角色不在其中——这是**有意的架构防线**：
          ``AuthContext.require_tenant_id()`` 的 docstring 明确写了
          「这样可以在架构上防止商户接口被平台角色误用而绕过租户过滤」。

        因此结论是：平台要看这些数据必须走 ``/platform/**``（权限更粗但更明确），
        而**不能**借商户端接口拿到「不加租户过滤」的视图。若哪天这条变成 200，
        说明角色守卫被摘掉了，需要重新评估越权面。
        """
        path = path_template.format(resource=world["device_a"].id)
        headers = await auth.platform_headers()
        response = await client.request(method, path, headers=headers)

        assert response.status_code == 403, (
            f"实测与预期不符：{method} {path} → {response.status_code} {response.text}"
        )
        assert response.json()["code"] == "PERMISSION_DENIED"

    async def test_platform_scope_is_truly_global_on_platform_endpoints(
        self, client: AsyncClient, auth: Any, world: dict[str, Any]
    ) -> None:
        """配套证据：``scoped()`` 对平台确实不过滤——平台端点看得到两个租户。

        这条与上一条合起来才能说明「403 来自角色守卫，而不是来自数据层」。
        """
        headers = await auth.platform_headers()
        for device in (world["device_a"], world["device_b"]):
            page = await _page(
                client, headers, f"{PLATFORM}/devices", keyword=device.sn
            )
            assert page["total"] == 1, f"平台应能看到 {device.sn}：{page}"
            assert page["records"][0]["tenantId"] in {
                world["tenant_a"].id,
                world["tenant_b"].id,
            }


# ===========================================================================
# 六、★ 矩阵自身不腐化：路由覆盖元测试
# ===========================================================================


class TestRouteMatrixIsComplete:
    """读取 FastAPI 路由表，防止「新增端点 → 矩阵静默失效」。"""

    def test_merchant_route_matrix_has_no_gap(self) -> None:
        """★ 商户端端点集合与覆盖清单必须严格相等（双向）。

        * 漏登记 → 新端点没有隔离用例，越权缺口无人看守；
        * 多登记 → 清单里躺着已删除的端点，让人误以为它还受测。

        实现说明（为什么这条元测试**可行**）：``app.routes`` 上的
        ``APIRoute.path`` 已是含 ``/api/v1`` 前缀的完整模板（如
        ``/api/v1/merchant/devices/{device_id}``），``methods`` 是方法集合，
        因此可以直接与清单做集合比较，无需解析 OpenAPI 文档。
        """
        discovered: set[tuple[str, str]] = set()
        for route in fastapi_app.routes:
            path = getattr(route, "path", "")
            if not path.startswith(f"{API_PREFIX}/merchant"):
                continue
            methods = getattr(route, "methods", None) or set()
            for method in methods:
                if method in {"HEAD", "OPTIONS"}:
                    continue
                discovered.add((method, path))

        # 先做一次「枚举是否真的工作」的自检：否则路由全被改名时这条会假绿
        assert len(discovered) >= 12, f"路由枚举异常，只发现 {sorted(discovered)}"

        declared = set(MERCHANT_ROUTE_COVERAGE)
        missing = discovered - declared
        stale = declared - discovered
        assert not missing, f"新增的商户端端点未纳入隔离矩阵：{sorted(missing)}"
        assert not stale, f"隔离矩阵登记了不存在的端点：{sorted(stale)}"

    def test_merchant_audits_endpoint_does_not_exist(self) -> None:
        """★ 登记一条容易复发的假设：商户端**没有** ``/merchant/audits``。

        P5 todolist 里写了这个端点，但实现里并不存在（商户端只有
        ``merchant.py`` 一个模块）。这条用例的作用不是「要求它不存在」，
        而是把这个事实钉住：一旦审计端点落地，这条会红灯提醒把它
        纳入隔离矩阵（``/merchant/audits`` 必须只返回本租户审计）。
        """
        paths = {
            (getattr(route, "path", ""), method)
            for route in fastapi_app.routes
            for method in (getattr(route, "methods", None) or set())
        }
        assert not [item for item in paths if item[0].startswith(f"{MERCHANT}/audit")], (
            "商户审计端点已落地——请把它纳入隔离矩阵后再更新本断言"
        )
