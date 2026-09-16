"""集成测试：P5 分配单（``create`` → ``execute`` 逐行落库）。

本文件是独立验证者的产物：用测试去**证伪** ``allocation_service`` 的实现，
而不是给它背书。每条断言都对应一条可被破坏的业务规则。

覆盖的验收标准
--------------
* ① **创建**：单号 ``AL-YYYYMMDD-XXXX``（字符集剔除 ``0/O/1/I``）、``DRAFT`` 初值、
  ``totalCount`` 正确、``Idempotency-Key`` 重放返回同一条、目标租户不存在
  404、租户已停用 403 ``TENANT_DISABLED``、产品不属于该租户 404、
  空 ``deviceIds`` 400。
* ② **执行正常路径**：``allocated_count`` 精确、设备 ``tenantId`` /
  ``clientProductId`` / ``assetStatus=ALLOCATED`` 全部落对，且写入
  ``device_events`` 的 ``ALLOCATED`` 事件（P6 放宽后 ``IN_STOCK`` 与
  ``SHIPPED`` 都可分配，两种来源各测一遍）。
* ③ ★ **幂等重放**：``COMPLETED`` 再执行 → 不迁移、不新增事件、返回既有计数。
* ④ ★ **部分失败不回滚**：成功的那台**保留**在 ``ALLOCATED``；失败行有
  ``errorMessage``；整单 ``FAILED``；修复后重跑只补失败那台，成功那台不重复分配。
* ⑤ **逐行失败原因**：设备不存在 ``RESOURCE_NOT_FOUND`` / 属于别的租户
  ``DEVICE_NOT_IN_TENANT`` / 已在本租户且已 ``ALLOCATED`` 跳过 / ``FROZEN``
  ``DEVICE_FROZEN`` / 其余不可分配状态 ``DEVICE_NOT_AVAILABLE``。
* ⑥ **本次新增 vs 整单现状**：``allocatedCount``（增量）与分配单上的累计
  ``allocatedCount`` 语义不同且都正确。
* ⑦ **整单失败**：产品授权被撤销后执行 → 待执行行全失败、一台都不分出去、
  已完成的行不受影响。
* ⑧ **权限**：商户访问平台端分配端点 → 403 ``PERMISSION_DENIED``。

写法对齐 ``tests/integration/test_binding.py``：模块内自定义 helper、
camelCase 断言、库级造数（不靠 HTTP 造数），中文注释说明「这里坏了
说明哪条业务规则被破坏」。不 import 其它测试模块的私有函数。
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext
from app.core.ids import new_id
from app.db.base import utcnow
from app.models.allocation import AllocationItem, AllocationOrder
from app.models.catalog import ClientProduct, ProductAuthorization
from app.models.device import Device, DeviceEvent
from app.models.enums import (
    ALLOCATABLE_ASSET_STATUSES,
    ActivationStatus,
    AllocationItemStatus,
    AllocationStatus,
    AssetStatus,
    BindStatus,
    EnableStatus,
    OnlineStatus,
    RoleType,
    TenantStatus,
)
from app.services import catalog_service
from tests.conftest import API_PREFIX

pytestmark = pytest.mark.integration

PLATFORM = f"{API_PREFIX}/platform"

#: 商户测试账号口令（仅存在于测试进程）
MERCHANT_PASSWORD = "Merch4nt-Alloc#2026"

#: 单号格式：AL-YYYYMMDD-XXXX，后缀字符集剔除形近字符 0/O/1/I
ALLOCATION_NO_PATTERN = re.compile(
    r"^AL-\d{8}-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{4}$"
)

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
}


# ---------------------------------------------------------------------------
# 模块级辅助（与 test_binding.py 同风格的独立实现，不跨模块私有导入）
# ---------------------------------------------------------------------------


def _platform_ctx() -> AuthContext:
    """平台超管上下文（通配权限），供服务层建链使用。"""
    return AuthContext(
        user_id="u-allocation-it",
        account="allocation-it",
        role_code="PLATFORM_ADMIN",
        role_type=RoleType.PLATFORM,
        tenant_id=None,
        permissions=["*"],
    )


async def _seed_catalog(
    db: AsyncSession, tenant_id: str, *, suffix: str, network_type: str = "WIFI"
) -> dict[str, str]:
    """建「云服务商 → 模板 → 授权 → 客户产品」最小链条（走真实服务层）。"""
    platform = _platform_ctx()
    cloud = await catalog_service.create_cloud(
        db,
        payload={
            "code": f"CLOUD-{suffix}",
            "name": f"{suffix} 测试云",
            "vendor": "JOYINSIDE" if network_type == "WIFI" else "JIXIAN",
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


async def _make_device(
    db: AsyncSession,
    *,
    sn: str,
    tenant_id: str | None = None,
    product_id: str | None = None,
    asset_status: str = str(AssetStatus.IN_STOCK),
    network_type: str = "WIFI",
) -> Device:
    """直接落一台设备（库级造数，避免造数过程本身成为变量）。

    ``tenant_id`` 为空即「平台自有库存」——分配链路的合法输入来源。
    """
    device = Device(
        id=new_id("device"),
        sn=sn,
        tenant_id=tenant_id,
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


async def _create_allocation(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    tenant_id: str,
    product_id: str,
    device_ids: list[str],
    key: str | None = None,
    remark: str | None = None,
) -> Any:
    """调创建分配单（不预设状态码）。"""
    request_headers = dict(headers)
    if key:
        request_headers["Idempotency-Key"] = key
    body: dict[str, Any] = {
        "tenantId": tenant_id,
        "clientProductId": product_id,
        "deviceIds": device_ids,
    }
    if remark is not None:
        body["remark"] = remark
    return await client.post(f"{PLATFORM}/allocations", headers=request_headers, json=body)


async def _create_ok(
    client: AsyncClient, headers: dict[str, str], **kwargs: Any
) -> dict[str, Any]:
    response = await _create_allocation(client, headers, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()


async def _execute(
    client: AsyncClient, headers: dict[str, str], allocation_id: str
) -> Any:
    """调执行分配单（不预设状态码）。"""
    return await client.post(
        f"{PLATFORM}/allocations/{allocation_id}/execute", headers=headers
    )


async def _execute_ok(
    client: AsyncClient, headers: dict[str, str], allocation_id: str
) -> dict[str, Any]:
    response = await _execute(client, headers, allocation_id)
    assert response.status_code == 200, response.text
    return response.json()


async def _detail(
    client: AsyncClient, headers: dict[str, str], allocation_id: str
) -> dict[str, Any]:
    response = await client.get(f"{PLATFORM}/allocations/{allocation_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _reload_device(db: AsyncSession, device_id: str) -> Device:
    """重新读一遍设备（execute 内部会 commit，ORM 上的状态需重新取）。"""
    return (await db.execute(select(Device).where(Device.id == device_id))).scalar_one()


async def _items_of(db: AsyncSession, allocation_id: str) -> list[AllocationItem]:
    return list(
        (
            await db.execute(
                select(AllocationItem)
                .where(AllocationItem.allocation_order_id == allocation_id)
                .order_by(AllocationItem.created_at.asc())
            )
        ).scalars()
    )


async def _events(db: AsyncSession, device_id: str, event_type: str) -> list[DeviceEvent]:
    return list(
        (
            await db.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == device_id,
                    DeviceEvent.event_type == event_type,
                )
            )
        ).scalars()
    )


def _failure_by_device(result: dict[str, Any], device_id: str) -> dict[str, Any]:
    """从执行结果的 ``failures`` 里取出指定设备的失败记录。"""
    matched = [item for item in result["failures"] if item["deviceId"] == device_id]
    assert len(matched) == 1, f"应恰好有一条失败记录：{result['failures']}"
    return matched[0]


def _assert_error(response: Any, status: int, code: str) -> None:
    """断言失败响应的状态码与错误码，并顺带钉死错误码契约。"""
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code, response.text
    assert body["code"] in _ERROR_CODES, f"返回了未登记的错误码：{body['code']}"
    assert isinstance(body["message"], str) and body["message"]
    assert "traceId" in body


# ===========================================================================
# 一、创建分配单
# ===========================================================================


class TestCreateAllocation:
    """创建阶段：单号、初值、幂等键与三类硬校验。"""

    async def test_create_returns_draft_with_formatted_no_and_total_count(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """成功创建：单号格式、``DRAFT`` 初值、``totalCount`` 与明细行数一致。

        这里坏了说明：分配单的「人类可读单号」契约被破坏（运维要照单号
        在纸上核对，形近字符混入会造成错单），或创建时漏记台数。
        """
        tenant = await make_tenant(code="AL-CREATE", name="分配创建租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="ALCREATE")
        device_a = await _make_device(db, sn="SN-AL-CREATE-01")
        device_b = await _make_device(db, sn="SN-AL-CREATE-02")
        headers = await auth.platform_headers()

        body = await _create_ok(
            client,
            headers,
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            device_ids=[device_a.id, device_b.id],
            remark="首批试点",
        )

        assert ALLOCATION_NO_PATTERN.match(body["allocationNo"]), (
            f"单号格式必须为 AL-YYYYMMDD-XXXX 且剔除 0/O/1/I：{body['allocationNo']}"
        )
        assert body["status"] == str(AllocationStatus.DRAFT)
        assert body["statusLabel"] == "草稿"
        assert body["totalCount"] == 2
        assert body["allocatedCount"] == 0, "刚创建的单子不可能已有分配"
        assert body["failedCount"] == 0
        assert body["tenantId"] == tenant.id
        assert body["clientProductId"] == catalog["product_id"]
        assert body["createdBy"]
        assert body["executedAt"] is None
        assert body["remark"] == "首批试点"

        # 库内落一行单 + 两行 PENDING 明细（创建不推进任何设备状态）
        items = await _items_of(db, body["id"])
        assert len(items) == 2
        assert {item.status for item in items} == {str(AllocationItemStatus.PENDING)}
        assert {item.device_id for item in items} == {device_a.id, device_b.id}
        assert (await _reload_device(db, device_a.id)).asset_status == str(AssetStatus.IN_STOCK)

    async def test_create_is_idempotent_by_key(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """同 ``Idempotency-Key`` + 同请求体 → 返回同一条分配单，不新建第二张。

        这里坏了说明：网络重试会造出两张分配单，第二张执行时才发现设备
        已被分走（表现为一堆莫名其妙的 ``DEVICE_NOT_IN_TENANT``）。
        """
        tenant = await make_tenant(code="AL-IDEM", name="分配幂等租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="ALIDEM")
        device = await _make_device(db, sn="SN-AL-IDEM-01")
        headers = await auth.platform_headers()
        key = f"it-{uuid.uuid4().hex}"

        first = await _create_ok(
            client,
            headers,
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            device_ids=[device.id],
            key=key,
        )
        second = await _create_ok(
            client,
            headers,
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            device_ids=[device.id],
            key=key,
        )

        assert second["id"] == first["id"]
        assert second["allocationNo"] == first["allocationNo"]
        assert second["totalCount"] == first["totalCount"]
        assert int(
            (
                await db.execute(
                    select(func.count()).select_from(AllocationOrder)
                )
            ).scalar_one()
        ) == 1, "同键重放不得新建第二条分配单"

    async def test_create_same_key_different_body_is_conflict(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """同键**异体** → 409 ``IDEMPOTENCY_CONFLICT``（不能把首次响应错发给另一个请求）。"""
        tenant = await make_tenant(code="AL-CONF", name="分配幂等冲突租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="ALCONF")
        device = await _make_device(db, sn="SN-AL-CONF-01")
        headers = await auth.platform_headers()
        key = f"it-{uuid.uuid4().hex}"

        await _create_ok(
            client,
            headers,
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            device_ids=[device.id],
            key=key,
            remark="第一次",
        )
        conflict = await _create_allocation(
            client,
            headers,
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            device_ids=[device.id],
            key=key,
            remark="换了请求体",
        )
        _assert_error(conflict, 409, "IDEMPOTENCY_CONFLICT")

    async def test_create_for_unknown_tenant_is_not_found(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """目标租户不存在 → 404（不给「这个租户 ID 存在吗」的回答）。"""
        tenant = await make_tenant(code="AL-NF", name="分配租户不存在")
        catalog = await _seed_catalog(db, tenant.id, suffix="ALNF")
        device = await _make_device(db, sn="SN-AL-NF-01")
        headers = await auth.platform_headers()

        response = await _create_allocation(
            client,
            headers,
            tenant_id="tenant-does-not-exist",
            product_id=catalog["product_id"],
            device_ids=[device.id],
        )
        _assert_error(response, 404, "RESOURCE_NOT_FOUND")

    async def test_create_for_disabled_tenant_is_tenant_disabled(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """租户已停用 → 403 ``TENANT_DISABLED``。

        这里坏了说明：设备被划给了一个登不进来的租户，立即变成
        谁也访问不到的孤儿资产（商户看不到，平台也难察觉）。
        """
        tenant = await make_tenant(code="AL-DIS", name="停用租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="ALDIS")
        device = await _make_device(db, sn="SN-AL-DIS-01")
        headers = await auth.platform_headers()

        # 先建链再把租户停用：客户产品的创建本身就要求租户 ACTIVE
        # （见 test_catalog.py::test_create_product_for_disabled_tenant_is_rejected），
        # 因此「停用后再分配」这个场景只能这样构造。
        tenant.status = str(TenantStatus.DISABLED)
        await db.flush()

        response = await _create_allocation(
            client,
            headers,
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            device_ids=[device.id],
        )
        _assert_error(response, 403, "TENANT_DISABLED")

    async def test_create_with_other_tenants_product_is_not_found(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """产品不属于目标租户 → 404（防止把 A 品牌的设备挂到 B 品牌的产品上）。"""
        tenant_a = await make_tenant(code="AL-PA", name="产品归属租户A")
        tenant_b = await make_tenant(code="AL-PB", name="产品归属租户B")
        catalog_b = await _seed_catalog(db, tenant_b.id, suffix="ALPB")
        device = await _make_device(db, sn="SN-AL-PB-01")
        headers = await auth.platform_headers()

        response = await _create_allocation(
            client,
            headers,
            tenant_id=tenant_a.id,  # 目标租户是 A
            product_id=catalog_b["product_id"],  # 但产品属于 B
            device_ids=[device.id],
        )
        _assert_error(response, 404, "RESOURCE_NOT_FOUND")

    @pytest.mark.parametrize("device_ids", [[], ["   "], ["", "\t"]])
    async def test_create_with_blank_device_ids_is_validation_error(
        self,
        client: AsyncClient,
        auth: Any,
        db: AsyncSession,
        make_tenant: Any,
        device_ids: list[str],
    ) -> None:
        """空 / 纯空白的 ``deviceIds`` → 400 ``VALIDATION_ERROR``（一张空单没有意义）。"""
        tenant = await make_tenant(code="AL-BLANK", name="空设备列表租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="ALBLANK")
        headers = await auth.platform_headers()

        response = await _create_allocation(
            client,
            headers,
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            device_ids=device_ids,
        )
        _assert_error(response, 400, "VALIDATION_ERROR")

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", f"{PLATFORM}/allocations"),
            ("GET", f"{PLATFORM}/allocations"),
        ],
    )
    async def test_merchant_cannot_touch_platform_allocations(
        self,
        client: AsyncClient,
        auth: Any,
        db: AsyncSession,
        make_tenant: Any,
        make_user: Any,
        method: str,
        path: str,
    ) -> None:
        """★ 商户 token 访问平台端分配端点 → 403 ``PERMISSION_DENIED``。

        这里坏了说明：分配（把平台库存划给谁）这个平台独占动作被商户
        自己触发了——商户可以给自己「分设备」，等于绕过平台授权。
        """
        tenant = await make_tenant(code="AL-DENY", name="越权分配租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="ALDENY")
        device = await _make_device(db, sn="SN-AL-DENY-01")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13210000001")

        body: dict[str, Any] | None = None
        if method == "POST":
            body = {
                "tenantId": tenant.id,
                "clientProductId": catalog["product_id"],
                "deviceIds": [device.id],
            }
        response = await client.request(method, path, headers=merchant, json=body)
        _assert_error(response, 403, "PERMISSION_DENIED")


# ===========================================================================
# 二、执行：正常路径与幂等重放
# ===========================================================================


class TestExecuteHappyPath:
    """执行把设备从平台库存划到租户名下，并逐台留痕。"""

    @pytest.mark.parametrize(
        "asset_status",
        [str(AssetStatus.IN_STOCK), str(AssetStatus.SHIPPED)],
    )
    async def test_execute_allocates_devices_and_writes_events(
        self,
        client: AsyncClient,
        auth: Any,
        db: AsyncSession,
        make_tenant: Any,
        asset_status: str,
    ) -> None:
        """★ ``IN_STOCK`` / ``SHIPPED`` 都可分配：归属与状态全部落对 + 写事件。

        这里坏了说明三条规则之一被破坏：
        ① 分配没写 ``tenant_id``（设备成孤儿）或没挂 ``client_product_id``；
        ② 设备没被推到 ``ALLOCATED``（商户端仍看不到「已分配待激活」）；
        ③ 少了 ``ALLOCATED`` 设备事件——设备时间线断链，等于这次分配
           「在系统里没发生过」。
        """
        tenant = await make_tenant(code=f"AL-OK-{asset_status[:6]}", name="分配成功租户")
        catalog = await _seed_catalog(db, tenant.id, suffix=f"ALOK{asset_status[:6]}")
        device = await _make_device(db, sn=f"SN-AL-OK-{asset_status}", asset_status=asset_status)
        headers = await auth.platform_headers()

        order = await _create_ok(
            client,
            headers,
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            device_ids=[device.id],
        )
        result = await _execute_ok(client, headers, order["id"])

        assert result["allocatedCount"] == 1, "本次新增必须精确为 1"
        assert result["skippedCount"] == 0
        assert result["failedCount"] == 0
        assert result["failures"] == []
        assert result["allocation"]["status"] == str(AllocationStatus.COMPLETED)
        assert result["allocation"]["allocatedCount"] == 1
        assert result["allocation"]["failedCount"] == 0
        assert result["allocation"]["executedBy"]
        assert result["allocation"]["executedAt"] is not None
        assert result["allocation"]["failureReason"] is None

        stored = await _reload_device(db, device.id)
        assert stored.tenant_id == tenant.id, "设备必须落到目标租户名下"
        assert stored.client_product_id == catalog["product_id"], "必须挂上分配单指定的产品"
        assert stored.asset_status == str(AssetStatus.ALLOCATED)

        events = await _events(db, device.id, "ALLOCATED")
        assert len(events) == 1, "每次分配必须恰好留一条 ALLOCATED 事件"
        assert events[0].dimension == "asset"
        assert events[0].from_status == asset_status
        assert events[0].to_status == str(AssetStatus.ALLOCATED)

        # 明细行也必须是 ALLOCATED 且带分配时刻
        items = await _items_of(db, order["id"])
        assert [item.status for item in items] == [str(AllocationItemStatus.ALLOCATED)]
        assert items[0].allocated_at is not None
        assert items[0].device_sn == device.sn, "明细要存 SN 快照，错误报告才对得上人"

    async def test_completed_order_is_replayed_without_second_migration(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """★ ``COMPLETED`` 再执行 → 不迁移、不新增事件、返回既有计数。

        这里坏了说明：重复点击「执行」会把设备再分配一次（重复写事件、
        重复迁移），或返回 409 让运维以为出了故障。
        """
        tenant = await make_tenant(code="AL-REPLAY", name="分配回放租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="ALREPLAY")
        device = await _make_device(db, sn="SN-AL-REPLAY-01")
        headers = await auth.platform_headers()

        order = await _create_ok(
            client,
            headers,
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            device_ids=[device.id],
        )
        first = await _execute_ok(client, headers, order["id"])
        assert first["allocatedCount"] == 1

        second = await _execute_ok(client, headers, order["id"])

        # 回放的是**既有计数**：本单累计已分配 1 台，明细已是 ALLOCATED（=「无需再动」）
        assert second["allocatedCount"] == 1
        assert second["skippedCount"] == 1
        assert second["failedCount"] == 0
        assert second["failures"] == []
        assert second["allocation"]["status"] == str(AllocationStatus.COMPLETED)
        assert second["allocation"]["allocatedCount"] == 1

        # 只有一条 ALLOCATED 事件：回放没有产生第二次状态迁移
        assert len(await _events(db, device.id, "ALLOCATED")) == 1
        items = await _items_of(db, order["id"])
        assert [item.status for item in items] == [str(AllocationItemStatus.ALLOCATED)]


# ===========================================================================
# 三、★ 部分失败不回滚 + 重跑只补失败的行
# ===========================================================================


class TestExecutePartialFailure:
    """一张单里有失败时：成功的行保留，失败的行只标自己。"""

    async def test_partial_failure_keeps_successful_row_and_marks_order_failed(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """★ 1 台可分配 + 1 台已属别的租户 → 成功那台**保留**，整单 ``FAILED``。

        这里坏了说明：整单回滚把 299 台白分配一遍（重试时还要重新校验），
        或失败信息丢掉了「哪一台、为什么」。
        """
        tenant_a = await make_tenant(code="AL-PF-A", name="部分失败租户A")
        tenant_b = await make_tenant(code="AL-PF-B", name="部分失败租户B")
        catalog_a = await _seed_catalog(db, tenant_a.id, suffix="ALPFA")
        good = await _make_device(db, sn="SN-AL-PF-GOOD")
        foreign = await _make_device(db, sn="SN-AL-PF-FOREIGN", tenant_id=tenant_b.id)
        headers = await auth.platform_headers()

        order = await _create_ok(
            client,
            headers,
            tenant_id=tenant_a.id,
            product_id=catalog_a["product_id"],
            device_ids=[good.id, foreign.id],
        )
        result = await _execute_ok(client, headers, order["id"])

        assert result["allocation"]["status"] == str(AllocationStatus.FAILED)
        assert result["allocatedCount"] == 1, "成功的那台必须被算进本次新增"
        assert result["failedCount"] == 1
        assert result["allocation"]["allocatedCount"] == 1
        assert result["allocation"]["failedCount"] == 1
        assert result["allocation"]["failureReason"], "整单失败必须留下可读原因"

        failure = _failure_by_device(result, foreign.id)
        assert failure["code"] == "DEVICE_NOT_IN_TENANT"
        assert failure["deviceSn"] == foreign.sn, "失败记录必须指出是哪一台"

        # ★ 成功的那台真的留在 ALLOCATED（partial 不回滚）
        good_stored = await _reload_device(db, good.id)
        assert good_stored.tenant_id == tenant_a.id
        assert good_stored.asset_status == str(AssetStatus.ALLOCATED)
        # 越权目标设备归属未被动过
        assert (await _reload_device(db, foreign.id)).tenant_id == tenant_b.id

        items = {item.device_id: item for item in await _items_of(db, order["id"])}
        assert items[good.id].status == str(AllocationItemStatus.ALLOCATED)
        assert items[foreign.id].status == str(AllocationItemStatus.FAILED)
        assert items[foreign.id].error_message, "失败行必须写 errorMessage"

    async def test_rerun_after_fix_only_allocates_the_failed_device(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """★ ``FAILED → EXECUTING`` 重跑：只补失败那台，成功那台记为 ``SKIPPED``。

        这里坏了说明：重跑把上一轮已成功的设备**再分一次**（重复迁移、
        重复写事件），或重跑仍然失败导致「修复后也恢复不了」。
        """
        tenant_a = await make_tenant(code="AL-RR-A", name="重跑租户A")
        tenant_b = await make_tenant(code="AL-RR-B", name="重跑租户B")
        catalog_a = await _seed_catalog(db, tenant_a.id, suffix="ALRRA")
        ok_device = await _make_device(db, sn="SN-AL-RR-OK")
        fixed_device = await _make_device(db, sn="SN-AL-RR-FIX", tenant_id=tenant_b.id)
        headers = await auth.platform_headers()

        order = await _create_ok(
            client,
            headers,
            tenant_id=tenant_a.id,
            product_id=catalog_a["product_id"],
            device_ids=[ok_device.id, fixed_device.id],
        )
        first = await _execute_ok(client, headers, order["id"])
        assert first["allocation"]["status"] == str(AllocationStatus.FAILED)
        assert first["allocatedCount"] == 1

        # 运维处置：把设备从别的租户收回平台库存（模拟线下协调完成）
        fixed = await _reload_device(db, fixed_device.id)
        fixed.tenant_id = None
        fixed.client_product_id = None
        fixed.asset_status = str(AssetStatus.IN_STOCK)
        await db.flush()

        second = await _execute_ok(client, headers, order["id"])

        assert second["allocation"]["status"] == str(AllocationStatus.COMPLETED)
        assert second["allocatedCount"] == 1, "本次只新分配了失败的那一台"
        assert second["skippedCount"] == 1, "上一轮成功的那一台计入 skipped，而不是再次分配"
        assert second["failedCount"] == 0
        assert second["allocation"]["allocatedCount"] == 2, "整单累计应为 2 台"

        # ★ 成功那台没有被重复分配：只有一条 ALLOCATED 事件
        assert len(await _events(db, ok_device.id, "ALLOCATED")) == 1
        assert len(await _events(db, fixed_device.id, "ALLOCATED")) == 1
        fixed_stored = await _reload_device(db, fixed_device.id)
        assert fixed_stored.tenant_id == tenant_a.id
        assert fixed_stored.asset_status == str(AssetStatus.ALLOCATED)

    async def test_increment_and_cumulative_counts_have_distinct_meaning(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """★ ``allocatedCount`` 本次增量 ≠ 分配单上的累计 ``allocatedCount``。

        这里坏了说明：运维看到「新增 1 台」却以为整单只有 1 台（漏看了
        上一轮已成功的那台），或反过来把累计值当成本次增量去对账。
        """
        tenant_a = await make_tenant(code="AL-INCR-A", name="增量语义租户A")
        tenant_b = await make_tenant(code="AL-INCR-B", name="增量语义租户B")
        catalog_a = await _seed_catalog(db, tenant_a.id, suffix="ALINCRA")
        first_device = await _make_device(db, sn="SN-AL-INCR-01")
        second_device = await _make_device(db, sn="SN-AL-INCR-02", tenant_id=tenant_b.id)
        headers = await auth.platform_headers()

        order = await _create_ok(
            client,
            headers,
            tenant_id=tenant_a.id,
            product_id=catalog_a["product_id"],
            device_ids=[first_device.id, second_device.id],
        )
        await _execute_ok(client, headers, order["id"])

        second = await _reload_device(db, second_device.id)
        second.tenant_id = None
        second.client_product_id = None
        second.asset_status = str(AssetStatus.IN_STOCK)
        await db.flush()

        result = await _execute_ok(client, headers, order["id"])

        increment = result["allocatedCount"]
        cumulative = result["allocation"]["allocatedCount"]
        assert increment == 1, "本次执行只新分配了 1 台"
        assert cumulative == 2, "这张单现在一共分配了 2 台"
        assert increment != cumulative, "两个语义必须可区分（否则对账会错）"

        # 明细接口（整单现状）与 response.allocation 的累计值必须一致
        detail = await _detail(client, headers, order["id"])
        statuses = [item["status"] for item in detail["items"]]
        assert statuses.count(str(AllocationItemStatus.ALLOCATED)) == cumulative
        assert detail["itemTotal"] == 2
        assert detail["tenantName"] == tenant_a.name
        assert detail["clientProductName"]

    async def test_skipped_row_is_reported_but_not_counted_as_allocated(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """设备已在本租户且已 ``ALLOCATED`` → 明细 ``SKIPPED``，不再迁移。

        这里坏了说明：把一台早就属于本租户、已在目标状态的设备重新
        「分配」一遍（多写一条事件、多算一次成功），运维会以为又出了一批货。
        """
        tenant = await make_tenant(code="AL-SKIP", name="跳过租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="ALSKIP")
        # 早已分配：归属正确 + 已在 ALLOCATED（例如上一张单已成功分配）
        device = await _make_device(
            db,
            sn="SN-AL-SKIP-01",
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            asset_status=str(AssetStatus.ALLOCATED),
        )
        headers = await auth.platform_headers()

        order = await _create_ok(
            client,
            headers,
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            device_ids=[device.id],
        )
        result = await _execute_ok(client, headers, order["id"])

        assert result["allocatedCount"] == 0, "没有发生新的分配"
        assert result["skippedCount"] == 1
        assert result["failedCount"] == 0
        assert result["allocation"]["status"] == str(AllocationStatus.COMPLETED)

        items = await _items_of(db, order["id"])
        assert [item.status for item in items] == [str(AllocationItemStatus.SKIPPED)]
        assert items[0].error_message is None
        # 没有产生第二次资产迁移
        assert await _events(db, device.id, "ALLOCATED") == []


# ===========================================================================
# 四、逐行失败原因（错误码即优先级）
# ===========================================================================


class TestExecuteRowFailures:
    """逐个状态钉死失败错误码——前端的分类展示与客服话术都依赖它。"""

    async def test_unknown_device_is_resource_not_found(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """设备不存在 → ``RESOURCE_NOT_FOUND``（明细指向了一个不存在的目标）。"""
        tenant = await make_tenant(code="AL-RF-NF", name="失败_设备不存在")
        catalog = await _seed_catalog(db, tenant.id, suffix="ALRFNF")
        headers = await auth.platform_headers()
        ghost_id = new_id("device")

        order = await _create_ok(
            client,
            headers,
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            device_ids=[ghost_id],
        )
        result = await _execute_ok(client, headers, order["id"])

        assert result["allocation"]["status"] == str(AllocationStatus.FAILED)
        assert result["allocatedCount"] == 0
        failure = _failure_by_device(result, ghost_id)
        assert failure["code"] == "RESOURCE_NOT_FOUND"
        assert failure["message"]

    async def test_foreign_device_is_device_not_in_tenant(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """设备属于别的租户 → 403 ``DEVICE_NOT_IN_TENANT``（不能抢别人的资产）。"""
        tenant_a = await make_tenant(code="AL-RF-A", name="失败_跨租户A")
        tenant_b = await make_tenant(code="AL-RF-B", name="失败_跨租户B")
        catalog_a = await _seed_catalog(db, tenant_a.id, suffix="ALRFA")
        foreign = await _make_device(db, sn="SN-AL-RF-FOREIGN", tenant_id=tenant_b.id)
        headers = await auth.platform_headers()

        order = await _create_ok(
            client,
            headers,
            tenant_id=tenant_a.id,
            product_id=catalog_a["product_id"],
            device_ids=[foreign.id],
        )
        result = await _execute_ok(client, headers, order["id"])

        assert _failure_by_device(result, foreign.id)["code"] == "DEVICE_NOT_IN_TENANT"
        assert (await _reload_device(db, foreign.id)).tenant_id == tenant_b.id, (
            "越权失败必须真的没发生：归属不得被改写"
        )

    async def test_frozen_device_is_device_frozen(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """冻结设备 → 409 ``DEVICE_FROZEN``（专门错误码，冻结是可解释可解除的状态）。"""
        tenant = await make_tenant(code="AL-RF-FRZ", name="失败_冻结")
        catalog = await _seed_catalog(db, tenant.id, suffix="ALRFFRZ")
        frozen = await _make_device(
            db,
            sn="SN-AL-RF-FROZEN",
            asset_status=str(AssetStatus.FROZEN),
        )
        headers = await auth.platform_headers()

        order = await _create_ok(
            client,
            headers,
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            device_ids=[frozen.id],
        )
        result = await _execute_ok(client, headers, order["id"])

        assert _failure_by_device(result, frozen.id)["code"] == "DEVICE_FROZEN"
        stored = await _reload_device(db, frozen.id)
        assert stored.asset_status == str(AssetStatus.FROZEN)
        assert stored.tenant_id is None, "被拒的设备不得被写入归属"

    @pytest.mark.parametrize(
        "asset_status",
        [
            str(AssetStatus.PENDING_GEN),
            str(AssetStatus.GENERATED),
            str(AssetStatus.PRODUCING),
            str(AssetStatus.PRODUCED),
            str(AssetStatus.ALLOCATED),
            str(AssetStatus.BOUND),
            str(AssetStatus.RETIRED),
        ],
    )
    async def test_non_allocatable_status_is_device_not_available(
        self,
        client: AsyncClient,
        auth: Any,
        db: AsyncSession,
        make_tenant: Any,
        asset_status: str,
    ) -> None:
        """白名单是 ``ALLOCATABLE_ASSET_STATUSES``：其余状态一律 409 ``DEVICE_NOT_AVAILABLE``。

        这里坏了说明：可以分配一台还在工厂车间（``PRODUCED``）、
        尚未入库（``GENERATED``）或已报废（``RETIRED``）的设备。
        ``ALLOCATED`` 变体用「平台库存（``tenantId`` 为空）+ 已分配」的形态，
        避免落进「已在本租户且已分配 → 跳过」那条分支。
        """
        assert AssetStatus(asset_status) not in ALLOCATABLE_ASSET_STATUSES
        tenant = await make_tenant(code=f"AL-NA-{asset_status[:6]}", name="不可分配租户")
        catalog = await _seed_catalog(db, tenant.id, suffix=f"ALNA{asset_status[:6]}")
        device = await _make_device(
            db, sn=f"SN-AL-NA-{asset_status}", asset_status=asset_status
        )
        headers = await auth.platform_headers()

        order = await _create_ok(
            client,
            headers,
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            device_ids=[device.id],
        )
        result = await _execute_ok(client, headers, order["id"])

        assert _failure_by_device(result, device.id)["code"] == "DEVICE_NOT_AVAILABLE"
        stored = await _reload_device(db, device.id)
        assert stored.tenant_id is None
        assert stored.asset_status == asset_status


# ===========================================================================
# 五、★ 整单失败：产品授权被撤销
# ===========================================================================


class TestExecuteWholeOrderFailure:
    """授权是分配的前提；撤销后待执行行全失败，已完成的行不受影响。"""

    async def test_revoked_authorization_fails_pending_rows_only(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """★ 撤销授权后重跑 → 待执行行全标失败、一台都不分出去、已成功行不受影响。

        这里坏了说明两种事故之一：
        ① 授权已撤销却仍把设备分出去（「已停服的产品还在出货」）；
        ② 为了「整单失败」把上一轮已分配的设备退回去（把客户资产弄丢，
           而撤销授权的正确处置是停新增，不是回收存量）。
        """
        tenant_a = await make_tenant(code="AL-AUTH-A", name="授权撤销租户A")
        tenant_b = await make_tenant(code="AL-AUTH-B", name="授权撤销租户B")
        catalog_a = await _seed_catalog(db, tenant_a.id, suffix="ALAUTHA")
        ok_device = await _make_device(db, sn="SN-AL-AUTH-OK")
        pending_device = await _make_device(db, sn="SN-AL-AUTH-PEND", tenant_id=tenant_b.id)
        headers = await auth.platform_headers()

        order = await _create_ok(
            client,
            headers,
            tenant_id=tenant_a.id,
            product_id=catalog_a["product_id"],
            device_ids=[ok_device.id, pending_device.id],
        )
        first = await _execute_ok(client, headers, order["id"])
        assert first["allocation"]["status"] == str(AllocationStatus.FAILED)
        assert first["allocatedCount"] == 1

        # 运维把待执行那台修好（收回平台库存），随后产品授权被平台撤销
        fixed = await _reload_device(db, pending_device.id)
        fixed.tenant_id = None
        fixed.client_product_id = None
        fixed.asset_status = str(AssetStatus.IN_STOCK)
        await db.flush()

        authorization = (
            await db.execute(
                select(ProductAuthorization).where(
                    ProductAuthorization.tenant_id == tenant_a.id,
                    ProductAuthorization.template_id == catalog_a["template_id"],
                )
            )
        ).scalar_one()
        await db.delete(authorization)
        await db.flush()

        second = await _execute_ok(client, headers, order["id"])

        assert second["allocation"]["status"] == str(AllocationStatus.FAILED)
        assert second["allocatedCount"] == 0, "授权已撤，本次一台都不该分出去"
        assert second["failedCount"] == 1
        assert second["skippedCount"] == 1, "上一轮成功的那台属于「无需再动」"
        assert second["allocation"]["allocatedCount"] == 1, "累计仍是上一轮的 1 台"
        assert _failure_by_device(second, pending_device.id)["code"] == "PRODUCT_NOT_AUTHORIZED"

        # ★ 待执行设备一台都没出去
        still_pending = await _reload_device(db, pending_device.id)
        assert still_pending.tenant_id is None
        assert still_pending.asset_status == str(AssetStatus.IN_STOCK)
        # ★ 已完成的行不受影响
        still_ok = await _reload_device(db, ok_device.id)
        assert still_ok.tenant_id == tenant_a.id
        assert still_ok.asset_status == str(AssetStatus.ALLOCATED)

    async def test_disabled_product_blocks_execution_entirely(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """产品被停用（而非撤销授权）→ 同样是整单 ``PRODUCT_NOT_AUTHORIZED``。

        与上一条同源（都走 ``_product_usable``），但触发路径不同：
        上一条删授权行，这条改 ``client_products.status``。
        """
        tenant = await make_tenant(code="AL-DISPROD", name="停用产品租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="ALDISPROD")
        device = await _make_device(db, sn="SN-AL-DISPROD-01")
        headers = await auth.platform_headers()

        order = await _create_ok(
            client,
            headers,
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            device_ids=[device.id],
        )

        product = (
            await db.execute(
                select(ClientProduct).where(ClientProduct.id == catalog["product_id"])
            )
        ).scalar_one()
        product.status = str(EnableStatus.DISABLED)
        await db.flush()

        result = await _execute_ok(client, headers, order["id"])

        assert result["allocation"]["status"] == str(AllocationStatus.FAILED)
        assert result["allocatedCount"] == 0
        assert _failure_by_device(result, device.id)["code"] == "PRODUCT_NOT_AUTHORIZED"
        assert (await _reload_device(db, device.id)).tenant_id is None


# ===========================================================================
# 六、执行接口的状态机与可见性入口
# ===========================================================================


class TestExecuteGuards:
    """执行前的两道门：分配单存在（对当前上下文可见）与状态允许。"""

    async def test_execute_unknown_allocation_is_not_found(
        self, client: AsyncClient, auth: Any
    ) -> None:
        """分配单不存在 → 404（不回 500，也不静默成功）。"""
        headers = await auth.platform_headers()
        response = await _execute(client, headers, "allocation-does-not-exist")
        _assert_error(response, 404, "RESOURCE_NOT_FOUND")

    async def test_completed_allocation_is_terminal_and_replay_keeps_status(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """``COMPLETED`` 是终态：非回放路径不可再进入（状态机表里出边为空）。

        这里坏了说明：终态被绕过，一张已完成的单可以再次走 EXECUTING 分支。
        实测口径：``COMPLETED`` 命中幂等回放（200），而 ``execute`` 对
        已完成单返回的分配单状态仍必须是 ``COMPLETED``——绝不会落回
        ``EXECUTING`` 或 ``DRAFT``。
        """
        tenant = await make_tenant(code="AL-TERM", name="终态租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="ALTERM")
        device = await _make_device(db, sn="SN-AL-TERM-01")
        headers = await auth.platform_headers()

        order = await _create_ok(
            client,
            headers,
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            device_ids=[device.id],
        )
        await _execute_ok(client, headers, order["id"])
        again = await _execute_ok(client, headers, order["id"])

        assert again["allocation"]["status"] == str(AllocationStatus.COMPLETED)
        stored = (
            await db.execute(
                select(AllocationOrder).where(AllocationOrder.id == order["id"])
            )
        ).scalar_one()
        assert stored.status == str(AllocationStatus.COMPLETED)

    async def test_allocation_list_and_items_are_consistent(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any
    ) -> None:
        """列表 / 明细 / 详情三处对同一张单的结论必须一致。

        这里坏了说明：同一份事实在多处查询给出了不同答案（运维在列表看到
        「已完成」，点进详情却是「执行失败」）。
        """
        tenant = await make_tenant(code="AL-CONSIST", name="一致性租户")
        catalog = await _seed_catalog(db, tenant.id, suffix="ALCONSIST")
        devices = [
            await _make_device(db, sn=f"SN-AL-CONSIST-{index:02d}") for index in range(1, 4)
        ]
        headers = await auth.platform_headers()

        order = await _create_ok(
            client,
            headers,
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            device_ids=[device.id for device in devices],
        )
        await _execute_ok(client, headers, order["id"])

        listing = await client.get(
            f"{PLATFORM}/allocations",
            headers=headers,
            params={"keyword": order["allocationNo"]},
        )
        assert listing.status_code == 200, listing.text
        page = listing.json()
        assert page["total"] == 1
        record = page["records"][0]
        assert record["id"] == order["id"]
        assert record["status"] == str(AllocationStatus.COMPLETED)
        assert record["allocatedCount"] == 3
        assert record["totalCount"] == 3

        detail = await _detail(client, headers, order["id"])
        assert detail["status"] == record["status"]
        assert detail["allocatedCount"] == record["allocatedCount"]
        assert detail["itemTotal"] == 3

        items_response = await client.get(
            f"{PLATFORM}/allocations/{order['id']}/items",
            headers=headers,
            params={"status": str(AllocationItemStatus.ALLOCATED)},
        )
        assert items_response.status_code == 200, items_response.text
        assert items_response.json()["total"] == 3
