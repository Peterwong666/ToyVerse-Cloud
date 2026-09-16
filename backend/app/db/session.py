"""数据库会话管理。

同时支持 SQLite（默认，零配置）与 PostgreSQL（可选）。
两种数据库的差异在此模块内抹平，业务代码无需关心。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import ensure_runtime_dirs, settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 引擎构造
# ---------------------------------------------------------------------------


def _build_engine_kwargs() -> dict[str, Any]:
    """按数据库类型构造引擎参数。"""
    if settings.is_sqlite:
        # SQLite 单文件，连接池意义不大；关闭同线程检查以配合异步
        return {
            "connect_args": {"check_same_thread": False},
            "poolclass": NullPool,
        }

    return {
        "pool_size": settings.DATABASE_POOL_SIZE,
        "max_overflow": settings.DATABASE_POOL_MAX_OVERFLOW,
        "pool_recycle": settings.DATABASE_POOL_RECYCLE,
        "pool_pre_ping": True,
    }


# 保证 SQLite 文件目录与本地存储目录存在（幂等）
ensure_runtime_dirs()

engine: AsyncEngine = create_async_engine(
    settings.resolved_database_url,
    echo=settings.DATABASE_ECHO,
    future=True,
    **_build_engine_kwargs(),
)


# SQLite 默认不强制外键约束，需逐连接开启，否则 FK 形同虚设
if settings.is_sqlite:

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# ---------------------------------------------------------------------------
# 会话工厂
# ---------------------------------------------------------------------------

SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：提供一个请求级数据库会话。

    事务边界由路由/服务层显式控制；出现异常时自动回滚。
    """
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# 生命周期辅助
# ---------------------------------------------------------------------------


async def check_connection() -> bool:
    """探测数据库连通性（供 ``/health/ready`` 使用）。"""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("数据库连通性探测失败：%s", exc)
        return False
    return True


async def dispose_engine() -> None:
    """关闭连接池。应用退出时调用。"""
    await engine.dispose()
    logger.info("数据库连接池已关闭")


def database_backend_name() -> str:
    """返回当前数据库类型的中文名（供启动日志与健康检查展示）。"""
    return "SQLite" if settings.is_sqlite else "PostgreSQL"
