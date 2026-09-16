"""集成测试：平台端端点独占与租户作用域收口（ADR-08）。

覆盖三件事
----------
1. **平台端端点独占**：商户 / 工厂角色访问 ``/platform/**`` 一律 403。
2. **平台运营只读**：``PLATFORM_OPERATOR`` 可读租户列表，但无
   ``platform:tenant:write`` / ``cloud:write`` / ``template:write``，写操作 403。
3. ★ **租户作用域收口**：直接调用服务层验证 :func:`app.db.scope.scoped`
   的隔离效果——商户角色只能看到自身租户的数据，平台角色可见全部。
   本组用例标记 ``tenant_isolation``，与 P5 的租户隔离矩阵同源。

为什么第 3 组直调服务层
----------------------
路由层不可信（可能漏挂依赖），而 ``scoped`` 是隔离的**唯一收口点**。
直接从服务层验证，可以排除 HTTP 层干扰，把断言钉在架构红线上。
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.deps import AuthContext
from app.db.scope import assert_visible, scoped, scoped_value
from app.models.enums import RoleType
from app.models.identity import Tenant
from app.services import catalog_service
from tests.conftest import API_PREFIX, TEST_PLATFORM_ACCOUNT

pytestmark = pytest.mark.integration

PLATFORM = f"{API_PREFIX}/platform"

OPERATOR_PASSWORD = "Operat0r-Pass#2026"

#: 平台端端点矩阵：``(方法, 路径, 请求体)``
_PLATFORM_ENDPOINTS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/tenants", None),
    ("POST", "/tenants", {"code": "HACK-01", "name": "越权创建"}),
    ("GET", "/clouds", None),
    ("POST", "/clouds", {"code": "HACK-C1", "name": "越权创建"}),
    ("GET", "/templates", None),
    ("POST", "/templates", {"code": "HACK-T1", "name": "越权创建"}),
    ("GET", "/client-products", None),
    ("DELETE", "/tenants/t-001", None),
]


def _platform_ctx() -> AuthContext:
    """平台超管上下文（通配权限）。"""
    return AuthContext(
        user_id="u-platform-it",
        account=TEST_PLATFORM_ACCOUNT,
        role_code="PLATFORM_ADMIN",
        role_type=RoleType.PLATFORM,
        tenant_id=None,
        permissions=["*"],
    )


def _merchant_ctx(tenant_id: str) -> AuthContext:
    """商户管理员上下文（绑定指定租户）。"""
    return AuthContext(
        user_id="u-merchant-it",
        account="merchant-it",
        role_code="MERCHANT_ADMIN",
        role_type=RoleType.MERCHANT,
        tenant_id=tenant_id,
        permissions=["merchant:product:read"],
    )


def _factory_ctx() -> AuthContext:
    """工厂管理员上下文（跨租户）。"""
    return AuthContext(
        user_id="u-factory-it",
        account="factory-it",
        role_code="FACTORY_ADMIN",
        role_type=RoleType.FACTORY,
        tenant_id=None,
        permissions=["factory:order:read"],
    )


async def _seed_catalog(db: Any, tenant_ids: list[str]) -> dict[str, str]:
    """用服务层铺一份「模板授权给多租户 + 各建一个客户产品」的数据。"""
    platform = _platform_ctx()
    cloud = await catalog_service.create_cloud(
        db,
        payload={
            "code": "CLOUD-SCOPE",
            "name": "作用域测试云",
            "vendor": "JIXIAN",
            "network_type": "4G",
        },
        actor=platform,
    )
    template = await catalog_service.create_template(
        db,
        payload={
            "code": "TPL-SCOPE",
            "name": "作用域测试模板",
            "network_type": "4G",
            "cloud_provider_id": cloud.id,
            "firmware_version": "1.0.0",
            "status": "ENABLED",
        },
        actor=platform,
    )
    await catalog_service.authorize_template(
        db, template_id=template.id, tenant_ids=tenant_ids, actor=platform
    )

    products: dict[str, str] = {}
    for index, tenant_id in enumerate(tenant_ids, start=1):
        product = await catalog_service.create_client_product(
            db,
            payload={
                "tenant_id": tenant_id,
                "template_id": template.id,
                "code": f"PROD-SCOPE-{index}",
            },
            actor=platform,
        )
        products[tenant_id] = product.id

    return {"template_id": template.id, "cloud_id": cloud.id, **products}


# ===========================================================================
# 一、平台端端点独占
# ===========================================================================


class TestPlatformEndpointsAreExclusive:
    """商户 / 工厂角色访问平台端端点必须被拒绝。"""

    @pytest.mark.parametrize("role", ["merchant", "factory"])
    @pytest.mark.parametrize(("method", "path", "body"), _PLATFORM_ENDPOINTS)
    async def test_non_platform_role_gets_permission_denied(
        self,
        client: AsyncClient,
        auth,
        role: str,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> None:
        headers = (
            await auth.merchant_headers() if role == "merchant" else await auth.factory_headers()
        )
        response = await client.request(
            method, f"{PLATFORM}{path}", headers=headers, json=body
        )
        assert response.status_code == 403, f"{method} {path} 应被拒绝：{response.text}"
        assert response.json()["code"] == "PERMISSION_DENIED"

    async def test_missing_token_is_rejected_as_unauthenticated(
        self, client: AsyncClient
    ) -> None:
        """平台端点同样受统一鉴权守卫保护。"""
        response = await client.get(f"{PLATFORM}/tenants")
        assert response.status_code == 401
        assert response.json()["code"] == "UNAUTHENTICATED"


# ===========================================================================
# 二、平台运营只读
# ===========================================================================


class TestPlatformOperatorReadOnly:
    """``PLATFORM_OPERATOR`` 可读不可写（租户 / 云服务商 / 模板）。"""

    async def test_operator_can_read_but_cannot_write(
        self, client: AsyncClient, auth, make_user
    ) -> None:
        await make_user(
            account="13700000001",
            password=OPERATOR_PASSWORD,
            role_code="PLATFORM_OPERATOR",
            tenant_id=None,
        )
        headers = auth.headers(await auth.token("13700000001", OPERATOR_PASSWORD))

        # 读：三个领域都允许
        for path in ("/tenants", "/clouds", "/templates", "/client-products"):
            response = await client.get(f"{PLATFORM}{path}", headers=headers)
            assert response.status_code == 200, f"GET {path}：{response.text}"

        # 写：租户 / 云服务商 / 模板均被拒绝
        cases: list[tuple[str, str, dict[str, Any]]] = [
            ("post", "/tenants", {"code": "OP-01", "name": "运营不该能建"}),
            ("post", "/clouds", {"code": "OP-C1", "name": "运营不该能建"}),
            ("post", "/templates", {"code": "OP-T1", "name": "运营不该能建"}),
        ]
        for method, path, body in cases:
            response = await client.request(method, f"{PLATFORM}{path}", headers=headers, json=body)
            assert response.status_code == 403, f"{method.upper()} {path}：{response.text}"
            body_json = response.json()
            assert body_json["code"] == "PERMISSION_DENIED"
            assert body_json["details"]["requiredPermission"].startswith("platform:")
            assert body_json["details"]["requiredPermission"].endswith(":write")

    async def test_operator_permission_surface_excludes_tenant_write(
        self, client: AsyncClient, auth, make_user
    ) -> None:
        """运营的权限码中不含租户 / 云 / 模板的写权限。

        注意：内置角色表里运营**保留了** ``platform:product:write`` 等写权限
        （见 ``app/core/permissions.py`` 的 ``_PLATFORM_OPERATOR_PERMS``），
        与「平台运营只有读权限」的表述存在出入——此处仅锁定与
        ``/platform/tenants`` 相关的写权限，其余留待产品决策。
        """
        await make_user(
            account="13700000002",
            password=OPERATOR_PASSWORD,
            role_code="PLATFORM_OPERATOR",
            tenant_id=None,
        )
        headers = auth.headers(await auth.token("13700000002", OPERATOR_PASSWORD))

        body = (await client.get(f"{API_PREFIX}/me", headers=headers)).json()
        permissions = set(body["permissions"])
        assert body["role"] == "PLATFORM_OPERATOR"
        assert body["tenantId"] is None
        assert "platform:tenant:read" in permissions
        assert "platform:tenant:write" not in permissions
        assert "platform:cloud:write" not in permissions
        assert "platform:template:write" not in permissions

    async def test_platform_admin_still_has_wildcard(
        self, client: AsyncClient, auth
    ) -> None:
        """反向验证：平台超管不受影响，仍是通配权限。"""
        headers = await auth.platform_headers()
        body = (await client.get(f"{API_PREFIX}/me", headers=headers)).json()
        assert body["permissions"] == ["*"]


# ===========================================================================
# 三、★ 租户作用域收口（ADR-08）
# ===========================================================================


@pytest.mark.tenant_isolation
class TestTenantScopeEnforcement:
    """``app.db.scope.scoped`` 的隔离语义。"""

    async def test_merchant_only_sees_own_client_products(self, db, make_tenant) -> None:
        tenant_a = await make_tenant(code="SCOPE-A", name="作用域租户A")
        tenant_b = await make_tenant(code="SCOPE-B", name="作用域租户B")
        await _seed_catalog(db, [tenant_a.id, tenant_b.id])

        rows, total = await catalog_service.list_client_products(db, _merchant_ctx(tenant_a.id))

        assert total == 1, "商户只应看到自己租户的产品"
        assert {row.code for row in rows} == {"PROD-SCOPE-1"}
        assert all(row.tenant_id == tenant_a.id for row in rows)
        assert all(row.tenant_id != tenant_b.id for row in rows)

    async def test_platform_sees_all_client_products(self, db, make_tenant) -> None:
        tenant_a = await make_tenant(code="SCOPE-C", name="作用域租户C")
        tenant_b = await make_tenant(code="SCOPE-D", name="作用域租户D")
        await _seed_catalog(db, [tenant_a.id, tenant_b.id])

        rows, total = await catalog_service.list_client_products(db, _platform_ctx())

        assert total == 2, "平台角色可见全部租户的产品"
        assert {row.tenant_id for row in rows} == {tenant_a.id, tenant_b.id}

    async def test_merchant_scope_applies_to_authorizations_too(self, db, make_tenant) -> None:
        """授权列表同样收口（模型不同，规则一致）。"""
        tenant_a = await make_tenant(code="SCOPE-E", name="作用域租户E")
        tenant_b = await make_tenant(code="SCOPE-F", name="作用域租户F")
        await _seed_catalog(db, [tenant_a.id, tenant_b.id])

        rows, total = await catalog_service.list_authorizations(db, _merchant_ctx(tenant_a.id))
        assert total == 1
        assert all(row.tenant_id == tenant_a.id for row in rows)

        _, platform_total = await catalog_service.list_authorizations(db, _platform_ctx())
        assert platform_total == 2

    async def test_factory_role_is_not_tenant_filtered(self, db, make_tenant) -> None:
        """工厂角色刻意**不做**租户过滤（其数据保护靠字段脱敏，而非租户隔离）。"""
        tenant_a = await make_tenant(code="SCOPE-G", name="作用域租户G")
        tenant_b = await make_tenant(code="SCOPE-H", name="作用域租户H")
        await _seed_catalog(db, [tenant_a.id, tenant_b.id])

        _, total = await catalog_service.list_client_products(db, _factory_ctx())
        assert total == 2

    async def test_merchant_without_tenant_is_rejected(self, db) -> None:
        """商户上下文缺少 ``tenant_id`` 时直接拒绝，而不是退化成「看全部」。"""
        broken = AuthContext(
            user_id="u-broken",
            account="broken",
            role_code="MERCHANT_ADMIN",
            role_type=RoleType.MERCHANT,
            tenant_id=None,
        )
        with pytest.raises(Exception) as excinfo:
            await catalog_service.list_client_products(db, broken)
        assert getattr(excinfo.value, "code", None) == "PERMISSION_DENIED"

    async def test_scoped_rejects_model_without_tenant_id(self) -> None:
        """防御分支：``Tenant`` 模型没有 ``tenant_id`` 字段，不能用于作用域查询。"""
        with pytest.raises(RuntimeError, match="不含 tenant_id 字段"):
            scoped(select(Tenant), Tenant, _merchant_ctx("t-any"))

    async def test_scoped_value_and_assert_visible(self) -> None:
        """作用域辅助函数的取值与越权语义。"""
        merchant = _merchant_ctx("t-001")
        assert scoped_value(merchant) == "t-001"
        assert scoped_value(_platform_ctx()) is None
        assert scoped_value(_factory_ctx()) is None

        # 商户访问其他租户的资源 → 以「不存在」响应，避免探测资源是否存在
        with pytest.raises(Exception) as excinfo:
            assert_visible("t-002", merchant, resource="客户产品")
        assert getattr(excinfo.value, "code", None) == "RESOURCE_NOT_FOUND"

        # 访问自己租户的资源 / 平台角色：放行
        assert_visible("t-001", merchant, resource="客户产品")
        assert_visible("t-999", _platform_ctx(), resource="客户产品")

    async def test_platform_client_product_list_is_not_filtered_via_api(
        self, client: AsyncClient, auth, db, make_tenant
    ) -> None:
        """HTTP 层与直调服务层结论一致：平台看得到两个租户的产品。"""
        tenant_a = await make_tenant(code="SCOPE-I", name="作用域租户I")
        tenant_b = await make_tenant(code="SCOPE-J", name="作用域租户J")
        await _seed_catalog(db, [tenant_a.id, tenant_b.id])

        headers = await auth.platform_headers()
        response = await client.get(f"{PLATFORM}/client-products", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 2
