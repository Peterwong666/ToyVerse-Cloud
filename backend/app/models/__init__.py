"""ORM 模型集合。

在此统一导入全部模型，使 Alembic 的 ``autogenerate`` 能够发现所有表。
新增模型模块时，务必在此登记。
"""

from app.db.base import Base
from app.models.ai import (
    AiConfig,
    AiProvider,
    DialogueMessage,
    DialogueSession,
    KbFile,
    KnowledgeBase,
    RolePreset,
    VoiceProfile,
)
from app.models.audit import AuditLog, IdempotencyKey, OutboxEvent
from app.models.catalog import (
    ClientProduct,
    CloudProvider,
    MiniAppConfig,
    ProductAuthorization,
    ProductTemplate,
)
from app.models.identity import RefreshToken, Role, RolePermission, Tenant, UserAccount
from app.models.org import Factory, Organization, Position

__all__ = [
    "AiConfig",
    "AiProvider",
    "AuditLog",
    "Base",
    "ClientProduct",
    "CloudProvider",
    "DialogueMessage",
    "DialogueSession",
    "Factory",
    "IdempotencyKey",
    "KbFile",
    "KnowledgeBase",
    "MiniAppConfig",
    "Organization",
    "OutboxEvent",
    "Position",
    "ProductAuthorization",
    "ProductTemplate",
    "RefreshToken",
    "Role",
    "RolePermission",
    "RolePreset",
    "Tenant",
    "UserAccount",
    "VoiceProfile",
]
