"""集成测试：商户端「AI 配置保存」全链路。

为什么单独有一份集成测试
------------------------
``tests/unit/test_ai_config_service.py`` 已经在服务层把分支铺满，但「商户在
配置页点保存」这个动作实际走的是 **HTTP → 登录鉴权 → 权限点校验 → 服务层 →
落库 → 审计** 的完整调用链。链路上任何一环断了（路由挂错权限点、请求模型
别名写错、响应序列化丢字段），单元测试都发现不了——它们直接调服务函数，
绕开了 HTTP 层。

本文件覆盖的真实链路
--------------------
* 未配置过的产品：GET 返回**默认视图**（``status=DISABLED``）而不是 404——
  否则配置页打不开，商户找不到入口去配
* 首次保存：``PUT /merchant/products/{id}/ai-config`` 落库并写 **CREATE** 审计，
  ``temperature`` 入参小数 ×100 取整落库，``status=ENABLED`` 同步产品 ``ai_enabled``
* 部分更新语义：未传的字段不动，显式传 ``null`` 才清空
* 专用端点：``/prompt`` 只动采样参数、``/safety`` 只动内容安全三开关
* 业务规则：Wi-Fi 产品写角色 → 409（厂商智能体负责角色）；未配置密钥的
  供应商 → 409（选定后对话会安全失败，必须在保存时拦住）
* 租户隔离：访问他租户产品 → 404（不返回 403，避免探测他人资源是否存在）

全程离线：注册表里只有 ``mock`` 供应商（conftest 钉死
``AI_DEFAULT_PROVIDER=mock``、厂商凭证全空），不依赖任何外部 API key。
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id
from app.models.ai import AiConfig
from app.models.audit import AuditLog
from app.models.catalog import ClientProduct, ProductTemplate
from app.models.enums import NetworkType
from tests.conftest import API_PREFIX

pytestmark = pytest.mark.integration

MERCHANT = f"{API_PREFIX}/merchant"

#: 种子商户管理员固定属于 t-001（见 app/db/seed.py 的 _seed_admin_users）。
MERCHANT_TENANT = "t-001"
OTHER_TENANT = "t-002"


@pytest.fixture
async def it_products(db: AsyncSession) -> dict[str, ClientProduct]:
    """为种子商户（t-001）准备两个**还没配过 AI** 的产品，外加一个他租户产品。

    模板刻意**不绑定云服务商**（``cloud_provider_id=None``）：供应商解析会落到
    平台默认（mock，已配置），``status=ENABLED`` 才能保存成功。
    """
    products: dict[str, ClientProduct] = {}
    for key, network_type in (("4g", NetworkType.FOUR_G), ("wifi", NetworkType.WIFI)):
        template = ProductTemplate(
            id=new_id("product_template"),
            code=f"TPL-IT-{key.upper()}",
            name=f"集成测试 {network_type.value} 模板",
            network_type=str(network_type),
        )
        db.add(template)
        product = ClientProduct(
            id=new_id("client_product"),
            tenant_id=MERCHANT_TENANT,
            template_id=template.id,
            code=f"CP-IT-{key.upper()}",
            name=f"集成测试 {network_type.value} 故事机",
            network_type=template.network_type,
            cloud_provider_id=None,
        )
        db.add(product)
        products[key] = product

    foreign_template = ProductTemplate(
        id=new_id("product_template"),
        code="TPL-IT-FOREIGN",
        name="他租户模板",
        network_type=str(NetworkType.FOUR_G),
    )
    db.add(foreign_template)
    products["foreign"] = ClientProduct(
        id=new_id("client_product"),
        tenant_id=OTHER_TENANT,
        template_id=foreign_template.id,
        code="CP-IT-FOREIGN",
        name="他租户产品",
        network_type=foreign_template.network_type,
        cloud_provider_id=None,
    )
    db.add(products["foreign"])
    await db.commit()
    return products


def _url(product: ClientProduct) -> str:
    return f"{MERCHANT}/products/{product.id}/ai-config"


async def _audits(db: AsyncSession, action: str) -> list[AuditLog]:
    rows = await db.execute(
        select(AuditLog).where(AuditLog.action == action, AuditLog.resource_type == "ai_config")
    )
    return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# 核心链路：默认视图 → 首次保存 → 持久化 → 审计
# ---------------------------------------------------------------------------


async def test_unconfigured_product_returns_default_view(
    client: AsyncClient, auth: Any, it_products: dict[str, ClientProduct]
) -> None:
    """没配过的产品 GET 给默认视图而不是 404——否则配置页打不开。"""
    response = await client.get(_url(it_products["4g"]), headers=await auth.merchant_headers())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "DISABLED"
    assert body["temperature"] is None
    assert body["systemPrompt"] is None
    # 默认视图也要如实给出解析到的平台默认供应商与其健康态
    assert body["providerCode"] == "mock"
    assert body["providerConfigured"] is True
    assert body["providerHealth"] == "UP"
    assert body["safety"] == {"enabled": True, "sensitiveWords": True, "llmReview": False}
    assert body["updatedAt"] is None


async def test_save_ai_config_full_chain(
    client: AsyncClient, auth: Any, db: AsyncSession, it_products: dict[str, ClientProduct]
) -> None:
    """★ 核心验收：一次「保存」走完 HTTP → 权限 → 服务层 → 落库 → 审计。"""
    product = it_products["4g"]
    headers = await auth.merchant_headers()

    saved = await client.put(
        _url(product),
        headers=headers,
        json={
            "providerCode": "mock",
            "systemPrompt": "你是儿童故事机，只讲温暖的故事。",
            "greeting": "你好呀，我们开始讲故事吧",
            "temperature": 1.2,
            "maxTokens": 512,
            "status": "ENABLED",
        },
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["providerCode"] == "mock"
    assert body["systemPrompt"] == "你是儿童故事机，只讲温暖的故事。"
    assert body["greeting"] == "你好呀，我们开始讲故事吧"
    assert body["temperature"] == 1.2
    assert body["maxTokens"] == 512
    assert body["status"] == "ENABLED"
    assert body["updatedAt"] is not None

    # ---- 落库校验：temperature 以小数入参、×100 整数落库 ----
    config = (
        await db.execute(select(AiConfig).where(AiConfig.client_product_id == product.id))
    ).scalar_one()
    assert config.provider_code == "mock"
    assert config.temperature == 120
    assert config.max_tokens == 512
    assert config.status == "ENABLED"

    # ---- 启用要同步产品的 ai_enabled 开关（激活链路的判据） ----
    await db.refresh(product)
    assert product.ai_enabled is True

    # ---- 审计：首次保存写 CREATE，resource_id 指向配置本身 ----
    creates = await _audits(db, "CREATE")
    assert len(creates) == 1
    assert creates[0].resource_id == config.id

    # ---- 再 GET 一次：刷新页面后看到的是持久化后的值，而不是缓存 ----
    reread = await client.get(_url(product), headers=headers)
    assert reread.status_code == 200, reread.text
    assert reread.json()["systemPrompt"] == "你是儿童故事机，只讲温暖的故事。"
    assert reread.json()["status"] == "ENABLED"


async def test_partial_update_keeps_untouched_fields(
    client: AsyncClient, auth: Any, it_products: dict[str, ClientProduct]
) -> None:
    """部分更新：只传 greeting，供应商 / 温度等已保存字段必须原样保留。"""
    product = it_products["4g"]
    headers = await auth.merchant_headers()
    first = await client.put(
        _url(product),
        headers=headers,
        json={"providerCode": "mock", "temperature": 1.2, "systemPrompt": "原始提示词"},
    )
    assert first.status_code == 200, first.text

    second = await client.put(_url(product), headers=headers, json={"greeting": "只改问候"})
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["greeting"] == "只改问候"
    assert body["providerCode"] == "mock"
    assert body["temperature"] == 1.2
    assert body["systemPrompt"] == "原始提示词"


async def test_explicit_null_clears_field(
    client: AsyncClient, auth: Any, it_products: dict[str, ClientProduct]
) -> None:
    """显式传 ``null`` 表示清空——与「未传不动」是两种语义，都要守住。"""
    product = it_products["4g"]
    headers = await auth.merchant_headers()
    first = await client.put(
        _url(product), headers=headers, json={"systemPrompt": "待清空的提示词"}
    )
    assert first.status_code == 200, first.text

    cleared = await client.put(_url(product), headers=headers, json={"systemPrompt": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["systemPrompt"] is None


async def test_disable_syncs_product_ai_enabled(
    client: AsyncClient, auth: Any, db: AsyncSession, it_products: dict[str, ClientProduct]
) -> None:
    """停用 AI 后产品开关同步关闭——否则停用产品在激活链路仍被当成 AI 产品。"""
    product = it_products["4g"]
    headers = await auth.merchant_headers()
    enabled = await client.put(_url(product), headers=headers, json={"status": "ENABLED"})
    assert enabled.status_code == 200, enabled.text
    await db.refresh(product)
    assert product.ai_enabled is True

    disabled = await client.put(_url(product), headers=headers, json={"status": "DISABLED"})
    assert disabled.status_code == 200, disabled.text
    await db.refresh(product)
    assert product.ai_enabled is False


# ---------------------------------------------------------------------------
# 专用端点：prompt / safety
# ---------------------------------------------------------------------------


async def test_prompt_endpoint_only_touches_sampling(
    client: AsyncClient, auth: Any, db: AsyncSession, it_products: dict[str, ClientProduct]
) -> None:
    """``/prompt`` 只动提示词与采样参数，供应商等其余字段不动，且写 UPDATE 审计。"""
    product = it_products["4g"]
    headers = await auth.merchant_headers()
    first = await client.put(
        _url(product), headers=headers, json={"providerCode": "mock", "greeting": "保留我"}
    )
    assert first.status_code == 200, first.text

    updated = await client.put(
        f"{_url(product)}/prompt",
        headers=headers,
        json={"systemPrompt": "新提示词", "temperature": 0.3, "maxTokens": 256},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["systemPrompt"] == "新提示词"
    assert body["temperature"] == 0.3
    assert body["maxTokens"] == 256
    assert body["providerCode"] == "mock"
    assert body["greeting"] == "保留我"

    config = (
        await db.execute(select(AiConfig).where(AiConfig.client_product_id == product.id))
    ).scalar_one()
    assert config.temperature == 30
    assert await _audits(db, "UPDATE"), "prompt 端点也要写审计，不能只改库不留痕"


async def test_safety_endpoint_updates_switches(
    client: AsyncClient, auth: Any, db: AsyncSession, it_products: dict[str, ClientProduct]
) -> None:
    """``/safety`` 三开关改完立即生效（对话链路直接读这三列），且写审计。"""
    product = it_products["4g"]
    headers = await auth.merchant_headers()
    # 先建配置（写 CREATE 审计），再走 safety 端点——此时应写 UPDATE 审计。
    first = await client.put(_url(product), headers=headers, json={"providerCode": "mock"})
    assert first.status_code == 200, first.text

    updated = await client.put(
        f"{_url(product)}/safety",
        headers=headers,
        json={"enabled": True, "sensitiveWords": False, "llmReview": True},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["safety"] == {
        "enabled": True,
        "sensitiveWords": False,
        "llmReview": True,
    }

    config = (
        await db.execute(select(AiConfig).where(AiConfig.client_product_id == product.id))
    ).scalar_one()
    assert config.safety_enabled is True
    assert config.safety_sensitive_words is False
    assert config.safety_llm_review is True
    assert await _audits(db, "UPDATE"), "安全开关是合规敏感配置，必须留审计痕"


# ---------------------------------------------------------------------------
# 业务规则与隔离
# ---------------------------------------------------------------------------


async def test_wifi_product_role_rejected_but_null_allowed(
    client: AsyncClient, auth: Any, it_products: dict[str, ClientProduct]
) -> None:
    """Wi-Fi 产品的角色由厂商智能体配置：写角色 409，显式清空放行。"""
    product = it_products["wifi"]
    headers = await auth.merchant_headers()

    rejected = await client.put(
        f"{_url(product)}/role", headers=headers, json={"rolePresetCode": "any-role"}
    )
    assert rejected.status_code == 409, rejected.text
    body = rejected.json()
    assert body["code"] == "INVALID_STATE_TRANSITION"
    assert body["details"]["networkType"] == "WIFI"

    cleared = await client.put(
        f"{_url(product)}/role", headers=headers, json={"rolePresetCode": None}
    )
    assert cleared.status_code == 200, cleared.text


async def test_unconfigured_provider_rejected(
    client: AsyncClient, auth: Any, it_products: dict[str, ClientProduct]
) -> None:
    """已注册但没配密钥的供应商必须在保存时拦住——选定后对话会安全失败。"""
    response = await client.put(
        _url(it_products["4g"]),
        headers=await auth.merchant_headers(),
        json={"providerCode": "volcano"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "PRODUCT_NOT_AUTHORIZED"


async def test_cross_tenant_product_is_404(
    client: AsyncClient, auth: Any, it_products: dict[str, ClientProduct]
) -> None:
    """访问他租户产品返回 404 而不是 403——不暴露「资源存在但不归你」。"""
    headers = await auth.merchant_headers()
    foreign = _url(it_products["foreign"])

    read = await client.get(foreign, headers=headers)
    assert read.status_code == 404, read.text

    write = await client.put(foreign, headers=headers, json={"greeting": "越权写入"})
    assert write.status_code == 404, write.text


async def test_anonymous_request_is_401(
    client: AsyncClient, it_products: dict[str, ClientProduct]
) -> None:
    """不带令牌直接打保存端点 → 401（权限点之前先过认证这一关）。"""
    response = await client.put(_url(it_products["4g"]), json={"greeting": "匿名写入"})
    assert response.status_code == 401, response.text
