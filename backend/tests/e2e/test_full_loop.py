"""端到端全闭环：从客户开通到终端用户对话（P0–P8 的最终验证）。

这条链路是项目的**主干**，也是里程碑 M2「业务闭环（无 AI）」的验收对象：

    客户开通 → 云服务商 → 产品模板 → 授权 → 客户产品
      → 商户下单 → 平台审核 → 生成设备 → 入库
      → 派单工厂 → 烧录上报 → 抽检 → 出货
      → 分配设备给租户
      → 终端用户登录 → 扫码解析 → 激活并绑定 → 对话

为什么写成**一个**测试函数
--------------------------
闭环的价值在于「上一环的产出就是下一环的输入」，拆成多个测试函数就必须在
每个函数里重建前置状态——那样每个函数都只验证了自己那一小段，而**跨环的
衔接**（例如「出货后的设备还能不能被分配」「激活后的设备能不能对话」）
恰好是历次阶段验收里真缺陷的高发区（P6 的二维码范围、P8 的绑定缺失都是
衔接问题）。因此这里刻意串成一条线，任何一环断了，后面的断言会直接指出
断点位置。

代价是失败时的定位粒度较粗——所以每一环都用一个具名变量承接产出，
断言消息里带上该环的标识。

端到端测试与「接口验收脚本」的分工
----------------------------------
`/tmp` 里的验收脚本跑在**真实 uvicorn 进程**上（含网络栈、静态托管、
启动期校验）；本文件跑在 `ASGITransport` 上（不含网络栈），但**纳入
`make test` 与 CI 的回归范围**。两者互补：前者更真实，后者可重复。
"""

from __future__ import annotations

import contextlib
import json
import secrets
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id
from app.models.ai import AiConfig, DialogueMessage, DialogueSession
from app.models.device import Device
from app.services import qrcode_service
from tests.conftest import API_PREFIX

pytestmark = pytest.mark.e2e

PLATFORM = f"{API_PREFIX}/platform"
MERCHANT = f"{API_PREFIX}/merchant"
FACTORY = f"{API_PREFIX}/factory"
MINIAPP = f"{API_PREFIX}/miniapp"

MERCHANT_PASSWORD = "Str0ng-Passw0rd!"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _ok(resp: httpx.Response, phase: str, expect: int = 200) -> dict[str, Any]:
    """断言某一环成功，失败消息里带上环标识与响应体。"""
    assert resp.status_code == expect, f"[{phase}] {resp.status_code} {resp.text[:400]}"
    body = resp.json()
    assert isinstance(body, dict), f"[{phase}] 期望对象响应，实际 {type(body)}"
    return body


async def _headers(auth: Any, account: str, password: str) -> dict[str, str]:
    return auth.headers(await auth.token(account, password))


