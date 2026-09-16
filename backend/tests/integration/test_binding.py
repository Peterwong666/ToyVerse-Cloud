"""集成测试：P5 设备绑定（``precheck`` → ``bind`` → ``unbind``）。

覆盖本阶段验收标准
------------------
* ① **两步流程**：``precheck`` 签发一次性确认令牌（明文仅一次），
  ``bind`` 用令牌完成绑定并把设备推到 ``BOUND``。
* ② ★ **令牌用后即销毁**：``bind`` 成功后库内 ``confirm_token_hash`` 置空；
  解绑后再用**旧令牌**必须失败——「扫了码但没点确认」不会占住绑定名额。
* ③ ★ **令牌过期**：过期后 ``bind`` → 409 ``QR_EXPIRED``；重新 ``precheck``
  应能拿到**新令牌**并复用**同一行**绑定记录。
* ④ ★ **幂等重放先于令牌校验**：同 ``Idempotency-Key`` 重放返回首次响应体
  （同一条 ``id``、``bindCount`` 不变）；同键异体 → 409 ``IDEMPOTENCY_CONFLICT``。
* ⑤ ★ **单绑约束在数据库层**：``device_bindings.device_id`` 唯一索引
  ——直接插第二行必须抛 ``IntegrityError``，证明它不是只在服务层「先查后写」。
* ⑥ ★ **二维码防篡改**：JD 字段改动 / 2、3 段互换 / 用真密钥为**别的租户**重算签名、
  JX（无签名格式）篡改 IMEI —— 全部 404 ``QR_INVALID``。
* ⑦ **租户匹配**：跨租户扫码 → 403 ``DEVICE_NOT_IN_TENANT``；
  未分配的平台库存设备（JX 格式可解析）→ 403。
* ⑧ **冻结拦截**：``IN_STOCK`` 冻结后的设备 ``precheck`` 必须 409 ``DEVICE_FROZEN``
  （冻结检查先于「状态是否可绑」）。
* ⑨ **解绑**：原因必填（空串 400）、未绑定不可解绑、解绑后 ``bind_count`` 保留
  且 ``unbound_at`` / ``unbound_by`` 落库。
* ⑩ **产品授权**：客户产品停用或授权行被删 → 409 ``PRODUCT_NOT_AUTHORIZED``。

测试写法对齐 ``tests/integration/test_order_flow.py``：模块内自定义 helper、
camelCase 断言、库级用例直接落数据（不靠 HTTP 造数）、中文注释说明断言依据。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext
from app.core.ids import new_id
from app.db.base import utcnow
from app.models.allocation import DeviceBinding
from app.models.catalog import ClientProduct, ProductAuthorization
from app.models.device import Device, DeviceEvent
from app.models.enums import (
    ActivationStatus,
    AssetStatus,
    BindingRecordStatus,
    BindStatus,
    EnableStatus,
    OnlineStatus,
    RoleType,
)
from app.services import catalog_service, device_service, qrcode_service
from tests.conftest import API_PREFIX

pytestmark = pytest.mark.integration

PLATFORM = f"{API_PREFIX}/platform"
MERCHANT = f"{API_PREFIX}/merchant"

#: 商户测试账号口令（仅存在于测试进程）
MERCHANT_PASSWORD = "Merch4nt-It#2026"

#: 错误码取值集合（用于断言失败响应不返回编造的错误码）
_ERROR_CODES = {
    "UNAUTHENTICATED",
    "PERMISSION_DENIED",
    "VALIDATION_ERROR",
    "RESOURCE_NOT_FOUND",
    "IDEMPOTENCY_CONFLICT",
    "PRODUCT_NOT_AUTHORIZED",
    "DEVICE_NOT_AVAILABLE",
    "DEVICE_NOT_FOUND",
    "DEVICE_ALREADY_BOUND",
    "DEVICE_FROZEN",
    "DEVICE_NOT_IN_TENANT",
    "INVALID_STATE_TRANSITION",
    "QR_INVALID",
    "QR_EXPIRED",
    "BIND_FAILED",
}


# ---------------------------------------------------------------------------
# 模块级辅助（不跨模块私有导入，避免耦合既有测试文件）
# ---------------------------------------------------------------------------


def _platform_ctx() -> AuthContext:
    """平台超管上下文（通配权限），供服务层建链使用。"""
    return AuthContext(
        user_id="u-binding-it",
        account="binding-it",
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
    """建「云服务商 → 模板 → 授权 → 客户产品」最小链条（走真实服务层）。"""
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


async def _make_device(
    db: AsyncSession,
    *,
    sn: str,
    tenant_id: str | None = None,
    product_id: str | None = None,
    asset_status: str = str(AssetStatus.IN_STOCK),
    network_type: str = "WIFI",
    imei: str | None = None,
    iccid: str | None = None,
    vendor_device_id: str | None = None,
    bind_status: str = str(BindStatus.UNBOUND),
    previous_asset_status: str | None = None,
) -> Device:
    """直接落一台设备（库级造数，避免造数过程本身成为变量）。"""
    device = Device(
        id=new_id("device"),
        sn=sn,
        tenant_id=tenant_id,
        client_product_id=product_id,
        network_type=network_type,
        imei=imei,
        iccid=iccid,
        vendor_device_id=vendor_device_id,
        asset_status=asset_status,
        previous_asset_status=previous_asset_status,
        activation_status=str(ActivationStatus.NOT_ACTIVATED),
        online_status=str(OnlineStatus.NEVER_ONLINE),
        bind_status=bind_status,
        generated_at=utcnow(),
    )
    db.add(device)
    await db.flush()
    return device


async def _allocated_device(
    db: AsyncSession,
    tenant_id: str,
    *,
    suffix: str,
    sn: str,
    network_type: str = "WIFI",
    imei: str | None = None,
    iccid: str | None = None,
    vendor_device_id: str | None = None,
) -> dict[str, Any]:
    """建一台「已分配给租户」的设备（可绑定的唯一合法前置状态）。"""
    catalog = await _seed_catalog(
        db,
        tenant_id,
        suffix=suffix,
        network_type=network_type,
        vendor="JOYINSIDE" if network_type == "WIFI" else "JIXIAN",
    )
    device = await _make_device(
        db,
        sn=sn,
        tenant_id=tenant_id,
        product_id=catalog["product_id"],
        asset_status=str(AssetStatus.ALLOCATED),
        network_type=network_type,
        imei=imei,
        iccid=iccid,
        vendor_device_id=vendor_device_id,
    )
    return {**catalog, "device": device}


def _jd_payload(tenant_id: str, product_id: str, sn: str) -> str:
    """生成一台合法 JD 二维码载荷（走生产实现的签名函数）。"""
    return qrcode_service.build_jd_payload(tenant_id, product_id, sn)


def _jx_payload(device: Device) -> str:
    """按设备记录生成合法 JX 载荷（与 ``precheck`` 的规范载荷完全一致）。"""
    return qrcode_service.build_jx_payload(
        device.sn, device.imei, device.iccid, device.vendor_device_id
    )


async def _precheck(client: AsyncClient, headers: dict[str, str], payload: str) -> Any:
    """调 precheck（不预设状态码）。"""
    return await client.post(
        f"{MERCHANT}/devices/bind/precheck", headers=headers, json={"qrPayload": payload}
    )


async def _precheck_ok(
    client: AsyncClient, headers: dict[str, str], payload: str
) -> dict[str, Any]:
    """调 precheck 并断言 200，返回响应体。"""
    response = await _precheck(client, headers, payload)
    assert response.status_code == 200, response.text
    return response.json()


async def _bind(
    client: AsyncClient,
    headers: dict[str, str],
    device_id: str,
    token: str,
    *,
    key: str | None = None,
    end_user_id: str | None = None,
    remark: str | None = None,
) -> Any:
    """调 bind（不预设状态码）。"""
    request_headers = dict(headers)
    if key:
        request_headers["Idempotency-Key"] = key
    body: dict[str, Any] = {"confirmToken": token}
    if end_user_id is not None:
        body["endUserId"] = end_user_id
    if remark is not None:
        body["remark"] = remark
    return await client.post(
        f"{MERCHANT}/devices/{device_id}/bind", headers=request_headers, json=body
    )


async def _bind_ok(
    client: AsyncClient, headers: dict[str, str], device_id: str, token: str, **kwargs: Any
) -> dict[str, Any]:
    response = await _bind(client, headers, device_id, token, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


async def _unbind(
    client: AsyncClient,
    headers: dict[str, str],
    device_id: str,
    reason: str,
    *,
    key: str | None = None,
) -> Any:
    request_headers = dict(headers)
    if key:
        request_headers["Idempotency-Key"] = key
    return await client.post(
        f"{MERCHANT}/devices/{device_id}/unbind",
        headers=request_headers,
        json={"reason": reason},
    )


async def _db_binding(db: AsyncSession, device_id: str) -> DeviceBinding:
    return (
        await db.execute(select(DeviceBinding).where(DeviceBinding.device_id == device_id))
    ).scalar_one()


def _assert_error(response: Any, status: int, code: str) -> None:
    """断言失败响应的状态码与错误码，并顺带钉死错误码契约。"""
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code, response.text
    assert body["code"] in _ERROR_CODES, f"返回了未登记的错误码：{body['code']}"
    assert isinstance(body["message"], str) and body["message"]
    assert "traceId" in body


# ===========================================================================
# 一、precheck：判定顺序与二维码防篡改
# ===========================================================================


class TestPrecheck:
    """``precheck`` 的判定顺序就是错误码优先级，逐条钉死。"""

    async def test_precheck_issues_token_and_persists_pending_row(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """成功预检：返回明文令牌、库内只存摘要、绑定行置为 PENDING。"""
        tenant = await make_tenant(code="BIND-OK", name="绑定测试租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000001")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDOK", sn="SN-BIND-OK-01")
        device = seeded["device"]
        payload = _jd_payload(tenant.id, seeded["product_id"], device.sn)

        body = await _precheck_ok(client, merchant, payload)

        assert body["deviceId"] == device.id
        assert body["sn"] == device.sn
        assert body["qrFormat"] == "JD"
        assert body["assetStatus"] == str(AssetStatus.ALLOCATED)
        assert body["tenantId"] == tenant.id
        assert body["tenantName"] == tenant.name
        assert body["clientProductId"] == seeded["product_id"]
        assert body["clientProductName"]
        assert body["expiresInSeconds"] > 0
        # 令牌明文足够长（避免「短令牌可枚举」这类实现退化）
        assert len(body["confirmToken"]) >= 32

        row = await _db_binding(db, device.id)
        assert row.id == body["bindingId"]
        assert row.status == str(BindingRecordStatus.PENDING)
        assert row.tenant_id == tenant.id
        # ★ 库内只有摘要，且不是明文
        assert row.confirm_token_hash is not None
        assert row.confirm_token_hash != body["confirmToken"]
        assert len(row.confirm_token_hash) == 64
        # ★ 二维码载荷也只存摘要，不存可复制的绑定凭据
        assert row.qr_payload_hash is not None
        assert payload not in (row.qr_payload_hash or "")

    async def test_precheck_unknown_sn_is_device_not_found(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """格式与签名都对，但设备不在库里 → 404 ``DEVICE_NOT_FOUND``。"""
        tenant = await make_tenant(code="BIND-NF", name="设备不存在租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000002")
        catalog = await _seed_catalog(db, tenant.id, suffix="BINDNF")

        payload = _jd_payload(tenant.id, catalog["product_id"], "SN-DOES-NOT-EXIST")
        response = await _precheck(client, merchant, payload)
        _assert_error(response, 404, "DEVICE_NOT_FOUND")

    async def test_unallocated_platform_stock_device_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ 未分配给任何租户的平台库存设备 → 403 ``DEVICE_NOT_IN_TENANT``。

        为什么用 **JX（4G）** 格式：JD 格式的规范载荷需要 ``tenant_id`` /
        ``client_product_id``，平台库存两者皆空，会在「载荷摘要」那一步
        先抛 ``QR_INVALID``。JX 格式无签名、可直接构造，才能把断言打在
        「租户匹配」这一步上（即用户真正该看到的错误）。
        """
        tenant = await make_tenant(code="BIND-STOCK", name="未分配设备租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000003")
        device = await _make_device(
            db, sn="SN-STOCK-JX-01", network_type="4G", imei="860000000000001"
        )
        assert device.tenant_id is None

        response = await _precheck(client, merchant, _jx_payload(device))
        _assert_error(response, 403, "DEVICE_NOT_IN_TENANT")

    async def test_unallocated_jd_device_is_qr_invalid(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """JD 格式的平台库存设备：**摘要校验先于租户匹配**，故为 404 ``QR_INVALID``。

        这不是缺陷：JD 载荷含租户与产品，设备记录里没有这两个值就
        无法重算出规范载荷，"无法证明这张码属于这台设备" 只能报无效码。
        """
        tenant = await make_tenant(code="BIND-STOCK-JD", name="未分配JD设备租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000004")
        device = await _make_device(db, sn="SN-STOCK-JD-01", network_type="WIFI")

        payload = _jd_payload(tenant.id, "cprod-anything", device.sn)
        response = await _precheck(client, merchant, payload)
        _assert_error(response, 404, "QR_INVALID")

    async def test_jd_payload_with_swapped_segments_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ 把 JD 的第 2、3 段互换 → 签名内容随之变化 → 404 ``QR_INVALID``。

        这正是附录 B 那个「租户 / 产品整体错位」缺陷在签名层面的堵法：
        顺序错 → 签名验不过，而不是「看起来正常但绑错产品」。
        """
        tenant = await make_tenant(code="BIND-SWAP", name="字段错位租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000005")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDSWAP", sn="SN-SWAP-01")
        payload = _jd_payload(tenant.id, seeded["product_id"], "SN-SWAP-01")

        segments = payload.split("|")
        swapped = "|".join([segments[0], segments[2], segments[1], segments[3], segments[4]])
        assert swapped != payload

        response = await _precheck(client, merchant, swapped)
        _assert_error(response, 404, "QR_INVALID")

    async def test_jd_payload_forged_for_other_tenant_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ 用**真密钥**为「别的租户」重算签名的 JD 载荷 → 404 ``QR_INVALID``。

        签名本身有效（不是坏签名），但重算出的规范载荷与设备记录不一致，
        被 SHA-256 命中检查挡住——这道检查是「合法格式但字段被改过」的唯一防线。
        """
        tenant_a = await make_tenant(code="BIND-FA", name="伪造租户A")
        tenant_b = await make_tenant(code="BIND-FB", name="伪造租户B")
        merchant_b = await _merchant_headers(auth, make_user, tenant_b.id, "13200000006")
        seeded_a = await _allocated_device(db, tenant_a.id, suffix="BINDFA", sn="SN-FORGE-01")

        # 设备属于 A，但载荷伪造成「属于 B」并用真密钥重算签名
        forged = _jd_payload(tenant_b.id, seeded_a["product_id"], "SN-FORGE-01")
        response = await _precheck(client, merchant_b, forged)
        _assert_error(response, 404, "QR_INVALID")

    async def test_jd_payload_with_tampered_sn_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """改动 JD 载荷任一字符 → 404 ``QR_INVALID``。"""
        tenant = await make_tenant(code="BIND-TAM", name="篡改租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000007")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDTAM", sn="SN-TAM-01")
        payload = _jd_payload(tenant.id, seeded["product_id"], "SN-TAM-01")

        tampered = payload.replace("SN-TAM-01", "SN-TAM-02")
        assert tampered != payload
        _assert_error(await _precheck(client, merchant, tampered), 404, "QR_INVALID")

        # 只改最后一个字符（签名），同样必须失败
        _assert_error(await _precheck(client, merchant, payload[:-1] + "0"), 404, "QR_INVALID")

    async def test_jx_payload_with_tampered_imei_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ JX 格式没有签名，篡改 IMEI 只能靠 SHA-256 命中挡住 → 404 ``QR_INVALID``。"""
        tenant = await make_tenant(code="BIND-JX", name="JX篡改租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000008")
        seeded = await _allocated_device(
            db,
            tenant.id,
            suffix="BINDJX",
            sn="SN-JX-01",
            network_type="4G",
            imei="860000000000111",
            iccid="8986000000000000111",
            vendor_device_id="vd-jx-01",
        )
        device = seeded["device"]

        original = _jx_payload(device)
        assert original == "JX|SN-JX-01|860000000000111|8986000000000000111|vd-jx-01"
        tampered = original.replace("860000000000111", "860000000000999")

        _assert_error(await _precheck(client, merchant, tampered), 404, "QR_INVALID")

    async def test_cross_tenant_precheck_is_403(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ 租户 B 的商户扫租户 A 的设备（合法载荷）→ 403 ``DEVICE_NOT_IN_TENANT``。"""
        tenant_a = await make_tenant(code="BIND-CTA", name="跨租户A")
        tenant_b = await make_tenant(code="BIND-CTB", name="跨租户B")
        merchant_b = await _merchant_headers(auth, make_user, tenant_b.id, "13200000009")
        seeded_a = await _allocated_device(db, tenant_a.id, suffix="BINDCTA", sn="SN-CT-01")

        payload = _jd_payload(tenant_a.id, seeded_a["product_id"], "SN-CT-01")
        _assert_error(await _precheck(client, merchant_b, payload), 403, "DEVICE_NOT_IN_TENANT")

    async def test_frozen_device_precheck_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ 冻结拦截：状态为 ``FROZEN`` 的设备 → 409 ``DEVICE_FROZEN``。

        P5 时这条分支只能靠**落库构造脏数据**才观测得到：
        ``FREEZABLE_ASSET_STATUSES`` 当时只含 ``IN_STOCK``，而绑定只接受
        ``ALLOCATED``，两者之间没有迁移边，「已分配 + 已冻结」不可达，
        于是冻结校验是「语义正确但正常流程不可达」的防御性代码。

        P6 起冻结范围放宽到 ``{IN_STOCK, ALLOCATED, BOUND}``，这条分支
        在生产流程里**真的可达**了（见
        ``test_allocated_device_can_be_frozen_then_precheck_rejected``）。
        本用例保留不复原的脏数据形态，覆盖「冻结前状态为空」的边界。

        冻结检查在「状态是否可绑」之前，故错误码必须是 ``DEVICE_FROZEN``
        而不是 ``DEVICE_NOT_AVAILABLE``。
        """
        tenant = await make_tenant(code="BIND-FRZ", name="冻结租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000010")
        catalog = await _seed_catalog(db, tenant.id, suffix="BINDFRZ")
        device = await _make_device(
            db,
            sn="SN-FRZ-01",
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            asset_status=str(AssetStatus.FROZEN),
            previous_asset_status=str(AssetStatus.IN_STOCK),
        )

        payload = _jd_payload(tenant.id, catalog["product_id"], device.sn)
        _assert_error(await _precheck(client, merchant, payload), 409, "DEVICE_FROZEN")

        # 库里不能留下 PENDING 绑定行（被拒的预检不得签发任何令牌）
        assert int(
            (
                await db.execute(
                    select(func.count()).select_from(DeviceBinding).where(
                        DeviceBinding.device_id == device.id
                    )
                )
            ).scalar_one()
        ) == 0

    async def test_unfreeze_then_precheck_succeeds(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """对抗性补充：解冻并分配后，同一张码可以正常预检。

        与上一条互为反证：``DEVICE_FROZEN`` 是「冻结态」造成的，而不是
        「这张码永远绑不了」——解冻后流程必须恢复。
        """
        tenant = await make_tenant(code="BIND-THAW", name="解冻租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000012")
        catalog = await _seed_catalog(db, tenant.id, suffix="BINDTHAW")
        device = await _make_device(
            db,
            sn="SN-THAW-01",
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            asset_status=str(AssetStatus.FROZEN),
            previous_asset_status=str(AssetStatus.IN_STOCK),
        )
        payload = _jd_payload(tenant.id, catalog["product_id"], device.sn)
        _assert_error(await _precheck(client, merchant, payload), 409, "DEVICE_FROZEN")

        # 模拟「解冻 → 分配」之后的合法前置状态
        device.asset_status = str(AssetStatus.ALLOCATED)
        device.previous_asset_status = None
        await db.flush()

        body = await _precheck_ok(client, merchant, payload)
        assert body["assetStatus"] == str(AssetStatus.ALLOCATED)
        assert body["confirmToken"]

    async def test_allocated_device_can_be_frozen_then_precheck_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ P5 遗留①的正面验证：**已分配给商户的设备现在真的能被冻结**。

        这条用例在 P5 是**写不出来**的：``FREEZABLE_ASSET_STATUSES`` 当时
        只含 ``IN_STOCK``，与「绑定只接受 ``ALLOCATED``」交集为空，
        「已分配 + 已冻结」在数据上不可达，冻结校验只能在脏数据上观测。

        P6 放宽冻结范围后，整条链路可以用**生产接口**走通：
        分配（``ALLOCATED``）→ 平台冻结（欠费停机）→ 终端扫码被拦。
        冻结的是已交付到商户手里的设备，这正是「停服」这个运营动作的落点。
        """
        tenant = await make_tenant(code="BIND-FRZALLOC", name="停服租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000014")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDFRZALLOC", sn="SN-FRZALLOC-01")
        device = seeded["device"]
        payload = _jd_payload(tenant.id, seeded["product_id"], device.sn)

        # 冻结前：已分配设备可以正常预检（证明拦截确实来自「冻结」）
        assert (await _precheck_ok(client, merchant, payload))["confirmToken"]

        # 走生产实现冻结（平台超管上下文），而不是直接改库
        await device_service.freeze_device(
            db, _platform_ctx(), device, reason="欠费停机（P6 验收）"
        )
        assert device.asset_status == str(AssetStatus.FROZEN)
        assert device.previous_asset_status == str(AssetStatus.ALLOCATED), (
            "冻结前状态必须被记录，否则解冻无法原路恢复为 ALLOCATED"
        )

        _assert_error(await _precheck(client, merchant, payload), 409, "DEVICE_FROZEN")

        # 解冻必须回到 ALLOCATED（而不是回落 IN_STOCK）——设备早已分配给商户，
        # 回落到库存态会让它在商户端消失，等于把客户的资产弄丢了。
        await device_service.thaw_device(db, _platform_ctx(), device, reason="缴费恢复")
        assert device.asset_status == str(AssetStatus.ALLOCATED)

        assert (await _precheck_ok(client, merchant, payload))["assetStatus"] == str(
            AssetStatus.ALLOCATED
        )

    async def test_already_bound_device_precheck_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ 单绑约束：已绑定设备再 precheck → 409 ``DEVICE_ALREADY_BOUND``。

        单绑检查先于「状态是否可绑」：用户真正需要知道的是「它已经绑过了、
        绑给谁」，而 ``details`` 必须带上原绑定信息供客服核对。
        """
        tenant = await make_tenant(code="BIND-DUP", name="重复绑定租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000013")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDDUP", sn="SN-DUP-01")
        device = seeded["device"]
        payload = _jd_payload(tenant.id, seeded["product_id"], device.sn)

        first = await _precheck_ok(client, merchant, payload)
        await _bind_ok(client, merchant, device.id, first["confirmToken"], end_user_id="eu-001")

        response = await _precheck(client, merchant, payload)
        _assert_error(response, 409, "DEVICE_ALREADY_BOUND")
        details = response.json()["details"]
        assert details["bindingId"] == first["bindingId"]
        assert details["endUserId"] == "eu-001"
        assert details["boundAt"], "已绑定错误必须回显原绑定时间，供客服核对"

    @pytest.mark.parametrize(
        "asset_status",
        [
            str(AssetStatus.PENDING_GEN),
            str(AssetStatus.GENERATED),
            str(AssetStatus.IN_STOCK),
            str(AssetStatus.PRODUCING),
            str(AssetStatus.PRODUCED),
            str(AssetStatus.SHIPPED),
            str(AssetStatus.BOUND),
            str(AssetStatus.RETIRED),
        ],
    )
    async def test_only_allocated_devices_are_bindable(
        self,
        client: AsyncClient,
        auth: Any,
        db: AsyncSession,
        make_tenant: Any,
        make_user: Any,
        asset_status: str,
    ) -> None:
        """绑定白名单是 ``{ALLOCATED}``：其余资产状态一律 409 ``DEVICE_NOT_AVAILABLE``。

        ``BOUND`` 变体特意落在单绑分支之后：这里用「资产状态是 BOUND 但
        ``bind_status`` 仍是 UNBOUND、且无绑定行」的形态，避免与
        ``DEVICE_ALREADY_BOUND`` 混淆（后者由 ``bind_status`` 决定）。
        """
        tenant = await make_tenant(code=f"BIND-{asset_status[:6]}", name="状态白名单租户")
        merchant = await _merchant_headers(
            auth, make_user, tenant.id, f"1321{abs(hash(asset_status)) % 10000000:07d}"
        )
        catalog = await _seed_catalog(db, tenant.id, suffix=f"B{asset_status[:6]}")
        device = await _make_device(
            db,
            sn=f"SN-STATUS-{asset_status}",
            tenant_id=tenant.id,
            product_id=catalog["product_id"],
            asset_status=asset_status,
        )

        payload = _jd_payload(tenant.id, catalog["product_id"], device.sn)
        _assert_error(await _precheck(client, merchant, payload), 409, "DEVICE_NOT_AVAILABLE")

    async def test_disabled_product_precheck_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """客户产品停用 → 409 ``PRODUCT_NOT_AUTHORIZED``。"""
        tenant = await make_tenant(code="BIND-DIS", name="停用产品租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000014")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDDIS", sn="SN-DIS-01")

        product = (
            await db.execute(
                select(ClientProduct).where(ClientProduct.id == seeded["product_id"])
            )
        ).scalar_one()
        product.status = str(EnableStatus.DISABLED)
        await db.flush()

        payload = _jd_payload(tenant.id, seeded["product_id"], "SN-DIS-01")
        _assert_error(await _precheck(client, merchant, payload), 409, "PRODUCT_NOT_AUTHORIZED")

    async def test_removed_authorization_precheck_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """删掉 ``product_authorizations`` 行 → 409 ``PRODUCT_NOT_AUTHORIZED``。

        与分配单口径一致（同一个 ``is_product_authorized``）：产品启用但模板
        对该租户未授权，同样不能绑——否则会出现「授权已撤销、设备仍可绑定」。
        """
        tenant = await make_tenant(code="BIND-NOAUTH", name="无授权租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000015")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDNAUTH", sn="SN-NOAUTH-01")

        row = (
            await db.execute(
                select(ProductAuthorization).where(
                    ProductAuthorization.tenant_id == tenant.id,
                    ProductAuthorization.template_id == seeded["template_id"],
                )
            )
        ).scalar_one()
        await db.delete(row)
        await db.flush()

        payload = _jd_payload(tenant.id, seeded["product_id"], "SN-NOAUTH-01")
        _assert_error(await _precheck(client, merchant, payload), 409, "PRODUCT_NOT_AUTHORIZED")

    @pytest.mark.parametrize(
        "payload",
        [
            "JD|x|y|z",  # 段数不足（4 段）
            "JD||||",  # 5 段但字段全空
            "JD|t|p|sn|deadbeef",  # 签名不匹配
            "XX|a|b|c|d",  # 未知前缀
            "JX|SN-ONLY",  # JX 段数不足
            "JD|t|p|sn|" + "0" * 64,  # 伪造 64 位十六进制签名
            "jd|t|p|sn|deadbeef",  # 小写前缀（前缀大小写归一后仍是 JD，签名错）
        ],
    )
    async def test_malformed_payloads_are_qr_invalid(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any, payload: str
    ) -> None:
        """畸形载荷一律 404 ``QR_INVALID``，绝不 500。"""
        tenant = await make_tenant(code="BIND-MAL", name="畸形载荷租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000016")
        _assert_error(await _precheck(client, merchant, payload), 404, "QR_INVALID")

    @pytest.mark.parametrize("payload", ["", "   ", "\n\t "])
    async def test_blank_payload_is_validation_error(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any, payload: str
    ) -> None:
        """空白载荷由请求模型拦下 → 400 ``VALIDATION_ERROR``（不是 500）。"""
        tenant = await make_tenant(code="BIND-BLANK", name="空载荷租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000017")
        _assert_error(await _precheck(client, merchant, payload), 400, "VALIDATION_ERROR")

    async def test_payload_with_surrounding_whitespace_is_normalized(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ 对抗性：换行 / 空白包裹的载荷必须**被接受**（而不是误报无效码）。

        扫码器给出的文本常带换行；``parse_payload`` 与 ``payload_hash`` 的
        ``strip`` 口径必须一致，否则会出现「内容看着一样却校验不过」。
        这是「归一化」的正确行为，不是安全漏洞——签名仍在 strip 后的文本上校验。
        """
        tenant = await make_tenant(code="BIND-WS", name="空白归一租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000019")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDWS", sn="SN-WS-01")
        payload = _jd_payload(tenant.id, seeded["product_id"], "SN-WS-01")

        for wrapped in (f"\n{payload}\n", f"  {payload}  ", f"\t{payload}\r\n"):
            body = await _precheck_ok(client, merchant, wrapped)
            assert body["sn"] == "SN-WS-01"
            assert body["deviceId"] == seeded["device"].id

    async def test_lowercase_prefix_is_tolerated(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """观察项：前缀大小写被归一（``jd|`` 与 ``JD|`` 等价）。

        记录实际行为而不是当成漏洞：前缀是**格式标识**而非字段，
        归一化不改变签名校验对象（字段段仍逐字符比对）。
        报告里登记为「设计选择待产品确认」。
        """
        tenant = await make_tenant(code="BIND-CASE", name="前缀大小写租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000020")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDCASE", sn="SN-CASE-01")
        payload = _jd_payload(tenant.id, seeded["product_id"], "SN-CASE-01")

        lowercased = "jd" + payload[2:]
        body = await _precheck_ok(client, merchant, lowercased)
        assert body["qrFormat"] == "JD"

    async def test_oversized_payload_is_validation_error(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """超过 2048 字符的载荷 → 400（限制在请求模型层，不进入解析逻辑）。"""
        tenant = await make_tenant(code="BIND-BIG", name="超长载荷租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000018")
        _assert_error(await _precheck(client, merchant, "JD|" + "x" * 2100), 400, "VALIDATION_ERROR")


# ===========================================================================
# 二、bind：令牌生命周期
# ===========================================================================


class TestBindTokenLifecycle:
    """令牌「用一次即销毁」「过期即失效」的两条硬规则。"""

    async def test_bind_success_destroys_token_and_writes_events(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ 绑定成功：令牌摘要置空、设备转 BOUND、写 BOUND 事件与审计。"""
        tenant = await make_tenant(code="BIND-LIFE", name="令牌生命周期租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000019")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDLIFE", sn="SN-LIFE-01")
        device = seeded["device"]
        payload = _jd_payload(tenant.id, seeded["product_id"], device.sn)
        pre = await _precheck_ok(client, merchant, payload)

        body = await _bind_ok(
            client, merchant, device.id, pre["confirmToken"], end_user_id="eu-life", remark="试点用户"
        )
        assert body["id"] == pre["bindingId"]
        assert body["status"] == str(BindingRecordStatus.BOUND)
        assert body["statusLabel"] == "已绑定"
        assert body["bindCount"] == 1
        assert body["endUserId"] == "eu-life"
        assert body["boundAt"] is not None
        assert body["unboundAt"] is None
        # 响应体不得出现令牌明文或摘要
        assert pre["confirmToken"] not in str(body)
        for node in _iter_nodes(body):
            assert node not in {"confirmToken", "confirm_token_hash", "confirmTokenHash"}

        row = await _db_binding(db, device.id)
        assert row.status == str(BindingRecordStatus.BOUND)
        assert row.confirm_token_hash is None, "★ 令牌明文/摘要必须销毁"
        assert row.confirm_expires_at is None
        assert row.bound_by is not None
        assert row.bind_count == 1

        stored = (await db.execute(select(Device).where(Device.id == device.id))).scalar_one()
        assert stored.asset_status == str(AssetStatus.BOUND)
        assert stored.bind_status == str(BindStatus.BOUND)
        assert stored.bound_at is not None

        events = list(
            (
                await db.execute(
                    select(DeviceEvent).where(
                        DeviceEvent.device_id == device.id, DeviceEvent.event_type == "BOUND"
                    )
                )
            ).scalars()
        )
        assert len(events) == 1
        assert events[0].dimension == "bind"
        assert events[0].from_status == str(AssetStatus.ALLOCATED)
        assert events[0].to_status == str(AssetStatus.BOUND)

    async def test_reusing_token_after_bind_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ 同一令牌再 bind → 非 200。

        为什么 ``DEVICE_ALREADY_BOUND`` 与 ``QR_EXPIRED`` 都合理？
        ``bind`` 的判定顺序是「单绑 → 令牌状态」：绑定成功后设备
        ``bind_status=BOUND``，因此先命中 ``DEVICE_ALREADY_BOUND``；
        若实现把令牌校验提前，则会命中 ``QR_EXPIRED``（令牌摘要已销毁）。
        两者都正确表达了「这次请求不该成功」，且都不泄漏令牌是否曾有效。
        """
        tenant = await make_tenant(code="BIND-REUSE", name="令牌复用租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000020")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDREUSE", sn="SN-REUSE-01")
        device = seeded["device"]
        payload = _jd_payload(tenant.id, seeded["product_id"], device.sn)
        pre = await _precheck_ok(client, merchant, payload)
        await _bind_ok(client, merchant, device.id, pre["confirmToken"])

        again = await _bind(client, merchant, device.id, pre["confirmToken"])
        assert again.status_code != 200, again.text
        assert again.json()["code"] in {"DEVICE_ALREADY_BOUND", "QR_EXPIRED"}
        assert again.json()["code"] in _ERROR_CODES

        # 库内仍只有一条 BOUND 行、bind_count 没有涨
        row = await _db_binding(db, device.id)
        assert row.bind_count == 1
        assert int(
            (
                await db.execute(
                    select(func.count()).select_from(DeviceBinding).where(
                        DeviceBinding.device_id == device.id
                    )
                )
            ).scalar_one()
        ) == 1

    async def test_old_token_after_unbind_is_expired(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ 解绑后用**旧令牌** bind → 409 ``QR_EXPIRED``（令牌不会因解绑而复活）。"""
        tenant = await make_tenant(code="BIND-OLD", name="旧令牌租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000021")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDOLD", sn="SN-OLD-01")
        device = seeded["device"]
        payload = _jd_payload(tenant.id, seeded["product_id"], device.sn)
        pre = await _precheck_ok(client, merchant, payload)
        await _bind_ok(client, merchant, device.id, pre["confirmToken"])

        unbound = await _unbind(client, merchant, device.id, "终端用户退换货")
        assert unbound.status_code == 200, unbound.text
        assert unbound.json()["status"] == str(BindingRecordStatus.UNBOUND)

        response = await _bind(client, merchant, device.id, pre["confirmToken"])
        _assert_error(response, 409, "QR_EXPIRED")

    async def test_expired_confirm_token_is_rejected_then_reprecheck_works(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ 令牌过期 → 409 ``QR_EXPIRED``；重新 precheck 拿到新令牌并成功绑定。

        时间不靠 ``sleep``：直接把库里的 ``confirm_expires_at`` 拨到过去，
        等价于「时间流逝」，用例耗时为 0。
        """
        tenant = await make_tenant(code="BIND-EXP", name="令牌过期租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000022")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDEXPIRE", sn="SN-EXP-01")
        device = seeded["device"]
        payload = _jd_payload(tenant.id, seeded["product_id"], device.sn)
        pre = await _precheck_ok(client, merchant, payload)

        row = await _db_binding(db, device.id)
        row.confirm_expires_at = utcnow() - timedelta(seconds=5)
        await db.flush()

        expired = await _bind(client, merchant, device.id, pre["confirmToken"])
        _assert_error(expired, 409, "QR_EXPIRED")

        # 过期后重新扫码：签发新令牌，且复用同一行绑定记录
        pre2 = await _precheck_ok(client, merchant, payload)
        assert pre2["confirmToken"] != pre["confirmToken"], "重新扫码必须换发新令牌"
        assert pre2["bindingId"] == pre["bindingId"], "一台设备恒一行绑定记录"

        bound = await _bind_ok(client, merchant, device.id, pre2["confirmToken"])
        assert bound["bindCount"] == 1
        assert int(
            (
                await db.execute(
                    select(func.count()).select_from(DeviceBinding).where(
                        DeviceBinding.device_id == device.id
                    )
                )
            ).scalar_one()
        ) == 1

    async def test_reprecheck_replaces_previous_pending_token(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """再次 precheck 会覆盖摘要：**旧**令牌立即失效（防「留一张备用码」）。"""
        tenant = await make_tenant(code="BIND-REP", name="重签令牌租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000023")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDREP", sn="SN-REP-01")
        device = seeded["device"]
        payload = _jd_payload(tenant.id, seeded["product_id"], device.sn)

        first = await _precheck_ok(client, merchant, payload)
        second = await _precheck_ok(client, merchant, payload)
        assert second["confirmToken"] != first["confirmToken"]

        stale = await _bind(client, merchant, device.id, first["confirmToken"])
        _assert_error(stale, 404, "QR_INVALID")
        await _bind_ok(client, merchant, device.id, second["confirmToken"])

    async def test_wrong_token_on_pending_binding_is_qr_invalid(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """绑定行处于 PENDING 时提交**错误**令牌 → 404 ``QR_INVALID``（区别于过期）。"""
        tenant = await make_tenant(code="BIND-WRONG", name="错误令牌租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000024")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDWRONG", sn="SN-WRONG-01")
        device = seeded["device"]
        payload = _jd_payload(tenant.id, seeded["product_id"], device.sn)
        await _precheck_ok(client, merchant, payload)

        response = await _bind(client, merchant, device.id, "not-the-right-token-at-all")
        _assert_error(response, 404, "QR_INVALID")

    async def test_bind_without_precheck_is_qr_expired(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """从未 precheck（无绑定行）就 bind → 409 ``QR_EXPIRED``。"""
        tenant = await make_tenant(code="BIND-NOPRE", name="未预检租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000025")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDNOPRE", sn="SN-NOPRE-01")

        response = await _bind(
            client, merchant, seeded["device"].id, "some-token-value-1234"
        )
        _assert_error(response, 409, "QR_EXPIRED")

    async def test_bind_cross_tenant_is_404(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ 跨租户令牌重放：拿 A 的令牌、以 B 的身份绑 A 的设备 → 404（不是 403）。

        404 而非 403：错误码本身不能成为「他人资源是否存在」的探测器。
        """
        tenant_a = await make_tenant(code="BIND-TKA", name="令牌跨租户A")
        tenant_b = await make_tenant(code="BIND-TKB", name="令牌跨租户B")
        merchant_a = await _merchant_headers(auth, make_user, tenant_a.id, "13200000026")
        merchant_b = await _merchant_headers(auth, make_user, tenant_b.id, "13200000027")
        seeded = await _allocated_device(db, tenant_a.id, suffix="BINDTKA", sn="SN-TKA-01")
        device = seeded["device"]
        payload = _jd_payload(tenant_a.id, seeded["product_id"], device.sn)
        pre = await _precheck_ok(client, merchant_a, payload)

        response = await _bind(client, merchant_b, device.id, pre["confirmToken"])
        _assert_error(response, 404, "RESOURCE_NOT_FOUND")

        # 设备归属未被改写（越权失败必须真的没发生）
        stored = (await db.execute(select(Device).where(Device.id == device.id))).scalar_one()
        assert stored.tenant_id == tenant_a.id
        assert stored.asset_status == str(AssetStatus.ALLOCATED)

    async def test_unbind_then_rebind_increments_bind_count(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """解绑 → 重新扫码 → 再次绑定：``bind_count`` 累加，用于识别反复解绑重绑。"""
        tenant = await make_tenant(code="BIND-AGAIN", name="重复绑定计数租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000028")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDAGAIN", sn="SN-AGAIN-01")
        device = seeded["device"]
        payload = _jd_payload(tenant.id, seeded["product_id"], device.sn)

        first = await _precheck_ok(client, merchant, payload)
        await _bind_ok(client, merchant, device.id, first["confirmToken"])
        await _unbind(client, merchant, device.id, "换机")

        second = await _precheck_ok(client, merchant, payload)
        body = await _bind_ok(client, merchant, device.id, second["confirmToken"])
        assert body["bindCount"] == 2

        row = await _db_binding(db, device.id)
        assert row.status == str(BindingRecordStatus.BOUND)
        assert row.unbind_reason == "换机"
        assert row.unbound_at is not None
        assert row.unbound_by is not None


# ===========================================================================
# 三、bind 幂等
# ===========================================================================


class TestBindIdempotency:
    """``Idempotency-Key``：同键同体回放，同键异体拒绝。"""

    async def test_replay_returns_same_binding_without_second_bind(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ 同键同体重放 → 200、同一 ``id``、``bindCount`` 不增。

        这条同时钉死「**幂等回放先于令牌校验**」：令牌在首次绑定成功时
        已被销毁，若先校验令牌再查快照，第二次请求会拿到 ``QR_EXPIRED``。
        """
        tenant = await make_tenant(code="BIND-IDEM", name="绑定幂等租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000029")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDIDEM", sn="SN-IDEM-01")
        device = seeded["device"]
        payload = _jd_payload(tenant.id, seeded["product_id"], device.sn)
        pre = await _precheck_ok(client, merchant, payload)
        key = f"it-{uuid.uuid4().hex}"

        first = await _bind_ok(
            client, merchant, device.id, pre["confirmToken"], key=key, end_user_id="eu-idem"
        )
        second = await _bind_ok(
            client, merchant, device.id, pre["confirmToken"], key=key, end_user_id="eu-idem"
        )

        assert second["id"] == first["id"]
        assert second["bindCount"] == first["bindCount"] == 1
        assert second["boundAt"] == first["boundAt"]
        assert second["createdAt"] == first["createdAt"]

        # 库里只有一条行、bind_count 仍是 1（回放绝不能真的再绑一次）
        row = await _db_binding(db, device.id)
        assert row.bind_count == 1
        assert row.status == str(BindingRecordStatus.BOUND)
        assert int(
            (
                await db.execute(
                    select(func.count()).select_from(DeviceBinding).where(
                        DeviceBinding.device_id == device.id
                    )
                )
            ).scalar_one()
        ) == 1
        # BOUND 事件也只有一条（回放没有产生第二次状态迁移）
        assert int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(DeviceEvent)
                    .where(DeviceEvent.device_id == device.id, DeviceEvent.event_type == "BOUND")
                )
            ).scalar_one()
        ) == 1

    async def test_same_key_with_different_body_is_conflict(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """同键**异体** → 409 ``IDEMPOTENCY_CONFLICT``（不能把首次响应错发给另一个请求）。"""
        tenant = await make_tenant(code="BIND-CONF", name="绑定幂等冲突租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000030")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDCONF", sn="SN-CONF-01")
        device = seeded["device"]
        payload = _jd_payload(tenant.id, seeded["product_id"], device.sn)
        pre = await _precheck_ok(client, merchant, payload)
        key = f"it-{uuid.uuid4().hex}"

        await _bind_ok(client, merchant, device.id, pre["confirmToken"], key=key, end_user_id="eu-a")
        conflict = await _bind(
            client, merchant, device.id, pre["confirmToken"], key=key, end_user_id="eu-b"
        )
        _assert_error(conflict, 409, "IDEMPOTENCY_CONFLICT")

        # 冲突请求不得改动库内数据
        row = await _db_binding(db, device.id)
        assert row.end_user_id == "eu-a"
        assert row.bind_count == 1

    async def test_concurrent_bind_same_token_different_keys(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ 对抗性：同一令牌 + **不同**幂等键并发 bind，只能有一次成功。

        断言的是「不会产生两条 BOUND 记录、不会 500」——单绑约束由
        ``device_bindings.device_id`` 唯一索引在库层保证，服务层的
        「先查后写」在并发下必然漏。

        注意：本用例的 ``client`` 通过 ASGITransport 共享**同一个** AsyncSession，
        并发请求会在会话层面交错（SQLAlchemy AsyncSession 非并发安全）。
        因此这里断言的是**产物不变量**（只有一条 BOUND 行、bind_count 为 1），
        而不是「两个响应一个 200 一个 409」——后者依赖会话隔离，属于夹具限制。
        若并发导致 500，那是**测试夹具**的产物而非生产缺陷（生产每个请求
        一个独立会话），见报告中的说明。
        """
        tenant = await make_tenant(code="BIND-RACE", name="并发绑定额租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000031")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDRACE", sn="SN-RACE-01")
        device = seeded["device"]
        payload = _jd_payload(tenant.id, seeded["product_id"], device.sn)
        pre = await _precheck_ok(client, merchant, payload)

        responses = await asyncio.gather(
            _bind(
                client,
                merchant,
                device.id,
                pre["confirmToken"],
                key=f"it-race-a-{uuid.uuid4().hex}",
            ),
            _bind(
                client,
                merchant,
                device.id,
                pre["confirmToken"],
                key=f"it-race-b-{uuid.uuid4().hex}",
            ),
            return_exceptions=True,
        )

        statuses = [
            item.status_code if hasattr(item, "status_code") else "EXC" for item in responses
        ]
        # 不变量 ①：至多一次成功
        assert statuses.count(200) <= 1, f"并发绑定不允许出现两次成功：{statuses}"
        # 不变量 ②：失败侧不得是编造的错误码 / 500
        error_codes: list[str] = []
        for item in responses:
            if hasattr(item, "status_code") and item.status_code != 200:
                body = item.json()
                error_codes.append(str(body.get("code")))
                assert body.get("code") in _ERROR_CODES, item.text
                assert item.status_code < 500, item.text

        # 不变量 ③：库里至多一条 BOUND 行、bind_count 不超过 1
        rows = list(
            (
                await db.execute(
                    select(DeviceBinding).where(DeviceBinding.device_id == device.id)
                )
            ).scalars()
        )
        assert len(rows) == 1
        assert rows[0].bind_count <= 1
        assert rows[0].status in {
            str(BindingRecordStatus.BOUND),
            str(BindingRecordStatus.PENDING),
        }


# ===========================================================================
# 四、数据库层单绑约束
# ===========================================================================


class TestSingleBindingConstraintAtDatabase:
    """单绑约束必须在**库层**成立，而不是只在服务层「先查后写」。"""

    async def test_duplicate_binding_row_raises_integrity_error(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ 直接对库插入第二行同 ``device_id`` 的绑定 → ``IntegrityError``。"""
        tenant = await make_tenant(code="BIND-UNIQ", name="唯一索引租户")
        await _merchant_headers(auth, make_user, tenant.id, "13200000032")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDUNIQ", sn="SN-UNIQ-01")
        device = seeded["device"]

        db.add(
            DeviceBinding(
                id=new_id("device_binding"),
                device_id=device.id,
                tenant_id=tenant.id,
                status=str(BindingRecordStatus.PENDING),
                bind_count=0,
            )
        )
        await db.flush()

        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                db.add(
                    DeviceBinding(
                        id=new_id("device_binding"),
                        device_id=device.id,
                        tenant_id=tenant.id,
                        status=str(BindingRecordStatus.BOUND),
                        bind_count=1,
                    )
                )
                await db.flush()

        # savepoint 回滚后，库内仍只有一行
        assert int(
            (
                await db.execute(
                    select(func.count()).select_from(DeviceBinding).where(
                        DeviceBinding.device_id == device.id
                    )
                )
            ).scalar_one()
        ) == 1


# ===========================================================================
# 五、解绑
# ===========================================================================


class TestUnbind:
    """解绑的校验与留痕。"""

    async def test_unbind_unbound_device_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """未绑定的设备不可解绑 → 409 ``DEVICE_NOT_AVAILABLE``。"""
        tenant = await make_tenant(code="BIND-UNB", name="未绑定解绑租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000033")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDUNB", sn="SN-UNB-01")

        response = await _unbind(client, merchant, seeded["device"].id, "还没绑过")
        _assert_error(response, 409, "DEVICE_NOT_AVAILABLE")

    async def test_unbind_requires_reason(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """解绑原因为空串 → 400 ``VALIDATION_ERROR``。"""
        tenant = await make_tenant(code="BIND-REASON", name="解绑原因租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000034")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDREASON", sn="SN-REASON-01")
        device = seeded["device"]
        payload = _jd_payload(tenant.id, seeded["product_id"], device.sn)
        pre = await _precheck_ok(client, merchant, payload)
        await _bind_ok(client, merchant, device.id, pre["confirmToken"])

        response = await _unbind(client, merchant, device.id, "")
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "VALIDATION_ERROR"

    async def test_whitespace_only_unbind_reason_should_be_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ 期望正确行为：纯空白的原因不算「给了原因」，应 400 ``VALIDATION_ERROR``。

        与 ``test_order_flow.py::test_reject_without_reason_is_rejected`` 的
        变体 2（``rejectReason="   "`` → 400）保持同一口径。

        （本用例原为 ``xfail(strict=True)`` 的观察项：缺陷已由主会话修复，
        故摘掉标记——``BindingUnbindRequest`` 与 P4 的 ``DeviceFreezeRequest`` /
        ``DeviceRetireRequest`` 现在都带 strip 校验器。）
        """
        tenant = await make_tenant(code="BIND-WSR", name="空白原因租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000035")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDWSR", sn="SN-WSR-01")
        device = seeded["device"]
        payload = _jd_payload(tenant.id, seeded["product_id"], device.sn)
        pre = await _precheck_ok(client, merchant, payload)
        await _bind_ok(client, merchant, device.id, pre["confirmToken"])

        response = await _unbind(client, merchant, device.id, "   ")
        assert response.status_code == 400, (
            f"纯空白不应被视为有效解绑原因；实际 [{response.status_code}]：{response.text}"
        )
        assert response.json()["code"] == "VALIDATION_ERROR"

    async def test_unbind_records_audit_fields_and_keeps_bind_count(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """解绑后 ``unbound_at`` / ``unbound_by`` / 原因落库，``bind_count`` 保留。"""
        tenant = await make_tenant(code="BIND-UNBREC", name="解绑留痕租户")
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13200000035")
        seeded = await _allocated_device(db, tenant.id, suffix="BINDUNBREC", sn="SN-UNBREC-01")
        device = seeded["device"]
        payload = _jd_payload(tenant.id, seeded["product_id"], device.sn)
        pre = await _precheck_ok(client, merchant, payload)
        await _bind_ok(client, merchant, device.id, pre["confirmToken"], end_user_id="eu-unb")

        response = await _unbind(client, merchant, device.id, "终端用户退货")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == str(BindingRecordStatus.UNBOUND)
        assert body["statusLabel"] == "已解绑"
        assert body["unbindReason"] == "终端用户退货"
        assert body["unboundAt"] is not None
        assert body["unboundBy"]
        assert body["bindCount"] == 1, "解绑不清零 bind_count（用于识别反复解绑重绑）"
        assert body["endUserId"] == "eu-unb"

        row = await _db_binding(db, device.id)
        assert row.unbound_at is not None
        assert row.unbound_by is not None
        assert row.bind_count == 1

        # 设备退回 ALLOCATED（不是 IN_STOCK）：仍属该租户，只是不再绑定终端用户
        stored = (await db.execute(select(Device).where(Device.id == device.id))).scalar_one()
        assert stored.asset_status == str(AssetStatus.ALLOCATED)
        assert stored.bind_status == str(BindStatus.UNBOUND)

        unbind_events = list(
            (
                await db.execute(
                    select(DeviceEvent).where(
                        DeviceEvent.device_id == device.id, DeviceEvent.event_type == "UNBOUND"
                    )
                )
            ).scalars()
        )
        assert len(unbind_events) == 1
        assert unbind_events[0].dimension == "bind"
        assert unbind_events[0].detail == {"reason": "终端用户退货"}

    async def test_unbind_cross_tenant_is_404(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """跨租户解绑 → 404（越权语义与「不存在」不可区分）。"""
        tenant_a = await make_tenant(code="BIND-UBA", name="解绑跨租户A")
        tenant_b = await make_tenant(code="BIND-UBB", name="解绑跨租户B")
        merchant_a = await _merchant_headers(auth, make_user, tenant_a.id, "13200000036")
        merchant_b = await _merchant_headers(auth, make_user, tenant_b.id, "13200000037")
        seeded = await _allocated_device(db, tenant_a.id, suffix="BINDUBA", sn="SN-UBA-01")
        device = seeded["device"]
        payload = _jd_payload(tenant_a.id, seeded["product_id"], device.sn)
        pre = await _precheck_ok(client, merchant_a, payload)
        await _bind_ok(client, merchant_a, device.id, pre["confirmToken"])

        response = await _unbind(client, merchant_b, device.id, "越权解绑")
        _assert_error(response, 404, "RESOURCE_NOT_FOUND")

        # 越权失败必须真的没发生
        row = await _db_binding(db, device.id)
        assert row.status == str(BindingRecordStatus.BOUND)


# ---------------------------------------------------------------------------
# 递归遍历工具（用于「响应里不得出现令牌 / 摘要」的断言）
# ---------------------------------------------------------------------------


def _iter_nodes(node: Any) -> list[Any]:
    """递归展开 JSON 结构（dict / list / 标量）。"""
    if isinstance(node, dict):
        nodes: list[Any] = []
        for key, value in node.items():
            nodes.append(key)
            nodes.extend(_iter_nodes(value))
        return nodes
    if isinstance(node, list):
        nodes = []
        for item in node:
            nodes.extend(_iter_nodes(item))
        return nodes
    return [node]
