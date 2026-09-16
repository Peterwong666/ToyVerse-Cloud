"""集成测试：P5 设备密钥签发与心跳（在线窗口的写入侧 + 读取侧口径）。

本文件是独立验证者的产物：用测试去**证伪** ``device_service`` /
``heartbeat_service`` 的实现，而不是给它背书。

覆盖的验收标准
--------------
* ① **密钥签发**：201、明文仅一次返回、库内只存 64 位 hex 摘要
  （**断言库里没有明文**）、``secretHint`` 为前 4 位 + ``****``、
  同 ``(deviceId, credentialType)`` 续签是**覆盖**而非新增（仍只有 1 行）、
  旧密钥立即失效。
* ② **真实心跳**（设备侧、**不挂 JWT**）：200、``onlineStatus=ONLINE``、
  ``simulated=false``、``lastHeartbeatAt`` 更新、固件版本回填。
* ③ **负路径**：错误密钥 401 ``UNAUTHENTICATED``、未知 SN 404
  ``DEVICE_NOT_FOUND``、缺字段 400、已冻结 / 已报废设备的心跳实际行为。
* ④ ★ **180 秒窗口口径**：心跳后把 ``last_heartbeat_at`` 拨到窗口外
  （库级造数，不真的睡 3 分钟）→ 派生布尔 ``online=false``、
  ``GET /platform/devices/stats`` 把它从 ``ONLINE`` 归入 ``OFFLINE``、
  ``?onlineStatus=ONLINE`` 筛选返回 0 条。
* ⑤ ★ **落库投影与派生结论可以不一致**——这是设计意图，写成显式断言，
  避免以后被「顺手修掉」。
* ⑥ **窗口边界**（``lastHeartbeatAt == now - 180s``）的实测行为。
* ⑦ **模拟心跳**：``simulated=true``、``online=false`` 可模拟窗口超时并写
  ``OFFLINE`` 事件、模拟心跳**写审计**（``SIMULATE_HEARTBEAT``）而
  真实心跳**不写**审计。

写法对齐 ``tests/integration/test_binding.py``：模块内自定义 helper、
camelCase 断言、库级造数，中文注释说明「这里坏了说明哪条业务规则被破坏」。
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id
from app.db.base import utcnow
from app.models.audit import AuditLog
from app.models.device import Device, DeviceCredential, DeviceEvent
from app.models.enums import (
    ActivationStatus,
    AssetStatus,
    AuditAction,
    BindStatus,
    CredentialType,
    OnlineStatus,
)
from app.services import device_service
from tests.conftest import API_PREFIX

pytestmark = pytest.mark.integration

PLATFORM = f"{API_PREFIX}/platform"
DEVICE = f"{API_PREFIX}/device"

#: 商户测试账号口令（仅存在于测试进程）
MERCHANT_PASSWORD = "Merch4nt-Hb#2026"

#: 项目统一错误码集合（用于钉死「不返回编造的错误码」）
_ERROR_CODES = {
    "UNAUTHENTICATED",
    "PERMISSION_DENIED",
    "VALIDATION_ERROR",
    "RESOURCE_NOT_FOUND",
    "DEVICE_NOT_AVAILABLE",
    "DEVICE_NOT_FOUND",
    "DEVICE_FROZEN",
    "DEVICE_NOT_IN_TENANT",
    "INVALID_STATE_TRANSITION",
}


# ---------------------------------------------------------------------------
# 模块级辅助
# ---------------------------------------------------------------------------


async def _make_device(
    db: AsyncSession,
    *,
    sn: str,
    asset_status: str = str(AssetStatus.IN_STOCK),
    online_status: str = str(OnlineStatus.NEVER_ONLINE),
    last_heartbeat_at: Any = None,
    tenant_id: str | None = None,
) -> Device:
    """直接落一台设备（库级造数，避免造数过程本身成为变量）。"""
    device = Device(
        id=new_id("device"),
        sn=sn,
        tenant_id=tenant_id,
        network_type="WIFI",
        asset_status=asset_status,
        activation_status=str(ActivationStatus.NOT_ACTIVATED),
        online_status=online_status,
        bind_status=str(BindStatus.UNBOUND),
        last_heartbeat_at=last_heartbeat_at,
        generated_at=utcnow(),
    )
    db.add(device)
    await db.flush()
    return device


async def _issue_credential(
    client: AsyncClient,
    headers: dict[str, str],
    device_id: str,
    *,
    credential_type: str | None = None,
    expires_in_days: int | None = None,
) -> Any:
    """调签发设备密钥（不预设状态码）。"""
    body: dict[str, Any] = {}
    if credential_type is not None:
        body["credentialType"] = credential_type
    if expires_in_days is not None:
        body["expiresInDays"] = expires_in_days
    return await client.post(
        f"{PLATFORM}/devices/{device_id}/credentials", headers=headers, json=body
    )


async def _issue_ok(
    client: AsyncClient, headers: dict[str, str], device_id: str, **kwargs: Any
) -> dict[str, Any]:
    """签发设备密钥并断言 201，返回响应体。"""
    response = await _issue_credential(client, headers, device_id, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()


async def _heartbeat(
    client: AsyncClient,
    sn: str,
    secret: str | None,
    *,
    firmware_version: str | None = None,
    with_jwt: dict[str, str] | None = None,
    omit_sn: bool = False,
) -> Any:
    """调设备侧心跳（不预设状态码）。

    ``with_jwt`` 用于验证「带不带 JWT 都一样」——设备端点在设计上不使用 JWT。
    """
    body: dict[str, Any] = {}
    if not omit_sn:
        body["sn"] = sn
    if secret is not None:
        body["secret"] = secret
    if firmware_version is not None:
        body["firmwareVersion"] = firmware_version
    return await client.post(f"{DEVICE}/heartbeat", json=body, headers=with_jwt)


async def _heartbeat_ok(
    client: AsyncClient, sn: str, secret: str, **kwargs: Any
) -> dict[str, Any]:
    response = await _heartbeat(client, sn, secret, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


async def _simulate(
    client: AsyncClient,
    headers: dict[str, str],
    device_id: str,
    *,
    online: bool = True,
    firmware_version: str | None = None,
) -> Any:
    """调平台模拟心跳（不预设状态码）。"""
    body: dict[str, Any] = {"online": online}
    if firmware_version is not None:
        body["firmwareVersion"] = firmware_version
    return await client.post(
        f"{PLATFORM}/devices/{device_id}/simulate-heartbeat", headers=headers, json=body
    )


async def _stats(client: AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    """取设备统计。"""
    response = await client.get(f"{PLATFORM}/devices/stats", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _device_detail(
    client: AsyncClient, headers: dict[str, str], device_id: str
) -> dict[str, Any]:
    response = await client.get(f"{PLATFORM}/devices/{device_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _reload_device(db: AsyncSession, device_id: str) -> Device:
    return (await db.execute(select(Device).where(Device.id == device_id))).scalar_one()


async def _credential_rows(db: AsyncSession, device_id: str) -> list[DeviceCredential]:
    return list(
        (
            await db.execute(
                select(DeviceCredential).where(DeviceCredential.device_id == device_id)
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


async def _audit_count(db: AsyncSession, device_id: str, action: str | None = None) -> int:
    """统计以该设备为资源对象的审计条数（可按动作过滤）。"""
    stmt = select(func.count()).select_from(AuditLog).where(AuditLog.resource_id == device_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return int((await db.execute(stmt)).scalar_one())


def _iter_nodes(node: Any) -> list[Any]:
    """递归展开 JSON 结构，返回全部键名与标量值（用于「不得出现明文」的断言）。"""
    nodes: list[Any] = []
    if isinstance(node, dict):
        for key, value in node.items():
            nodes.append(key)
            nodes.extend(_iter_nodes(value))
    elif isinstance(node, list):
        for item in node:
            nodes.extend(_iter_nodes(item))
    else:
        nodes.append(node)
    return nodes


def _assert_no_plaintext(payload: Any, secret: str, *, where: str) -> None:
    """响应里既不得出现明文密钥，也不得有名为 ``secret`` 的字段。"""
    for node in _iter_nodes(payload):
        if isinstance(node, str):
            assert secret not in node, f"{where}：响应出现明文密钥"
            assert node != "secret", f"{where}：响应出现 secret 字段名"


def _assert_error(response: Any, status: int, code: str) -> None:
    """断言失败响应的状态码与错误码，并顺带钉死错误码契约。"""
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code, response.text
    assert body["code"] in _ERROR_CODES, f"返回了未登记的错误码：{body['code']}"
    assert isinstance(body["message"], str) and body["message"]
    assert "traceId" in body


async def _merchant_headers(
    auth: Any, make_user: Any, tenant_id: str, account: str
) -> dict[str, str]:
    """建一个商户管理员并登录，返回鉴权头（用于「平台独占」的越权断言）。"""
    await make_user(
        account=account,
        password=MERCHANT_PASSWORD,
        role_code="MERCHANT_ADMIN",
        tenant_id=tenant_id,
    )
    return auth.headers(await auth.token(account, MERCHANT_PASSWORD))


# ===========================================================================
# 一、设备密钥签发
# ===========================================================================


class TestIssueCredential:
    """明文只出一次、库里只有摘要、续签即轮换。"""

    async def test_issue_returns_plaintext_once_and_stores_only_digest(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """★ 201 + 明文仅一次 + 库内 64 位 hex 摘要 + ``secretHint`` = 前 4 位 + ****。

        这里坏了说明两种事故之一：
        ① 库内落了明文（一次数据库泄露就等于所有设备密钥泄露）；
        ② 掩码暴露过多（掩码是给运维核对的，不应泄漏超过 4 位）。
        """
        device = await _make_device(db, sn="SN-HB-ISSUE-01")
        headers = await auth.platform_headers()

        body = await _issue_ok(client, headers, device.id)

        secret = body["secret"]
        credential = body["credential"]
        assert isinstance(secret, str) and len(secret) >= 20, "明文密钥必须有足够熵"
        assert credential["credentialType"] == str(CredentialType.DEVICE_SECRET)
        assert credential["algorithm"] == "sha256"
        assert credential["secretHint"] == f"{secret[:4]}****"
        assert credential["expiresAt"] is None, "未指定 expiresInDays 时长期有效"
        assert credential["issuedAt"] is not None
        assert credential["revokedAt"] is None

        rows = await _credential_rows(db, device.id)
        assert len(rows) == 1
        row = rows[0]
        assert len(row.secret_hash) == 64, "摘要必须是 SHA-256 的 64 位 hex"
        assert row.secret_hash == hashlib.sha256(secret.encode("utf-8")).hexdigest()
        assert row.secret_hash != secret
        # ★ 逐列扫一遍：库内任何字段都不得含明文
        for column in DeviceCredential.__table__.columns:
            value = getattr(row, column.name, None)
            if isinstance(value, str):
                assert secret not in value, f"库内 {column.name} 出现明文密钥"

    async def test_detail_endpoint_never_exposes_plaintext_or_hash(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """设备详情只出掩码：既无明文，也无摘要，更无 ``secret`` 字段名。

        这里坏了说明：把可离线爆破的摘要或明文带到了普通查询接口上——
        「明文只出一次」的承诺当场失效。
        """
        device = await _make_device(db, sn="SN-HB-ISSUE-02")
        headers = await auth.platform_headers()
        secret = (await _issue_ok(client, headers, device.id))["secret"]

        detail = await _device_detail(client, headers, device.id)
        assert len(detail["credentials"]) == 1
        assert detail["credentials"][0]["secretHint"] == f"{secret[:4]}****"
        _assert_no_plaintext(detail, secret, where="设备详情")

        row = (await _credential_rows(db, device.id))[0]
        for node in _iter_nodes(detail):
            if isinstance(node, str):
                assert node not in {"secretHash", "secret_hash"}, "摘要不得下发"
                assert row.secret_hash not in node

    async def test_reissue_overwrites_row_and_invalidates_old_secret(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """★ 续签 = 覆盖同一行 + 旧密钥立即失效（设备丢失后的处置动作）。

        这里坏了说明：旧密钥仍然可用（丢了的那台设备还能继续上报），
        或续签新增了一行（心跳校验时不知道用哪一条）。
        """
        device = await _make_device(db, sn="SN-HB-ROTATE-01")
        headers = await auth.platform_headers()

        first = await _issue_ok(client, headers, device.id)
        second = await _issue_ok(client, headers, device.id)

        assert second["secret"] != first["secret"]
        assert second["credential"]["id"] == first["credential"]["id"], "同一行被覆盖"
        assert second["credential"]["secretHint"] == f"{second['secret'][:4]}****"
        assert len(await _credential_rows(db, device.id)) == 1, "一台设备一个设备密钥"

        # 旧密钥立即失效（心跳侧鉴权失败）
        _assert_error(
            await _heartbeat(client, device.sn, first["secret"]), 401, "UNAUTHENTICATED"
        )
        # 新密钥可用
        assert (await _heartbeat_ok(client, device.sn, second["secret"]))["onlineStatus"] == (
            str(OnlineStatus.ONLINE)
        )

    async def test_reissue_clears_revoked_state(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """先吊销、后重新签发 → 新密钥必须可用（否则设备侧「修不好」）。"""
        device = await _make_device(db, sn="SN-HB-REVOKE-01")
        headers = await auth.platform_headers()
        await _issue_ok(client, headers, device.id)

        row = (await _credential_rows(db, device.id))[0]
        row.revoked_at = utcnow()
        await db.flush()
        _assert_error(
            await _heartbeat(client, device.sn, "any-wrong-secret-1"), 401, "UNAUTHENTICATED"
        )

        fresh = await _issue_ok(client, headers, device.id)
        assert fresh["credential"]["revokedAt"] is None, "重新签发必须解除吊销状态"
        assert (await _heartbeat_ok(client, device.sn, fresh["secret"]))["online"] is True

    async def test_expired_credential_is_unauthenticated(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """已过期密钥 → 401（有效期是签发时的明确承诺，不能静默延长）。"""
        device = await _make_device(db, sn="SN-HB-EXP-01")
        headers = await auth.platform_headers()
        body = await _issue_ok(client, headers, device.id, expires_in_days=1)
        assert body["credential"]["expiresAt"] is not None

        row = (await _credential_rows(db, device.id))[0]
        row.expires_at = utcnow() - timedelta(seconds=1)
        await db.flush()

        _assert_error(
            await _heartbeat(client, device.sn, body["secret"]), 401, "UNAUTHENTICATED"
        )

    async def test_unsupported_credential_type_is_validation_error(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """不支持的凭证类型 → 400（否则库里会积累永远匹配不上的垃圾类型）。"""
        device = await _make_device(db, sn="SN-HB-TYPE-01")
        headers = await auth.platform_headers()

        response = await _issue_credential(
            client, headers, device.id, credential_type="NOT_A_TYPE"
        )
        _assert_error(response, 400, "VALIDATION_ERROR")
        assert await _credential_rows(db, device.id) == []

    async def test_unknown_device_is_not_found(
        self, client: AsyncClient, auth: Any
    ) -> None:
        """设备不存在 → 404 ``DEVICE_NOT_FOUND``（设备域专用码，不签发悬空凭证）。"""
        headers = await auth.platform_headers()
        response = await _issue_credential(client, headers, new_id("device"))
        _assert_error(response, 404, "DEVICE_NOT_FOUND")

    @pytest.mark.parametrize("path_kind", ["credentials", "simulate"])
    async def test_merchant_cannot_issue_or_simulate(
        self,
        client: AsyncClient,
        auth: Any,
        db: AsyncSession,
        make_tenant: Any,
        make_user: Any,
        path_kind: str,
    ) -> None:
        """★ 商户 token 访问签发密钥 / 模拟心跳 → 403（平台独占）。

        这里坏了说明：商户可以给自己签设备密钥或伪造设备在线——
        两者都会让设备侧的数据彻底失去可信度。
        """
        tenant = await make_tenant(code="HB-DENY", name="心跳越权租户")
        device = await _make_device(db, sn="SN-HB-DENY-01", tenant_id=tenant.id)
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13230000001")

        if path_kind == "credentials":
            response = await _issue_credential(client, merchant, device.id)
        else:
            response = await _simulate(client, merchant, device.id, online=True)
        _assert_error(response, 403, "PERMISSION_DENIED")


# ===========================================================================
# 二、真实心跳（设备侧，无 JWT）
# ===========================================================================


class TestRealHeartbeat:
    """心跳把投影推到 ONLINE 并回填固件版本；只在状态真变时写事件。"""

    async def test_heartbeat_marks_online_and_backfills_firmware(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """★ 200 + ``ONLINE`` + ``simulated=false`` + 心跳时间与固件版本落库。

        这里坏了说明：真机上报被当成模拟上报（``simulated=true`` 会让人
        无法区分演示数据与设备事实），或固件版本没有同步（设备详情永远
        显示出厂版本），或在线状态没有推进（设备明明活着却显示从未在线）。
        """
        device = await _make_device(db, sn="SN-HB-REAL-01")
        headers = await auth.platform_headers()
        secret = (await _issue_ok(client, headers, device.id))["secret"]

        body = await _heartbeat_ok(
            client, device.sn, secret, firmware_version="1.2.3"
        )

        assert body["deviceId"] == device.id
        assert body["sn"] == device.sn
        assert body["onlineStatus"] == str(OnlineStatus.ONLINE)
        assert body["online"] is True
        assert body["simulated"] is False, "真实心跳绝不能被标成模拟"
        assert body["lastHeartbeatAt"] is not None
        assert body["onlineWindowSeconds"] == 180, "固件据此自适应上报间隔"
        assert body["heartbeatIntervalSeconds"] == 30
        assert body["serverTime"] is not None

        stored = await _reload_device(db, device.id)
        assert stored.online_status == str(OnlineStatus.ONLINE)
        assert stored.last_heartbeat_at is not None
        assert stored.firmware_version == "1.2.3"
        assert device_service.is_online(stored) is True

        credential = (await _credential_rows(db, device.id))[0]
        assert credential.last_used_at is not None, "心跳要记录密钥最后使用时间"

        events = await _events(db, device.id, "ONLINE")
        assert len(events) == 1
        assert events[0].dimension == "online"
        assert events[0].from_status == str(OnlineStatus.NEVER_ONLINE)
        assert events[0].to_status == str(OnlineStatus.ONLINE)

    async def test_second_heartbeat_does_not_duplicate_online_event(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """★ 状态没变就不写事件：两次心跳只留一条 ``ONLINE``。

        这里坏了说明：每 30 秒一条事件会把设备时间线淹掉，
        「真正的状态变化」再也看不见（这是高频写入端点的核心取舍）。
        """
        device = await _make_device(db, sn="SN-HB-REAL-02")
        headers = await auth.platform_headers()
        secret = (await _issue_ok(client, headers, device.id))["secret"]

        await _heartbeat_ok(client, device.sn, secret)
        await _heartbeat_ok(client, device.sn, secret)

        assert len(await _events(db, device.id, "ONLINE")) == 1
        stored = await _reload_device(db, device.id)
        assert stored.online_status == str(OnlineStatus.ONLINE)

    async def test_heartbeat_needs_no_jwt_but_works_with_one(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """★ 设备端点**不使用 JWT**：不带 ``Authorization`` 必须能上报。

        这里坏了说明：真实设备永远上报不了心跳（设备没有账号、登不了录），
        这是「接口能跑通、设备用不了」的典型形态。
        顺带验证「带了无关的 JWT 也不影响」——鉴权只认设备密钥。
        """
        device = await _make_device(db, sn="SN-HB-NOJWT-01")
        headers = await auth.platform_headers()
        secret = (await _issue_ok(client, headers, device.id))["secret"]

        # 不带 Authorization
        body = await _heartbeat_ok(client, device.sn, secret)
        assert body["onlineStatus"] == str(OnlineStatus.ONLINE)

        # 带平台 JWT（不应改变结果，也不应被当成平台操作）
        second = await _heartbeat_ok(client, device.sn, secret, with_jwt=headers)
        assert second["simulated"] is False
        assert await _audit_count(db, device.id) == 0, "真实心跳不写审计"

    async def test_wrong_secret_is_unauthenticated(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """错误密钥 → 401 ``UNAUTHENTICATED``，且不得推进任何在线状态。

        这里坏了说明：只要知道 SN（印在机身与包装上，不算秘密）就能把
        任意设备刷成在线。
        """
        device = await _make_device(db, sn="SN-HB-WRONG-01")
        headers = await auth.platform_headers()
        await _issue_ok(client, headers, device.id)

        _assert_error(
            await _heartbeat(client, device.sn, "totally-wrong-secret"), 401, "UNAUTHENTICATED"
        )
        stored = await _reload_device(db, device.id)
        assert stored.online_status == str(OnlineStatus.NEVER_ONLINE)
        assert stored.last_heartbeat_at is None

    async def test_unknown_sn_is_device_not_found(
        self, client: AsyncClient
    ) -> None:
        """未知 SN → 404 ``DEVICE_NOT_FOUND``（先查设备再比密钥，故不是 401）。"""
        _assert_error(
            await _heartbeat(client, "SN-NOT-EXIST-AT-ALL", "any-secret-value-1"),
            404,
            "DEVICE_NOT_FOUND",
        )

    async def test_device_without_credential_is_unauthenticated(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """从未签发密钥的设备 → 401（不能让「没配密钥」退化成「免鉴权」）。"""
        device = await _make_device(db, sn="SN-HB-NOCRED-01")
        _assert_error(
            await _heartbeat(client, device.sn, "any-secret-value-1"), 401, "UNAUTHENTICATED"
        )

    @pytest.mark.parametrize(
        ("secret", "omit_sn"),
        [
            (None, False),  # 缺 secret
            ("short", False),  # secret 太短（min_length=8）
            ("any-secret-value-1", True),  # 缺 sn
        ],
        ids=["缺 secret", "secret 过短", "缺 sn"],
    )
    async def test_missing_fields_are_validation_errors(
        self, client: AsyncClient, secret: str | None, omit_sn: bool
    ) -> None:
        """缺字段 / 过短 → 400 ``VALIDATION_ERROR``（不是 500，也不是静默放过）。"""
        response = await _heartbeat(
            client, "SN-HB-BADBODY-01", secret, omit_sn=omit_sn
        )
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "VALIDATION_ERROR"

    async def test_frozen_device_heartbeat_is_rejected_after_auth(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """★ 已冻结设备心跳 → 409 ``DEVICE_FROZEN``；但**先认证再判状态**。

        这里坏了说明两种规则之一被破坏：
        ① 冻结形同虚设（停服的设备仍在上报在线）；
        ② 状态判断跑到了认证之前——未通过认证的调用方能靠错误码
           （409 vs 401）判断出「这台设备存在且已冻结」，属于信息泄漏。
        """
        device = await _make_device(
            db, sn="SN-HB-FROZEN-01", asset_status=str(AssetStatus.FROZEN)
        )
        headers = await auth.platform_headers()
        secret = (await _issue_ok(client, headers, device.id))["secret"]

        _assert_error(await _heartbeat(client, device.sn, secret), 409, "DEVICE_FROZEN")
        stored = await _reload_device(db, device.id)
        assert stored.last_heartbeat_at is None, "被拒的心跳不得写入心跳时间"

        # ★ 认证优先：密钥错误时必须是 401（而不是暴露「它已冻结」的 409）
        _assert_error(
            await _heartbeat(client, device.sn, "wrong-secret-value-1"),
            401,
            "UNAUTHENTICATED",
        )

    async def test_retired_device_heartbeat_is_rejected(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """已报废设备心跳 → 409 ``DEVICE_NOT_AVAILABLE``（报废是终态）。"""
        device = await _make_device(
            db, sn="SN-HB-RETIRED-01", asset_status=str(AssetStatus.RETIRED)
        )
        headers = await auth.platform_headers()
        secret = (await _issue_ok(client, headers, device.id))["secret"]

        _assert_error(
            await _heartbeat(client, device.sn, secret), 409, "DEVICE_NOT_AVAILABLE"
        )


# ===========================================================================
# 三、★ 180 秒在线窗口
# ===========================================================================


class TestOnlineWindow:
    """窗口是**单一事实来源**；落库的 ``online_status`` 只是投影。"""

    async def test_stale_projection_is_reported_offline_everywhere(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """★ 心跳后把 ``last_heartbeat_at`` 拨到窗口外 → 详情派生离线、
        统计归入 ``OFFLINE``、``?onlineStatus=ONLINE`` 返回 0 条。

        这里坏了说明三条读取口径（展示 / 统计 / 筛选）不一致，会出现
        「列表说在线、统计说离线」这类无法向用户解释的矛盾。
        用库级改时间等价于「时间流逝」，用例耗时为 0。
        """
        device = await _make_device(db, sn="SN-HB-STALE-01")
        headers = await auth.platform_headers()
        secret = (await _issue_ok(client, headers, device.id))["secret"]
        await _heartbeat_ok(client, device.sn, secret)

        # 心跳刚发生：三处一致认为在线
        fresh_detail = await _device_detail(client, headers, device.id)
        assert fresh_detail["online"] is True
        assert fresh_detail["onlineStatus"] == str(OnlineStatus.ONLINE)
        stats_before = await _stats(client, headers)
        online_before = stats_before["byOnlineStatus"].get(str(OnlineStatus.ONLINE), 0)
        offline_before = stats_before["byOnlineStatus"].get(str(OnlineStatus.OFFLINE), 0)
        online_filter_before = await client.get(
            f"{PLATFORM}/devices",
            headers=headers,
            params={"onlineStatus": "ONLINE", "keyword": device.sn},
        )
        assert online_filter_before.json()["total"] == 1

        # ★ 时间流逝：心跳拨到窗口外（181 秒前），**投影刻意不动**
        stored = await _reload_device(db, device.id)
        stored.last_heartbeat_at = utcnow() - timedelta(seconds=181)
        await db.flush()

        stale_detail = await _device_detail(client, headers, device.id)
        assert stale_detail["online"] is False, "派生结论必须以心跳窗口为准"

        stats_after = await _stats(client, headers)
        assert stats_after["byOnlineStatus"].get(str(OnlineStatus.ONLINE), 0) == online_before - 1
        assert stats_after["byOnlineStatus"].get(str(OnlineStatus.OFFLINE), 0) == offline_before + 1

        online_filter_after = await client.get(
            f"{PLATFORM}/devices",
            headers=headers,
            params={"onlineStatus": "ONLINE", "keyword": device.sn},
        )
        assert online_filter_after.status_code == 200, online_filter_after.text
        assert online_filter_after.json()["total"] == 0, "超窗设备不得再被筛成在线"

        offline_filter = await client.get(
            f"{PLATFORM}/devices",
            headers=headers,
            params={"onlineStatus": "OFFLINE", "keyword": device.sn},
        )
        assert offline_filter.json()["total"] == 1, (
            "「投影 ONLINE 但已超窗」必须能被 OFFLINE 筛出，否则它从两个筛选里同时消失"
        )

    async def test_projection_and_derived_verdict_may_disagree_by_design(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """★ 落库的 ``online_status`` 与派生结论 ``online`` **可以不一致**。

        这是设计意图，不是缺陷：``online_status`` 是历史投影（心跳写入 /
        离线模拟改写），它不会自己随时间变化；``online`` 才是在线判据的
        结论。``onlineStatus`` 只用于定位与四维标签，**结论以窗口为准**。

        本用例的存在就是为了防止将来有人「顺手把枚举也一起改掉」而破坏
        这条契约——那会引入两个会互相漂移的真相来源。
        """
        device = await _make_device(db, sn="SN-HB-DISAGREE-01")
        headers = await auth.platform_headers()
        secret = (await _issue_ok(client, headers, device.id))["secret"]
        await _heartbeat_ok(client, device.sn, secret)

        stored = await _reload_device(db, device.id)
        stored.last_heartbeat_at = utcnow() - timedelta(seconds=181)
        await db.flush()

        detail = await _device_detail(client, headers, device.id)
        assert detail["onlineStatus"] == str(OnlineStatus.ONLINE), (
            "落库投影保持 ONLINE（枚举不随时间变化，这正是它不能作为结论的原因）"
        )
        assert detail["online"] is False, "结论以 180 秒窗口为准"

        # 库内投影也确实还是 ONLINE（不是序列化层造出来的假象）
        assert (await _reload_device(db, device.id)).online_status == str(OnlineStatus.ONLINE)

    async def test_heartbeat_restores_online_after_window_expiry(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """对抗性反证：超窗设备再上报一次心跳 → 立刻恢复在线并写 ``ONLINE`` 事件。

        没有这条，「超窗即离线」可能只是「一旦离线就再也回不来」的实现错误。
        """
        device = await _make_device(db, sn="SN-HB-RECOVER-01")
        headers = await auth.platform_headers()
        secret = (await _issue_ok(client, headers, device.id))["secret"]
        await _heartbeat_ok(client, device.sn, secret)

        stored = await _reload_device(db, device.id)
        stored.last_heartbeat_at = utcnow() - timedelta(seconds=181)
        await db.flush()
        assert (await _device_detail(client, headers, device.id))["online"] is False

        body = await _heartbeat_ok(client, device.sn, secret)
        assert body["online"] is True
        assert body["onlineStatus"] == str(OnlineStatus.ONLINE)
        assert len(await _events(db, device.id, "ONLINE")) == 1, (
            "投影本来就是 ONLINE，因此不新增事件（事件只在状态真变时写）"
        )

    async def test_window_boundary_behavior_is_documented(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """★ 窗口边界实测：``lastHeartbeatAt == now - 180s`` 判为**离线**。

        实现的判据是 ``last_heartbeat_at >= now - ONLINE_WINDOW_SECONDS``
        （左闭右开：窗口内的心跳算在线）。但「恰好 180.000 秒前」这个点
        在真实时钟下不可观测——写入边界值的时刻与比较的时刻必然相差
        若干微秒，比较时窗口已经又往前走了一点，于是恰好落在旧边界上的
        心跳被算作出窗 → **离线**。

        本用例把这个实测结论钉住（两侧 179s / 181s 也一并钉死，
        避免「窗口宽度写错」这类问题被边界用例掩盖）。
        """
        device = await _make_device(db, sn="SN-HB-EDGE-01")
        headers = await auth.platform_headers()
        await _issue_ok(client, headers, device.id)
        stored = await _reload_device(db, device.id)

        # 窗口内侧 1 秒 → 在线
        stored.last_heartbeat_at = utcnow() - timedelta(seconds=179)
        await db.flush()
        stored = await _reload_device(db, device.id)
        assert device_service.is_online(stored) is True
        assert (await _device_detail(client, headers, device.id))["online"] is True

        # 恰好在边界上（now - 180s）→ 实测为离线
        stored.last_heartbeat_at = device_service.online_cutoff()
        await db.flush()
        stored = await _reload_device(db, device.id)
        assert device_service.is_online(stored) is False, (
            "实测：恰好落在左边界的旧心跳在比较时已经出窗（窗口左闭右开）"
        )
        assert (await _device_detail(client, headers, device.id))["online"] is False

        # 窗口外侧 1 秒 → 离线
        stored.last_heartbeat_at = utcnow() - timedelta(seconds=181)
        await db.flush()
        stored = await _reload_device(db, device.id)
        assert device_service.is_online(stored) is False

    async def test_merchant_device_list_uses_derived_window(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """商户端「我的设备」同样以窗口为准（不是各端一套口径）。

        这里坏了说明：商户端仍按落库枚举展示，掉线设备会长期显示「在线」，
        平台端与商户端对同一台设备给出两个答案。
        """
        tenant = await make_tenant(code="HB-MERCH", name="商户在线口径租户")
        device = await _make_device(db, sn="SN-HB-MERCH-01", tenant_id=tenant.id)
        headers = await auth.platform_headers()
        secret = (await _issue_ok(client, headers, device.id))["secret"]
        merchant = await _merchant_headers(auth, make_user, tenant.id, "13230000002")
        await _heartbeat_ok(client, device.sn, secret)

        listed = await client.get(
            f"{API_PREFIX}/merchant/devices", headers=merchant, params={"keyword": device.sn}
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["records"][0]["online"] is True

        stored = await _reload_device(db, device.id)
        stored.last_heartbeat_at = utcnow() - timedelta(seconds=181)
        await db.flush()

        stale = await client.get(
            f"{API_PREFIX}/merchant/devices", headers=merchant, params={"keyword": device.sn}
        )
        record = stale.json()["records"][0]
        assert record["online"] is False
        assert record["onlineStatus"] == str(OnlineStatus.ONLINE), "投影未变（枚举不随时间变）"

        filtered = await client.get(
            f"{API_PREFIX}/merchant/devices",
            headers=merchant,
            params={"keyword": device.sn, "onlineStatus": "ONLINE"},
        )
        assert filtered.json()["total"] == 0


# ===========================================================================
# 四、模拟心跳与审计
# ===========================================================================


class TestSimulateHeartbeat:
    """模拟心跳（``simulated=true``）：写审计、能模拟超窗、受状态拦截。"""

    async def test_simulate_online_marks_online_and_writes_audit(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """★ 模拟心跳 → ``simulated=true`` + 写 ``SIMULATE_HEARTBEAT`` 审计。

        这里坏了说明两种事故之一：
        ① 模拟心跳被标成真实上报（演示数据会被当成设备事实）；
        ② 模拟没留痕（有人可以手工把设备刷成在线且查不到是谁干的）。
        """
        device = await _make_device(db, sn="SN-HB-SIM-01")
        headers = await auth.platform_headers()

        assert await _audit_count(db, device.id) == 0

        response = await _simulate(client, headers, device.id, online=True)
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["simulated"] is True
        assert body["online"] is True
        assert body["onlineStatus"] == str(OnlineStatus.ONLINE)
        assert body["deviceId"] == device.id

        assert await _audit_count(db, device.id, str(AuditAction.SIMULATE_HEARTBEAT)) == 1
        audit = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.resource_id == device.id,
                    AuditLog.action == str(AuditAction.SIMULATE_HEARTBEAT),
                )
            )
        ).scalar_one()
        assert audit.detail is not None
        assert audit.detail["simulated"] is True
        assert audit.detail["toStatus"] == str(OnlineStatus.ONLINE)

    async def test_real_heartbeat_writes_no_audit(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """★ 真实心跳**不写审计**：心跳是每 30 秒一次的无人工决策写入。

        这里坏了说明：真正需要人工决策的动作被心跳噪声淹掉，
        审计表还会以每设备每天约 2880 条的速度膨胀。
        """
        device = await _make_device(db, sn="SN-HB-NOAUDIT-01")
        headers = await auth.platform_headers()
        secret = (await _issue_ok(client, headers, device.id))["secret"]

        await _heartbeat_ok(client, device.sn, secret)
        await _heartbeat_ok(client, device.sn, secret)

        assert await _audit_count(db, device.id) == 0
        assert await _audit_count(db, device.id, str(AuditAction.SIMULATE_HEARTBEAT)) == 0

    async def test_simulate_offline_moves_heartbeat_out_of_window_and_writes_event(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """★ 模拟掉线：心跳时间挪到窗口外 + 投影置 ``OFFLINE`` + 写 ``OFFLINE`` 事件。

        为什么不能只改投影？派生判据只看心跳时间，只改投影会留下
        「``onlineStatus=OFFLINE`` 但 ``online=true``」的自相矛盾状态。
        这里坏了说明：演示掉线后设备仍被判为在线（两个来源打架）。
        """
        device = await _make_device(db, sn="SN-HB-SIMOFF-01")
        headers = await auth.platform_headers()
        secret = (await _issue_ok(client, headers, device.id))["secret"]
        await _heartbeat_ok(client, device.sn, secret)

        response = await _simulate(client, headers, device.id, online=False)
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["simulated"] is True
        assert body["online"] is False, "派生判据必须同步为离线（否则状态自相矛盾）"
        assert body["onlineStatus"] == str(OnlineStatus.OFFLINE)

        stored = await _reload_device(db, device.id)
        assert stored.online_status == str(OnlineStatus.OFFLINE)
        assert stored.last_heartbeat_at is not None
        assert stored.last_heartbeat_at < device_service.online_cutoff(), (
            "心跳时间必须真的被挪到窗口外，而不是只改投影"
        )

        events = await _events(db, device.id, "OFFLINE")
        assert len(events) == 1
        assert events[0].dimension == "online"
        assert events[0].from_status == str(OnlineStatus.ONLINE)
        assert events[0].to_status == str(OnlineStatus.OFFLINE)

        # 审计也要留痕（与在线模拟同一条审计动作）
        assert await _audit_count(db, device.id, str(AuditAction.SIMULATE_HEARTBEAT)) == 1

        # 统计口径同步：该设备进入 OFFLINE
        stats = await _stats(client, headers)
        assert stats["byOnlineStatus"].get(str(OnlineStatus.OFFLINE), 0) >= 1

    async def test_simulate_offline_is_idempotent_in_event_count(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """状态没变就不重复写事件（连续两次模拟掉线只留一条 ``OFFLINE``）。"""
        device = await _make_device(db, sn="SN-HB-SIMDUP-01")
        headers = await auth.platform_headers()
        secret = (await _issue_ok(client, headers, device.id))["secret"]
        await _heartbeat_ok(client, device.sn, secret)

        await _simulate(client, headers, device.id, online=False)
        await _simulate(client, headers, device.id, online=False)

        assert len(await _events(db, device.id, "OFFLINE")) == 1
        assert await _audit_count(db, device.id, str(AuditAction.SIMULATE_HEARTBEAT)) == 2, (
            "审计逐次记录：每次人工动作都要留痕"
        )

    @pytest.mark.parametrize(
        ("asset_status", "expected_code"),
        [
            (str(AssetStatus.FROZEN), "DEVICE_FROZEN"),
            (str(AssetStatus.RETIRED), "DEVICE_NOT_AVAILABLE"),
        ],
        ids=["冻结", "报废"],
    )
    async def test_simulate_on_unserviceable_device_is_rejected(
        self,
        client: AsyncClient,
        auth: Any,
        db: AsyncSession,
        asset_status: str,
        expected_code: str,
    ) -> None:
        """冻结 / 报废设备不可被模拟成在线（演示能力不得绕过状态机）。"""
        device = await _make_device(db, sn=f"SN-HB-SIMBAD-{asset_status}", asset_status=asset_status)
        headers = await auth.platform_headers()

        response = await _simulate(client, headers, device.id, online=True)
        _assert_error(response, 409, expected_code)
        assert await _audit_count(db, device.id, str(AuditAction.SIMULATE_HEARTBEAT)) == 0
