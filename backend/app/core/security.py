"""密码哈希与 JWT 令牌。

安全设计
--------
* 密码使用 **bcrypt** 哈希，cost 可配置（默认 12）。
* JWT 为无状态访问令牌，claims 中携带 ``role`` / ``tenantId`` / ``perms``
  / ``factoryId``，使租户与工厂作用域过滤、权限校验全部无需回查数据库。
* 刷新令牌只存 SHA-256 摘要，支持轮换与吊销，能检测令牌重放。
"""

from __future__ import annotations

import hashlib
import secrets
import string
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import bcrypt
import jwt

from app.core.config import settings
from app.core.errors import unauthenticated, validation_error

TokenType = Literal["access", "refresh"]


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    """对明文密码做 bcrypt 哈希。"""
    if not plain:
        raise validation_error("密码不能为空")
    salt = bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文密码与哈希是否匹配。

    对非法的哈希串返回 ``False`` 而非抛异常，避免把内部实现细节暴露给调用方。
    """
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def validate_password_strength(password: str) -> None:
    """校验密码强度。不通过时抛 :class:`AppException`。"""
    if len(password) < settings.MIN_PASSWORD_LENGTH:
        raise validation_error(
            f"密码长度不得少于 {settings.MIN_PASSWORD_LENGTH} 位",
            details={"minLength": settings.MIN_PASSWORD_LENGTH},
        )
    if password.isdigit():
        raise validation_error("密码不能为纯数字")
    if password.lower() in _WEAK_PASSWORDS:
        raise validation_error("密码过于常见，请更换")


#: 常见弱口令黑名单（含历史项目中出现过的弱口令）
_WEAK_PASSWORDS: frozenset[str] = frozenset(    {
        "admin",
        "admin123",
        "admin@2024",
        "administrator",
        "password",
        "password1",
        "12345678",
        "123456789",
        "1234567890",
        "qwertyui",
        "abc12345",
        "changeme",
        "toyverse",
    }
)


def generate_password(length: int = 16) -> str:
    """生成强随机初始密码。

    用于「平台代为开通租户账号」与「重置密码」场景：管理员点一下即可
    得到合规口令，**只在下发时返回一次**，且账号被置为「下次登录必须改密」。
    这样既不把弱口令写死进代码（附录 B 的硬编码弱口令问题），
    也不让管理员去手工编造密码。

    Returns:
        满足 :func:`validate_password_strength` 的随机密码。

    Example:
        >>> pwd = generate_password()
        >>> validate_password_strength(pwd)  # 一定通过
    """
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(max(12, length)))
        # 保证四类字符都出现，避免「纯数字」或「纯字母」被强度校验拒绝
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
            and any(c in "!@#$%^&*" for c in candidate)
        ):
            return candidate


# ---------------------------------------------------------------------------
# 令牌载荷
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TokenPayload:
    """JWT 载荷（解码后）。"""

    subject: str  # 用户 ID
    account: str
    role: str
    role_type: str
    tenant_id: str | None
    tenant_code: str | None
    #: 工厂账号所属工厂（P6 起）。非工厂账号恒为 ``None``。
    #: 与 ``tenant_id`` 同一处理方式：进 claims，使工厂作用域过滤无需回查数据库。
    factory_id: str | None = None
    permissions: list[str] = field(default_factory=list)
    token_type: TokenType = "access"
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    jti: str = ""

    def has_permission(self, code: str) -> bool:
        """是否拥有指定权限码。平台角色的 ``*`` 通配表示拥有全部权限。"""
        return "*" in self.permissions or code in self.permissions


# ---------------------------------------------------------------------------
# 签发
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _encode(payload: dict[str, Any]) -> str:
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def issue_access_token(
    *,
    user_id: str,
    account: str,
    role: str,
    role_type: str,
    tenant_id: str | None,
    tenant_code: str | None,
    permissions: list[str],
    factory_id: str | None = None,
) -> tuple[str, datetime]:
    """签发访问令牌。

    Args:
        factory_id: 工厂账号所属工厂；非工厂账号传 ``None``。
            放进 claims 而不是每次请求回查 ``user_accounts``，
            与 ``tenantId`` 同一权衡：换取无状态鉴权，代价是
            「换绑工厂」需要重新登录才生效（与换租户一致）。

    Returns:
        ``(token, 过期时间)``
    """
    now = _now()
    expires_at = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload: dict[str, Any] = {
        "sub": user_id,
        "account": account,
        "role": role,
        "roleType": role_type,
        "tenantId": tenant_id,
        "tenantCode": tenant_code,
        "factoryId": factory_id,
        "perms": permissions,
        "type": "access",
        "iss": settings.JWT_ISSUER,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": secrets.token_urlsafe(16),
    }
    return _encode(payload), expires_at


def issue_refresh_token() -> tuple[str, datetime, str]:
    """生成刷新令牌。

    刷新令牌是**随机串**而非 JWT——服务端只存其 SHA-256 摘要，
    因此可以随时吊销，也便于检测重放。

    Returns:
        ``(明文令牌, 过期时间, 令牌摘要)``
    """
    raw = secrets.token_urlsafe(48)
    expires_at = _now() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    return raw, expires_at, hash_token(raw)


def hash_token(raw: str) -> str:
    """计算令牌摘要（用于存储与比对）。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def decode_access_token(token: str) -> TokenPayload:
    """解码并校验访问令牌。

    Raises:
        AppException: 令牌无效、过期或类型不符。
    """
    try:
        raw = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            issuer=settings.JWT_ISSUER,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise unauthenticated("登录态已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise unauthenticated("登录态无效，请重新登录") from exc

    if raw.get("type") != "access":
        raise unauthenticated("令牌类型不正确")

    return TokenPayload(
        subject=raw["sub"],
        account=raw.get("account", ""),
        role=raw.get("role", ""),
        role_type=raw.get("roleType", ""),
        tenant_id=raw.get("tenantId"),
        tenant_code=raw.get("tenantCode"),
        # 兼容 P6 之前签发的旧令牌：claims 里没有 factoryId 时取 None，
        # 而不是让整张令牌失效——过期时间本来就会自然淘汰它们。
        factory_id=raw.get("factoryId"),
        permissions=list(raw.get("perms") or []),
        token_type="access",
        issued_at=datetime.fromtimestamp(raw["iat"], tz=UTC) if raw.get("iat") else None,
        expires_at=datetime.fromtimestamp(raw["exp"], tz=UTC) if raw.get("exp") else None,
        jti=raw.get("jti", ""),
    )


def mask_secret(value: str | None, *, keep: int = 4) -> str:
    """脱敏展示密钥：保留前 ``keep`` 位，其余以星号替代。

    用于任何需要展示密钥前缀的场景（如页面提示），**绝不用于返回完整密钥**。

    Example:
        >>> mask_secret("JX_AK_8fa3b21c")
        'JX_A****'
    """
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * 4
