"""ORM 模型集合。

在此统一导入全部模型，使 Alembic 的 ``autogenerate`` 能够发现所有表。
新增模型模块时，务必在此登记。
"""

from app.db.base import Base
from app.models.audit import AuditLog, IdempotencyKey, OutboxEvent
from app.models.identity import RefreshToken, Role, RolePermission, Tenant, UserAccount
from app.models.org import Factory, Organization, Position

__all__ = [
    "AuditLog",
    "Base",
    "Factory",
    "IdempotencyKey",
    "Organization",
    "OutboxEvent",
    "Position",
    "RefreshToken",
    "Role",
    "RolePermission",
    "Tenant",
    "UserAccount",
]
