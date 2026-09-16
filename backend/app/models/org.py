"""组织架构与工厂模型。

包含：组织架构、职位、烧录工厂。

设计要点
--------
* ``Organization`` 支持树形结构（``parent_id`` 自引用），按租户隔离。
* ``Position`` 为租户内的职位（如店长、销售）。
* ``Factory`` 独立于租户——工厂跨租户承接生产任务，
  因此工厂账号的 ``tenant_id`` 为 ``None``，通过字段脱敏而非租户过滤来保护数据。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.enums import EnableStatus

if TYPE_CHECKING:
    pass


class Organization(Base, TimestampMixin):
    """组织架构节点（树形）。"""

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    remark: Mapped[str | None] = mapped_column(Text)

    children: Mapped[list[Organization]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )
    parent: Mapped[Organization | None] = relationship(
        back_populates="children", remote_side="Organization.id"
    )

    def __repr__(self) -> str:
        return f"<Organization {self.name} tenant={self.tenant_id}>"


class Position(Base, TimestampMixin):
    """职位（租户内）。"""

    __tablename__ = "positions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remark: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<Position {self.code} {self.name}>"


class Factory(Base, TimestampMixin):
    """烧录工厂。

    工厂跨租户承接生产任务，因此不属于任何租户。
    工厂端接口通过 :class:`~app.services.serializers.FactoryOrderSerializer`
    做字段白名单脱敏——客户名、金额、联系方式一律不下发。
    """

    __tablename__ = "factories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(64))
    contact_phone: Mapped[str | None] = mapped_column(String(32))
    address: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EnableStatus.ENABLED, index=True
    )
    remark: Mapped[str | None] = mapped_column(Text)

    # ---- 产能与资质（供工厂端工作台展示） ----
    daily_capacity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, doc="日烧录产能（台）"
    )
    is_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, doc="是否已通过资质审核"
    )

    def __repr__(self) -> str:
        return f"<Factory {self.code} {self.name}>"
