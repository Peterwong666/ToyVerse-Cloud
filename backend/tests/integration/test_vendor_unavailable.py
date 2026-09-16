"""集成测试：未接入厂商必须**安全失败且不伪造成功**（ADR-07 架构红线）。

这是 P7 最重要的一条断言测试。它同时验证四件事：

1. 返回 ``503 VENDOR_UNAVAILABLE``（前端与固件能据此降级）
2. ``details.ok == false``（用一个布尔字段判断「是不是安全失败」，不必解析中文提示）
3. **审计表新增 ``VENDOR_CALL_FAILED`` 记录**（安全拒绝必须留痕，否则无法自证）
4. **没有任何业务数据落库**（没有会话、没有消息）——「不伪造成功」的实质含义：
   既不能返回假的成功响应，也不能留下「看起来聊过」的痕迹

另外覆盖两个边界：
* 能力缺口（集贤只做设备，不做对话）也要安全失败，但 ``details.capability`` 指出缺口
* 未注册的注册键在健康探测端点上失败，``details.reason=PROVIDER_NOT_REGISTERED``
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id
from app.models.ai import DialogueMessage, DialogueSession
from app.models.audit import AuditLog
from app.models.catalog import ClientProduct, ProductTemplate
from app.models.enums import AuditAction
from tests.conftest import API_PREFIX

pytestmark = pytest.mark.integration

AI = f"{API_PREFIX}/ai"

#: 审计动作的字面量（与 ``AuditAction.VENDOR_CALL_FAILED`` 的存储值一致）
VENDOR_CALL_FAILED = str(AuditAction.VENDOR_CALL_FAILED)


@pytest.fixture
async def p7_product(db: AsyncSession, make_tenant: Any) -> ClientProduct:
    """最小可用的客户产品（对话需要归属租户，故先建租户与模板）。"""
    tenant = await make_tenant(code="P7-VENDOR", name="P7 厂商测试租户")
    template = ProductTemplate(
        id=new_id("product_template"), code="P7V-TPL", name="P7V 模板", network_type="WIFI"
    )
    db.add(template)
    await db.flush()

    product = ClientProduct(
        id=new_id("client_product"),
        tenant_id=tenant.id,
        template_id=template.id,
        code="P7V-PROD",
        name="P7V 客户产品",
        network_type="WIFI",
    )
    db.add(product)
    await db.commit()
    return product


async def _vendor_failure_count(db: AsyncSession) -> int:
    """统计 ``VENDOR_CALL_FAILED`` 审计条数。"""
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == VENDOR_CALL_FAILED)
            )
        ).scalar_one()
    )


async def _table_count(db: AsyncSession, model: Any) -> int:
    """统计某张表的行数（用于断言「没有留下伪造痕迹」）。"""
    return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


# ---------------------------------------------------------------------------
# 未配置密钥的真实厂商：三个能力端点都必须 503 + 审计
# ---------------------------------------------------------------------------


async def test_chat_with_unconfigured_vendor_fails_safely(
    client: AsyncClient, auth: Any, db: AsyncSession, p7_product: ClientProduct
) -> None:
    """★ 核心断言：未接入厂商的对话请求安全失败，且不落任何业务数据。"""
    before = await _vendor_failure_count(db)

    response = await client.post(
        f"{AI}/chat",
        json={
            "message": "你好",
            "clientProductId": p7_product.id,
            "providerCode": "volcano",
        },
        headers=await auth.platform_headers(),
    )

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "VENDOR_UNAVAILABLE"
    assert body["details"]["ok"] is False
    assert body["details"]["vendor"] == "VOLCANO"
    # 绝不伪造成功：响应里不存在回复内容
    assert "reply" not in body
    assert not body.get("data")

    # 审计必须留痕
    assert await _vendor_failure_count(db) == before + 1

    # 也不允许留下「看起来聊过」的痕迹
    assert await _table_count(db, DialogueSession) == 0
    assert await _table_count(db, DialogueMessage) == 0


async def test_asr_with_unconfigured_vendor_fails_safely(
    client: AsyncClient, auth: Any, db: AsyncSession
) -> None:
    """语音识别同样安全失败（不因为「只是语音」就放过）。"""
    before = await _vendor_failure_count(db)

    response = await client.post(
        f"{AI}/asr",
        json={"hintText": "你好", "providerCode": "volcano"},
        headers=await auth.platform_headers(),
    )

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "VENDOR_UNAVAILABLE"
    assert body["details"]["ok"] is False
    assert "text" not in body
    assert await _vendor_failure_count(db) == before + 1


async def test_tts_with_unconfigured_vendor_fails_safely(
    client: AsyncClient, auth: Any, db: AsyncSession
) -> None:
    """语音合成同样安全失败。"""
    before = await _vendor_failure_count(db)

    response = await client.post(
        f"{AI}/tts",
        json={"text": "你好", "providerCode": "volcano"},
        headers=await auth.platform_headers(),
    )

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "VENDOR_UNAVAILABLE"
    assert body["details"]["ok"] is False
    assert "audioBase64" not in body
    assert await _vendor_failure_count(db) == before + 1


async def test_unconfigured_vendor_health_reports_down_without_faking(
    client: AsyncClient, auth: Any, db: AsyncSession
) -> None:
    """健康探测对未接入厂商返回 ``DOWN`` + ``ok=false``，并写审计。

    为什么这里返回 200 而不是 503：探测接口的职责是**报告**状态。
    若探测失败也返回错误码，运维就无法区分「厂商不可用」与「探测接口坏了」。
    """
    before = await _vendor_failure_count(db)

    response = await client.get(
        f"{AI}/providers/volcano/health", headers=await auth.platform_headers()
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["status"] == "DOWN"
    assert body["detail"]["called"] is False, "未配置密钥时不应发起任何真实网络调用"
    assert "access_key" in body["detail"]["missing"]
    assert await _vendor_failure_count(db) == before + 1


# ---------------------------------------------------------------------------
# 能力缺口与未注册键
# ---------------------------------------------------------------------------


async def test_capability_gap_reports_the_missing_capability(
    client: AsyncClient, auth: Any, db: AsyncSession, p7_product: ClientProduct
) -> None:
    """集贤只做设备云：对话请求应失败并指出缺的是 ``dialogue`` 能力。

    与「未配置密钥」区分开很重要——前者的解决办法是换供应商，
    后者是去补密钥，前端给的提示语完全不同。
    """
    before = await _vendor_failure_count(db)

    response = await client.post(
        f"{AI}/chat",
        json={"message": "你好", "clientProductId": p7_product.id, "providerCode": "jixian"},
        headers=await auth.platform_headers(),
    )

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "VENDOR_UNAVAILABLE"
    assert body["details"]["capability"] == "dialogue"
    assert body["details"]["ok"] is False
    assert await _vendor_failure_count(db) == before + 1


async def test_unregistered_provider_health_fails_safely(
    client: AsyncClient, auth: Any, db: AsyncSession
) -> None:
    """未注册的注册键在探测端点安全失败，并说明原因是「未注册」。"""
    before = await _vendor_failure_count(db)

    response = await client.get(
        f"{AI}/providers/not-registered/health", headers=await auth.platform_headers()
    )

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "VENDOR_UNAVAILABLE"
    assert body["details"]["reason"] == "PROVIDER_NOT_REGISTERED"
    assert await _vendor_failure_count(db) == before + 1


async def test_mock_still_works_while_vendor_is_blocked(
    client: AsyncClient, auth: Any, p7_product: ClientProduct
) -> None:
    """对照组：同一时刻 mock 仍然可用——安全失败不是「一刀切停服」。

    这条断言防的是「为了安全把所有 AI 能力关掉」这种过度反应：
    未接入的厂商必须拒绝，已就绪的离线引擎必须照常服务。
    """
    response = await client.post(
        f"{AI}/asr",
        json={"hintText": "你好", "providerCode": "mock", "clientProductId": p7_product.id},
        headers=await auth.platform_headers(),
    )

    assert response.status_code == 200, response.text
    assert response.json()["text"] == "你好"
