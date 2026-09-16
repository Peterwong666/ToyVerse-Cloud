"""P8 终端用户小程序端：激活链路 / 绑定归属 / 充值 / 内容安全的集成测试。

为什么这份测试直接复用生产种子函数
----------------------------------
`tests/conftest.py` 的 ``seeded`` 夹具只灌「角色 + 租户 + 管理员账号」，
不含目录域 / 设备 / P8 的套餐与待激活设备。P8 的用例需要一台
**「已分配待激活」**的设备与若干流量套餐——手写一套造数辅助函数会与
``app/db/seed.py`` 里的演示数据分叉（两处对「待激活设备长什么样」的理解
迟早不一致），因此这里直接调用生产的 ``_seed_catalog`` / ``_seed_orders_devices``
/ ``_seed_miniapp``。

副作用是**顺带覆盖了种子数据本身**：如果种子写坏了（比如没给演示产品配
AI 供应商），这里的用例会一起红灯。

设计口径（与实现一致，改动实现前先读这里）
* 激活 ≠ 绑定：``activation_status`` 与 ``bind_status`` 是两个维度；
  **解绑刻意不改激活状态**（设备确实激活过），因此「已激活但未绑定」
  是合法状态——重复激活必须把绑定补齐，见
  :meth:`TestActivation::test_reactivate_after_unbind_rebinds`。
* 终端用户的可见范围由**绑定关系**决定，越权一律 404（不泄漏存在性）。
* 令牌类型是双向隔离的：管理端令牌调小程序端点 401，反之亦然。
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.allocation import DeviceBinding
from app.models.device import Device
from app.services import qrcode_service
from tests.conftest import API_PREFIX

pytestmark = pytest.mark.integration

MINIAPP = f"{API_PREFIX}/miniapp"
PLATFORM = f"{API_PREFIX}/platform"

#: 演示设备（来自 app/db/seed.py 的 DEMO_END_USER_DEVICES）
SN_4G = "SN-DEMO-4G-001"
SN_WIFI = "SN-DEMO-WIFI-001"

PHONE_A = "13700000001"
PHONE_B = "13700000002"


# ---------------------------------------------------------------------------
# 夹具与辅助
# ---------------------------------------------------------------------------


@pytest.fixture
async def demo(db: AsyncSession) -> AsyncSession:
    """把完整演示数据灌进测试库（含 P8 的套餐与两台待激活设备）。"""
    from app.db import seed as seed_module

    await seed_module._seed_catalog(db)
    await seed_module._seed_orders_devices(db)
    await seed_module._seed_miniapp(db)
    await db.commit()
    return db


async def _login(client: httpx.AsyncClient, phone: str) -> dict[str, str]:
    """走真实验证码链路登录，返回小程序请求头。"""
    code_resp = await client.post(f"{MINIAPP}/auth/code", json={"phone": phone})
    assert code_resp.status_code == 200, code_resp.text
    body = code_resp.json()
    assert body["mock"] is True, "演示环境应回显验证码（生产环境启动期就会拒绝 mock）"
    resp = await client.post(
        f"{MINIAPP}/auth/login", json={"phone": phone, "code": body["mockCode"]}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['accessToken']}"}


    # 平台端请求头统一用 conftest 的 ``auth`` 夹具（见各用例签名）——
    # 测试环境的平台口令由夹具决定，直接读 ``settings`` 会拿到 .env 的值。



async def _resolve(client: httpx.AsyncClient, headers: dict[str, str], payload: str) -> Any:
    return await client.post(f"{MINIAPP}/scan/resolve", headers=headers, json={"payload": payload})


async def _device_id_of(client: httpx.AsyncClient, headers: dict[str, str], sn: str) -> str:
    """扫码解析拿到设备 ID（这是终端用户获得设备 ID 的唯一正当途径）。"""
    if sn == SN_4G:
        payload = qrcode_service.build_jx_payload(
            sn, "866000000000001", "8986000000000000001", "jx-demo-device-001"
        )
    else:
        payload = qrcode_service.build_jd_payload("t-001", "prod-t001-cube", sn)
    resp = await _resolve(client, headers, payload)
    assert resp.status_code == 200, resp.text
    return str(resp.json()["device"]["id"])


async def _device_row(db: AsyncSession, device_id: str) -> Device:
    return (await db.execute(select(Device).where(Device.id == device_id))).scalar_one()


async def _binding_rows(db: AsyncSession, device_id: str) -> list[DeviceBinding]:
    return list(
        (
            await db.execute(select(DeviceBinding).where(DeviceBinding.device_id == device_id))
        )
        .scalars()
        .all()
    )


async def _sse_frames(
    client: httpx.AsyncClient, headers: dict[str, str], device_id: str, text: str
) -> list[dict[str, Any]]:
    """消费一次 SSE 对话流，返回收到的帧列表。"""
    frames: list[dict[str, Any]] = []
    async with client.stream(
        "POST",
        f"{MINIAPP}/chat/stream",
        headers=headers,
        json={"deviceId": device_id, "text": text},
    ) as resp:
        assert resp.status_code == 200, resp.text
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            # 非 JSON 的 data 行直接跳过（SSE 的保活注释等）
            with contextlib.suppress(json.JSONDecodeError):
                frames.append(json.loads(line[5:].strip()))
    return frames


# ---------------------------------------------------------------------------
# 一、登录与令牌隔离
# ---------------------------------------------------------------------------


class TestAuth:
    async def test_wrong_code_reports_attempts_left(self, client: httpx.AsyncClient, demo: Any) -> None:
        """验证码错误 → 400 且带 attemptsLeft（前端要能显示「还能试几次」）。"""
        await client.post(f"{MINIAPP}/auth/code", json={"phone": PHONE_A})
        resp = await client.post(
            f"{MINIAPP}/auth/login", json={"phone": PHONE_A, "code": "000000"}
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "VALIDATION_ERROR"
        assert resp.json()["details"]["attemptsLeft"] is not None

    async def test_invalid_phone_is_rejected(self, client: httpx.AsyncClient, demo: Any) -> None:
        resp = await client.post(f"{MINIAPP}/auth/code", json={"phone": "123"})
        assert resp.status_code == 400, resp.text

    async def test_admin_token_is_rejected_on_miniapp(
        self, client: httpx.AsyncClient, auth: Any, demo: Any
    ) -> None:
        """★ 管理端令牌调小程序端点 → 401（``type`` claim 在解签阶段就被拒）。"""
        resp = await client.get(f"{MINIAPP}/profile", headers=await auth.platform_headers())
        assert resp.status_code == 401, resp.text

    async def test_end_user_token_is_rejected_on_admin(self, client: httpx.AsyncClient, demo: Any) -> None:
        """★ 反向同样隔离：小程序令牌调管理端 → 401。"""
        headers = await _login(client, PHONE_A)
        resp = await client.get(f"{PLATFORM}/devices", headers=headers)
        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# 二、扫码解析
# ---------------------------------------------------------------------------


class TestScanResolve:
    async def test_jx_routes_to_4g(self, client: httpx.AsyncClient, demo: Any) -> None:
        headers = await _login(client, PHONE_A)
        resp = await _resolve(
            client,
            headers,
            qrcode_service.build_jx_payload(
                SN_4G, "866000000000001", "8986000000000000001", "jx-demo-device-001"
            ),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["format"] == "JX"
        assert body["networkType"] == "4G"
        assert body["bindable"] is True

    async def test_jd_routes_to_wifi(self, client: httpx.AsyncClient, demo: Any) -> None:
        headers = await _login(client, PHONE_A)
        resp = await _resolve(
            client, headers, qrcode_service.build_jd_payload("t-001", "prod-t001-cube", SN_WIFI)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["format"] == "JD"
        assert body["networkType"] == "WIFI"

    async def test_unknown_payload_and_sn(self, client: httpx.AsyncClient, demo: Any) -> None:
        headers = await _login(client, PHONE_A)
        bad_format = await _resolve(client, headers, "XX|garbage")
        assert bad_format.status_code == 404, bad_format.text
        assert bad_format.json()["code"] == "QR_INVALID"

        unknown_sn = await _resolve(client, headers, "JX|SN-NOT-EXIST|1|2|3")
        assert unknown_sn.status_code == 404, unknown_sn.text
        assert unknown_sn.json()["code"] == "DEVICE_NOT_FOUND"


# ---------------------------------------------------------------------------
# 三、激活与绑定
# ---------------------------------------------------------------------------


class TestActivation:
    async def test_activate_4g_activates_and_binds(
        self, client: httpx.AsyncClient, db: AsyncSession, demo: Any
    ) -> None:
        """4G 激活：``NOT_ACTIVATED → ACTIVATED`` 且绑定到当前终端用户。"""
        headers = await _login(client, PHONE_A)
        device_id = await _device_id_of(client, headers, SN_4G)

        resp = await client.post(f"{MINIAPP}/devices/{device_id}/activate-4g", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["activationStatus"] == "ACTIVATED"
        assert body["bindStatus"] == "BOUND"
        assert body["mock"] is True, "演示产品配的是离线模拟引擎，必须显式标注 mock"

        device = await _device_row(db, device_id)
        assert device.activation_status == "ACTIVATED"
        assert device.bind_status == "BOUND"

    async def test_reactivate_after_unbind_rebinds(
        self, client: httpx.AsyncClient, db: AsyncSession, demo: Any
    ) -> None:
        """★ 回归：解绑后再次激活**必须把绑定补齐**（P8 验收发现的真缺陷）。

        缺陷原形：``_activate`` 的幂等分支只回放激活结果就返回。
        而解绑**刻意不改变** ``activation_status``（设备确实激活过），
        于是「已激活 + 未绑定」的设备再次激活时，接口回 200，
        界面显示「激活成功」，但库里没有绑定关系——用户一进对话页
        就被 WS 以 4403 拒掉，且看不出原因。

        本用例把「重新激活 → 绑定恢复 → 能查到设备」整条链路钉住。
        """
        headers = await _login(client, PHONE_A)
        device_id = await _device_id_of(client, headers, SN_4G)
        assert (
            await client.post(f"{MINIAPP}/devices/{device_id}/activate-4g", headers=headers)
        ).status_code == 200

        unbind = await client.post(
            f"{MINIAPP}/devices/{device_id}/unbind", headers=headers, json={"reason": "换新玩具"}
        )
        assert unbind.status_code == 200, unbind.text
        mid = await _device_row(db, device_id)
        assert mid.bind_status == "UNBOUND"
        assert mid.activation_status == "ACTIVATED", "解绑不该改变激活状态（设备确实激活过）"

        # 再次激活（走幂等分支）——这一步就是缺陷所在
        again = await client.post(f"{MINIAPP}/devices/{device_id}/activate-4g", headers=headers)
        assert again.status_code == 200, again.text
        assert again.json()["bindStatus"] == "BOUND", (
            "幂等回放必须同时保证绑定关系，否则「显示成功但绑不上」"
        )
        after = await _device_row(db, device_id)
        assert after.bind_status == "BOUND"
        assert after.asset_status == "BOUND"

        # 真的能查到它（列表按绑定关系过滤，这一步验证绑定落库生效）
        listed = await client.get(f"{MINIAPP}/devices", headers=headers)
        assert device_id in {d["id"] for d in listed.json()["records"]}

    async def test_activation_is_idempotent_single_binding_row(
        self, client: httpx.AsyncClient, db: AsyncSession, demo: Any
    ) -> None:
        """反复激活不新增绑定行（``UNIQUE(device_id)``），``bind_count`` 如实累加。"""
        headers = await _login(client, PHONE_A)
        device_id = await _device_id_of(client, headers, SN_4G)
        for _ in range(3):
            assert (
                await client.post(f"{MINIAPP}/devices/{device_id}/activate-4g", headers=headers)
            ).status_code == 200
        rows = await _binding_rows(db, device_id)
        assert len(rows) == 1, "一台设备恒一行绑定记录"
        assert rows[0].bind_count == 1, "已绑定状态下重复激活不该继续累加 bind_count"

    async def test_wifi_sn_mismatch_marks_bind_failed(
        self, client: httpx.AsyncClient, db: AsyncSession, demo: Any
    ) -> None:
        """★ SN 与二维码不一致 → 409 ``BIND_FAILED`` 且落库 ``BIND_FAILED``。"""
        headers = await _login(client, PHONE_A)
        device_id = await _device_id_of(client, headers, SN_WIFI)

        resp = await client.post(
            f"{MINIAPP}/devices/{device_id}/activate-wifi",
            headers=headers,
            json={"sn": "SN-WRONG-0001", "mac": "AA:BB:CC:00:01:01"},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "BIND_FAILED"
        assert resp.json()["details"]["qrSn"] == SN_WIFI
        assert resp.json()["details"]["reportedSn"] == "SN-WRONG-0001"

        device = await _device_row(db, device_id)
        assert device.activation_status == "BIND_FAILED"
        assert device.bind_status == "UNBOUND", "校验失败不得留下绑定关系"

        # 修正 SN 后可以正常激活（失败必须可恢复，否则设备就废了）
        ok = await client.post(
            f"{MINIAPP}/devices/{device_id}/activate-wifi",
            headers=headers,
            json={"sn": SN_WIFI, "mac": "AA:BB:CC:00:01:01"},
        )
        assert ok.status_code == 200, ok.text
        assert (await _device_row(db, device_id)).activation_status == "ACTIVATED"

    async def test_unbind_returns_device_to_allocated(
        self, client: httpx.AsyncClient, db: AsyncSession, demo: Any
    ) -> None:
        """解绑：``bind_status → UNBOUND``、资产状态回 ``ALLOCATED``、激活态不变。"""
        headers = await _login(client, PHONE_A)
        device_id = await _device_id_of(client, headers, SN_4G)
        await client.post(f"{MINIAPP}/devices/{device_id}/activate-4g", headers=headers)

        resp = await client.post(f"{MINIAPP}/devices/{device_id}/unbind", headers=headers, json={})
        assert resp.status_code == 200, resp.text
        device = await _device_row(db, device_id)
        assert device.bind_status == "UNBOUND"
        assert device.asset_status == "ALLOCATED"
        assert device.activation_status == "ACTIVATED"


# ---------------------------------------------------------------------------
# 四、越权（终端用户的收口点是绑定关系，不是租户）
# ---------------------------------------------------------------------------


class TestOwnershipIsolation:
    async def test_other_user_cannot_touch_device(
        self, client: httpx.AsyncClient, demo: Any
    ) -> None:
        """★ 他人设备详情 / 改设置 / 解绑一律 404（不泄漏存在性）。"""
        headers_a = await _login(client, PHONE_A)
        device_id = await _device_id_of(client, headers_a, SN_4G)
        await client.post(f"{MINIAPP}/devices/{device_id}/activate-4g", headers=headers_a)

        headers_b = await _login(client, PHONE_B)
        assert (await client.get(f"{MINIAPP}/devices/{device_id}", headers=headers_b)).status_code == 404
        assert (
            await client.put(
                f"{MINIAPP}/devices/{device_id}/settings", headers=headers_b, json={"volume": 1}
            )
        ).status_code == 404
        assert (
            await client.post(f"{MINIAPP}/devices/{device_id}/unbind", headers=headers_b, json={})
        ).status_code == 404
        assert (await client.get(f"{MINIAPP}/devices", headers=headers_b)).json()["total"] == 0

    async def test_bound_device_cannot_be_taken_over(
        self, client: httpx.AsyncClient, demo: Any
    ) -> None:
        """★ 单绑约束：已绑定给 A 的设备，B 直接调激活接口也拿不走。"""
        headers_a = await _login(client, PHONE_A)
        device_id = await _device_id_of(client, headers_a, SN_4G)
        await client.post(f"{MINIAPP}/devices/{device_id}/activate-4g", headers=headers_a)

        headers_b = await _login(client, PHONE_B)
        resp = await client.post(f"{MINIAPP}/devices/{device_id}/activate-4g", headers=headers_b)
        assert resp.status_code in (403, 404, 409), resp.text


# ---------------------------------------------------------------------------
# 五、设备设置
# ---------------------------------------------------------------------------


class TestDeviceSettings:
    async def test_defaults_then_update_and_range(
        self, client: httpx.AsyncClient, demo: Any
    ) -> None:
        headers = await _login(client, PHONE_A)
        device_id = await _device_id_of(client, headers, SN_4G)
        await client.post(f"{MINIAPP}/devices/{device_id}/activate-4g", headers=headers)

        initial = await client.get(f"{MINIAPP}/devices/{device_id}/settings", headers=headers)
        assert initial.status_code == 200, initial.text
        assert initial.json()["volume"] == 60, "未设置过时应给默认值而不是 null"

        updated = await client.put(
            f"{MINIAPP}/devices/{device_id}/settings",
            headers=headers,
            json={"volume": 80, "childMode": True, "hacked": "x"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["volume"] == 80
        assert updated.json()["childMode"] is True
        assert "hacked" not in updated.json(), "未知键必须被白名单挡掉"

        too_loud = await client.put(
            f"{MINIAPP}/devices/{device_id}/settings", headers=headers, json={"volume": 999}
        )
        assert too_loud.status_code == 400, too_loud.text


# ---------------------------------------------------------------------------
# 六、充值
# ---------------------------------------------------------------------------


class TestRecharge:
    async def test_only_4g_supports_recharge(self, client: httpx.AsyncClient, demo: Any) -> None:
        """★ 仅 4G 设备展示充值：Wi-Fi 返回 ``supported=false`` + 原因（不是报错）。"""
        headers = await _login(client, PHONE_A)
        four_g = await _device_id_of(client, headers, SN_4G)
        wifi = await _device_id_of(client, headers, SN_WIFI)
        # 套餐端点按**绑定关系**收口，因此 Wi-Fi 设备也要先激活
        # （它同样走 mock 供应商，能离线激活成功）
        assert (
            await client.post(f"{MINIAPP}/devices/{four_g}/activate-4g", headers=headers)
        ).status_code == 200
        assert (
            await client.post(
                f"{MINIAPP}/devices/{wifi}/activate-wifi",
                headers=headers,
                json={"sn": SN_WIFI, "mac": "AA:BB:CC:00:01:01"},
            )
        ).status_code == 200

        g = await client.get(f"{MINIAPP}/recharge/plans", headers=headers, params={"deviceId": four_g})
        assert g.status_code == 200, g.text
        assert g.json()["supported"] is True
        assert len(g.json()["records"]) >= 1

        w = await client.get(f"{MINIAPP}/recharge/plans", headers=headers, params={"deviceId": wifi})
        assert w.status_code == 200, w.text
        assert w.json()["supported"] is False
        assert w.json()["reason"], "不支持时必须给出人话原因"
        assert w.json()["records"] == []

    async def test_order_snapshots_price_and_pay_is_single_shot(
        self, client: httpx.AsyncClient, demo: Any
    ) -> None:
        headers = await _login(client, PHONE_A)
        device_id = await _device_id_of(client, headers, SN_4G)
        await client.post(f"{MINIAPP}/devices/{device_id}/activate-4g", headers=headers)

        plans = (
            await client.get(f"{MINIAPP}/recharge/plans", headers=headers, params={"deviceId": device_id})
        ).json()["records"]
        plan = plans[0]

        created = await client.post(
            f"{MINIAPP}/recharge/orders", headers=headers, json={"deviceId": device_id, "planId": plan["id"]}
        )
        assert created.status_code == 201, created.text
        order = created.json()
        assert order["status"] == "PENDING"
        assert order["amount"] == plan["price"], "金额必须是下单时的套餐快照"

        paid = await client.post(f"{MINIAPP}/recharge/orders/{order['id']}/pay", headers=headers)
        assert paid.status_code == 200, paid.text
        assert paid.json()["order"]["status"] == "PAID"
        assert paid.json()["mock"] is True, "模拟支付通道必须显式标注"

        again = await client.post(f"{MINIAPP}/recharge/orders/{order['id']}/pay", headers=headers)
        assert again.status_code == 409, again.text
        assert again.json()["code"] == "INVALID_STATE_TRANSITION"


# ---------------------------------------------------------------------------
# 七、对话流与内容安全
# ---------------------------------------------------------------------------


class TestDialogue:
    async def test_stream_emits_protocol_frames(
        self, client: httpx.AsyncClient, demo: Any
    ) -> None:
        """SSE 降级端点按协议发帧：``session.ready`` → ``assistant.delta`` → ``assistant.done``。"""
        headers = await _login(client, PHONE_A)
        device_id = await _device_id_of(client, headers, SN_4G)
        await client.post(f"{MINIAPP}/devices/{device_id}/activate-4g", headers=headers)

        frames = await _sse_frames(client, headers, device_id, "讲个故事")
        types = [str(f.get("type")) for f in frames]
        assert "session.ready" in types
        assert types.count("assistant.delta") >= 2, f"应当是分块流式，实际：{types}"
        done = next((f for f in frames if f.get("type") == "assistant.done"), None)
        assert done is not None and done.get("messageId"), f"缺 assistant.done：{types}"

    async def test_content_safety_blocks_and_flags(
        self, client: httpx.AsyncClient, demo: Any
    ) -> None:
        """★ 命中内容安全 → 标注 ``safetyFlag`` 与 ``blocked``，且回复被替换为兜底文案。

        判定用的是离线引擎的关键词表（``app/ai/mock/scenarios.SAFETY_KEYWORDS``）：
        这里挑表里确实存在的词，否则用例会「看起来在测安全、实际什么都没命中」。
        """
        from app.ai.mock.scenarios import SAFETY_KEYWORDS

        headers = await _login(client, PHONE_A)
        device_id = await _device_id_of(client, headers, SN_4G)
        await client.post(f"{MINIAPP}/devices/{device_id}/activate-4g", headers=headers)

        frames = await _sse_frames(client, headers, device_id, f"我想聊{SAFETY_KEYWORDS[0]}")
        done = next((f for f in frames if f.get("type") == "assistant.done"), None)
        assert done is not None, "缺 assistant.done"
        assert done.get("blocked") is True, f"应当被拦截：{done}"
        assert done.get("safetyFlag"), "被拦截必须留下 safetyFlag 供运营回溯"

        text = "".join(
            str(f.get("delta") or "") for f in frames if f.get("type") == "assistant.delta"
        )
        assert SAFETY_KEYWORDS[0] not in text, "被拦截的原文绝不能出现在回复里"

    async def test_dialogue_requires_ownership(
        self, client: httpx.AsyncClient, demo: Any
    ) -> None:
        """未绑定当前账号的设备不能对话（404，与其它端点同口径）。"""
        headers = await _login(client, PHONE_B)
        resp = await client.post(
            f"{MINIAPP}/chat/stream", headers=headers, json={"deviceId": "d-demo-4g-01", "text": "hi"}
        )
        assert resp.status_code == 404, resp.text