# ---------------------------------------------------------------------------
# 全闭环
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("seeded")
async def test_full_loop_from_onboarding_to_dialogue(
    client: httpx.AsyncClient,
    auth: Any,
    db: AsyncSession,
    make_tenant: Any,
    make_user: Any,
) -> None:
    """一条测试走完 M2 主干，并在每一环留下可核对的断言。"""
    platform = await auth.platform_headers()
    # 每跑一次用一组随机标识，避免与其它用例的编码 / 账号撞车
    token = secrets.token_hex(3).upper()
    digits = str(int(token, 16) % 10**8).zfill(8)
    suffix = token
    merchant_account = f"139{digits}"
    factory_account = f"137{digits}"
    phone = f"138{digits}"

    # =====================================================================
    # 一、平台端：客户开通 → 云服务商 → 产品模板 → 授权 → 客户产品
    # =====================================================================
    tenant = await make_tenant(code=f"E2E-{suffix}", name=f"端到端演示品牌 {suffix}")

    cloud = _ok(
        await client.post(
            f"{PLATFORM}/clouds",
            headers=platform,
            json={
                "code": f"CLOUD-{suffix}",
                "name": f"端到端 Wi-Fi 云 {suffix}",
                "vendor": "JOYINSIDE",
                "networkType": "WIFI",
                **{"remark": "端到端测试用（不配置密钥 → 激活将走已配置的离线引擎）"},
            },
        ),
        "建云服务商",
        201,
    )

    template = _ok(
        await client.post(
            f"{PLATFORM}/templates",
            headers=platform,
            json={
                "code": f"TPL-{suffix}",
                "name": f"端到端智能玩具 {suffix}",
                "model": "ESP32-S3",
                "chip": "乐鑫 ESP32-S3",
                "networkType": "WIFI",
                "cloudProviderId": cloud["id"],
                "firmwareVersion": "9.9.9",
                "referencePrice": 199.0,
            },
        ),
        "建产品模板",
        201,
    )

    _ok(
        await client.post(
            f"{PLATFORM}/templates/{template['id']}/authorize",
            headers=platform,
            json={"tenantIds": [tenant.id], "remark": "端到端授权"},
        ),
        "授权租户",
    )

    product = _ok(
        await client.post(
            f"{PLATFORM}/client-products",
            headers=platform,
            json={
                "tenantId": tenant.id,
                "templateId": template["id"],
                "code": f"CP-{suffix}",
                "name": f"端到端故事机 {suffix}",
            },
        ),
        "建客户产品",
        201,
    )
    assert product["networkType"] == "WIFI", "联网方式应从模板快照"

    # 把该产品的 AI 供应商显式指向离线模拟引擎。
    #
    # 生产里这是 P9 商户端「AI 配置」页的职责；端到端测试直接落库，
    # 否则激活会按模板厂商（无密钥）正确返回 503——那是正确行为，
    # 但会让「激活 → 对话」这两环无法在离线环境里验证。
    db.add(
        AiConfig(
            id=new_id("ai_config"),
            tenant_id=tenant.id,
            client_product_id=product["id"],
            provider_code="mock",
            remark="端到端测试：使用离线模拟引擎",
        )
    )
    await db.flush()

    # =====================================================================
    # 二、商户下单 → 平台审核 → 生成设备（Wi-Fi 走本地 SN）→ 入库
    # =====================================================================
    await make_user(
        account=merchant_account,
        password=MERCHANT_PASSWORD,
        role_code="MERCHANT_ADMIN",
        tenant_id=tenant.id,
    )
    merchant = await _headers(auth, merchant_account, MERCHANT_PASSWORD)

    order = _ok(
        await client.post(
            f"{MERCHANT}/orders",
            headers=merchant,
            json={
                "clientProductId": product["id"],
                "quantity": 2,
                "applicantName": "端到端申请人",
                "applicantPhone": "13900000000",
                "remark": "端到端全闭环",
            },
        ),
        "商户下单",
        201,
    )
    assert order["status"] == "PENDING_AUDIT"

    audited = _ok(
        await client.post(
            f"{PLATFORM}/orders/{order['id']}/audit",
            headers=platform,
            json={"decision": "APPROVED", "remark": "端到端审核通过"},
        ),
        "平台审核",
    )
    assert audited["status"] == "APPROVED"

    generated = _ok(
        await client.post(f"{PLATFORM}/orders/{order['id']}/generate", headers=platform),
        "生成设备",
    )
    assert generated["generated"] == 2, f"应生成 2 台，实际 {generated['generated']}"
    device_ids = [d["id"] for d in generated["devices"]]

    order_detail = _ok(await client.get(f"{PLATFORM}/orders/{order['id']}", headers=platform), "订单详情")
    assert order_detail["status"] == "IN_STOCK", "生成后应自动入库"
    devices = [
        _ok(await client.get(f"{PLATFORM}/devices/{did}", headers=platform), "设备详情")
        for did in device_ids
    ]
    assert all(d["assetStatus"] == "IN_STOCK" for d in devices), "设备应已入库"

    # =====================================================================
    # 三、平台派单给工厂 → 烧录 → 抽检 → 出货
    # =====================================================================
    from app.db import seed as seed_module

    await seed_module._seed_factories(db)
    await db.flush()
    factory = (
        await db.execute(select(seed_module.Factory).where(seed_module.Factory.code == "FACTORY-DEMO-01"))
    ).scalar_one()
    factory_user = await make_user(
        account=factory_account, password=MERCHANT_PASSWORD,
        role_code="FACTORY_ADMIN", tenant_id=None,
    )
    factory_user.factory_id = factory.id
    await db.flush()
    factory_headers = await _headers(auth, factory_account, MERCHANT_PASSWORD)

    work_order = _ok(
        await client.post(
            f"{PLATFORM}/orders/{order['id']}/dispatch",
            headers=platform,
            json={"factoryId": factory.id, "productionNote": "端到端：先烧 9.9.9"},
        ),
        "派单给工厂",
        201,
    )
    work_order_id = work_order["id"]
    assert work_order["quantity"] == 2
    assert work_order["status"] == "PENDING"
    assert order_detail["quantity"] == work_order["orderQuantity"], "合同数量应原样下发"

    after_dispatch = _ok(await client.get(f"{PLATFORM}/orders/{order['id']}", headers=platform), "派单后订单")
    assert after_dispatch["status"] == "PRODUCING", "派单应把订单推到生产中"

    burned = _ok(
        await client.post(
            f"{FACTORY}/orders/{work_order_id}/burn", headers=factory_headers, json={"burnedCount": 2}
        ),
        "烧录上报",
    )
    assert burned["status"] == "COMPLETED", "报满应转烧录完成"

    inspected = _ok(
        await client.post(
            f"{FACTORY}/orders/{work_order_id}/inspect",
            headers=factory_headers,
            json={"sn": devices[0]["sn"], "result": "PASS"},
        ),
        "抽检",
        201,
    )
    assert inspected["result"] == "PASS"

    shipped = _ok(
        await client.post(f"{FACTORY}/orders/{work_order_id}/ship", headers=factory_headers), "出货登记"
    )
    assert shipped["status"] == "SHIPPED", "出货后工单进入终态"

    # 脱敏红线：整段工厂端响应里不得出现客户名 / 联系方式 / 金额字段
    factory_raw = json.dumps(shipped, ensure_ascii=False)
    for leaked in (tenant.name, "13900000000", "端到端申请人"):
        assert leaked not in factory_raw, f"工厂端响应泄漏了「{leaked}」"
    assert "unitPrice" not in factory_raw and "totalAmount" not in factory_raw

    # =====================================================================
    # 四、分配设备给租户（设备此时是 SHIPPED —— P6 接通的那条边）
    # =====================================================================
    allocation = _ok(
        await client.post(
            f"{PLATFORM}/allocations",
            headers=platform,
            json={"tenantId": tenant.id, "clientProductId": product["id"], "deviceIds": device_ids},
        ),
        "创建分配单",
        201,
    )
    result = _ok(
        await client.post(f"{PLATFORM}/allocations/{allocation['id']}/execute", headers=platform),
        "执行分配",
    )
    assert result["allocatedCount"] == 2, f"应分配 2 台，实际 {result}"
    assert not result["failures"], f"不应有失败行：{result['failures']}"

    allocated = [
        _ok(await client.get(f"{PLATFORM}/devices/{did}", headers=platform), "分配后设备")
        for did in device_ids
    ]
    assert all(d["assetStatus"] == "ALLOCATED" for d in allocated)
    assert all(d["tenantId"] == tenant.id for d in allocated)

    # =====================================================================
    # 五、终端用户：登录 → 扫码解析 → 激活并绑定
    # =====================================================================
    code_resp = _ok(await client.post(f"{MINIAPP}/auth/code", json={"phone": phone}), "获取验证码")
    assert code_resp["mock"] is True, "演示环境应回显验证码"
    logged = _ok(
        await client.post(
            f"{MINIAPP}/auth/login", json={"phone": phone, "code": code_resp["mockCode"]}
        ),
        "终端用户登录",
    )
    end_user = {"Authorization": f"Bearer {logged['accessToken']}"}
    assert logged["devices"] == [], "新用户还没有设备"

    target = device_ids[0]
    target_device = await db.get(Device, target)
    assert target_device is not None
    payload = qrcode_service.build_jd_payload(tenant.id, product["id"], target_device.sn)
    resolved = _ok(
        await client.post(f"{MINIAPP}/scan/resolve", headers=end_user, json={"payload": payload}),
        "扫码解析",
    )
    assert resolved["format"] == "JD" and resolved["networkType"] == "WIFI"
    assert resolved["bindable"] is True, f"应当可激活：{resolved['reason']}"

    activated = _ok(
        await client.post(
            f"{MINIAPP}/devices/{target}/activate-wifi",
            headers=end_user,
            json={"sn": target_device.sn, "mac": "AA:BB:CC:99:88:77"},
        ),
        "Wi-Fi 激活",
    )
    assert activated["activationStatus"] == "ACTIVATED"
    assert activated["bindStatus"] == "BOUND", "激活必须同时完成绑定"

    bound = _ok(await client.get(f"{MINIAPP}/devices", headers=end_user), "我的设备")
    assert bound["total"] == 1, f"应只有 1 台：{bound}"

    # =====================================================================
    # 六、终端对话：SSE 流式 → 会话与消息落库
    # =====================================================================
    frames: list[dict[str, Any]] = []
    async with client.stream(
        "POST",
        f"{MINIAPP}/chat/stream",
        headers=end_user,
        json={"deviceId": target, "text": "讲个故事"},
    ) as resp:
        assert resp.status_code == 200, f"[对话] {resp.status_code} {resp.text[:200]}"
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            with contextlib.suppress(json.JSONDecodeError):
                frames.append(json.loads(line[5:].strip()))

    frame_types = [str(f.get("type")) for f in frames]
    assert "session.ready" in frame_types, f"[对话] 缺 session.ready：{frame_types}"
    assert frame_types.count("assistant.delta") >= 2, f"[对话] 应当是分块流式：{frame_types}"
    done_frame = next((f for f in frames if f.get("type") == "assistant.done"), None)
    assert done_frame is not None and done_frame.get("messageId"), f"[对话] 缺 assistant.done：{frame_types}"

    sessions = int(
        (
            await db.execute(
                select(func.count())
                .select_from(DialogueSession)
                .where(DialogueSession.end_user_id == logged["user"]["id"])
            )
        ).scalar_one()
    )
    assert sessions >= 1, "对话会话应当落库"
    messages = int(
        (
            await db.execute(
                select(func.count())
                .select_from(DialogueMessage)
                .where(DialogueMessage.session_id == done_frame["sessionId"])
            )
        ).scalar_one()
    )
    assert messages >= 2, f"一轮对话至少应落「用户 + 助手」两条消息，实际 {messages}"

    # =====================================================================
    # 七、终态核对：设备四维全链路留痕
    # =====================================================================
    final = _ok(await client.get(f"{PLATFORM}/devices/{target}", headers=platform), "设备终态")
    assert final["assetStatus"] == "BOUND", f"资产状态应为 BOUND：{final['assetStatus']}"
    assert final["activationStatus"] == "ACTIVATED"
    assert final["bindStatus"] == "BOUND"

    events = _ok(
        await client.get(f"{PLATFORM}/devices/{target}/events", headers=platform), "设备时间线"
    )
    event_types = {row["eventType"] for row in events["records"]}
    for expected in ("GENERATED", "IN_STOCK", "PRODUCING", "PRODUCED", "SHIPPED", "ALLOCATED", "BOUND"):
        assert expected in event_types, f"设备时间线缺少 {expected}：{sorted(event_types)}"
