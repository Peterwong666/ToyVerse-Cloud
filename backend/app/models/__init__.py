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
from app.models.device import (
    Device,
    DeviceBatch,
    DeviceBatchLine,
    DeviceCredential,
    DeviceEvent,
)
from app.models.factory import BurnReport, FactoryOrder, Inspection
from app.models.identity import RefreshToken, Role, RolePermission, Tenant, UserAccount
from app.models.order import Order
from app.models.org import Factory, Organization, Position

__all__ = [
    "AiConfig",
    "AiProvider",
    "AuditLog",
    "Base",
    "BurnReport",
    "ClientProduct",
    "CloudProvider",
    "Device",
    "DeviceBatch",
    "DeviceBatchLine",
    "DeviceCredential",
    "DeviceEvent",
    "DialogueMessage",
    "DialogueSession",
    "Factory",
    "FactoryOrder",
    "IdempotencyKey",
    "Inspection",
    "KbFile",
    "KnowledgeBase",
    "MiniAppConfig",
    "Order",
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
