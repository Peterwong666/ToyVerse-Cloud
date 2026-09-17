"""集成测试：认证与鉴权。

覆盖登录成功/失败、账号锁定、租户禁用（修复缺陷 P-04）、
令牌刷新与重放检测、权限校验、以及 traceId 贯穿。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.db.base import utcnow
from app.models.enums import TenantStatus, UserStatus
from tests.conftest import (
    API_PREFIX,
    TEST_FACTORY_PASSWORD,
    TEST_MERCHANT_PASSWORD,
    TEST_PLATFORM_ACCOUNT,
    TEST_PLATFORM_PASSWORD,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------


class TestLogin:
    """登录流程。"""

    async def test_platform_admin_login_succeeds(self, client: AsyncClient) -> None:
        response = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": TEST_PLATFORM_ACCOUNT, "password": TEST_PLATFORM_PASSWORD},
        )
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["accessToken"]
        assert body["refreshToken"]
        assert body["tokenType"] == "Bearer"
        assert body["expiresIn"] > 0

        user = body["user"]
        assert user["account"] == TEST_PLATFORM_ACCOUNT
        assert user["role"] == "PLATFORM_ADMIN"
        assert user["roleType"] == "PLATFORM"
        # ADR-01：平台管理员是全局长，不属于任何租户
        assert user["tenantId"] is None
        # 平台超管拥有通配权限
        assert user["permissions"] == ["*"]

    async def test_merchant_admin_login_returns_tenant_and_permissions(
        self, client: AsyncClient
    ) -> None:
        response = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": "13812345679", "password": TEST_MERCHANT_PASSWORD},
        )
        assert response.status_code == 200, response.text

        user = response.json()["user"]
        assert user["role"] == "MERCHANT_ADMIN"
        assert user["roleType"] == "MERCHANT"
        assert user["tenantId"] == "t-001"
        assert user["tenantCode"] == "DEMO-BRAND"
        assert "merchant:device:read" in user["permissions"]
        # 商户端不得拿到平台端权限
        assert not any(p.startswith("platform:") for p in user["permissions"])

    async def test_factory_admin_login(self, client: AsyncClient) -> None:
        response = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": "13600000000", "password": TEST_FACTORY_PASSWORD},
        )
        assert response.status_code == 200, response.text

        user = response.json()["user"]
        assert user["role"] == "FACTORY_ADMIN"
        assert user["roleType"] == "FACTORY"
        # 工厂跨租户
        assert user["tenantId"] is None
        assert set(user["permissions"]) >= {"factory:order:read", "factory:burn:write"}

    async def test_wrong_password_returns_401(self, client: AsyncClient) -> None:
        response = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": TEST_PLATFORM_ACCOUNT, "password": "definitely-wrong"},
        )
        assert response.status_code == 401
        assert response.json()["code"] == "INVALID_CREDENTIALS"

    async def test_unknown_account_returns_same_error_as_wrong_password(
        self, client: AsyncClient
    ) -> None:
        """不区分「账号不存在」与「密码错误」，避免账号枚举。"""
        unknown = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": "19900000000", "password": "whatever"},
        )
        wrong = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": TEST_PLATFORM_ACCOUNT, "password": "definitely-wrong"},
        )
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["code"] == wrong.json()["code"] == "INVALID_CREDENTIALS"

    async def test_missing_password_field_returns_validation_error(
        self, client: AsyncClient
    ) -> None:
        response = await client.post(f"{API_PREFIX}/auth/login", json={"account": TEST_PLATFORM_ACCOUNT})
        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION_ERROR"
        assert response.json()["details"]

    async def test_login_response_carries_trace_id_header(self, client: AsyncClient) -> None:
        """traceId 必须贯穿响应头，便于与日志、审计串联。"""
        response = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": TEST_PLATFORM_ACCOUNT, "password": TEST_PLATFORM_PASSWORD},
        )
        assert response.headers.get("x-trace-id")

    async def test_client_supplied_trace_id_is_echoed(self, client: AsyncClient) -> None:
        response = await client.get(
            f"{API_PREFIX}/health", headers={"x-trace-id": "my-trace-0001"}
        )
        assert response.headers.get("x-trace-id") == "my-trace-0001"


# ---------------------------------------------------------------------------
# 账号锁定
# ---------------------------------------------------------------------------


class TestAccountLockout:
    """连续失败锁定策略。"""

    async def test_locks_after_max_failures(
        self, client: AsyncClient, make_user, make_tenant
    ) -> None:
        tenant = await make_tenant(code="LOCK-TEST")
        await make_user(
            account="13800009999", password="Correct-Pass1!", tenant_id=tenant.id
        )

        # 前 N-1 次失败：仍返回凭据错误
        for attempt in range(1, settings.MAX_LOGIN_FAILURES):
            response = await client.post(
                f"{API_PREFIX}/auth/login",
                json={"account": "13800009999", "password": "wrong"},
            )
            assert response.status_code == 401, f"第 {attempt} 次应为 401"
            assert response.json()["code"] == "INVALID_CREDENTIALS"

        # 第 N 次失败触发锁定
        response = await client.post(
            f"{API_PREFIX}/auth/login", json={"account": "13800009999", "password": "wrong"}
        )
        assert response.status_code == 401

        # 此后即使密码正确也被拒绝
        response = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": "13800009999", "password": "Correct-Pass1!"},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "ACCOUNT_LOCKED"

    async def test_lock_expires_and_login_resumes(
        self, client: AsyncClient, make_user, make_tenant
    ) -> None:
        """锁定期过后可以正常登录。"""
        tenant = await make_tenant(code="LOCK-EXPIRE")
        await make_user(
            account="13800008888",
            password="Correct-Pass1!",
            tenant_id=tenant.id,
            login_failures=settings.MAX_LOGIN_FAILURES,
            # 已过期 1 分钟
            locked_until=utcnow() - timedelta(minutes=1),
        )

        response = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": "13800008888", "password": "Correct-Pass1!"},
        )
        assert response.status_code == 200, response.text

    async def test_successful_login_resets_failure_counter(
        self, client: AsyncClient, make_user, make_tenant
    ) -> None:
        tenant = await make_tenant(code="RESET-COUNTER")
        await make_user(
            account="13800007777",
            password="Correct-Pass1!",
            tenant_id=tenant.id,
            login_failures=settings.MAX_LOGIN_FAILURES - 1,
        )

        response = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": "13800007777", "password": "Correct-Pass1!"},
        )
        assert response.status_code == 200, response.text

        # 失败过一次后仍不会被锁定（计数已被重置）
        for _ in range(settings.MAX_LOGIN_FAILURES - 1):
            await client.post(
                f"{API_PREFIX}/auth/login",
                json={"account": "13800007777", "password": "wrong"},
            )
        response = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": "13800007777", "password": "Correct-Pass1!"},
        )
        assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# 禁用状态（缺陷 P-04）
# ---------------------------------------------------------------------------


class TestDisabledStates:
    """账号与租户被禁用时拒绝登录。"""

    async def test_disabled_account_cannot_login(
        self, client: AsyncClient, make_user, make_tenant
    ) -> None:
        tenant = await make_tenant(code="DISABLED-USER")
        await make_user(
            account="13800006666",
            password="Correct-Pass1!",
            tenant_id=tenant.id,
            status=UserStatus.DISABLED,
        )
        response = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": "13800006666", "password": "Correct-Pass1!"},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "ACCOUNT_DISABLED"

    async def test_disabled_tenant_cannot_login(
        self, client: AsyncClient, make_tenant, make_user
    ) -> None:
        """修复遗留原型缺陷 P-04：禁用客户仍可登录。

        账号本身是 ACTIVE，但所属租户被禁用，登录必须被拒绝。
        """
        tenant = await make_tenant(code="DISABLED-TENANT", status=TenantStatus.DISABLED)
        await make_user(
            account="13800005555",
            password="Correct-Pass1!",
            tenant_id=tenant.id,
            status=UserStatus.ACTIVE,
        )

        response = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": "13800005555", "password": "Correct-Pass1!"},
        )
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "TENANT_DISABLED"
        assert "禁用" in response.json()["message"]

    async def test_enabled_tenant_login_still_works_after_disabling_another(
        self, client: AsyncClient, make_tenant, make_user
    ) -> None:
        """反向验证：禁用某个租户不影响其他租户登录。"""
        await make_tenant(code="DISABLED-02", status=TenantStatus.DISABLED)
        active = await make_tenant(code="ACTIVE-02", status=TenantStatus.ACTIVE)
        await make_user(
            account="13800004444",
            password="Correct-Pass1!",
            tenant_id=active.id,
        )

        response = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": "13800004444", "password": "Correct-Pass1!"},
        )
        assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# 令牌刷新
# ---------------------------------------------------------------------------


class TestTokenRefresh:
    """刷新与轮换。"""

    async def test_refresh_returns_new_token_pair(self, client: AsyncClient) -> None:
        login = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": TEST_PLATFORM_ACCOUNT, "password": TEST_PLATFORM_PASSWORD},
        )
        original = login.json()

        response = await client.post(
            f"{API_PREFIX}/auth/refresh", json={"refreshToken": original["refreshToken"]}
        )
        assert response.status_code == 200, response.text

        refreshed = response.json()
        assert refreshed["accessToken"]
        # 刷新令牌必须轮换
        assert refreshed["refreshToken"] != original["refreshToken"]

    async def test_old_refresh_token_is_revoked_after_rotation(self, client: AsyncClient) -> None:
        login = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": TEST_PLATFORM_ACCOUNT, "password": TEST_PLATFORM_PASSWORD},
        )
        old_refresh = login.json()["refreshToken"]

        await client.post(f"{API_PREFIX}/auth/refresh", json={"refreshToken": old_refresh})

        # 旧令牌再次使用 → 判定为重放，拒绝
        replay = await client.post(
            f"{API_PREFIX}/auth/refresh", json={"refreshToken": old_refresh}
        )
        assert replay.status_code == 401

    async def test_replay_revokes_all_sessions(self, client: AsyncClient) -> None:
        """检测到重放后，该用户全部登录态失效。"""
        first = (
            await client.post(
                f"{API_PREFIX}/auth/login",
                json={"account": TEST_PLATFORM_ACCOUNT, "password": TEST_PLATFORM_PASSWORD},
            )
        ).json()
        second = (
            await client.post(
                f"{API_PREFIX}/auth/login",
                json={"account": TEST_PLATFORM_ACCOUNT, "password": TEST_PLATFORM_PASSWORD},
            )
        ).json()

        # 轮换第一条
        rotated = await client.post(
            f"{API_PREFIX}/auth/refresh", json={"refreshToken": first["refreshToken"]}
        )
        assert rotated.status_code == 200

        # 重放已轮换的旧令牌
        replay = await client.post(
            f"{API_PREFIX}/auth/refresh", json={"refreshToken": first["refreshToken"]}
        )
        assert replay.status_code == 401

        # 另一条独立登录的刷新令牌也被吊销
        other = await client.post(
            f"{API_PREFIX}/auth/refresh", json={"refreshToken": second["refreshToken"]}
        )
        assert other.status_code == 401

    async def test_invalid_refresh_token_rejected(self, client: AsyncClient) -> None:
        response = await client.post(
            f"{API_PREFIX}/auth/refresh", json={"refreshToken": "not-a-real-token"}
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# 鉴权守卫
# ---------------------------------------------------------------------------


class TestAuthGuard:
    """受保护端点的访问控制。"""

    @pytest.mark.parametrize(
        "path",
        [f"{API_PREFIX}/me"],
    )
    async def test_missing_token_returns_401(self, client: AsyncClient, path: str) -> None:
        response = await client.get(path)
        assert response.status_code == 401
        assert response.json()["code"] == "UNAUTHENTICATED"

    async def test_malformed_authorization_header_returns_401(
        self, client: AsyncClient
    ) -> None:
        response = await client.get(
            f"{API_PREFIX}/me", headers={"Authorization": "Bearer not-a-jwt"}
        )
        assert response.status_code == 401

    async def test_non_bearer_scheme_returns_401(self, client: AsyncClient) -> None:
        response = await client.get(
            f"{API_PREFIX}/me", headers={"Authorization": "Basic YWRtaW46YWRtaW4="}
        )
        assert response.status_code == 401

    async def test_valid_token_grants_access(self, client: AsyncClient, auth) -> None:
        headers = await auth.platform_headers()
        response = await client.get(f"{API_PREFIX}/me", headers=headers)
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# 当前用户与修改密码
# ---------------------------------------------------------------------------


class TestCurrentUser:
    """``/me`` 端点。"""

    async def test_returns_profile_with_permissions(self, client: AsyncClient, auth) -> None:
        headers = await auth.merchant_headers()
        response = await client.get(f"{API_PREFIX}/me", headers=headers)
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["account"] == "13812345679"
        assert body["role"] == "MERCHANT_ADMIN"
        assert body["tenantId"] == "t-001"
        assert body["tenantName"]
        assert body["permissions"]
        assert body["permissions"] == sorted(body["permissions"])

    async def test_platform_admin_has_null_tenant(self, client: AsyncClient, auth) -> None:
        headers = await auth.platform_headers()
        body = (await client.get(f"{API_PREFIX}/me", headers=headers)).json()
        assert body["tenantId"] is None
        assert body["tenantName"] is None


class TestChangePassword:
    """修改密码。"""

    async def test_change_password_then_login_with_new_one(self, client: AsyncClient, auth) -> None:
        headers = await auth.merchant_headers()
        new_password = "Brand-New-Pass#2026"

        response = await client.post(
            f"{API_PREFIX}/me/password",
            headers=headers,
            json={"oldPassword": TEST_MERCHANT_PASSWORD, "newPassword": new_password},
        )
        assert response.status_code == 200, response.text

        # 新密码可登录
        login = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"account": "13812345679", "password": new_password},
        )
        assert login.status_code == 200, login.text

    async def test_wrong_old_password_is_rejected(self, client: AsyncClient, auth) -> None:
        headers = await auth.merchant_headers()
        response = await client.post(
            f"{API_PREFIX}/me/password",
            headers=headers,
            json={"oldPassword": "wrong-old", "newPassword": "Brand-New-Pass#2026"},
        )
        assert response.status_code == 401
        assert response.json()["code"] == "INVALID_CREDENTIALS"

    async def test_weak_new_password_is_rejected(self, client: AsyncClient, auth) -> None:
        headers = await auth.merchant_headers()
        response = await client.post(
            f"{API_PREFIX}/me/password",
            headers=headers,
            json={"oldPassword": TEST_MERCHANT_PASSWORD, "newPassword": "12345678"},
        )
        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 登出与租户下拉
# ---------------------------------------------------------------------------


class TestLogout:
    """登出。"""

    async def test_logout_revokes_refresh_token(self, client: AsyncClient) -> None:
        login = (
            await client.post(
                f"{API_PREFIX}/auth/login",
                json={"account": TEST_PLATFORM_ACCOUNT, "password": TEST_PLATFORM_PASSWORD},
            )
        ).json()

        response = await client.post(
            f"{API_PREFIX}/auth/logout",
            headers={"Authorization": f"Bearer {login['accessToken']}"},
            json={"refreshToken": login["refreshToken"]},
        )
        assert response.status_code == 200

        # 已吊销，无法再刷新
        refresh = await client.post(
            f"{API_PREFIX}/auth/refresh", json={"refreshToken": login["refreshToken"]}
        )
        assert refresh.status_code == 401


class TestLoginTenants:
    """登录页租户下拉。"""

    async def test_returns_active_tenants_only(self, client: AsyncClient, make_tenant) -> None:
        await make_tenant(code="VISIBLE-01", name="可见租户")
        await make_tenant(code="HIDDEN-01", name="禁用租户", status=TenantStatus.DISABLED)

        response = await client.get(f"{API_PREFIX}/auth/tenants")
        assert response.status_code == 200

        body = response.json()
        codes = {record["code"] for record in body["records"]}
        assert "VISIBLE-01" in codes
        assert "HIDDEN-01" not in codes

    async def test_does_not_leak_contact_information(self, client: AsyncClient) -> None:
        """租户下拉不得泄漏联系方式（登录页无需认证）。"""
        body = (await client.get(f"{API_PREFIX}/auth/tenants")).json()
        for record in body["records"]:
            assert "contactPhone" not in record
            assert "contact_phone" not in record
            assert "contactName" not in record
            assert "email" not in record


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------


class TestHealth:
    """健康检查端点。"""

    async def test_health_needs_no_auth(self, client: AsyncClient) -> None:
        response = await client.get(f"{API_PREFIX}/health")
        assert response.status_code == 200

        body = response.json()
        assert body["status"] == "UP"
        assert body["version"]
        assert body["environment"] == "testing"
        assert body["database"] == "SQLite"

    async def test_readiness_reports_database_up(self, client: AsyncClient) -> None:
        response = await client.get(f"{API_PREFIX}/health/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "UP"

    async def test_api_routes_not_shadowed_by_frontend_mount(self, client: AsyncClient) -> None:
        """前端静态挂载不得遮蔽 API 路由（曾在设计中被列为风险）。"""
        response = await client.get(f"{API_PREFIX}/health")
        assert response.status_code == 200
        assert response.json()["status"] == "UP"

    async def test_unknown_api_path_returns_structured_error(self, client: AsyncClient) -> None:
        """未知 API 路径返回统一错误结构，而不是 HTML。"""
        response = await client.get(f"{API_PREFIX}/definitely-not-a-route")
        assert response.status_code == 404
        body = response.json()
        assert "code" in body
        assert "traceId" in body
