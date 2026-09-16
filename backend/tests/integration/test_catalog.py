"""集成测试：P3 目录域（租户 / 云服务商 / 模板 / 授权 / 客户产品 / 小程序配置）。

覆盖本阶段验收标准
------------------
* 目录闭环全链路：建租户 → 建云服务商 → 建模板 → 授权 → 建客户产品
* ★ 安全红线：``SecretKey`` / ``AppSecret`` 绝不出现在**任何**响应中
  （递归扫描整棵 JSON，既查明文哨兵也查密文字段名与 Fernet 密文特征）
* ★ 级联删除校验（修复 P-06）：被引用对象的 DELETE 返回
  ``409 CASCADE_CONFLICT``，且 ``details`` 给出具体数量
* ★ P-03 验收视角：新建客户产品后，租户详情「建后可见」且计数正确
* 授权前置校验（``PRODUCT_NOT_AUTHORIZED``）、模板停用拦截
* 编码唯一性与规范化、授权幂等、模板快照解耦
* 连通性检测不伪造成功（ADR-07）
* 租户账号与密码管理（一次性密码 / 强制改密 / 重置后旧密码失效）

测试写法对齐 ``tests/integration/test_auth.py``：camelCase 请求与断言、
中文注释说明「为什么这样断言」。
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.models.catalog import CloudProvider, MiniAppConfig
from tests.conftest import API_PREFIX

pytestmark = pytest.mark.integration

PLATFORM = f"{API_PREFIX}/platform"

#: 可识别的哨兵值——一旦出现在响应里就说明发生了密钥泄漏
AK_SENTINEL = "AK_SENTINEL_1234567890"
SK_SENTINEL = "SK_SENTINEL_abcdefghij"
APPSECRET_SENTINEL = "WX_APPSECRET_SENTINEL_0987"

#: 掩码契约：前 4 位明文 + ``****``
AK_HINT = "AK_S****"
SK_HINT = "SK_S****"
APPSECRET_HINT = "WX_A****"

#: 绝不允许出现在响应中的密文字段名（snake / camel 两种写法都查）
_FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "access_key_enc",
        "secret_key_enc",
        "app_secret_enc",
        "accessKeyEnc",
        "secretKeyEnc",
        "appSecretEnc",
        "accessKey",
        "secretKey",
        "appSecret",
    }
)

#: Fernet 密文的固定前缀（配合长度判断，避免把普通短字符串误判为密文）
_FERNET_PREFIX = "gAAAAA"


# ---------------------------------------------------------------------------
# 递归泄漏扫描工具
# ---------------------------------------------------------------------------


def _iter_json_nodes(node: Any) -> list[Any]:
    """递归展开 JSON 结构，返回全部**键名与标量值**（含任意深度的嵌套）。

    只扫第一层是不够的：客户产品详情会内嵌 ``miniappConfig``，
    租户详情会内嵌 ``clientProducts`` 与 ``accounts``，泄漏点可能藏在里面。
    """
    nodes: list[Any] = []
    if isinstance(node, dict):
        for key, value in node.items():
            nodes.append(key)
            nodes.extend(_iter_json_nodes(value))
    elif isinstance(node, list):
        for item in node:
            nodes.extend(_iter_json_nodes(item))
    else:
        nodes.append(node)
    return nodes


def assert_no_secret_leak(payload: Any, *, where: str) -> None:
    """递归断言响应中既无明文哨兵值，也无密文字段名与密文本身。"""
    for node in _iter_json_nodes(payload):
        if isinstance(node, str):
            for sentinel in (AK_SENTINEL, SK_SENTINEL, APPSECRET_SENTINEL):
                assert sentinel not in node, f"{where}：响应出现明文密钥「{sentinel}」"
            assert node not in _FORBIDDEN_FIELD_NAMES, (
                f"{where}：响应出现密钥字段名「{node}」"
            )
            assert not (node.startswith(_FERNET_PREFIX) and len(node) > 60), (
                f"{where}：响应出现 Fernet 密文"
            )


def test_recursive_scanner_detects_nested_secrets() -> None:
    """自检：扫描器必须能发现**嵌套**在数组 / 对象里的哨兵（避免断言形同虚设）。"""
    nested = {"records": [{"miniappConfig": {"appSecretHint": APPSECRET_SENTINEL}}]}
    with pytest.raises(AssertionError):
        assert_no_secret_leak(nested, where="自检")
    assert_no_secret_leak({"accessKeyHint": APPSECRET_HINT}, where="自检")


def assert_no_ciphertext(text: str, *, where: str) -> None:
    """兜底：整段响应文本（含未被 JSON 解析的部分）不得出现密文特征。"""
    assert _FERNET_PREFIX not in text, f"{where}：响应文本出现 Fernet 密文特征"


# ---------------------------------------------------------------------------
# 建链辅助函数（模块级，避免污染 conftest）
# ---------------------------------------------------------------------------


async def _create_tenant(
    client: AsyncClient,
    headers: dict[str, str],
    code: str,
    *,
    name: str = "集成测试租户",
    **extra: Any,
) -> dict[str, Any]:
    response = await client.post(
        f"{PLATFORM}/tenants",
        headers=headers,
        json={"code": code, "name": name, **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_cloud(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    code: str = "CLOUD-IT",
    name: str = "集贤云账号",
    vendor: str = "JIXIAN",
    network_type: str = "4G",
    with_credentials: bool = True,
    with_api_base: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """建云服务商。

    刻意让 ``with_credentials`` 与 ``with_api_base`` 互斥地使用，
    以保证任何用例都不会触发真实网络探测（``_probe`` 需二者同时具备）。
    """
    payload: dict[str, Any] = {
        "code": code,
        "name": name,
        "vendor": vendor,
        "networkType": network_type,
    }
    if with_credentials:
        payload["accessKey"] = AK_SENTINEL
        payload["secretKey"] = SK_SENTINEL
    if with_api_base:
        payload["apiBase"] = "https://vendor.invalid/api"
    payload.update(extra)

    response = await client.post(f"{PLATFORM}/clouds", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def _create_template(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    code: str = "TPL-IT",
    name: str = "AI 故事机",
    cloud_provider_id: str | None = None,
    firmware_version: str | None = "1.0.0",
    status: str = "ENABLED",
    network_type: str = "4G",
    category: str = "故事机",
    model: str = "TS-100",
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": code,
        "name": name,
        "category": category,
        "model": model,
        "networkType": network_type,
        "firmwareVersion": firmware_version,
        "referencePrice": 199.0,
        "status": status,
        **extra,
    }
    if cloud_provider_id:
        payload["cloudProviderId"] = cloud_provider_id
    response = await client.post(f"{PLATFORM}/templates", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def _authorize(
    client: AsyncClient,
    headers: dict[str, str],
    template_id: str,
    tenant_ids: list[str],
    *,
    max_devices: int | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"tenantIds": tenant_ids}
    if max_devices is not None:
        body["maxDevices"] = max_devices
    response = await client.post(
        f"{PLATFORM}/templates/{template_id}/authorize", headers=headers, json=body
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _create_product(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    tenant_id: str,
    template_id: str,
    code: str = "PROD-IT-001",
    name: str | None = None,
    expect: int = 201,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tenantId": tenant_id,
        "templateId": template_id,
        "code": code,
    }
    if name is not None:
        payload["name"] = name
    response = await client.post(
        f"{PLATFORM}/client-products", headers=headers, json=payload
    )
    assert response.status_code == expect, response.text
    return response.json()


async def _list_authorization_id(
    client: AsyncClient, headers: dict[str, str], template_id: str
) -> str:
    response = await client.get(
        f"{PLATFORM}/templates/{template_id}/authorizations", headers=headers
    )
    assert response.status_code == 200, response.text
    records = response.json()["records"]
    assert records, "模板应当至少有一条授权记录"
    return str(records[0]["id"])


async def _page(
    client: AsyncClient, headers: dict[str, str], path: str, **params: Any
) -> dict[str, Any]:
    """请求一个分页列表端点并返回响应体（顺带校验 200）。"""
    response = await client.get(path, headers=headers, params=params)
    assert response.status_code == 200, f"GET {path} {params}：{response.text}"
    return response.json()


async def _total(
    client: AsyncClient, headers: dict[str, str], path: str, **params: Any
) -> int:
    """请求一个分页列表端点并只取 ``total``。"""
    return int((await _page(client, headers, path, **params))["total"])


# ===========================================================================
# 一、目录闭环全链路
# ===========================================================================


class TestCatalogHappyPath:
    """建租户 → 建云服务商 → 建模板 → 授权 → 建客户产品。"""

    async def test_full_catalog_chain(self, client: AsyncClient, auth) -> None:
        headers = await auth.platform_headers()

        tenant = await _create_tenant(
            client, headers, "CHAIN-01", name="闭环租户", industry="玩具品牌商"
        )
        assert tenant["code"] == "CHAIN-01"
        assert tenant["status"] == "ACTIVE"

        cloud = await _create_cloud(client, headers, code="CLOUD-CHAIN")
        assert cloud["code"] == "CLOUD-CHAIN"
        assert cloud["hasCredentials"] is True
        assert cloud["accessKeyHint"] == AK_HINT

        template = await _create_template(
            client, headers, code="TPL-CHAIN", cloud_provider_id=cloud["id"]
        )
        assert template["networkType"] == "4G"
        assert template["cloudProviderId"] == cloud["id"]
        # 模板列表 / 详情会补齐云服务商名称
        detail = await client.get(f"{PLATFORM}/templates/{template['id']}", headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["cloudProviderName"] == cloud["name"]

        outcome = await _authorize(client, headers, template["id"], [tenant["id"]])
        assert outcome["created"] == [tenant["id"]]
        assert outcome["skipped"] == []
        assert outcome["denied"] == []
        assert outcome["totalAuthorized"] == 1

        product = await _create_product(
            client,
            headers,
            tenant_id=tenant["id"],
            template_id=template["id"],
            code="PROD-CHAIN-001",
            name="闭环客户产品",
        )
        assert product["tenantId"] == tenant["id"]
        assert product["tenantName"] == tenant["name"]
        assert product["templateId"] == template["id"]
        assert product["cloudProviderName"] == cloud["name"]
        # 快照字段：联网方式 / 固件版本从模板复制
        assert product["networkType"] == "4G"
        assert product["firmwareVersion"] == "1.0.0"
        assert product["status"] == "ENABLED"

    async def test_template_snapshot_does_not_track_later_changes(
        self, client: AsyncClient, auth
    ) -> None:
        """快照语义：改模板固件版本，已创建的客户产品不被追溯修改。"""
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "SNAP-01")
        template = await _create_template(
            client, headers, code="TPL-SNAP", firmware_version="1.0.0"
        )
        await _authorize(client, headers, template["id"], [tenant["id"]])
        product = await _create_product(
            client, headers, tenant_id=tenant["id"], template_id=template["id"], code="PROD-SNAP"
        )
        assert product["firmwareVersion"] == "1.0.0"

        updated = await client.put(
            f"{PLATFORM}/templates/{template['id']}",
            headers=headers,
            json={"firmwareVersion": "9.9.9"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["firmwareVersion"] == "9.9.9"

        after = await client.get(f"{PLATFORM}/client-products/{product['id']}", headers=headers)
        assert after.status_code == 200, after.text
        assert after.json()["firmwareVersion"] == "1.0.0", "客户产品固件版本应保持创建时的快照"

    async def test_template_detail_reports_cloud_name_and_product_count(
        self, client: AsyncClient, auth
    ) -> None:
        """回归保护：模板**详情**必须与列表一致地补齐 ``cloudProviderName`` 与计数。

        早期实现里 ``GET /platform/templates/{id}`` 直接返回模板 ORM 转换结果，
        既不查云服务商名称也不统计，导致同一字段「列表有、详情为 null」。

        ★ 与 ``TestClientProductVisibility::test_template_detail_fills_cloud_name_and_counts``
        的分工（两条都不可删）：那条覆盖「**创建后**」的装配与「列表 / 详情一致」；
        本用例额外覆盖「**更新（PUT）后**」的装配——缺陷 4 的另一半，
        若更新路径不再装配就会在此复发。
        """
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "TPLDETAIL-01")
        cloud = await _create_cloud(client, headers, code="CLOUD-TPLDETAIL")
        template = await _create_template(
            client, headers, code="TPL-DETAIL", cloud_provider_id=cloud["id"]
        )

        # 创建路径本身也应带出云服务商名称
        assert template["cloudProviderName"] == cloud["name"]

        await _authorize(client, headers, template["id"], [tenant["id"]])
        await _create_product(
            client,
            headers,
            tenant_id=tenant["id"],
            template_id=template["id"],
            code="PROD-DETAIL",
        )

        detail = await client.get(f"{PLATFORM}/templates/{template['id']}", headers=headers)
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["cloudProviderName"] == cloud["name"], "详情必须补齐云服务商名称"
        assert body["cloudProviderId"] == cloud["id"]
        assert body["clientProductCount"] == 1, "详情必须统计派生的客户产品数"
        assert body["authorizationCount"] == 1, "详情必须统计被授权租户数"

        # 更新路径同样走详情装配，避免「详情有、更新没有」
        updated = await client.put(
            f"{PLATFORM}/templates/{template['id']}",
            headers=headers,
            json={"remark": "仅改备注"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["cloudProviderName"] == cloud["name"]
        assert updated.json()["clientProductCount"] == 1


# ===========================================================================
# 二、★ 安全红线：密钥绝不出现在任何响应中
# ===========================================================================


class TestSecretNeverLeaks:
    """``SecretKey`` / ``AccessKey`` / ``AppSecret`` 的响应侧防泄漏。"""

    async def test_cloud_endpoints_never_expose_secret(self, client: AsyncClient, auth) -> None:
        headers = await auth.platform_headers()
        # 有密钥但无 apiBase：任何 /test 调用都会走 NOT_CONFIGURED 分支，不发真实请求
        cloud = await _create_cloud(client, headers, code="CLOUD-LEAK")
        cloud_id = cloud["id"]
        assert_no_secret_leak(cloud, where="云服务商创建")

        responses = {
            "列表": (
                await client.get(f"{PLATFORM}/clouds", headers=headers),
                200,
            ),
            "详情": (
                await client.get(f"{PLATFORM}/clouds/{cloud_id}", headers=headers),
                200,
            ),
            "更新": (
                await client.put(
                    f"{PLATFORM}/clouds/{cloud_id}",
                    headers=headers,
                    json={"remark": "仅改备注"},
                ),
                200,
            ),
            "连通性检测": (
                await client.post(f"{PLATFORM}/clouds/{cloud_id}/test", headers=headers),
                200,
            ),
        }

        for where, (response, expected) in responses.items():
            assert response.status_code == expected, response.text
            assert_no_ciphertext(response.text, where=f"云服务商{where}")
            assert_no_secret_leak(response.json(), where=f"云服务商{where}")

        body = responses["详情"][0].json()
        assert body["accessKeyHint"] == AK_HINT
        assert body["secretKeyHint"] == SK_HINT
        assert body["hasCredentials"] is True
        assert "secretKey" not in body and "secretKeyEnc" not in body

        # 更新后掩码不变（未提供新密钥 = 保持原值）
        assert responses["更新"][0].json()["secretKeyHint"] == SK_HINT

    async def test_providing_new_key_refreshes_hint_and_resets_status(
        self, client: AsyncClient, auth
    ) -> None:
        """提供新密钥会刷新掩码，并把接入状态退回「未连接」要求重新检测。"""
        headers = await auth.platform_headers()
        cloud = await _create_cloud(client, headers, code="CLOUD-ROTATE")

        response = await client.put(
            f"{PLATFORM}/clouds/{cloud['id']}",
            headers=headers,
            json={"accessKey": "NEW_AK_9999", "secretKey": "NEW_SK_8888"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["accessKeyHint"] == "NEW_****"
        assert body["secretKeyHint"] == "NEW_****"
        assert body["status"] == "NOT_CONNECTED"
        assert_no_secret_leak(body, where="更新云服务商（轮换密钥）")

    async def test_miniapp_secret_never_exposes_and_keeps_value_when_omitted(
        self, client: AsyncClient, auth
    ) -> None:
        """``AppSecret`` 只回掩码；再次 PUT 不传 appSecret 时保持原值。"""
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "MINI-01")
        template = await _create_template(client, headers, code="TPL-MINI")
        await _authorize(client, headers, template["id"], [tenant["id"]])
        product = await _create_product(
            client, headers, tenant_id=tenant["id"], template_id=template["id"], code="PROD-MINI"
        )
        product_id = product["id"]

        created = await client.put(
            f"{PLATFORM}/client-products/{product_id}/miniapp-config",
            headers=headers,
            json={
                "appName": "星辰玩具小程序",
                "appId": "wx0123456789abcdef",
                "appSecret": APPSECRET_SENTINEL,
                "themeColor": "#4F46E5",
            },
        )
        assert created.status_code == 200, created.text
        assert_no_ciphertext(created.text, where="保存小程序配置")
        assert_no_secret_leak(created.json(), where="保存小程序配置")
        assert created.json()["appSecretHint"] == APPSECRET_HINT
        assert created.json()["hasAppSecret"] is True

        fetched = await client.get(
            f"{PLATFORM}/client-products/{product_id}/miniapp-config", headers=headers
        )
        assert fetched.status_code == 200, fetched.text
        assert_no_secret_leak(fetched.json(), where="查询小程序配置")
        assert fetched.json()["appSecretHint"] == APPSECRET_HINT

        # 客户产品详情内嵌 miniappConfig，同样不得泄漏（嵌套扫描的价值所在）
        detail = await client.get(
            f"{PLATFORM}/client-products/{product_id}", headers=headers
        )
        assert detail.status_code == 200, detail.text
        assert_no_secret_leak(detail.json(), where="客户产品详情")
        embedded = detail.json()["miniappConfig"]
        assert embedded["appSecretHint"] == APPSECRET_HINT
        assert detail.json()["hasMiniappConfig"] is True

        # 不传 appSecret 的 upsert 应保持原值（掩码不变）
        updated = await client.put(
            f"{PLATFORM}/client-products/{product_id}/miniapp-config",
            headers=headers,
            json={"appName": "星辰玩具小程序（改名）", "themeColor": "#111111"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["appSecretHint"] == APPSECRET_HINT
        assert updated.json()["appName"] == "星辰玩具小程序（改名）"
        assert updated.json()["hasAppSecret"] is True

    async def test_unconfigured_miniapp_returns_null_and_no_hint(
        self, client: AsyncClient, auth
    ) -> None:
        """尚未配置时返回 ``null``（「还没配置」是正常状态，不是错误）。"""
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "MINI-02")
        template = await _create_template(client, headers, code="TPL-MINI2")
        await _authorize(client, headers, template["id"], [tenant["id"]])
        product = await _create_product(
            client, headers, tenant_id=tenant["id"], template_id=template["id"], code="PROD-MINI2"
        )

        response = await client.get(
            f"{PLATFORM}/client-products/{product['id']}/miniapp-config", headers=headers
        )
        assert response.status_code == 200, response.text
        assert response.json() is None


# ===========================================================================
# 三、★ 级联删除校验（P-06）
# ===========================================================================


class TestCascadeDeleteGuard:
    """有关联数据时删除必须失败，并说明「还剩什么」。"""

    async def test_delete_tenant_with_account_is_rejected(
        self, client: AsyncClient, auth
    ) -> None:
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "DEL-TENANT")
        created = await client.post(
            f"{PLATFORM}/tenants/{tenant['id']}/accounts",
            headers=headers,
            json={"account": "13900002222", "roleCode": "MERCHANT_ADMIN"},
        )
        assert created.status_code == 201, created.text

        response = await client.delete(f"{PLATFORM}/tenants/{tenant['id']}", headers=headers)
        assert response.status_code == 409, response.text
        body = response.json()
        assert body["code"] == "CASCADE_CONFLICT"
        assert body["details"]["users"] == 1
        assert body["details"]["clientProducts"] == 0

    async def test_delete_cloud_referenced_by_template_is_rejected(
        self, client: AsyncClient, auth
    ) -> None:
        headers = await auth.platform_headers()
        cloud = await _create_cloud(client, headers, code="CLOUD-DEL")
        await _create_template(client, headers, code="TPL-DEL", cloud_provider_id=cloud["id"])

        response = await client.delete(f"{PLATFORM}/clouds/{cloud['id']}", headers=headers)
        assert response.status_code == 409, response.text
        body = response.json()
        assert body["code"] == "CASCADE_CONFLICT"
        assert body["details"]["templates"] == 1

    async def test_delete_template_with_authorization_is_rejected(
        self, client: AsyncClient, auth
    ) -> None:
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "DEL-TPL")
        template = await _create_template(client, headers, code="TPL-DEL2")
        await _authorize(client, headers, template["id"], [tenant["id"]])

        response = await client.delete(f"{PLATFORM}/templates/{template['id']}", headers=headers)
        assert response.status_code == 409, response.text
        body = response.json()
        assert body["code"] == "CASCADE_CONFLICT"
        assert body["details"]["authorizations"] == 1
        assert body["details"]["clientProducts"] == 0

    async def test_revoke_authorization_with_client_product_is_rejected(
        self, client: AsyncClient, auth
    ) -> None:
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "DEL-AUTH")
        template = await _create_template(client, headers, code="TPL-DEL3")
        await _authorize(client, headers, template["id"], [tenant["id"]])
        await _create_product(
            client, headers, tenant_id=tenant["id"], template_id=template["id"], code="PROD-DEL3"
        )
        authorization_id = await _list_authorization_id(client, headers, template["id"])

        response = await client.delete(
            f"{PLATFORM}/authorizations/{authorization_id}", headers=headers
        )
        assert response.status_code == 409, response.text
        body = response.json()
        assert body["code"] == "CASCADE_CONFLICT"
        assert body["details"]["clientProducts"] == 1

    async def test_revoke_unused_authorization_succeeds(
        self, client: AsyncClient, auth
    ) -> None:
        """反向验证：无客户产品的授权可以正常撤销（校验不是「一律拒绝」）。"""
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "DEL-AUTH-OK")
        template = await _create_template(client, headers, code="TPL-DEL4")
        await _authorize(client, headers, template["id"], [tenant["id"]])
        authorization_id = await _list_authorization_id(client, headers, template["id"])

        response = await client.delete(
            f"{PLATFORM}/authorizations/{authorization_id}", headers=headers
        )
        assert response.status_code == 200, response.text

        remaining = await client.get(
            f"{PLATFORM}/templates/{template['id']}/authorizations", headers=headers
        )
        assert remaining.json()["total"] == 0

    async def test_delete_unreferenced_tenant_succeeds(
        self, client: AsyncClient, auth
    ) -> None:
        """无任何关联的租户可正常删除。"""
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "DEL-PLAIN")
        response = await client.delete(f"{PLATFORM}/tenants/{tenant['id']}", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["success"] is True

        missing = await client.get(f"{PLATFORM}/tenants/{tenant['id']}", headers=headers)
        assert missing.status_code == 404
        assert missing.json()["code"] == "RESOURCE_NOT_FOUND"


# ===========================================================================
# 四、★ P-03 验收视角：建后客户详情可见
# ===========================================================================


class TestClientProductVisibility:
    """新建客户产品后，租户详情必须能看到它。"""

    async def test_created_product_visible_in_tenant_detail(
        self, client: AsyncClient, auth
    ) -> None:
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "P03-01")
        template = await _create_template(client, headers, code="TPL-P03")
        await _authorize(client, headers, template["id"], [tenant["id"]])
        product = await _create_product(
            client, headers, tenant_id=tenant["id"], template_id=template["id"], code="PROD-P03"
        )

        detail = await client.get(f"{PLATFORM}/tenants/{tenant['id']}", headers=headers)
        assert detail.status_code == 200, detail.text
        body = detail.json()

        assert body["counts"]["clientProducts"] == 1
        assert body["counts"]["authorizations"] == 1
        codes = {item["code"] for item in body["clientProducts"]}
        assert "PROD-P03" in codes, "P-03：新建的客户产品必须在租户详情中可见"
        assert product["id"] in {item["id"] for item in body["clientProducts"]}

    async def test_template_detail_fills_cloud_name_and_counts(
        self, client: AsyncClient, auth
    ) -> None:
        """回归保护：模板详情必须补齐 ``cloudProviderName`` 与两个关联计数。

        早期版本只有列表端点填这些字段，详情端点返回 ``null`` / ``0``，
        造成「同一字段在列表有、详情没有」。这里同时锁住「建客户产品后
        ``clientProductCount`` 变为 1」，并校验列表与详情结论一致。

        ★ 与 ``TestCatalogHappyPath::test_template_detail_reports_cloud_name_and_product_count``
        的分工（两条都不可删）：本用例覆盖「**创建后**」装配 + 列表/详情一致性；
        那条额外覆盖「**更新（PUT）后**」的装配。合并会丢掉 PUT 这一半。
        """
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "TPLDET-01")
        cloud = await _create_cloud(client, headers, code="CLOUD-TPLDET")
        template = await _create_template(
            client, headers, code="TPL-DET", cloud_provider_id=cloud["id"]
        )
        await _authorize(client, headers, template["id"], [tenant["id"]])

        before = (await client.get(f"{PLATFORM}/templates/{template['id']}", headers=headers)).json()
        assert before["cloudProviderId"] == cloud["id"]
        assert before["cloudProviderName"] == cloud["name"], "模板详情必须回显云服务商名称"
        assert before["authorizationCount"] == 1, "模板详情必须回显被授权租户数"
        assert before["clientProductCount"] == 0

        await _create_product(
            client, headers, tenant_id=tenant["id"], template_id=template["id"], code="PROD-DET"
        )

        after = (await client.get(f"{PLATFORM}/templates/{template['id']}", headers=headers)).json()
        assert after["clientProductCount"] == 1, "建后模板详情的客户产品数应为 1"
        assert after["cloudProviderName"] == cloud["name"]
        assert after["authorizationCount"] == 1

        listed = await client.get(
            f"{PLATFORM}/templates", headers=headers, params={"keyword": "TPL-DET"}
        )
        record = next(r for r in listed.json()["records"] if r["id"] == template["id"])
        assert record["cloudProviderName"] == after["cloudProviderName"]
        assert record["clientProductCount"] == after["clientProductCount"]
        assert record["authorizationCount"] == after["authorizationCount"]

    async def test_product_list_filters_by_tenant(self, client: AsyncClient, auth) -> None:
        """客户产品列表按``tenantId``筛选，不串台。"""
        headers = await auth.platform_headers()
        tenant_a = await _create_tenant(client, headers, "FILTER-A")
        tenant_b = await _create_tenant(client, headers, "FILTER-B")
        template = await _create_template(client, headers, code="TPL-FILTER")
        await _authorize(
            client, headers, template["id"], [tenant_a["id"], tenant_b["id"]]
        )
        await _create_product(
            client, headers, tenant_id=tenant_a["id"], template_id=template["id"], code="PROD-FA"
        )
        await _create_product(
            client, headers, tenant_id=tenant_b["id"], template_id=template["id"], code="PROD-FB"
        )

        response = await client.get(
            f"{PLATFORM}/client-products",
            headers=headers,
            params={"tenantId": tenant_a["id"]},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 1
        assert body["records"][0]["tenantId"] == tenant_a["id"]

        all_products = await client.get(f"{PLATFORM}/client-products", headers=headers)
        assert all_products.json()["total"] == 2


# ===========================================================================
# 五、授权前置校验
# ===========================================================================


class TestAuthorizationGuards:
    """授权是创建客户产品的前置条件。"""

    async def test_create_product_without_authorization_is_rejected(
        self, client: AsyncClient, auth
    ) -> None:
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "AUTH-01")
        template = await _create_template(client, headers, code="TPL-AUTH")
        # 刻意不调用 /authorize

        response = await client.post(
            f"{PLATFORM}/client-products",
            headers=headers,
            json={"tenantId": tenant["id"], "templateId": template["id"], "code": "PROD-AUTH"},
        )
        assert response.status_code == 409, response.text
        body = response.json()
        assert body["code"] == "PRODUCT_NOT_AUTHORIZED"
        assert body["details"]["tenantId"] == tenant["id"]
        assert body["details"]["templateId"] == template["id"]

    async def test_create_product_with_disabled_template_is_rejected(
        self, client: AsyncClient, auth
    ) -> None:
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "AUTH-02")
        template = await _create_template(
            client, headers, code="TPL-DISABLED", status="DISABLED"
        )
        await _authorize(client, headers, template["id"], [tenant["id"]])

        response = await client.post(
            f"{PLATFORM}/client-products",
            headers=headers,
            json={"tenantId": tenant["id"], "templateId": template["id"], "code": "PROD-DIS"},
        )
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "VALIDATION_ERROR"

    async def test_create_product_for_disabled_tenant_is_rejected(
        self, client: AsyncClient, auth
    ) -> None:
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "AUTH-03")
        template = await _create_template(client, headers, code="TPL-AUTH3")
        await _authorize(client, headers, template["id"], [tenant["id"]])

        disabled = await client.post(
            f"{PLATFORM}/tenants/{tenant['id']}/status",
            headers=headers,
            json={"status": "DISABLED", "reason": "欠费"},
        )
        assert disabled.status_code == 200, disabled.text

        response = await client.post(
            f"{PLATFORM}/client-products",
            headers=headers,
            json={"tenantId": tenant["id"], "templateId": template["id"], "code": "PROD-DT"},
        )
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "VALIDATION_ERROR"

    async def test_authorize_to_unknown_tenant_is_denied_not_created(
        self, client: AsyncClient, auth
    ) -> None:
        """不存在的租户计入 ``denied``，而不是静默创建一条悬空授权。"""
        headers = await auth.platform_headers()
        template = await _create_template(client, headers, code="TPL-DENY")

        outcome = await _authorize(client, headers, template["id"], ["tenant-not-exist"])
        assert outcome["created"] == []
        assert outcome["denied"] == ["tenant-not-exist"]
        assert outcome["totalAuthorized"] == 0

    async def test_repeated_authorize_is_idempotent(self, client: AsyncClient, auth) -> None:
        """同一批租户重复授权：第二次 ``created == []``，租户进入 ``skipped``，不报错。"""
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "IDEM-01")
        template = await _create_template(client, headers, code="TPL-IDEM")

        first = await _authorize(client, headers, template["id"], [tenant["id"]])
        assert first["created"] == [tenant["id"]]
        assert first["totalAuthorized"] == 1

        second = await _authorize(client, headers, template["id"], [tenant["id"]])
        assert second["created"] == []
        assert second["skipped"] == [tenant["id"]]
        assert second["denied"] == []
        assert second["totalAuthorized"] == 1

        records = await client.get(
            f"{PLATFORM}/templates/{template['id']}/authorizations", headers=headers
        )
        assert records.json()["total"] == 1, "幂等授权不应产生重复记录"


# ===========================================================================
# 六、编码唯一性与规范化
# ===========================================================================


class TestCodeRules:
    """编码唯一性、规范化与重复校验。"""

    async def test_duplicate_tenant_code_returns_conflict(
        self, client: AsyncClient, auth
    ) -> None:
        headers = await auth.platform_headers()
        await _create_tenant(client, headers, "UNIQ-01")

        response = await client.post(
            f"{PLATFORM}/tenants",
            headers=headers,
            json={"code": "UNIQ-01", "name": "重复编码"},
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "TENANT_CODE_EXISTS"

    async def test_tenant_code_is_normalized_to_uppercase(
        self, client: AsyncClient, auth
    ) -> None:
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "demo-x")
        assert tenant["code"] == "DEMO-X"

        # 规范化后与已有编码冲突（大小写不构成两个租户）
        response = await client.post(
            f"{PLATFORM}/tenants",
            headers=headers,
            json={"code": "DEMO-X", "name": "大小写重复"},
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "TENANT_CODE_EXISTS"

    async def test_duplicate_client_product_code_returns_conflict(
        self, client: AsyncClient, auth
    ) -> None:
        """产品编码重复 → 409 PRODUCT_CODE_EXISTS（唯一键冲突，不是参数校验失败）。"""
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "UNIQ-02")
        template = await _create_template(client, headers, code="TPL-UNIQ")
        await _authorize(client, headers, template["id"], [tenant["id"]])
        await _create_product(
            client, headers, tenant_id=tenant["id"], template_id=template["id"], code="PROD-UNIQ"
        )

        body = await _create_product(
            client,
            headers,
            tenant_id=tenant["id"],
            template_id=template["id"],
            code="PROD-UNIQ",
            expect=409,
        )
        assert body["code"] == "PRODUCT_CODE_EXISTS"
        assert body["details"] == {"field": "code", "message": "编码已存在"}

    async def test_duplicate_cloud_code_returns_conflict(
        self, client: AsyncClient, auth
    ) -> None:
        """云服务商编码重复 → 409 CLOUD_CODE_EXISTS。"""
        headers = await auth.platform_headers()
        await _create_cloud(client, headers, code="CLOUD-UNIQ")

        response = await client.post(
            f"{PLATFORM}/clouds",
            headers=headers,
            json={"code": "CLOUD-UNIQ", "name": "重复云服务商", "vendor": "JIXIAN"},
        )
        assert response.status_code == 409, response.text
        body = response.json()
        assert body["code"] == "CLOUD_CODE_EXISTS"
        assert body["details"] == {"field": "code", "message": "编码已存在"}

    async def test_duplicate_template_code_returns_conflict(
        self, client: AsyncClient, auth
    ) -> None:
        """模板编码重复 → 409 TEMPLATE_CODE_EXISTS。"""
        headers = await auth.platform_headers()
        await _create_template(client, headers, code="TPL-UNIQ2")

        response = await client.post(
            f"{PLATFORM}/templates",
            headers=headers,
            json={"code": "TPL-UNIQ2", "name": "重复模板", "networkType": "WIFI"},
        )
        assert response.status_code == 409, response.text
        body = response.json()
        assert body["code"] == "TEMPLATE_CODE_EXISTS"
        assert body["details"] == {"field": "code", "message": "编码已存在"}

    async def test_template_with_unknown_cloud_returns_not_found(
        self, client: AsyncClient, auth
    ) -> None:
        headers = await auth.platform_headers()
        response = await client.post(
            f"{PLATFORM}/templates",
            headers=headers,
            json={"code": "TPL-BAD", "name": "坏引用", "cloudProviderId": "cloud-not-exist"},
        )
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "RESOURCE_NOT_FOUND"


# ===========================================================================
# 七、连通性检测不伪造成功（ADR-07）
# ===========================================================================


class TestCloudConnectivity:
    """未配置即安全失败，且不发起真实网络调用。"""

    @pytest.mark.parametrize(
        ("with_credentials", "with_api_base"),
        [(False, False), (True, False), (False, True)],
    )
    async def test_missing_configuration_never_fakes_success(
        self, client: AsyncClient, auth, with_credentials: bool, with_api_base: bool
    ) -> None:
        headers = await auth.platform_headers()
        cloud = await _create_cloud(
            client,
            headers,
            code=f"CLOUD-TEST-{int(with_credentials)}{int(with_api_base)}",
            with_credentials=with_credentials,
            with_api_base=with_api_base,
        )

        response = await client.post(f"{PLATFORM}/clouds/{cloud['id']}/test", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["result"] == "NOT_CONFIGURED"
        assert body["ok"] is False
        # 「没有发起真实调用」的可观测证据：没有耗时数据
        assert body["latencyMs"] is None
        assert body["message"]
        assert body["vendor"] == "JIXIAN"

        # 结论回写：状态保持「未连接」，lastTestOk 为 false
        detail = await client.get(f"{PLATFORM}/clouds/{cloud['id']}", headers=headers)
        assert detail.json()["status"] == "NOT_CONNECTED"
        assert detail.json()["lastTestOk"] is False


# ===========================================================================
# 八、租户账号与密码管理
# ===========================================================================


class TestTenantAccountPassword:
    """一次性初始密码、强制改密与重置后旧密码失效。"""

    async def test_generated_password_returned_once_and_must_change(
        self, client: AsyncClient, auth
    ) -> None:
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "PWD-01")

        response = await client.post(
            f"{PLATFORM}/tenants/{tenant['id']}/accounts",
            headers=headers,
            json={"account": "13900001111", "roleCode": "MERCHANT_ADMIN"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["generated"] is True
        assert body["password"], "未指定密码时必须返回一次性随机密码"
        assert body["account"]["mustChangePassword"] is True

        # 生成的密码可直接登录，且登录响应标记「需改密」
        login = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": "13900001111", "password": body["password"]},
        )
        assert login.status_code == 200, login.text
        assert login.json()["mustChangePassword"] is True

        # 租户详情回显账号摘要，且不含任何密码字段
        detail = await client.get(f"{PLATFORM}/tenants/{tenant['id']}", headers=headers)
        accounts = detail.json()["accounts"]
        assert len(accounts) == 1
        assert accounts[0]["account"] == "13900001111"
        assert accounts[0]["mustChangePassword"] is True
        assert_no_secret_leak(accounts, where="租户账号摘要")

    async def test_reset_password_invalidates_old_password(
        self, client: AsyncClient, auth
    ) -> None:
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "PWD-02")
        created = await client.post(
            f"{PLATFORM}/tenants/{tenant['id']}/accounts",
            headers=headers,
            json={"account": "13900003333", "roleCode": "MERCHANT_ADMIN"},
        )
        old_password = created.json()["password"]
        assert old_password

        # 先登录一次，制造一个「旧登录态」（其刷新令牌应被重置动作吊销）
        login = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": "13900003333", "password": old_password},
        )
        assert login.status_code == 200, login.text
        stale_refresh = login.json()["refreshToken"]

        reset = await client.post(
            f"{PLATFORM}/tenants/{tenant['id']}/reset-password",
            headers=headers,
            json={"account": "13900003333", "newPassword": "Brand-New-Pass#2026"},
        )
        assert reset.status_code == 200, reset.text
        assert reset.json()["generated"] is False
        assert reset.json()["password"] is None, "调用方自带密码时服务端不应回显"
        assert reset.json()["mustChangePassword"] is True

        # 旧密码失效
        stale_login = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": "13900003333", "password": old_password},
        )
        assert stale_login.status_code == 401, stale_login.text
        assert stale_login.json()["code"] == "INVALID_CREDENTIALS"

        # 旧刷新令牌被吊销，无法续期
        stale_refresh_response = await client.post(
            f"{API_PREFIX}/auth/refresh", json={"refreshToken": stale_refresh}
        )
        assert stale_refresh_response.status_code == 401, stale_refresh_response.text

        # 新密码可登录，并仍要求改密
        fresh_login = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": "13900003333", "password": "Brand-New-Pass#2026"},
        )
        assert fresh_login.status_code == 200, fresh_login.text
        assert fresh_login.json()["mustChangePassword"] is True

    async def test_generated_password_on_reset_is_returned_once(
        self, client: AsyncClient, auth
    ) -> None:
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "PWD-03")
        await client.post(
            f"{PLATFORM}/tenants/{tenant['id']}/accounts",
            headers=headers,
            json={"account": "13900004444", "roleCode": "MERCHANT_OPERATOR"},
        )

        reset = await client.post(
            f"{PLATFORM}/tenants/{tenant['id']}/reset-password",
            headers=headers,
            json={"account": "13900004444"},
        )
        assert reset.status_code == 200, reset.text
        assert reset.json()["generated"] is True
        assert reset.json()["password"]

    async def test_reset_unknown_account_returns_not_found(
        self, client: AsyncClient, auth
    ) -> None:
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "PWD-04")
        response = await client.post(
            f"{PLATFORM}/tenants/{tenant['id']}/reset-password",
            headers=headers,
            json={"account": "13900009999"},
        )
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "RESOURCE_NOT_FOUND"

    async def test_account_role_outside_tenant_scope_is_rejected(
        self, client: AsyncClient, auth
    ) -> None:
        """租户下只能建商户角色账号（平台 / 工厂角色不属于任何租户）。"""
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "PWD-05")
        response = await client.post(
            f"{PLATFORM}/tenants/{tenant['id']}/accounts",
            headers=headers,
            json={"account": "13900005555", "roleCode": "PLATFORM_ADMIN"},
        )
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "VALIDATION_ERROR"


# ===========================================================================
# 九、租户资料与行业下拉
# ===========================================================================


class TestTenantLifecycle:
    """租户资料更新与状态切换。"""

    async def test_update_tenant_ignores_code_change(self, client: AsyncClient, auth) -> None:
        """编码不可修改：``TenantUpdateRequest`` 里根本没有 code 字段。"""
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "UPD-01", name="旧名称")

        response = await client.put(
            f"{PLATFORM}/tenants/{tenant['id']}",
            headers=headers,
            json={"name": "新名称", "industry": "零售体验店", "code": "HACKED"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["name"] == "新名称"
        assert body["industry"] == "零售体验店"
        assert body["code"] == "UPD-01", "租户编码不应被修改"

    async def test_disable_then_enable_tenant(self, client: AsyncClient, auth) -> None:
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "UPD-02")

        disabled = await client.post(
            f"{PLATFORM}/tenants/{tenant['id']}/status",
            headers=headers,
            json={"status": "DISABLED", "reason": "违规"},
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["status"] == "DISABLED"

        enabled = await client.post(
            f"{PLATFORM}/tenants/{tenant['id']}/status",
            headers=headers,
            json={"status": "ACTIVE"},
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["status"] == "ACTIVE"

    async def test_tenant_industries_dropdown_reflects_existing_data(
        self, client: AsyncClient, auth
    ) -> None:
        headers = await auth.platform_headers()
        await _create_tenant(client, headers, "IND-01", industry="玩具品牌商")
        await _create_tenant(client, headers, "IND-02", industry="零售体验店")

        response = await client.get(f"{PLATFORM}/tenant-industries", headers=headers)
        assert response.status_code == 200, response.text
        values = {item["value"] for item in response.json()["records"]}
        assert {"玩具品牌商", "零售体验店"} <= values


# ===========================================================================
# 十、客户产品更新与删除
# ===========================================================================


class TestClientProductUpdate:
    """``PUT /platform/client-products/{id}`` 的可改字段与不可改字段。"""

    async def test_updatable_fields_take_effect(self, client: AsyncClient, auth) -> None:
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "UPD-PROD")
        template = await _create_template(client, headers, code="TPL-UPD-PROD")
        await _authorize(client, headers, template["id"], [tenant["id"]])
        product = await _create_product(
            client, headers, tenant_id=tenant["id"], template_id=template["id"], code="PROD-UPD"
        )
        assert product["aiEnabled"] is False

        response = await client.put(
            f"{PLATFORM}/client-products/{product['id']}",
            headers=headers,
            json={
                "name": "改名后的产品",
                "firmwareVersion": "3.2.1",
                "status": "DISABLED",
                "aiEnabled": True,
                "remark": "运营备注",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["name"] == "改名后的产品"
        assert body["firmwareVersion"] == "3.2.1"
        assert body["status"] == "DISABLED"
        assert body["aiEnabled"] is True
        assert body["remark"] == "运营备注"

        # 重新 GET 确认已持久化（而不是只在本次响应里回显）
        persisted = await client.get(
            f"{PLATFORM}/client-products/{product['id']}", headers=headers
        )
        assert persisted.status_code == 200, persisted.text
        assert persisted.json()["name"] == "改名后的产品"
        assert persisted.json()["firmwareVersion"] == "3.2.1"
        assert persisted.json()["aiEnabled"] is True

    async def test_cannot_reassign_tenant_or_template(
        self, client: AsyncClient, auth
    ) -> None:
        """★ P-03 防回退点：归属租户与模板不可通过请求体篡改。

        服务层只接受 ``name/firmware_version/status/ai_enabled/remark`` 五个字段；
        请求体里多余的 ``tenantId`` / ``templateId`` 必须被丢弃。
        """
        headers = await auth.platform_headers()
        tenant_a = await _create_tenant(client, headers, "UPD-A")
        tenant_b = await _create_tenant(client, headers, "UPD-B")
        template_a = await _create_template(client, headers, code="TPL-UPD-A")
        template_b = await _create_template(client, headers, code="TPL-UPD-B")
        await _authorize(
            client, headers, template_a["id"], [tenant_a["id"], tenant_b["id"]]
        )
        await _authorize(client, headers, template_b["id"], [tenant_a["id"], tenant_b["id"]])
        product = await _create_product(
            client,
            headers,
            tenant_id=tenant_a["id"],
            template_id=template_a["id"],
            code="PROD-UPD-OWN",
        )

        response = await client.put(
            f"{PLATFORM}/client-products/{product['id']}",
            headers=headers,
            json={
                "name": "顺便改名",
                "tenantId": tenant_b["id"],
                "templateId": template_b["id"],
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["name"] == "顺便改名", "合法字段仍应生效"
        assert body["tenantId"] == tenant_a["id"], "归属租户不可被篡改"
        assert body["templateId"] == template_a["id"], "派生模板不可被篡改"
        assert body["tenantCode"] == tenant_a["code"]

        # 数据库侧也未被改动（响应与持久化一致）
        detail = await client.get(f"{PLATFORM}/client-products/{product['id']}", headers=headers)
        assert detail.json()["tenantId"] == tenant_a["id"]
        assert detail.json()["templateId"] == template_a["id"]

        # 反向确认：租户 A 详情里仍看得见它，租户 B 详情里没有
        detail_a = await client.get(f"{PLATFORM}/tenants/{tenant_a['id']}", headers=headers)
        detail_b = await client.get(f"{PLATFORM}/tenants/{tenant_b['id']}", headers=headers)
        assert [item["code"] for item in detail_a.json()["clientProducts"]] == ["PROD-UPD-OWN"]
        assert detail_b.json()["clientProducts"] == []
        assert detail_b.json()["counts"]["clientProducts"] == 0


class TestClientProductDelete:
    """``DELETE /platform/client-products/{id}`` 的清理效果。"""

    async def test_delete_removes_it_from_tenant_detail(self, client: AsyncClient, auth) -> None:
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "DEL-PROD")
        template = await _create_template(client, headers, code="TPL-DEL-PROD")
        await _authorize(client, headers, template["id"], [tenant["id"]])
        product = await _create_product(
            client, headers, tenant_id=tenant["id"], template_id=template["id"], code="PROD-DEL"
        )

        before = await client.get(f"{PLATFORM}/tenants/{tenant['id']}", headers=headers)
        assert before.json()["counts"]["clientProducts"] == 1

        response = await client.delete(
            f"{PLATFORM}/client-products/{product['id']}", headers=headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["success"] is True

        missing = await client.get(
            f"{PLATFORM}/client-products/{product['id']}", headers=headers
        )
        assert missing.status_code == 404, missing.text
        assert missing.json()["code"] == "RESOURCE_NOT_FOUND"

        after = await client.get(f"{PLATFORM}/tenants/{tenant['id']}", headers=headers)
        assert after.status_code == 200, after.text
        assert after.json()["clientProducts"] == [], "已删除的产品不应再出现在租户详情里"
        assert after.json()["counts"]["clientProducts"] == 0, "计数应减 1"
        # 模板侧的派生计数同步回落
        template_detail = await client.get(
            f"{PLATFORM}/templates/{template['id']}", headers=headers
        )
        assert template_detail.json()["clientProductCount"] == 0

    async def test_delete_cascades_miniapp_config(
        self, client: AsyncClient, auth, db
    ) -> None:
        """小程序配置随产品一并消失（1:1 从属关系）。"""
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "DEL-MINI")
        template = await _create_template(client, headers, code="TPL-DEL-MINI")
        await _authorize(client, headers, template["id"], [tenant["id"]])
        product = await _create_product(
            client, headers, tenant_id=tenant["id"], template_id=template["id"], code="PROD-DELMINI"
        )

        saved = await client.put(
            f"{PLATFORM}/client-products/{product['id']}/miniapp-config",
            headers=headers,
            json={"appName": "待删除小程序", "appSecret": APPSECRET_SENTINEL},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["appSecretHint"] == APPSECRET_HINT

        deleted = await client.delete(
            f"{PLATFORM}/client-products/{product['id']}", headers=headers
        )
        assert deleted.status_code == 200, deleted.text

        # 直接查库确认配置行真的被级联删除（比只看 HTTP 更有力）
        remaining = int(
            (
                await db.execute(select(func.count()).select_from(MiniAppConfig))
            ).scalar_one()
        )
        assert remaining == 0, "小程序配置行应随客户产品一并删除"

        # ⚠ 期望待确认：产品已不存在，此端点返回 404（而非 null）。
        # 理由：服务层先校验产品存在（catalog_service.get_miniapp_config → get_client_product），
        # 产品不存在时语义上应报「资源不存在」；返回 null 会与「产品存在但未配置」混淆。
        config_after = await client.get(
            f"{PLATFORM}/client-products/{product['id']}/miniapp-config", headers=headers
        )
        assert config_after.status_code == 404, config_after.text
        assert config_after.json()["code"] == "RESOURCE_NOT_FOUND"


# ===========================================================================
# 十一、列表筛选
# ===========================================================================


class TestListFilters:
    """四类列表的筛选参数：每条都断言「筛选后 total 变化」+ 记录确实匹配。"""

    async def test_tenant_filters(self, client: AsyncClient, auth) -> None:
        headers = await auth.platform_headers()
        await _create_tenant(
            client, headers, "FLT-ALPHA", name="阿尔法玩具", industry="玩具品牌商"
        )
        await _create_tenant(client, headers, "FLT-BETA", name="贝塔零售", industry="零售体验店")
        await _create_tenant(
            client,
            headers,
            "FLT-GAMMA",
            name="伽马玩具",
            industry="玩具品牌商",
            status="DISABLED",
        )
        path = f"{PLATFORM}/tenants"
        baseline = await _total(client, headers, path)
        assert baseline >= 5, "应有 2 个种子租户 + 本用例新建的 3 个"

        # keyword 命中 name / code，且不命中时归零
        page = await _page(client, headers, path, keyword="阿尔法")
        assert page["total"] == 1 < baseline
        assert page["records"][0]["code"] == "FLT-ALPHA"

        page = await _page(client, headers, path, keyword="FLT-BETA")
        assert page["total"] == 1 < baseline
        assert page["records"][0]["name"] == "贝塔零售"

        page = await _page(client, headers, path, keyword="绝不存在关键字XYZ")
        assert page["total"] == 0 < baseline
        assert page["records"] == []

        # status
        page = await _page(client, headers, path, status="DISABLED")
        assert 0 < page["total"] < baseline
        assert {record["status"] for record in page["records"]} == {"DISABLED"}

        page = await _page(client, headers, path, status="ACTIVE")
        assert {record["status"] for record in page["records"]} == {"ACTIVE"}

        # industry
        page = await _page(client, headers, path, industry="玩具品牌商")
        assert 0 < page["total"] < baseline
        assert {record["industry"] for record in page["records"]} == {"玩具品牌商"}

        page = await _page(client, headers, path, industry="绝不存在行业")
        assert page["total"] == 0 < baseline

    async def test_template_filters(self, client: AsyncClient, auth) -> None:
        headers = await auth.platform_headers()
        await _create_template(client, headers, code="FLT-TPL-A", name="故事机A")
        await _create_template(
            client,
            headers,
            code="FLT-TPL-B",
            name="翻译机B",
            network_type="WIFI",
            firmware_version="2.0.0",
        )
        disabled = await _create_template(
            client, headers, code="FLT-TPL-C", name="停用款C", status="DISABLED"
        )
        assert disabled["status"] == "DISABLED"

        path = f"{PLATFORM}/templates"
        baseline = await _total(client, headers, path)
        assert baseline == 3

        page = await _page(client, headers, path, keyword="故事机")
        assert page["total"] == 1 < baseline
        assert page["records"][0]["code"] == "FLT-TPL-A"

        page = await _page(client, headers, path, keyword="FLT-TPL-B")
        assert page["total"] == 1 < baseline

        # networkType 用的是 camelCase 查询参数别名
        page = await _page(client, headers, path, networkType="WIFI")
        assert 0 < page["total"] < baseline
        assert {record["networkType"] for record in page["records"]} == {"WIFI"}

        page = await _page(client, headers, path, networkType="4G")
        assert 0 < page["total"] < baseline
        assert {record["networkType"] for record in page["records"]} == {"4G"}

        page = await _page(client, headers, path, status="DISABLED")
        assert page["total"] == 1 < baseline
        assert page["records"][0]["code"] == "FLT-TPL-C"

        page = await _page(client, headers, path, status="ENABLED")
        assert page["total"] == 2 < baseline

    async def test_cloud_filters(self, client: AsyncClient, auth, db) -> None:
        headers = await auth.platform_headers()
        await _create_cloud(client, headers, code="FLT-CLD-A", name="集贤主账号")
        cloud_b = await _create_cloud(
            client, headers, code="FLT-CLD-B", name="京东账号", vendor="JOYINSIDE", network_type="WIFI"
        )

        # 测试侧造数：把 B 直接置为 CONNECTED，使 status 筛选的两侧都有判别力。
        # （真实环境里 CONNECTED 只能由真实厂商探测产生，ADR-07 禁止伪造成功，
        #   因此这里绕开端点直接改库，仅为验证「筛选参数是否生效」。）
        row = await db.get(CloudProvider, cloud_b["id"])
        assert row is not None
        row.status = "CONNECTED"
        await db.commit()

        path = f"{PLATFORM}/clouds"
        baseline = await _total(client, headers, path)
        assert baseline == 2

        page = await _page(client, headers, path, keyword="京东")
        assert page["total"] == 1 < baseline
        assert page["records"][0]["code"] == "FLT-CLD-B"

        page = await _page(client, headers, path, keyword="FLT-CLD-A")
        assert page["total"] == 1 < baseline

        page = await _page(client, headers, path, vendor="JOYINSIDE")
        assert page["total"] == 1 < baseline
        assert {record["vendor"] for record in page["records"]} == {"JOYINSIDE"}

        page = await _page(client, headers, path, vendor="JIXIAN")
        assert page["total"] == 1 < baseline

        # status：造数后两侧都命中 1 条，各自都严格小于 baseline
        page = await _page(client, headers, path, status="CONNECTED")
        assert 0 < page["total"] < baseline
        assert page["records"][0]["code"] == "FLT-CLD-B"
        assert {record["status"] for record in page["records"]} == {"CONNECTED"}

        page = await _page(client, headers, path, status="NOT_CONNECTED")
        assert 0 < page["total"] < baseline
        assert page["records"][0]["code"] == "FLT-CLD-A"
        assert {record["status"] for record in page["records"]} == {"NOT_CONNECTED"}

    async def test_client_product_filters(self, client: AsyncClient, auth) -> None:
        headers = await auth.platform_headers()
        tenant_a = await _create_tenant(client, headers, "FLT-P-A")
        tenant_b = await _create_tenant(client, headers, "FLT-P-B")
        template_a = await _create_template(client, headers, code="FLT-PT-A")
        template_b = await _create_template(client, headers, code="FLT-PT-B", network_type="WIFI")
        await _authorize(
            client,
            headers,
            template_a["id"],
            [tenant_a["id"], tenant_b["id"]],
        )
        await _authorize(
            client,
            headers,
            template_b["id"],
            [tenant_a["id"], tenant_b["id"]],
        )
        await _create_product(
            client,
            headers,
            tenant_id=tenant_a["id"],
            template_id=template_a["id"],
            code="FLT-P-1",
            name="阿尔法产品",
        )
        await _create_product(
            client,
            headers,
            tenant_id=tenant_b["id"],
            template_id=template_a["id"],
            code="FLT-P-2",
            name="贝塔产品",
        )
        await _create_product(
            client,
            headers,
            tenant_id=tenant_a["id"],
            template_id=template_b["id"],
            code="FLT-P-3",
            name="伽马产品",
        )

        path = f"{PLATFORM}/client-products"
        baseline = await _total(client, headers, path)
        assert baseline == 3

        page = await _page(client, headers, path, tenantId=tenant_a["id"])
        assert page["total"] == 2 < baseline
        assert {record["tenantId"] for record in page["records"]} == {tenant_a["id"]}

        page = await _page(client, headers, path, templateId=template_b["id"])
        assert page["total"] == 1 < baseline
        assert {record["templateId"] for record in page["records"]} == {template_b["id"]}

        page = await _page(client, headers, path, keyword="贝塔")
        assert page["total"] == 1 < baseline
        assert page["records"][0]["code"] == "FLT-P-2"

        page = await _page(client, headers, path, keyword="FLT-P-3")
        assert page["total"] == 1 < baseline

        # 组合筛选：租户 A × 模板 B
        page = await _page(
            client, headers, path, tenantId=tenant_a["id"], templateId=template_b["id"]
        )
        assert page["total"] == 1 < baseline
        assert page["records"][0]["code"] == "FLT-P-3"

        page = await _page(client, headers, path, keyword="绝不存在")
        assert page["total"] == 0 < baseline

    async def test_template_category_filter(self, client: AsyncClient, auth) -> None:
        """模板 ``category`` 筛选（与 keyword/networkType/status 同源的收口逻辑）。"""
        headers = await auth.platform_headers()
        await _create_template(client, headers, code="FLT-CAT-A", category="故事机")
        await _create_template(client, headers, code="FLT-CAT-B", category="翻译机")

        path = f"{PLATFORM}/templates"
        baseline = await _total(client, headers, path)
        assert baseline == 2

        page = await _page(client, headers, path, category="翻译机")
        assert page["total"] == 1 < baseline
        assert page["records"][0]["code"] == "FLT-CAT-B"
        assert {record["category"] for record in page["records"]} == {"翻译机"}

        page = await _page(client, headers, path, category="不存在的品类")
        assert page["total"] == 0 < baseline
        assert page["records"] == []

    async def test_client_product_status_filter(self, client: AsyncClient, auth) -> None:
        """客户产品 ``status`` 筛选：停用后只应被 DISABLED 命中。"""
        headers = await auth.platform_headers()
        tenant = await _create_tenant(client, headers, "FLT-PS")
        template = await _create_template(client, headers, code="FLT-PT-S")
        await _authorize(client, headers, template["id"], [tenant["id"]])
        await _create_product(
            client, headers, tenant_id=tenant["id"], template_id=template["id"], code="FLT-PS-1"
        )
        disabled = await _create_product(
            client, headers, tenant_id=tenant["id"], template_id=template["id"], code="FLT-PS-2"
        )

        stopped = await client.put(
            f"{PLATFORM}/client-products/{disabled['id']}",
            headers=headers,
            json={"status": "DISABLED"},
        )
        assert stopped.status_code == 200, stopped.text

        path = f"{PLATFORM}/client-products"
        baseline = await _total(client, headers, path)
        assert baseline == 2

        page = await _page(client, headers, path, status="DISABLED")
        assert page["total"] == 1 < baseline
        assert page["records"][0]["code"] == "FLT-PS-2"
        assert {record["status"] for record in page["records"]} == {"DISABLED"}

        page = await _page(client, headers, path, status="ENABLED")
        assert page["total"] == 1 < baseline
        assert page["records"][0]["code"] == "FLT-PS-1"


# ===========================================================================
# 十二、模板 JSON 字段入参（字符串形态）
# ===========================================================================


class TestTemplateJsonInputs:
    """``aiFeatures`` / ``specs`` 允许传 JSON 字符串（前端表单直传），服务端解析为对象。"""

    async def test_json_string_is_parsed_into_object(self, client: AsyncClient, auth) -> None:
        headers = await auth.platform_headers()
        response = await client.post(
            f"{PLATFORM}/templates",
            headers=headers,
            json={
                "code": "JSON-STR",
                "name": "字符串入参模板",
                "aiFeatures": '{"story": true, "music": false}',
                "specs": '{"size": "120mm", "battery": 800}',
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["aiFeatures"] == {"story": True, "music": False}
        assert body["specs"] == {"size": "120mm", "battery": 800}

        # 对象形态同样接受
        template_id = body["id"]
        updated = await client.put(
            f"{PLATFORM}/templates/{template_id}",
            headers=headers,
            json={"specs": '{"size": "150mm"}'},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["specs"] == {"size": "150mm"}

        updated = await client.put(
            f"{PLATFORM}/templates/{template_id}",
            headers=headers,
            json={"aiFeatures": {"chat": True}},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["aiFeatures"] == {"chat": True}

    @pytest.mark.parametrize(
        "bad_value",
        ["not-a-json", "[1, 2, 3]", "123", '"just-a-string"'],
    )
    async def test_invalid_json_string_returns_validation_error(
        self, client: AsyncClient, auth, bad_value: str
    ) -> None:
        """非法 JSON / 非对象 JSON 一律 400，不能静默落库成垃圾数据。"""
        headers = await auth.platform_headers()
        response = await client.post(
            f"{PLATFORM}/templates",
            headers=headers,
            json={"code": "JSON-BAD", "name": "坏 JSON", "aiFeatures": bad_value},
        )
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "VALIDATION_ERROR"
        assert response.json()["details"]


# ===========================================================================
# 十三、云服务商更新语义（密钥字段留空 = 保持原值）
# ===========================================================================


class TestCloudUpdateSemantics:
    """密钥留空保持原值；提供新值则刷新掩码。"""

    async def test_omitting_keys_keeps_credentials(self, client: AsyncClient, auth) -> None:
        headers = await auth.platform_headers()
        cloud = await _create_cloud(client, headers, code="CLOUD-KEEP")
        assert cloud["hasCredentials"] is True

        response = await client.put(
            f"{PLATFORM}/clouds/{cloud['id']}", headers=headers, json={"name": "改个名字"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["name"] == "改个名字"
        assert body["accessKeyHint"] == AK_HINT, "未提供新密钥时掩码不应变化"
        assert body["secretKeyHint"] == SK_HINT
        assert body["hasCredentials"] is True, "密钥未被清空"
        assert_no_secret_leak(body, where="更新云服务商（不传密钥）")

    async def test_rotating_only_access_key_keeps_secret_key(
        self, client: AsyncClient, auth
    ) -> None:
        """只轮换 accessKey：该掩码刷新，secretKey 掩码保持原值。"""
        headers = await auth.platform_headers()
        cloud = await _create_cloud(client, headers, code="CLOUD-PARTIAL")

        response = await client.put(
            f"{PLATFORM}/clouds/{cloud['id']}",
            headers=headers,
            json={"accessKey": "ROTATED_AK_0001"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["accessKeyHint"] == "ROTA****"
        assert body["secretKeyHint"] == SK_HINT, "未提供的密钥应保持原值"
        assert body["hasCredentials"] is True
        assert body["status"] == "NOT_CONNECTED", "换密钥后应要求重新检测"
        assert_no_secret_leak(body, where="轮换 accessKey")

        # 掩码变化确实落库（重新 GET 一致）
        refetched = await client.get(f"{PLATFORM}/clouds/{cloud['id']}", headers=headers)
        assert refetched.json()["accessKeyHint"] == "ROTA****"
        assert refetched.json()["secretKeyHint"] == SK_HINT
