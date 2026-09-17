"""对称加密：外部厂商密钥的落库保护。

解决的问题
----------
早期原型把云服务商的 ``SecretKey`` **明文存在数据库并直接下发到前端**。
本项目按附录 B 的要求修正：

1. 密钥以对称加密后的密文落库（``access_key_enc`` / ``secret_key_enc``）
2. 任何响应体都不包含明文密钥，只返回**掩码提示**（前 4 位 + 星号）
3. 展示用的掩码在写入时就固化下来（``*_hint`` 字段），
   因此列表页**无需解密**即可展示「已配置哪个密钥」，把解密面收敛到最小

密钥来源
--------
优先使用 ``SECRET_ENCRYPTION_KEY``；未配置时由 ``JWT_SECRET_KEY`` 派生
（两者都经过启动期强度校验）。生产环境建议显式配置独立的
``SECRET_ENCRYPTION_KEY``，避免「换 JWT 密钥导致密文全部失效」。
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.core.errors import internal_error, validation_error

#: 由 JWT 密钥派生加密密钥时使用的域分隔前缀
#: （保证同一份 JWT 密钥在不同用途下派生出不同的密钥）
_DERIVE_SALT = "toyverse:kms:v1:"

#: 掩码保留的明文前缀长度
_HINT_KEEP = 4


def _raw_key_material() -> str:
    """返回用于派生加密密钥的原始材料。"""
    dedicated = settings.SECRET_ENCRYPTION_KEY.strip()
    if dedicated:
        return dedicated
    return f"{_DERIVE_SALT}{settings.JWT_SECRET_KEY}"


def fernet_key() -> bytes:
    """派生合法的 Fernet 密钥（32 字节后再做 urlsafe base64）。"""
    digest = hashlib.sha256(_raw_key_material().encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet() -> Fernet:
    return Fernet(fernet_key())


def encrypt_secret(plain: str) -> str:
    """加密明文密钥。

    Args:
        plain: 明文密钥（如 ``SecretKey``、小程序 ``AppSecret``）。

    Returns:
        Fernet 密文（URL-safe base64 字符串），可直接存进 ``VARCHAR``/``TEXT``。
    """
    if not plain:
        raise validation_error("密钥不能为空")
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """解密密文，还原明文密钥。

    **仅供服务端调用厂商接口时使用**，绝不可把返回值放进任何响应体。

    Raises:
        AppException: 密文损坏，或加密密钥已被更换。
    """
    if not token:
        raise validation_error("密文不能为空")
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise internal_error(
            "密钥解密失败：加密密钥可能已变更，请重新录入该厂商密钥"
        ) from exc


def secret_hint(plain: str) -> str:
    """生成用于展示的掩码提示（前 4 位 + 星号）。

    与 :func:`app.core.security.mask_secret` 的区别：本函数的结果会被
    **持久化**（``access_key_hint``），因此长度固定、不随密钥轮换变化。

    Example:
        >>> secret_hint("JX_AK_8fa3b21c")
        'JX_A****'
    """
    text = (plain or "").strip()
    if not text:
        return ""
    if len(text) <= _HINT_KEEP:
        return "*" * len(text)
    return f"{text[:_HINT_KEEP]}****"


def looks_encrypted(value: str) -> bool:
    """粗略判断一个字符串是否为 Fernet 密文（供运维排查与数据校验使用）。"""
    if not value or len(value) < 60:
        return False
    try:
        base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return False
    return value.startswith("gAAAAA")
