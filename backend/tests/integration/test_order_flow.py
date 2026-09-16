"""集成测试：P4 订单与设备生成闭环。

覆盖本阶段验收标准
------------------
* ① **订单闭环**：商户下单 → 平台审核 → 生成设备并入库（``IN_STOCK``）；
  每一跳的状态都能在平台列表 / 详情与商户详情中查回（可追溯，不是「只在内存里变过」）。
* ② **状态机收口**：终态（``REJECTED`` / ``COMPLETED``）再审核被拒；
  ``PENDING_AUDIT`` 直接生成被拒；``INVALID_STATE_TRANSITION`` 的 ``details``
  永远带 ``current`` / ``target``（前端据此显示「当前状态不允许该操作」）。
* ③ **驳回必须带原因**：``decision=REJECTED`` 缺 ``rejectReason`` → 400 ``VALIDATION_ERROR``。
* ④ **下单幂等**：同 ``Idempotency-Key`` + 同请求体 → 回放同一订单；同键异体 → 409。
* ⑤ ★ **租户隔离**（``tenant_isolation``）：跨租户读订单 / 产品一律 **404** 而非 403
  ——否则错误码本身就是「他人资源是否存在」的探测器。
* ⑥ ★ **ADR-07 安全失败**：4G 订单厂商未配置密钥时 → 503 ``VENDOR_UNAVAILABLE``，
  订单退回 ``APPROVED``、设备表零新增、失败留审计。**绝不伪造一批设备。**
* ⑦ **平台运营角色边界**：可审核订单（有 ``platform:order:write``），
  不可改租户 / 云服务商 / 产品模板。
* ⑧ **设备生命周期**：冻结 / 解冻 / 报废 + 事件时间线；
  P6 起可冻结 ``{IN_STOCK, ALLOCATED, BOUND}``（用户决策放宽，见
  ``FREEZABLE_ASSET_STATUSES``），★ **冻结 ``GENERATED`` 必须被拒**，
  且三类可冻结状态必须能**原路解冻恢复**。
* ⑨ **筛选与详情装配**：``GET /platform/devices?orderId=`` 按订单筛设备；
  订单详情与列表的 ``tenantName`` / ``clientProductName`` / ``deviceCount`` 一致；
  设备列表项**刻意不含** ``tenantName``（防止有人「顺手补上」拖慢列表）。
* ⑩ **越权读设备**：商户 token 访问 ``GET /platform/devices`` → 403（平台独占）。

测试写法对齐 ``tests/integration/test_catalog.py``：camelCase 断言、
中文注释说明「为什么这样断言」、库级用例直接落数据（不靠 HTTP 造数）。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext
from app.core.ids import new_id
from app.db.base import utcnow
from app.models.audit import AuditLog
from app.models.catalog import ClientProduct
from app.models.device import Device
from app.models.enums import (
    FREEZABLE_ASSET_STATUSES,
    ActivationStatus,
    AssetStatus,
    AuditAction,
    BindStatus,
    EnableStatus,
    OnlineStatus,
    OrderStatus,
    RoleType,
)
from app.models.order import Order
from app.services import catalog_service, qrcode_service
from tests.conftest import API_PREFIX

pytestmark = pytest.mark.integration

PLATFORM = f"{API_PREFIX}/platform"
MERCHANT = f"{API_PREFIX}/merchant"

#: 商户测试账号口令（仅存在于测试进程）
MERCHANT_PASSWORD = "Merch4nt-It#2026"

#: 平台端设备列表**刻意不含**的名称类字段（见 ``DeviceResponse`` 的说明）
_DEVICE_LIST_FORBIDDEN_FIELDS = ("tenantName", "tenantCode", "clientProductName")


# ---------------------------------------------------------------------------
# 认证上下文与建链辅助（模块级，避免污染 conftest）
# ---------------------------------------------------------------------------


def _platform_ctx() -> AuthContext:
    """平台超管上下文（通配权限），供服务层建链使用。"""
    return AuthContext(
        user_id="u-order-it",
        account="order-it",
        role_code="PLATFORM_ADMIN",
        role_type=RoleType.PLATFORM,
        tenant_id=None,
        permissions=["*"],
    )


async def _seed_catalog(
    db: AsyncSession,
    tenant_id: str,
    *,
    suffix: str,
    network_type: str = "WIFI",
    vendor: str = "JOYINSIDE",
) -> dict[str, str]:
    """建「云服务商 → 模板 → 授权 → 客户产品」最小链条（走真实服务层）。

    ``suffix`` 用于保证编码唯一；用例间会清库，但仍保持可读的确定性编码。
    """
    platform = _platform_ctx()
    cloud = await catalog_service.create_cloud(
        db,
        payload={
            "code": f"CLOUD-{suffix}",
            "name": f"{suffix} 测试云",
            "vendor": vendor,
            "network_type": network_type,
        },
        actor=platform,
    )
    template = await catalog_service.create_template(
        db,
        payload={
            "code": f"TPL-{suffix}",
            "name": f"{suffix} 测试模板",
            "network_type": network_type,
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


async def _create_order(
    client: AsyncClient,
    headers: dict[str, str],
    product_id: str,
    *,
    quantity: int = 2,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """商户下单（断言 201 并返回响应体）。"""
    request_headers = dict(headers)
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    response = await client.post(
        f"{MERCHANT}/orders",
        headers=request_headers,
        json={
            "clientProductId": product_id,
            "quantity": quantity,
            "applicantName": "王经理",
            "applicantPhone": "13800000001",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _audit(
    client: AsyncClient,
    headers: dict[str, str],
    order_id: str,
    *,
    decision: str = "APPROVED",
    **extra: Any,
) -> Any:
    """调审核端点（不预设状态码，便于失败用例断言 4xx）。"""
    return await client.post(
        f"{PLATFORM}/orders/{order_id}/audit",
        headers=headers,
        json={"decision": decision, **extra},
    )


async def _generate(
    client: AsyncClient, headers: dict[str, str], order_id: str
) -> Any:
    """调生成端点（不预设状态码）。"""
    return await client.post(f"{PLATFORM}/orders/{order_id}/generate", headers=headers)


async def _make_device(
    db: AsyncSession,
    *,
    sn: str,
    asset_status: str = str(AssetStatus.IN_STOCK),
    tenant_id: str | None = None,
    order_id: str | None = None,
    product_id: str | None = None,
    network_type: str = "WIFI",
) -> Device:
    """直接落一台设备。

    库级用例（冻结 / 报废规则、事件时间线）刻意不经 HTTP 造数：
    要验证的是**状态机与冻结白名单**，让造数过程本身不成为变量。
    """
    device = Device(
        id=new_id("device"),
        sn=sn,
        tenant_id=tenant_id,
        order_id=order_id,
        client_product_id=product_id,
        network_type=network_type,
        asset_status=asset_status,
        activation_status=str(ActivationStatus.NOT_ACTIVATED),
        online_status=str(OnlineStatus.NEVER_ONLINE),
        bind_status=str(BindStatus.UNBOUND),
        generated_at=utcnow(),
    )
    db.add(device)
    await db.flush()
    return device


async def _active_device_count(db: AsyncSession, order_id: str) -> int:
    """订单下在册设备数（用于「没有伪造设备」的断言）。"""
    return int(
        (
            await db.execute(
                select(func.count()).select_from(Device).where(Device.order_id == order_id)
            )
        ).scalar_one()
    )


# ===========================================================================
# 一、订单闭环
# ===========================================================================


class TestOrderClosedLoop:
    """``PENDING_AUDIT → APPROVED → GENERATED → IN_STOCK`` 全通且可追溯。"""

    async def test_wifi_order_closed_loop_with_traceable_states(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """Wi-Fi 产品（京东方案本地生成 SN）的完整闭环。"""
        tenant = await make_tenant(code="ORD-CLOSE", name="闭环测试租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13100000001")
        catalog = await _seed_catalog(db, tenant.id, suffix="CLOSE", network_type="WIFI")
        platform = await auth.platform_headers()

        # ---- ① 下单：落 PENDING_AUDIT，联网方式从客户产品快照 ----
        order = await _create_order(client, merchant, catalog["product_id"], quantity=3)
        assert order["status"] == str(OrderStatus.PENDING_AUDIT)
        assert order["statusLabel"] == "待审核"
        assert order["networkType"] == "WIFI"
        assert order["quantity"] == 3
        assert order["generatedCount"] == 0
        assert order["deviceCount"] == 0
        assert order["tenantName"] == tenant.name
        assert order["clientProductName"], "创建响应应已装配客户产品名（P3 同类装配缺陷的回归点）"

        # 平台列表能看到（可追溯：不是只存在于创建响应里）
        listed = await client.get(
            f"{PLATFORM}/orders", headers=platform, params={"keyword": order["orderNo"]}
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["total"] == 1
        assert listed.json()["records"][0]["status"] == str(OrderStatus.PENDING_AUDIT)

        # ---- ② 审核通过 ----
        audited = await _audit(client, platform, order["id"], decision="APPROVED", remark="资料齐全")
        assert audited.status_code == 200, audited.text
        assert audited.json()["status"] == str(OrderStatus.APPROVED)
        assert audited.json()["statusLabel"] == "已审核"
        assert audited.json()["auditRemark"] == "资料齐全"
        assert audited.json()["rejectReason"] is None

        # ---- ③ 生成设备并入库 ----
        generated = await _generate(client, platform, order["id"])
        assert generated.status_code == 200, generated.text
        payload = generated.json()
        assert payload["orderId"] == order["id"]
        assert payload["requested"] == 3
        assert payload["generated"] == 3
        assert payload["failed"] == 0
        assert len(payload["devices"]) == 3
        assert {item["assetStatus"] for item in payload["devices"]} == {str(AssetStatus.IN_STOCK)}

        # ---- ④ 详情：状态落到 IN_STOCK，计数与设备数一致 ----
        detail = await client.get(f"{PLATFORM}/orders/{order['id']}", headers=platform)
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["status"] == str(OrderStatus.IN_STOCK)
        assert body["statusLabel"] == "已入库"
        assert body["generatedCount"] == 3
        assert body["deviceCount"] == 3
        assert len(body["devices"]) == 3
        assert body["generationDetail"]["ok"] is True
        assert body["generationDetail"]["generated"] == 3
        assert body["tenantName"] == tenant.name

        # ---- ⑤ 商户端也能查到终态（两个视角看到同一事实）----
        merchant_detail = await client.get(f"{MERCHANT}/orders/{order['id']}", headers=merchant)
        assert merchant_detail.status_code == 200, merchant_detail.text
        assert merchant_detail.json()["status"] == str(OrderStatus.IN_STOCK)
        assert merchant_detail.json()["deviceCount"] == 3

        # ---- ⑤' 每条资产迁移都留下事件（状态不是「只在内存里变过」）----
        first_device_id = body["devices"][0]["id"]
        events = await client.get(f"{PLATFORM}/devices/{first_device_id}/events", headers=platform)
        assert events.status_code == 200, events.text
        asset_events = [
            (item["eventType"], item["fromStatus"], item["toStatus"])
            for item in events.json()["records"]
            if item["dimension"] == "asset"
        ]
        assert ("GENERATED", str(AssetStatus.PENDING_GEN), str(AssetStatus.GENERATED)) in asset_events
        assert ("IN_STOCK", str(AssetStatus.GENERATED), str(AssetStatus.IN_STOCK)) in asset_events

        # 按状态筛选也能命中（状态不是「只在详情里出现」）
        by_status = await client.get(
            f"{PLATFORM}/orders",
            headers=platform,
            params={"status": str(OrderStatus.IN_STOCK), "keyword": order["orderNo"]},
        )
        assert by_status.json()["total"] == 1

        # ---- ⑥ 二维码导出：JD 格式且第 2/3 段确为租户 / 产品（附录 B 的错位缺陷）----
        codes = await client.get(f"{PLATFORM}/orders/{order['id']}/qrcodes", headers=platform)
        assert codes.status_code == 200, codes.text
        items = codes.json()["records"]
        assert codes.json()["total"] == 3
        for item in items:
            assert item["format"] == "JD"
            segments = item["payload"].split("|")
            assert len(segments) == 5
            assert segments[1] == tenant.id, "第 2 段必须是 tenantId（修正参考实现把产品 ID 传错的缺陷）"
            assert segments[2] == catalog["product_id"]
            assert segments[4] == qrcode_service.sign_jd(
                tenant.id, catalog["product_id"], item["sn"]
            )

    async def test_generate_on_pending_audit_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """未审核不能生成：``PENDING_AUDIT → GENERATING`` 不是合法边。"""
        tenant = await make_tenant(code="ORD-GATE", name="门禁测试租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13100000002")
        catalog = await _seed_catalog(db, tenant.id, suffix="GATE", network_type="WIFI")
        platform = await auth.platform_headers()

        order = await _create_order(client, merchant, catalog["product_id"], quantity=1)
        response = await _generate(client, platform, order["id"])

        assert response.status_code == 409, response.text
        body = response.json()
        assert body["code"] == "INVALID_STATE_TRANSITION"
        assert body["details"]["current"] == str(OrderStatus.PENDING_AUDIT)
        assert body["details"]["target"] == str(OrderStatus.GENERATING)
        # 拒绝后订单与设备都不得变化
        assert await _active_device_count(db, order["id"]) == 0


# ===========================================================================
# 二、状态机收口（终态 / 重复动作）
# ===========================================================================


class TestOrderStateMachineIsClosed:
    """终态不可再迁移，且每次拒绝都给出 ``current`` / ``target``。"""

    async def test_rejected_order_cannot_be_audited_or_generated_again(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        tenant = await make_tenant(code="ORD-REJ", name="驳回测试租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13100000003")
        catalog = await _seed_catalog(db, tenant.id, suffix="REJ", network_type="WIFI")
        platform = await auth.platform_headers()

        order = await _create_order(client, merchant, catalog["product_id"], quantity=1)
        rejected = await _audit(
            client, platform, order["id"], decision="REJECTED", rejectReason="产品型号不符"
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["status"] == str(OrderStatus.REJECTED)
        assert rejected.json()["rejectReason"] == "产品型号不符"

        # 再审核 → 409，details 指回当前状态
        again = await _audit(client, platform, order["id"], decision="APPROVED")
        assert again.status_code == 409, again.text
        assert again.json()["code"] == "INVALID_STATE_TRANSITION"
        assert again.json()["details"]["current"] == str(OrderStatus.REJECTED)
        assert again.json()["details"]["target"] == str(OrderStatus.APPROVED)

        # 驳回单生成设备 → 409（REJECTED 不属于「已生成」状态集合）
        generate = await _generate(client, platform, order["id"])
        assert generate.status_code == 409, generate.text
        assert generate.json()["code"] == "INVALID_STATE_TRANSITION"
        assert generate.json()["details"]["current"] == str(OrderStatus.REJECTED)
        assert await _active_device_count(db, order["id"]) == 0

    async def test_completed_order_cannot_be_audited_again(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """``COMPLETED`` 是终态：再审核必须被拒。

        用库级置位构造终态（终端激活完成属 P8，本阶段无端点可走到 COMPLETED）。
        """
        tenant = await make_tenant(code="ORD-DONE", name="完成态测试租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13100000004")
        catalog = await _seed_catalog(db, tenant.id, suffix="DONE", network_type="WIFI")
        platform = await auth.platform_headers()

        order = await _create_order(client, merchant, catalog["product_id"], quantity=1)
        db_order = (
            await db.execute(select(Order).where(Order.id == order["id"]))
        ).scalar_one()
        db_order.status = str(OrderStatus.COMPLETED)
        await db.flush()

        response = await _audit(client, platform, order["id"], decision="APPROVED")
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "INVALID_STATE_TRANSITION"
        assert response.json()["details"]["current"] == str(OrderStatus.COMPLETED)

    async def test_generate_is_idempotent_after_generation(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """已生成订单再次 ``generate`` 走幂等回放：返回既有结果，不重复造设备。

        P4 的实现把「生成后状态」统一定义为幂等回放（见 ``_POST_GENERATION_STATUSES``），
        因此这里断言的是**结果一致**而不是 409——「不重复造设备」才是这条断言的实质。
        """
        tenant = await make_tenant(code="ORD-IDEM", name="幂等生成租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13100000005")
        catalog = await _seed_catalog(db, tenant.id, suffix="IDEM", network_type="WIFI")
        platform = await auth.platform_headers()

        order = await _create_order(client, merchant, catalog["product_id"], quantity=2)
        assert (await _audit(client, platform, order["id"])).status_code == 200
        first = await _generate(client, platform, order["id"])
        assert first.status_code == 200, first.text
        assert first.json()["generated"] == 2

        second = await _generate(client, platform, order["id"])
        assert second.status_code == 200, second.text
        assert second.json()["generated"] == 2
        assert {item["id"] for item in second.json()["devices"]} == {
            item["id"] for item in first.json()["devices"]
        }
        assert await _active_device_count(db, order["id"]) == 2, "幂等回放绝不能新增设备"


# ===========================================================================
# 三、审核校验
# ===========================================================================


class TestAuditValidation:
    """驳回必须给原因；审核结论必须落在合法取值内。"""

    async def test_reject_without_reason_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """``decision=REJECTED`` 但缺 ``rejectReason`` → 400 ``VALIDATION_ERROR``。

        为什么不接受「驳回不带原因」：商户端拿到驳回单却不知道改什么，
        只能重复下单，审核就变成了纯阻塞。
        """
        tenant = await make_tenant(code="ORD-VALID", name="校验测试租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13100000006")
        catalog = await _seed_catalog(db, tenant.id, suffix="VALID", network_type="WIFI")
        platform = await auth.platform_headers()

        order = await _create_order(client, merchant, catalog["product_id"], quantity=1)

        # 变体 1：完全不传 rejectReason
        missing = await _audit(client, platform, order["id"], decision="REJECTED")
        assert missing.status_code == 400, missing.text
        assert missing.json()["code"] == "VALIDATION_ERROR"
        assert missing.json()["details"]["field"] == "rejectReason"

        # 变体 2：只传空白串（同样不算「给了原因」）
        blank = await _audit(
            client, platform, order["id"], decision="REJECTED", rejectReason="   "
        )
        assert blank.status_code == 400, blank.text
        assert blank.json()["code"] == "VALIDATION_ERROR"

        # 驳回失败后订单必须仍是 PENDING_AUDIT（不能被半途改成 REJECTED）
        detail = await client.get(f"{PLATFORM}/orders/{order['id']}", headers=platform)
        assert detail.json()["status"] == str(OrderStatus.PENDING_AUDIT)

        # 变体 3：非法审核结论 → 请求模型直接拦下（400 而非 500）
        illegal = await _audit(client, platform, order["id"], decision="MAYBE")
        assert illegal.status_code == 400, illegal.text
        assert illegal.json()["code"] == "VALIDATION_ERROR"

        # 补齐原因后可以正常驳回
        ok = await _audit(
            client, platform, order["id"], decision="REJECTED", rejectReason="参数需调整"
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["rejectReason"] == "参数需调整"


# ===========================================================================
# 四、下单幂等
# ===========================================================================


class TestOrderCreationValidation:
    """下单的归属与可用性校验（P-03 口径：产品必须属于本租户且已启用）。"""

    async def test_using_another_tenants_product_is_404_and_creates_nothing(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """商户用**他人**的产品 ID 下单 → 404（不返回 403，避免探测产品是否存在）。"""
        tenant_a = await make_tenant(code="ORD-XA", name="跨租户下单A")
        tenant_b = await make_tenant(code="ORD-XB", name="跨租户下单B")
        merchant_a = await _merchant_headers(auth, make_user, tenant_a.id, "13100000021")
        catalog_b = await _seed_catalog(db, tenant_b.id, suffix="XB", network_type="WIFI")
        platform = await auth.platform_headers()

        response = await client.post(
            f"{MERCHANT}/orders",
            headers=merchant_a,
            json={"clientProductId": catalog_b["product_id"], "quantity": 1},
        )

        assert response.status_code == 404, response.text
        assert response.json()["code"] == "RESOURCE_NOT_FOUND"
        listed = await client.get(f"{PLATFORM}/orders", headers=platform)
        assert listed.json()["total"] == 0, "越权下单不得落单"

    async def test_disabled_product_cannot_be_ordered(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """停用的客户产品不允许再下单（否则会造出「产品已下架但仍在生产」的订单）。"""
        tenant = await make_tenant(code="ORD-DIS", name="停用产品租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13100000022")
        catalog = await _seed_catalog(db, tenant.id, suffix="DIS", network_type="WIFI")

        product = (
            await db.execute(
                select(ClientProduct).where(ClientProduct.id == catalog["product_id"])
            )
        ).scalar_one()
        product.status = str(EnableStatus.DISABLED)
        await db.flush()

        response = await client.post(
            f"{MERCHANT}/orders",
            headers=merchant,
            json={"clientProductId": catalog["product_id"], "quantity": 1},
        )

        assert response.status_code == 409, response.text
        assert response.json()["code"] == "PRODUCT_NOT_AUTHORIZED"


class TestOrderCreationIdempotency:
    """``Idempotency-Key``：同键同体回放，同键异体拒绝。"""

    async def test_replay_returns_same_order_and_conflict_on_different_body(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        tenant = await make_tenant(code="ORD-IDEMP", name="下单幂等租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13100000007")
        catalog = await _seed_catalog(db, tenant.id, suffix="IDEMP", network_type="WIFI")
        platform = await auth.platform_headers()
        key = f"it-{uuid.uuid4().hex}"

        first = await _create_order(
            client, merchant, catalog["product_id"], quantity=4, idempotency_key=key
        )
        second = await _create_order(
            client, merchant, catalog["product_id"], quantity=4, idempotency_key=key
        )

        assert second["id"] == first["id"], "同键同体必须回放同一订单，绝不能新建"
        assert second["orderNo"] == first["orderNo"]
        assert second["createdAt"] == first["createdAt"]

        # 数据库侧确认只有一张单
        listed = await client.get(
            f"{PLATFORM}/orders", headers=platform, params={"clientProductId": catalog["product_id"]}
        )
        assert listed.json()["total"] == 1

        # 同键换请求体 → 409（否则会把首次响应错发给另一个请求）
        conflict = await client.post(
            f"{MERCHANT}/orders",
            headers={**merchant, "Idempotency-Key": key},
            json={"clientProductId": catalog["product_id"], "quantity": 9},
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
        assert listed.json()["total"] == 1, "冲突请求不得落单"

        # 不带幂等键的两次请求会各建一单（反向验证：幂等来自键而非全局去重）
        await _create_order(client, merchant, catalog["product_id"], quantity=1)
        await _create_order(client, merchant, catalog["product_id"], quantity=1)
        after = await client.get(
            f"{PLATFORM}/orders", headers=platform, params={"clientProductId": catalog["product_id"]}
        )
        assert after.json()["total"] == 3


# ===========================================================================
# 五、★ 租户隔离
# ===========================================================================


@pytest.mark.tenant_isolation
class TestOrderTenantIsolation:
    """跨租户访问一律「不存在」，列表只见自己的数据。"""

    async def test_cross_tenant_order_detail_returns_404_not_403(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        tenant_a = await make_tenant(code="ISO-A", name="隔离租户A")
        tenant_b = await make_tenant(code="ISO-B", name="隔离租户B")
        merchant_a = await _merchant_headers(auth, make_user, tenant_a.id, "13100000008")
        merchant_b = await _merchant_headers(auth, make_user, tenant_b.id, "13100000009")
        catalog_b = await _seed_catalog(db, tenant_b.id, suffix="ISOB", network_type="WIFI")

        order_b = await _create_order(client, merchant_b, catalog_b["product_id"], quantity=1)

        # A 读 B 的订单：必须是 404（403 会泄漏「这条资源存在」）
        response = await client.get(f"{MERCHANT}/orders/{order_b['id']}", headers=merchant_a)
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "RESOURCE_NOT_FOUND"

        # 不存在的 ID 与越权 ID 的错误码完全一致（信息量不可区分）
        missing = await client.get(f"{MERCHANT}/orders/order-does-not-exist", headers=merchant_a)
        assert missing.status_code == 404
        assert missing.json()["code"] == response.json()["code"]

        # B 读自己的订单正常
        own = await client.get(f"{MERCHANT}/orders/{order_b['id']}", headers=merchant_b)
        assert own.status_code == 200, own.text

    async def test_order_list_only_contains_own_tenant_rows(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        tenant_a = await make_tenant(code="ISO-C", name="隔离租户C")
        tenant_b = await make_tenant(code="ISO-D", name="隔离租户D")
        merchant_a = await _merchant_headers(auth, make_user, tenant_a.id, "13100000010")
        merchant_b = await _merchant_headers(auth, make_user, tenant_b.id, "13100000011")
        catalog_a = await _seed_catalog(db, tenant_a.id, suffix="ISOC", network_type="WIFI")
        catalog_b = await _seed_catalog(db, tenant_b.id, suffix="ISOD", network_type="WIFI")

        await _create_order(client, merchant_a, catalog_a["product_id"], quantity=1)
        await _create_order(client, merchant_b, catalog_b["product_id"], quantity=2)

        list_a = await client.get(f"{MERCHANT}/orders", headers=merchant_a)
        assert list_a.status_code == 200, list_a.text
        body_a = list_a.json()
        assert body_a["total"] == 1
        assert {row["tenantId"] for row in body_a["records"]} == {tenant_a.id}
        assert all(row["clientProductId"] == catalog_a["product_id"] for row in body_a["records"])

        list_b = await client.get(f"{MERCHANT}/orders", headers=merchant_b)
        assert list_b.json()["total"] == 1
        assert {row["tenantId"] for row in list_b.json()["records"]} == {tenant_b.id}

    async def test_product_list_only_contains_own_tenant_rows(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        tenant_a = await make_tenant(code="ISO-E", name="隔离租户E")
        tenant_b = await make_tenant(code="ISO-F", name="隔离租户F")
        merchant_a = await _merchant_headers(auth, make_user, tenant_a.id, "13100000012")
        await _seed_catalog(db, tenant_a.id, suffix="ISOE", network_type="WIFI")
        await _seed_catalog(db, tenant_b.id, suffix="ISOF", network_type="WIFI")

        products_a = await client.get(f"{MERCHANT}/products", headers=merchant_a)
        assert products_a.status_code == 200, products_a.text
        body = products_a.json()
        assert body["total"] == 1
        assert {row["code"] for row in body["records"]} == {"PROD-ISOE"}
        assert {row["tenantId"] for row in body["records"]} == {tenant_a.id}


# ===========================================================================
# 六、★ ADR-07：未配置厂商必须安全失败
# ===========================================================================


class TestVendorUnavailableOnFourGGenerate:
    """4G 订单生成设备依赖厂商接口；未配置密钥时**宁可 503 也不伪造设备**。"""

    async def test_four_g_generate_fails_safely_without_faking_devices(
        self,
        client: AsyncClient,
        auth: Any,
        db: AsyncSession,
        make_tenant: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.ai import registry

        # 让用例与「本机是否恰好配了集贤密钥」解耦：明确把凭证清空
        registry.ensure_default_registry()
        provider = registry.get("jixian")
        assert provider is not None
        monkeypatch.setattr(provider, "credentials", {}, raising=False)
        assert provider.is_configured is False

        tenant = await make_tenant(code="ORD-4G", name="4G 测试租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13100000014")
        catalog = await _seed_catalog(
            db, tenant.id, suffix="4G", network_type="4G", vendor="JIXIAN"
        )
        platform = await auth.platform_headers()

        order = await _create_order(client, merchant, catalog["product_id"], quantity=3)
        assert order["networkType"] == "4G"
        assert (await _audit(client, platform, order["id"])).status_code == 200

        response = await _generate(client, platform, order["id"])

        # ---- ① 明确的安全失败语义 ----
        assert response.status_code == 503, response.text
        body = response.json()
        assert body["code"] == "VENDOR_UNAVAILABLE"
        assert body["details"]["ok"] is False
        assert body["details"]["vendor"] == "JIXIAN"

        # ---- ② 订单没有被伪造成 GENERATED ----
        detail = await client.get(f"{PLATFORM}/orders/{order['id']}", headers=platform)
        assert detail.status_code == 200, detail.text
        detail_body = detail.json()
        assert detail_body["status"] == str(OrderStatus.APPROVED), "失败后订单应退回 APPROVED"
        assert detail_body["generatedCount"] == 0
        assert detail_body["deviceCount"] == 0
        assert detail_body["devices"] == []
        assert detail_body["generationDetail"]["ok"] is False
        assert detail_body["generationDetail"]["reason"] == "VENDOR_UNAVAILABLE"

        # ---- ③ 设备表零新增（不伪造一批假设备）----
        assert await _active_device_count(db, order["id"]) == 0
        assert int(
            (await db.execute(select(func.count()).select_from(Device))).scalar_one()
        ) == 0

        # ---- ④ 失败必须留审计证据（含订单号，可回溯）----
        # 口径说明：厂商不可用属于 **VENDOR_CALL_FAILED**（与 P7 适配器同一口径，
        # ADR-07 的安全拒绝可被同一条检索命中）；非厂商原因才记
        # GENERATE_DEVICES(success=False)。两者都带 resource_id 便于按订单反查。
        failures = list(
            (
                await db.execute(
                    select(AuditLog).where(AuditLog.action == str(AuditAction.VENDOR_CALL_FAILED))
                )
            ).scalars()
        )
        assert any(
            log.success is False and (log.detail or {}).get("orderNo") == order["orderNo"]
            for log in failures
        ), "厂商不可用导致的生成失败必须写成功=false 的审计，否则无法自证拒绝过"
        assert any(
            log.resource_type == "order" and log.resource_id == order["id"] for log in failures
        ), "失败审计必须带资源标识，否则运维无法按订单反查"

    async def test_generate_failure_is_audited_as_vendor_call_failed(
        self,
        client: AsyncClient,
        auth: Any,
        db: AsyncSession,
        make_tenant: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """★ 与 P7 一致的口径：厂商安全拒绝应记为 ``VENDOR_CALL_FAILED``。

        依据：``AuditAction.VENDOR_CALL_FAILED`` 的语义是「因厂商原因被安全拒绝」，
        P7 的 AI 端点已按此口径落审计（见 ``tests/integration/test_vendor_unavailable.py``），
        ADR-07 要求同类拒绝在审计上可被同一条件检索。
        """
        from app.ai import registry

        registry.ensure_default_registry()
        provider = registry.get("jixian")
        assert provider is not None
        monkeypatch.setattr(provider, "credentials", {}, raising=False)

        tenant = await make_tenant(code="ORD-4GAUD", name="4G 审计租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13100000015")
        catalog = await _seed_catalog(
            db, tenant.id, suffix="4GAUD", network_type="4G", vendor="JIXIAN"
        )
        platform = await auth.platform_headers()

        order = await _create_order(client, merchant, catalog["product_id"], quantity=1)
        assert (await _audit(client, platform, order["id"])).status_code == 200
        assert (await _generate(client, platform, order["id"])).status_code == 503

        count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(AuditLog.action == str(AuditAction.VENDOR_CALL_FAILED))
                )
            ).scalar_one()
        )
        assert count == 1, (
            "厂商未配置导致的生成失败应记为 VENDOR_CALL_FAILED（与 P7 口径一致）；"
            f"实际 VENDOR_CALL_FAILED 审计条数={count}"
        )


# ===========================================================================
# 七、平台运营角色边界
# ===========================================================================


class TestPlatformOperatorBoundary:
    """运营可审核订单，但不能改租户 / 云服务商 / 产品模板。"""

    async def test_operator_can_audit_order_but_cannot_write_catalog(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        tenant = await make_tenant(code="OP-ORD", name="运营边界租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13100000016")
        catalog = await _seed_catalog(db, tenant.id, suffix="OPORD", network_type="WIFI")
        operator = await auth.platform_operator_headers()

        order = await _create_order(client, merchant, catalog["product_id"], quantity=1)

        # 运营有 platform:order:write → 审核放行
        audited = await _audit(client, operator, order["id"], decision="APPROVED")
        assert audited.status_code == 200, audited.text
        assert audited.json()["status"] == str(OrderStatus.APPROVED)

        # 运营可读订单 / 设备
        assert (await client.get(f"{PLATFORM}/orders", headers=operator)).status_code == 200
        assert (await client.get(f"{PLATFORM}/devices", headers=operator)).status_code == 200

        # 写目录域三件套 → 403（运营没有这些写权限）
        cases: list[tuple[str, dict[str, Any]]] = [
            ("/tenants", {"code": "OP-HACK-T", "name": "运营不该能建"}),
            ("/clouds", {"code": "OP-HACK-C", "name": "运营不该能建", "vendor": "JIXIAN"}),
            ("/templates", {"code": "OP-HACK-TPL", "name": "运营不该能建"}),
        ]
        for path, payload in cases:
            response = await client.post(f"{PLATFORM}{path}", headers=operator, json=payload)
            assert response.status_code == 403, f"POST {path}：{response.text}"
            assert response.json()["code"] == "PERMISSION_DENIED"
            assert response.json()["details"]["requiredPermission"].endswith(":write")


# ===========================================================================
# 八、设备生命周期（库级用例）
# ===========================================================================


class TestDeviceLifecycle:
    """冻结 / 解冻 / 报废的状态机与事件留痕。"""

    async def test_freeze_then_thaw_in_stock_device(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        platform = await auth.platform_headers()
        device = await _make_device(db, sn="SN-IT-FREEZE-01")

        frozen = await client.post(
            f"{PLATFORM}/devices/{device.id}/freeze",
            headers=platform,
            json={"reason": "疑似质量批次"},
        )
        assert frozen.status_code == 200, frozen.text
        body = frozen.json()
        assert body["assetStatus"] == str(AssetStatus.FROZEN)
        assert body["previousAssetStatus"] == str(AssetStatus.IN_STOCK)
        assert body["freezeReason"] == "疑似质量批次"
        assert body["frozenAt"] is not None
        assert body["label"] == "已冻结"

        events = await client.get(f"{PLATFORM}/devices/{device.id}/events", headers=platform)
        assert events.status_code == 200, events.text
        frozen_events = [
            item
            for item in events.json()["records"]
            if item["eventType"] == "FROZEN"
        ]
        assert len(frozen_events) == 1
        assert frozen_events[0]["dimension"] == "asset"
        assert frozen_events[0]["fromStatus"] == str(AssetStatus.IN_STOCK)
        assert frozen_events[0]["toStatus"] == str(AssetStatus.FROZEN)
        assert frozen_events[0]["detail"]["reason"] == "疑似质量批次"

        thawed = await client.post(
            f"{PLATFORM}/devices/{device.id}/thaw", headers=platform, json={"reason": "误报"}
        )
        assert thawed.status_code == 200, thawed.text
        thawed_body = thawed.json()
        assert thawed_body["assetStatus"] == str(AssetStatus.IN_STOCK)
        assert thawed_body["previousAssetStatus"] is None
        assert thawed_body["frozenAt"] is None

        events_after = await client.get(f"{PLATFORM}/devices/{device.id}/events", headers=platform)
        thaw_events = [
            item for item in events_after.json()["records"] if item["eventType"] == "THAWED"
        ]
        assert len(thaw_events) == 1
        assert thaw_events[0]["fromStatus"] == str(AssetStatus.FROZEN)
        assert thaw_events[0]["toStatus"] == str(AssetStatus.IN_STOCK)
        # 解冻后设备真的回到 IN_STOCK（事件与实体状态一致）
        stored_status = (
            await db.execute(select(Device.asset_status).where(Device.id == device.id))
        ).scalar_one()
        assert stored_status == str(AssetStatus.IN_STOCK)

    async def test_freeze_generated_device_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """★ ``GENERATED`` 设备不允许冻结。

        理由（见 ``FREEZABLE_ASSET_STATUSES``）：未入库的设备本来就不能被
        分配，冻结它对流转没有增量保护；而 ``ALLOCATABLE_ASSET_STATUSES``
        与它无关，冻结能提供的保护是零增量。

        P6 起可冻结集合放宽为 ``{IN_STOCK, ALLOCATED, BOUND}``，
        但 ``GENERATED`` **仍然不在其中**——本用例把这半边规则钉住。
        """
        platform = await auth.platform_headers()
        device = await _make_device(db, sn="SN-IT-GEN-01", asset_status=str(AssetStatus.GENERATED))

        response = await client.post(
            f"{PLATFORM}/devices/{device.id}/freeze",
            headers=platform,
            json={"reason": "不应被允许"},
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "INVALID_STATE_TRANSITION"
        assert response.json()["details"]["current"] == str(AssetStatus.GENERATED)
        assert response.json()["details"]["target"] == str(AssetStatus.FROZEN)

        # 实体未被改动，且没有留下 FROZEN 事件（「拒绝」必须是真的没发生）
        stored = (await db.execute(select(Device).where(Device.id == device.id))).scalar_one()
        assert stored.asset_status == str(AssetStatus.GENERATED)
        assert stored.previous_asset_status is None
        events = await client.get(f"{PLATFORM}/devices/{device.id}/events", headers=platform)
        assert all(item["eventType"] != "FROZEN" for item in events.json()["records"])

    @pytest.mark.parametrize(
        "asset_status",
        [
            str(AssetStatus.PENDING_GEN),
            str(AssetStatus.GENERATED),
            str(AssetStatus.PRODUCING),
            str(AssetStatus.PRODUCED),
            str(AssetStatus.SHIPPED),
            str(AssetStatus.RETIRED),
            str(AssetStatus.FROZEN),
        ],
    )
    async def test_non_freezable_statuses_are_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, asset_status: str
    ) -> None:
        """把「哪些状态不可冻结」钉成参数化矩阵。

        P6 起 ``ALLOCATED`` / ``BOUND`` 已**移出**本矩阵——它们被用户明确
        决策纳入了可冻结范围（欠费停机、内容违规停服）。这里保留的是
        仍然不可冻结的取值：未入库的（不入库本身已阻断流转）与终态。
        """
        platform = await auth.platform_headers()
        device = await _make_device(db, sn=f"SN-IT-FRZ-{asset_status}", asset_status=asset_status)

        response = await client.post(
            f"{PLATFORM}/devices/{device.id}/freeze", headers=platform, json={"reason": "不应被允许"}
        )
        assert response.status_code == 409, f"{asset_status}：{response.text}"
        assert response.json()["code"] == "INVALID_STATE_TRANSITION"

    @pytest.mark.parametrize(
        "asset_status",
        [str(AssetStatus.IN_STOCK), str(AssetStatus.ALLOCATED), str(AssetStatus.BOUND)],
    )
    async def test_freezable_statuses_are_accepted(
        self, client: AsyncClient, auth: Any, db: AsyncSession, asset_status: str
    ) -> None:
        """可冻结的三类状态都能真的冻结，且原状态被记入 ``previousAssetStatus``。

        这条用例补上了 P5 的能力缺口：在 P4 的收紧规则下，
        「冻结一台已分配给商户的设备」是不可达的，因此当时只能靠
        单元断言描述规则，无法端到端验证。
        """
        platform = await auth.platform_headers()
        device = await _make_device(db, sn=f"SN-IT-FRZOK-{asset_status}", asset_status=asset_status)

        response = await client.post(
            f"{PLATFORM}/devices/{device.id}/freeze",
            headers=platform,
            json={"reason": "欠费停机"},
        )
        assert response.status_code == 200, f"{asset_status}：{response.text}"
        body = response.json()
        assert body["assetStatus"] == str(AssetStatus.FROZEN)
        assert body["previousAssetStatus"] == asset_status

        # 解冻必须原路恢复（这是「可冻结集合 == FROZEN 出边集合」的直接收益）
        thawed = await client.post(
            f"{PLATFORM}/devices/{device.id}/thaw", headers=platform, json={"reason": "缴费恢复"}
        )
        assert thawed.status_code == 200, thawed.text
        assert thawed.json()["assetStatus"] == asset_status

    def test_freezable_status_set_is_pinned(self) -> None:
        """契约钉死：冻结白名单是业务决策，不是实现细节。"""
        assert set(FREEZABLE_ASSET_STATUSES) == {
            AssetStatus.IN_STOCK,
            AssetStatus.ALLOCATED,
            AssetStatus.BOUND,
        }

    async def test_retire_requires_reason_and_is_terminal(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        platform = await auth.platform_headers()
        device = await _make_device(db, sn="SN-IT-RETIRE-01")

        # 缺 reason → 请求模型拦下（400，而不是 500）
        missing = await client.post(f"{PLATFORM}/devices/{device.id}/retire", headers=platform, json={})
        assert missing.status_code == 400, missing.text
        assert missing.json()["code"] == "VALIDATION_ERROR"

        retired = await client.post(
            f"{PLATFORM}/devices/{device.id}/retire",
            headers=platform,
            json={"reason": "主板烧毁"},
        )
        assert retired.status_code == 200, retired.text
        assert retired.json()["assetStatus"] == str(AssetStatus.RETIRED)
        assert retired.json()["retireReason"] == "主板烧毁"
        assert retired.json()["label"] == "已报废"

        # 报废是终态：再冻结 / 再报废都必须被拒
        after_freeze = await client.post(
            f"{PLATFORM}/devices/{device.id}/freeze", headers=platform, json={"reason": "不应被允许"}
        )
        assert after_freeze.status_code == 409, after_freeze.text
        assert after_freeze.json()["details"]["current"] == str(AssetStatus.RETIRED)

        after_retire = await client.post(
            f"{PLATFORM}/devices/{device.id}/retire", headers=platform, json={"reason": "重复报废"}
        )
        assert after_retire.status_code == 409, after_retire.text

        events = await client.get(f"{PLATFORM}/devices/{device.id}/events", headers=platform)
        retire_events = [
            item for item in events.json()["records"] if item["eventType"] == "RETIRED"
        ]
        assert len(retire_events) == 1
        assert retire_events[0]["fromStatus"] == str(AssetStatus.IN_STOCK)
        assert retire_events[0]["toStatus"] == str(AssetStatus.RETIRED)
        assert retire_events[0]["detail"]["reason"] == "主板烧毁"

    async def test_freeze_requires_platform_device_write(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """商户端没有 ``platform:device:write``：即使设备归属自己的租户也不行。"""
        tenant = await make_tenant(code="ORD-DEVW", name="设备写权限租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13100000017")
        device = await _make_device(db, sn="SN-IT-PERM-01", tenant_id=tenant.id)

        response = await client.post(
            f"{PLATFORM}/devices/{device.id}/freeze", headers=merchant, json={"reason": "越权尝试"}
        )
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "PERMISSION_DENIED"


# ===========================================================================
# 九、筛选与详情装配
# ===========================================================================


class TestDeviceQueryAndDetailAssembly:
    """前端订单详情依赖的筛选项与字段装配必须齐备。"""

    async def test_devices_can_be_filtered_by_order_and_list_omits_names(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        tenant = await make_tenant(code="ORD-FILT", name="筛选测试租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13100000018")
        catalog = await _seed_catalog(db, tenant.id, suffix="FILT", network_type="WIFI")
        platform = await auth.platform_headers()

        order = await _create_order(client, merchant, catalog["product_id"], quantity=2)
        assert (await _audit(client, platform, order["id"])).status_code == 200
        assert (await _generate(client, platform, order["id"])).status_code == 200

        # 该订单下的设备（订单详情页的设备 Tab 就靠这个筛选）
        filtered = await client.get(
            f"{PLATFORM}/devices", headers=platform, params={"orderId": order["id"]}
        )
        assert filtered.status_code == 200, filtered.text
        body = filtered.json()
        assert body["total"] == 2
        assert {row["orderId"] for row in body["records"]} == {order["id"]}
        assert {row["assetStatus"] for row in body["records"]} == {str(AssetStatus.IN_STOCK)}
        assert all(row["label"] == "已入库待生产" for row in body["records"])

        # 反查：别的订单 ID 不应命中这些设备
        other = await client.get(
            f"{PLATFORM}/devices", headers=platform, params={"orderId": "order-does-not-exist"}
        )
        assert other.json()["total"] == 0

        # 设备列表**刻意不含**名称类字段（两套来源会显示改名前的旧值）
        for row in body["records"]:
            for field in _DEVICE_LIST_FORBIDDEN_FIELDS:
                assert field not in row, f"设备列表项不应包含 {field}（见 DeviceResponse 的有意设计）"

        # 详情才带名称（单条记录需要自解释）
        detail = await client.get(
            f"{PLATFORM}/devices/{body['records'][0]['id']}", headers=platform
        )
        assert detail.status_code == 200, detail.text
        detail_body = detail.json()
        assert detail_body["tenantName"] == tenant.name
        assert detail_body["orderNo"] == order["orderNo"]
        assert detail_body["clientProductName"]
        assert detail_body["eventTotal"] >= 2  # GENERATED + IN_STOCK

    async def test_order_detail_and_list_agree_on_assembled_fields(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """上一阶段的缺陷形态是「列表有、详情没有」；这里逐字段对齐两者。"""
        tenant = await make_tenant(code="ORD-ASM", name="装配测试租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13100000019")
        catalog = await _seed_catalog(db, tenant.id, suffix="ASM", network_type="WIFI")
        platform = await auth.platform_headers()

        order = await _create_order(client, merchant, catalog["product_id"], quantity=2)
        assert (await _audit(client, platform, order["id"])).status_code == 200
        assert (await _generate(client, platform, order["id"])).status_code == 200

        detail = (
            await client.get(f"{PLATFORM}/orders/{order['id']}", headers=platform)
        ).json()
        listed = await client.get(
            f"{PLATFORM}/orders", headers=platform, params={"keyword": order["orderNo"]}
        )
        assert listed.json()["total"] == 1
        item = listed.json()["records"][0]

        for field in (
            "id",
            "status",
            "statusLabel",
            "tenantId",
            "tenantName",
            "tenantCode",
            "clientProductId",
            "clientProductName",
            "deviceCount",
            "generatedCount",
            "networkType",
            "quantity",
        ):
            assert detail[field] == item[field], f"{field} 在列表与详情中不一致"

        assert detail["tenantName"] == tenant.name
        assert detail["deviceCount"] == 2
        assert [device["assetStatus"] for device in detail["devices"]] == [
            str(AssetStatus.IN_STOCK),
            str(AssetStatus.IN_STOCK),
        ]

    async def test_merchant_cannot_read_platform_device_endpoints(
        self, client: AsyncClient, auth: Any, make_tenant: Any, make_user: Any
    ) -> None:
        """平台设备域是平台独占（商户端设备的读接口属 P5）。"""
        tenant = await make_tenant(code="ORD-DEVEX", name="设备独占租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13100000020")

        for path in ("/devices", "/devices/stats", "/batches"):
            response = await client.get(f"{PLATFORM}{path}", headers=merchant)
            assert response.status_code == 403, f"GET {path}：{response.text}"
            assert response.json()["code"] == "PERMISSION_DENIED"
