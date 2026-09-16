"""认证相关请求/响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    """登录请求。"""

    account: str = Field(min_length=1, max_length=64, description="账号（手机号）")
    password: str = Field(min_length=1, max_length=128, description="密码")

    @field_validator("account")
    @classmethod
    def _strip_account(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("账号不能为空")
        return value


class RefreshRequest(BaseModel):
    """刷新令牌请求。"""

    refreshToken: str = Field(min_length=1, description="刷新令牌")  # noqa: N815 - 与前端契约一致


class LogoutRequest(BaseModel):
    """登出请求。"""

    refreshToken: str | None = Field(default=None, description="留空则吊销该用户全部登录态")  # noqa: N815


class TenantOption(BaseModel):
    """登录页的租户下拉项（脱敏：不含联系方式）。"""

    id: str
    code: str
    name: str
    industry: str | None = None


class UserProfile(BaseModel):
    """登录响应中的用户信息。"""

    userId: str  # noqa: N815
    account: str
    nickname: str | None = None
    avatar: str | None = None
    role: str = Field(description="角色编码")
    roleType: str = Field(description="角色类型：PLATFORM / MERCHANT / FACTORY")  # noqa: N815
    tenantId: str | None = None  # noqa: N815
    tenantCode: str | None = None  # noqa: N815
    tenantName: str | None = None  # noqa: N815
    permissions: list[str] = Field(default_factory=list)


class LoginResponse(BaseModel):
    """登录响应。"""

    accessToken: str  # noqa: N815
    refreshToken: str  # noqa: N815
    tokenType: str = "Bearer"  # noqa: N815
    expiresIn: int = Field(description="访问令牌有效期（秒）")  # noqa: N815
    mustChangePassword: bool = False  # noqa: N815
    user: UserProfile


class RefreshResponse(BaseModel):
    """刷新响应。"""

    accessToken: str  # noqa: N815
    refreshToken: str  # noqa: N815
    tokenType: str = "Bearer"  # noqa: N815
    expiresIn: int  # noqa: N815


class CurrentUserResponse(UserProfile):
    """``/me`` 响应。"""

    lastLoginAt: datetime | None = None  # noqa: N815
    mustChangePassword: bool = False  # noqa: N815


class ChangePasswordRequest(BaseModel):
    """修改密码请求。"""

    oldPassword: str = Field(min_length=1, max_length=128)  # noqa: N815
    newPassword: str = Field(min_length=8, max_length=128)  # noqa: N815

    @field_validator("newPassword")
    @classmethod
    def _new_password_strength(cls, value: str) -> str:
        from app.core.security import validate_password_strength

        validate_password_strength(value)
        return value


class HealthResponse(BaseModel):
    """健康检查响应。"""

    status: str = Field(description="UP / DOWN")
    app: str
    version: str
    environment: str
    database: str = Field(description="数据库类型")
    time: datetime
