"""pytest 公共夹具。

关键设计
--------
1. **环境变量必须在导入 app 之前设置**——``app.core.config.settings`` 是模块级单例，
   导入即固化配置。因此本文件顶部先设置 ``os.environ`` 再导入应用代码。
2. **每个测试用例运行在独立事务中并最终回滚**——保证用例之间互不干扰。
   使用 SQLAlchemy 2.0 的 ``join_transaction_mode="create_savepoint"``，
   使被测代码内部的 ``session.commit()`` 转为 SAVEPOINT，外层仍可整体回滚。
3. **测试用独立数据库文件**，绝不触碰开发库 ``data/toyverse.db``。
4. bcrypt 轮数降到 4，显著加快测试速度（生产为 12）。
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 1) 在导入应用代码之前设置环境变量
# ---------------------------------------------------------------------------

_TMP_DIR = Path(tempfile.mkdtemp(prefix="toyverse-test-"))
_TEST_DB = _TMP_DIR / "test.db"

# 测试用账号（与 conftest 下方注入的环境变量保持一致，供断言引用）
TEST_PLATFORM_ACCOUNT = "admin"
TEST_PLATFORM_OPERATOR_ACCOUNT = "13812345678"
TEST_MERCHANT_ACCOUNT = "13812345679"
TEST_FACTORY_ACCOUNT = "13600000000"

# 测试用管理员口令（满足强度校验，但仅存在于测试进程）
TEST_PLATFORM_PASSWORD = "Test-Platf0rm#2026"
TEST_PLATFORM_OPERATOR_PASSWORD = "Test-PlatOps#2026"
TEST_MERCHANT_PASSWORD = "Test-Merch4nt#2026"
TEST_FACTORY_PASSWORD = "Test-Fact0ry#2026"

os.environ.update(
    {
        "APP_ENV": "testing",
        "DEBUG": "true",
        "DATABASE_URL": f"sqlite+aiosqlite:///{_TEST_DB}",
        # JWT 密钥：长度 ≥32 字节且不含占位符关键词
        "JWT_SECRET_KEY": "pytest-only-jwt-key-A1b2C3d4E5f6G7h8I9j0K1l2M3n4",
        "QR_SIGN_SECRET": "pytest-only-qr-sign-A1b2C3d4E5f6",
        # 厂商密钥的落库加密密钥：显式设置，走「专用密钥」分支
        "SECRET_ENCRYPTION_KEY": "pytest-only-encryption-key-Z9y8X7w6V5u4T3s2",
        "BCRYPT_ROUNDS": "4",
        "PLATFORM_ADMIN_ACCOUNT": TEST_PLATFORM_ACCOUNT,
        "PLATFORM_ADMIN_PASSWORD": TEST_PLATFORM_PASSWORD,
        "PLATFORM_ADMIN_NICKNAME": "平台管理员",
        "PLATFORM_OPERATOR_ACCOUNT": TEST_PLATFORM_OPERATOR_ACCOUNT,
        "PLATFORM_OPERATOR_PASSWORD": TEST_PLATFORM_OPERATOR_PASSWORD,
        "PLATFORM_OPERATOR_NICKNAME": "平台运营",
        "MERCHANT_ADMIN_ACCOUNT": TEST_MERCHANT_ACCOUNT,
        "MERCHANT_ADMIN_PASSWORD": TEST_MERCHANT_PASSWORD,
        "MERCHANT_ADMIN_NICKNAME": "商户管理员",
        "FACTORY_ADMIN_ACCOUNT": TEST_FACTORY_ACCOUNT,
        "FACTORY_ADMIN_PASSWORD": TEST_FACTORY_PASSWORD,
        "FACTORY_ADMIN_NICKNAME": "工厂管理员",
        "SEED_DEMO_DATA": "true",
        "AI_DEFAULT_PROVIDER": "mock",
        "MOCK_LATENCY_MS": "0",
        "CORS_ALLOWED_ORIGINS": "http://localhost",
    }
)

# ---------------------------------------------------------------------------
# 2) 导入应用代码
# ---------------------------------------------------------------------------

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings  # noqa: E402
from app.core.ids import new_id  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.seed import seed_demo_data  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models.enums import TenantStatus, UserStatus  # noqa: E402
from app.models.identity import Tenant, UserAccount  # noqa: E402

API_PREFIX = "/api/v1"


# ---------------------------------------------------------------------------
# 断言：确认环境变量确实覆盖了 .env
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """启动自检：确保测试配置生效，避免误连开发库。"""
    assert settings.APP_ENV == "testing", f"APP_ENV 未生效：{settings.APP_ENV}"
    assert "toyverse-test-" in settings.DATABASE_URL, (
        f"测试未使用独立数据库，当前为 {settings.DATABASE_URL}——"
        "环境变量可能未覆盖 .env，请检查 conftest.py"
    )
    assert settings.BCRYPT_ROUNDS == 4, "测试应使用低 bcrypt 轮数以加速"


# ---------------------------------------------------------------------------
# 数据库夹具
# ---------------------------------------------------------------------------


#: 标记 schema 是否已在本测试进程内创建
_schema_created = False


@pytest_asyncio.fixture
async def db_engine() -> AsyncIterator[Any]:
    """函数级引擎：保证每个用例从干净数据开始。

    schema 只在**首个用例**创建一次；后续用例仅清空全部数据表。
    逐用例重建 schema 会让每个用例多消耗约 1 秒的 DDL 开销，
    而「清空数据」的隔离效果与之等价。

    为什么不用「外层事务 + 用例结束回滚」的经典方案？
    因为被测代码内部会显式调用 ``session.commit()``（登录、改密等写路径），
    可能穿透外层事务，导致数据在用例之间泄漏。
    """
    global _schema_created

    engine = create_async_engine(settings.resolved_database_url, future=True)
    async with engine.begin() as conn:
        if _schema_created:
            # 按依赖逆序删除，避免外键约束报错
            for table in reversed(Base.metadata.sorted_tables):
                await conn.execute(table.delete())
        else:
            await conn.run_sync(Base.metadata.create_all)
            _schema_created = True
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db(db_engine: Any) -> AsyncIterator[AsyncSession]:
    """函数级会话。

    每个用例都从空库开始（由 ``db_engine`` 负责重建），
    用例内的多次 ``commit()`` 是该用例自己的事。
    """
    factory = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def seeded(db: AsyncSession) -> AsyncSession:
    """在测试事务内写入基础数据（角色 / 租户 / 管理员账号）。

    直接调用生产种子函数，保证测试与线上初始化逻辑一致。
    """
    await _seed_within_session(db)
    return db


async def _seed_within_session(session: AsyncSession) -> None:
    """在当前事务内执行种子逻辑（复用生产实现，但共享同一会话）。"""
    from app.db import seed as seed_module

    await seed_module._seed_roles(session)
    await seed_module._seed_tenants(session)
    await seed_module._seed_admin_users(session)
    await session.flush()


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(seeded: AsyncSession) -> AsyncIterator[AsyncClient]:
    """已注入测试数据库会话的 HTTP 客户端。

    不触发应用 lifespan——避免它用独立连接向测试库写入数据而绕过事务回滚。
    演示数据由 ``seeded`` 夹具在测试事务内准备。
    """

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield seeded

    fastapi_app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client
    fastapi_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 登录辅助
# ---------------------------------------------------------------------------


class AuthHelper:
    """登录与带鉴权请求的便捷封装。"""

    def __init__(self, client: AsyncClient) -> None:
        self.client = client

    async def login(self, account: str, password: str) -> dict[str, Any]:
        """登录并返回响应体，非 200 时附带响应内容便于排查。"""
        response = await self.client.post(
            f"{API_PREFIX}/auth/login", json={"account": account, "password": password}
        )
        assert response.status_code == 200, (
            f"登录失败 [{response.status_code}]：{response.text}"
        )
        return response.json()

    async def token(self, account: str, password: str) -> str:
        """登录并返回访问令牌。"""
        return str((await self.login(account, password))["accessToken"])

    @staticmethod
    def headers(token: str) -> dict[str, str]:
        """构造带鉴权的请求头。"""
        return {"Authorization": f"Bearer {token}"}

    async def platform_headers(self) -> dict[str, str]:
        """平台超级管理员（通配权限）的请求头。"""
        return self.headers(await self.token(TEST_PLATFORM_ACCOUNT, TEST_PLATFORM_PASSWORD))

    async def platform_operator_headers(self) -> dict[str, str]:
        """平台运营（只有读权限 + 产品/订单/设备写权限）的请求头。"""
        return self.headers(
            await self.token(TEST_PLATFORM_OPERATOR_ACCOUNT, TEST_PLATFORM_OPERATOR_PASSWORD)
        )

    async def merchant_headers(self) -> dict[str, str]:
        return self.headers(await self.token(TEST_MERCHANT_ACCOUNT, TEST_MERCHANT_PASSWORD))

    async def factory_headers(self) -> dict[str, str]:
        return self.headers(await self.token(TEST_FACTORY_ACCOUNT, TEST_FACTORY_PASSWORD))


@pytest_asyncio.fixture
async def auth(client: AsyncClient) -> AuthHelper:
    """登录辅助夹具。"""
    return AuthHelper(client)


# ---------------------------------------------------------------------------
# 构造测试数据的小工具
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def make_tenant(db: AsyncSession) -> Any:
    """工厂夹具：创建一个租户。"""

    async def _make(
        *,
        code: str | None = None,
        name: str = "测试租户",
        status: TenantStatus | str = TenantStatus.ACTIVE,
        tenant_id: str | None = None,
    ) -> Tenant:
        tenant = Tenant(
            id=tenant_id or new_id("tenant"),
            code=code or new_id("tenant").upper(),
            name=name,
            status=str(status),
        )
        db.add(tenant)
        await db.flush()
        return tenant

    return _make


@pytest_asyncio.fixture
async def make_user(db: AsyncSession) -> Any:
    """工厂夹具：创建一个用户账号。"""

    async def _make(
        *,
        account: str,
        password: str = "Str0ng-Passw0rd!",
        role_code: str = "MERCHANT_ADMIN",
        tenant_id: str | None = None,
        status: UserStatus | str = UserStatus.ACTIVE,
        locked_until: Any = None,
        login_failures: int = 0,
        user_id: str | None = None,
    ) -> UserAccount:
        user = UserAccount(
            id=user_id or new_id("user"),
            account=account,
            password_hash=hash_password(password),
            nickname=account,
            role_code=role_code,
            tenant_id=tenant_id,
            status=str(status),
            login_failures=login_failures,
            locked_until=locked_until,
        )
        db.add(user)
        await db.flush()
        return user

    return _make


__all__ = [
    "API_PREFIX",
    "TEST_FACTORY_PASSWORD",
    "TEST_MERCHANT_PASSWORD",
    "TEST_PLATFORM_PASSWORD",
    "seed_demo_data",
]
