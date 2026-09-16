"""SQLAlchemy 声明式基类与通用混入。

时区处理
--------
SQLite 不原生支持带时区的时间戳，PostgreSQL 支持。为了让两种数据库行为一致，
这里定义 :class:`UTCDateTime` 类型装饰器：**写入时统一转为 UTC aware**，
**读取时统一补上 UTC 时区**。避免跨数据库的时区漂移。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, TypeDecorator, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: 约束与索引的统一命名约定。
#: 显式命名让 Alembic 迁移可预测（否则不同数据库会生成不同的匿名约束名，
#: 导致 downgrade 与跨库迁移失败）。
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UTCDateTime(TypeDecorator[datetime]):
    """始终以 UTC 读写的时间戳类型。

    * 写入：naive datetime 视为 UTC，aware datetime 转换到 UTC 后存储
    * 读取：附加 UTC 时区信息
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            msg = f"期望 datetime，实际为 {type(value).__name__}"
            raise TypeError(msg)
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            msg = f"数据库中取出非 datetime 值：{type(value).__name__}"
            raise TypeError(msg)
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def to_dict(self, *, exclude: set[str] | None = None) -> dict[str, Any]:
        """把模型实例转为字典（供审计与调试使用，不用于 API 响应）。"""
        skip = exclude or set()
        return {
            column.key: getattr(self, column.key)
            for column in self.__table__.columns
            if column.key not in skip
        }

    def __repr__(self) -> str:
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


def utcnow() -> datetime:
    """当前 UTC 时间。

    定义在 :class:`TimestampMixin` 之前：该混入把本函数作为列默认值
    （``default=utcnow`` / ``onupdate=utcnow``），类体在导入时即求值，
    因此必须先定义、后引用。
    """
    return datetime.now(UTC)


class TimestampMixin:
    """创建时间与更新时间。

    为什么**同时**使用 Python 侧与数据库侧默认值
    --------------------------------------------
    * ``default`` / ``onupdate`` 传 Python 可调用对象：ORM 写入时把值算在客户端，
      因此 UPDATE 之后对象上的 ``updated_at`` **不会**变成过期属性。
      早期版本只写了 ``server_default`` / ``onupdate=func.now()``（SQL 表达式），
      SQLAlchemy 会在 UPDATE 后把该列标记为已过期；紧接着序列化 ORM 对象
      （本项目服务层返回的就是 ORM 实例）就会触发**异步惰性加载**，
      在 async 会话中抛 ``MissingGreenlet``——这正是 P3 阶段暴露出来的缺陷。
    * ``server_default`` 传 SQL 表达式：保证**非 ORM 路径**（原生 SQL、
      外部工具、迁移脚本）写入时，这两列依然有合理默认值。
    """

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        default=utcnow,
        server_default=func.now(),
        nullable=False,
        doc="创建时间（UTC）",
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
        doc="最后更新时间（UTC）",
    )


class TenantScopedMixin:
    """带租户归属的模型。

    标记此混入的模型**必须**通过 repository 层的 ``tenant_scope`` 查询，
    不允许在路由层手写过滤（见 CONTRIBUTING.md 架构红线 #1）。
    """

    @property
    def is_tenant_scoped(self) -> bool:
        return True
