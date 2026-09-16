"""单元测试：密码哈希、JWT、脱敏工具。

不涉及数据库与 HTTP，纯函数级验证。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import settings
from app.core.errors import AppException, ErrorCode
from app.core.security import (
    TokenPayload,
    decode_access_token,
    hash_password,
    hash_token,
    issue_access_token,
    issue_refresh_token,
    mask_secret,
    validate_password_strength,
    verify_password,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 密码哈希
# ---------------------------------------------------------------------------


class TestPasswordHashing:
    """bcrypt 哈希与校验。"""

    def test_hash_then_verify_succeeds(self) -> None:
        hashed = hash_password("MyS3cret-Pass!")
        assert verify_password("MyS3cret-Pass!", hashed) is True

    def test_verify_rejects_wrong_password(self) -> None:
        hashed = hash_password("MyS3cret-Pass!")
        assert verify_password("Wrong-Pass!", hashed) is False

    def test_same_password_produces_different_hashes(self) -> None:
        """加盐：相同密码两次哈希结果必须不同。"""
        assert hash_password("Same-Pass!") != hash_password("Same-Pass!")

    def test_hash_never_returns_plaintext(self) -> None:
        plain = "PlainText-Pass!"
        assert plain not in hash_password(plain)

    @pytest.mark.parametrize("bad_hash", ["", "not-a-hash", "$2b$invalid"])
    def test_verify_tolerates_malformed_hash(self, bad_hash: str) -> None:
        """非法哈希串返回 False，不抛异常（避免暴露内部实现细节）。"""
        assert verify_password("anything", bad_hash) is False

    def test_hash_rejects_empty_password(self) -> None:
        with pytest.raises(AppException) as exc:
            hash_password("")
        assert exc.value.code is ErrorCode.VALIDATION_ERROR


# ---------------------------------------------------------------------------
# 密码强度
# ---------------------------------------------------------------------------


class TestPasswordStrength:
    """密码强度校验。"""

    def test_accepts_strong_password(self) -> None:
        validate_password_strength("Str0ng-Passw0rd!")

    def test_rejects_too_short(self) -> None:
        with pytest.raises(AppException) as exc:
            validate_password_strength("Ab1!")
        assert exc.value.code is ErrorCode.VALIDATION_ERROR
        assert "长度" in exc.value.message

    def test_rejects_all_digits(self) -> None:
        with pytest.raises(AppException):
            validate_password_strength("123456789012")

    @pytest.mark.parametrize(
        "weak",
        ["admin", "admin@2024", "password", "12345678", "changeme", "qwertyui"],
    )
    def test_rejects_weak_passwords(self, weak: str) -> None:
        """黑名单含历史项目中出现过的弱口令 ``admin@2024``。"""
        with pytest.raises(AppException) as exc:
            validate_password_strength(weak)
        assert exc.value.code is ErrorCode.VALIDATION_ERROR
        # 具体拒绝原因可能是「长度不足」「纯数字」或「过于常见」，均为合理拒绝
        assert any(
            keyword in exc.value.message for keyword in ("常见", "长度", "纯数字")
        ), f"{weak} 的拒绝原因不符合预期：{exc.value.message}"


# ---------------------------------------------------------------------------
# 访问令牌
# ---------------------------------------------------------------------------


class TestAccessToken:
    """JWT 签发与解码。"""

    @staticmethod
    def _issue(**overrides: object) -> tuple[str, datetime]:
        kwargs: dict[str, object] = {
            "user_id": "u-001",
            "account": "15811805314",
            "role": "MERCHANT_ADMIN",
            "role_type": "MERCHANT",
            "tenant_id": "t-001",
            "tenant_code": "DEMO-BRAND",
            "permissions": ["merchant:device:read", "merchant:device:write"],
        }
        kwargs.update(overrides)
        return issue_access_token(**kwargs)  # type: ignore[arg-type]

    def test_roundtrip_preserves_claims(self) -> None:
        token, expires_at = self._issue()
        payload = decode_access_token(token)

        assert isinstance(payload, TokenPayload)
        assert payload.subject == "u-001"
        assert payload.account == "15811805314"
        assert payload.role == "MERCHANT_ADMIN"
        assert payload.tenant_id == "t-001"
        assert payload.tenant_code == "DEMO-BRAND"
        assert payload.permissions == ["merchant:device:read", "merchant:device:write"]
        assert payload.token_type == "access"
        assert expires_at > datetime.now(UTC)

    def test_platform_admin_has_null_tenant(self) -> None:
        """架构决策 ADR-01：平台管理员不属于任何租户。"""
        token, _ = self._issue(role="PLATFORM_ADMIN", role_type="PLATFORM", tenant_id=None)
        payload = decode_access_token(token)
        assert payload.tenant_id is None

    def test_expired_token_is_rejected(self) -> None:
        """构造一个已过期的令牌。"""
        expired = jwt.encode(
            {
                "sub": "u-001",
                "role": "MERCHANT_ADMIN",
                "type": "access",
                "iss": settings.JWT_ISSUER,
                "iat": int((datetime.now(UTC) - timedelta(hours=3)).timestamp()),
                "exp": int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
            },
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )
        with pytest.raises(AppException) as exc:
            decode_access_token(expired)
        assert exc.value.code is ErrorCode.UNAUTHENTICATED
        assert "过期" in exc.value.message

    def test_wrong_signature_is_rejected(self) -> None:
        """用错误密钥签发的令牌必须被拒绝。"""
        forged = jwt.encode(
            {
                "sub": "u-001",
                "role": "PLATFORM_ADMIN",
                "type": "access",
                "iss": settings.JWT_ISSUER,
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            },
            "a-completely-different-secret-key-32bytes",
            algorithm=settings.JWT_ALGORITHM,
        )
        with pytest.raises(AppException) as exc:
            decode_access_token(forged)
        assert exc.value.code is ErrorCode.UNAUTHENTICATED

    def test_refresh_token_type_is_rejected_as_access(self) -> None:
        """类型不符的令牌（refresh）不得当作访问令牌使用。"""
        wrong_type = jwt.encode(
            {
                "sub": "u-001",
                "role": "MERCHANT_ADMIN",
                "type": "refresh",
                "iss": settings.JWT_ISSUER,
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            },
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )
        with pytest.raises(AppException) as exc:
            decode_access_token(wrong_type)
        assert "类型" in exc.value.message

    def test_tampered_payload_is_rejected(self) -> None:
        """篡改载荷（提权）必须导致签名校验失败。"""
        token, _ = self._issue(role="MERCHANT_OPERATOR")
        header, _, signature = token.split(".")
        forged_payload = jwt.utils.base64url_encode(
            b'{"sub":"u-001","role":"PLATFORM_ADMIN","type":"access"}'
        ).decode()
        with pytest.raises(AppException):
            decode_access_token(f"{header}.{forged_payload}.{signature}")


# ---------------------------------------------------------------------------
# 刷新令牌
# ---------------------------------------------------------------------------


class TestRefreshToken:
    """刷新令牌生成与摘要。"""

    def test_generates_distinct_tokens(self) -> None:
        raw1, _, hash1 = issue_refresh_token()
        raw2, _, hash2 = issue_refresh_token()
        assert raw1 != raw2
        assert hash1 != hash2

    def test_hash_is_sha256_hex_and_not_plaintext(self) -> None:
        raw, _, digest = issue_refresh_token()
        assert len(digest) == 64
        assert digest != raw
        assert hash_token(raw) == digest

    def test_expiry_is_in_the_future(self) -> None:
        _, expires_at, _ = issue_refresh_token()
        assert expires_at > datetime.now(UTC)


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------


class TestMaskSecret:
    """密钥脱敏展示。"""

    def test_keeps_prefix_and_masks_rest(self) -> None:
        assert mask_secret("JX_AK_8fa3b21c") == "JX_A****"

    def test_handles_empty_and_none(self) -> None:
        assert mask_secret(None) == ""
        assert mask_secret("") == ""

    def test_short_value_fully_masked(self) -> None:
        """长度不足时全部打码，避免泄漏短密钥。"""
        assert mask_secret("abc", keep=4) == "***"

    def test_never_reveals_full_secret(self) -> None:
        secret = "SuperSecretAccessKey123456"
        masked = mask_secret(secret)
        assert secret not in masked
        assert len(masked) < len(secret)
