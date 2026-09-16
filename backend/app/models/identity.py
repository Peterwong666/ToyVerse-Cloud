"""身份与租户领域模型。

包含：租户、角色、角色权限、用户账号、刷新令牌。

设计要点
--------
* ``Tenant`` 合并了原型中「客户 client」的业务字段（联系人/电话/行业等）。
* 平台管理员的 ``tenant_id`` 为 ``None``——平台是全局长，不属于任何租户
  （修正参考实现把平台账号挂在 ``t-001`` 下的矛盾）。
* ``UserAccount`` 归属单一租户，无多对多关联表。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UTCDateTime
from app.models.enums import RoleType, TenantStatus, UserStatus


class Tenant(Base, TimestampMixin):
    """租户（品牌方 / 客户）。

    平台端称之为「客户」，商户端自身即一个租户。
    ``code`` 为租户编码，全局唯一，是登录页下拉与运维排查的标识。
    """

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TenantStatus.ACTIVE, index=True
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")

    # ---- 以下字段合并自原型的「客户」实体 ----
    contact_name: Mapped[str | None] = mapped_column(String(64), doc="联系人")
    contact_phone: Mapped[str | None] = mapped_column(String(32), doc="联系电话（兼登录账号）")
    email: Mapped[str | None] = mapped_column(String(128))
    industry: Mapped[str | None] = mapped_column(String(64), doc="所属行业")
    remark: Mapped[str | None] = mapped_column(Text, doc="备注")

    # ---- 关系 ----
    # 注意：跨模块关系（产品授权 / 客户产品 / 设备 / 订单）在对应模型模块落地后
    # 再行补充，避免在分阶段交付中引入对未实现模块的依赖。
    users: Mapped[list[UserAccount]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Tenant {self.code} {self.name}>"


class Role(Base, TimestampMixin):
    """角色。

    ``role_type`` 决定数据可见范围，是租户隔离的第一层依据。
    """

    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    role_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    is_builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, doc="内置角色不可删除"
    )

    permissions: Mapped[list[RolePermission]] = relationship(
        back_populates="role", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def permission_codes(self) -> list[str]:
        """该角色的权限码列表。"""
        return sorted(p.permission for p in self.permissions)

    @property
    def type_enum(self) -> RoleType:
        return RoleType(self.role_type)

    def __repr__(self) -> str:
        return f"<Role {self.code} type={self.role_type}>"


class RolePermission(Base):
    """角色-权限关联（复合主键）。"""

    __tablename__ = "role_permissions"

    role_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission: Mapped[str] = mapped_column(String(128), primary_key=True)

    role: Mapped[Role] = relationship(back_populates="permissions")

    def __repr__(self) -> str:
        return f"<RolePermission {self.role_id}:{self.permission}>"


class UserAccount(Base, TimestampMixin):
    """用户账号。

    覆盖平台端、商户端、工厂端三类使用者的登录账号。
    ``tenant_id`` 为 ``None`` 表示平台或工厂账号（全局长 / 跨租户）。
    """

    __tablename__ = "user_accounts"
    __table_args__ = (
        UniqueConstraint("account", name="uq_user_accounts_account"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    nickname: Mapped[str | None] = mapped_column(String(128))
    avatar: Mapped[str | None] = mapped_column(String(512))

    role_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    organization_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL")
    )
    position_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("positions.id", ondelete="SET NULL")
    )

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=UserStatus.ACTIVE, index=True
    )
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, doc="首次登录是否强制改密"
    )

    # ---- 登录保护 ----
    login_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_login_ip: Mapped[str | None] = mapped_column(String(64))

    # ---- 关系 ----
    tenant: Mapped[Tenant | None] = relationship(back_populates="users")
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_locked(self) -> bool:
        """账号当前是否处于锁定期。"""
        from app.db.base import utcnow

        return self.locked_until is not None and self.locked_until > utcnow()

    def __repr__(self) -> str:
        return f"<UserAccount {self.account} role={self.role_code} tenant={self.tenant_id}>"


class RefreshToken(Base, TimestampMixin):
    """刷新令牌。

    只存 ``token_hash``（SHA-256），不存明文，防止数据库泄漏后被直接利用。
    支持轮换（``rotated_to``）与吊销（``revoked_at``）。
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    rotated_to: Mapped[str | None] = mapped_column(
        String(36), doc="轮换后的新令牌 ID，用于检测重放"
    )
    user_agent: Mapped[str | None] = mapped_column(String(256))
    client_ip: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[UserAccount] = relationship(back_populates="refresh_tokens")

    @property
    def is_usable(self) -> bool:
        from app.db.base import utcnow

        return self.revoked_at is None and self.expires_at > utcnow()

    def __repr__(self) -> str:
        return f"<RefreshToken user={self.user_id} expires={self.expires_at:%Y-%m-%d %H:%M}>"
