"""Alembic 环境配置。

要点
----
* 连接串从应用配置读取（``settings.resolved_database_url``），不在此硬编码，
  保证迁移与运行时使用**同一个**数据库。
* 使用异步引擎（``async_engine_from_config``）以匹配运行时的 aiosqlite / asyncpg 驱动。
* 导入 ``app.models`` 以让 ``autogenerate`` 发现全部表。
* ``render_as_batch=True``：SQLite 不支持 ``ALTER COLUMN``，
  开启批处理模式后 Alembic 会用「建新表-拷数据-换名」的方式实现列变更。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

# 导入全部模型，使 autogenerate 能发现所有表（务必保留）
import app.models  # noqa: F401
from app.core.config import ensure_runtime_dirs, settings
from app.db.base import Base

# 保证 SQLite 数据库文件目录存在（首次执行迁移时会自动创建）
ensure_runtime_dirs()

# Alembic 配置对象
config = context.config

# 注入数据库连接串
config.set_main_option("sqlalchemy.url", settings.resolved_database_url)

# 配置日志
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate 的元数据来源
target_metadata = Base.metadata


def _include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """过滤对象。

    排除 SQLite 内部表（如 ``sqlite_sequence``）与 Alembic 自身版本表。
    """
    if type_ == "table" and name in {"alembic_version", "sqlite_sequence"}:
        return False
    return True


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL 脚本，不连接数据库。

    用法：``alembic upgrade head --sql``
    """
    context.configure(
        url=settings.resolved_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: object) -> None:
    """在同步上下文里执行迁移（由异步入口调用）。"""
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
        # SQLite 不支持 ALTER COLUMN，需要批处理模式
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """在线模式：连接数据库并执行迁移。"""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = settings.resolved_database_url

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
