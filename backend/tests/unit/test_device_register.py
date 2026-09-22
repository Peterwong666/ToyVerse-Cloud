"""设备注册服务的单元测试（纯函数，无 IO）。

测试火山 DynamicRegister 的签名构造、请求体构建，
以及 AES-CBC 解密逻辑。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from app.services.device_register_service import (
    _aes_cbc_decrypt,
    _base64_decode,
    _base64_encode,
    _build_register_body,
    _build_register_signature,
    _hmac_sha256,
)

pytestmark = pytest.mark.unit


class TestHmacSha256:
    """HMAC-SHA256 签名。"""

    def test_returns_32_bytes(self) -> None:
        result = _hmac_sha256(b"secret", "hello")
        assert len(result) == 32

    def test_deterministic(self) -> None:
        r1 = _hmac_sha256(b"key", "data")
        r2 = _hmac_sha256(b"key", "data")
        assert r1 == r2

    def test_different_key_produces_different_result(self) -> None:
        r1 = _hmac_sha256(b"key1", "data")
        r2 = _hmac_sha256(b"key2", "data")
        assert r1 != r2

    def test_matches_manual_computation(self) -> None:
        key = b"test_secret"
        content = "test_content"
        expected = hmac.new(key, content.encode("utf-8"), hashlib.sha256).digest()
        result = _hmac_sha256(key, content)
        assert result == expected


class TestBase64:
    """Base64 编解码。"""

    def test_roundtrip(self) -> None:
        data = b"hello world"
        encoded = _base64_encode(data)
        decoded = _base64_decode(encoded)
        assert decoded == data

    def test_empty(self) -> None:
        assert _base64_encode(b"") == ""
        assert _base64_decode("") == b""


class TestBuildRegisterSignature:
    """构造 DynamicRegister 的签名。"""

    def test_signature_format(self) -> None:
        sig = _build_register_signature(
            product_secret="my_secret_key_12345",
            device_name="device-001",
            random_num="12345678",
            product_key="pk_abc",
            timestamp=1700000000000,
        )
        # 签名应该是 base64 编码的字符串
        assert isinstance(sig, str)
        assert len(sig) > 0
        # 验证是合法的 base64
        decoded = _base64_decode(sig)
        assert len(decoded) == 32  # SHA-256 digest

    def test_signature_matches_manual(self) -> None:
        """签名必须与手动计算一致。"""
        secret = "product_secret_123"
        device = "dev-001"
        rand = "99999"
        pk = "pk_xyz"
        ts = 1700000000000

        content = (
            f"auth_type=1&device_name={device}&random_num={rand}"
            f"&product_key={pk}&timestamp={ts}"
        )
        expected = _base64_encode(
            hmac.new(secret.encode("utf-8"), content.encode("utf-8"), hashlib.sha256).digest()
        )

        result = _build_register_signature(
            product_secret=secret,
            device_name=device,
            random_num=rand,
            product_key=pk,
            timestamp=ts,
        )
        assert result == expected

    def test_different_inputs_produce_different_signatures(self) -> None:
        sig1 = _build_register_signature("s", "d1", "r", "p", 1)
        sig2 = _build_register_signature("s", "d2", "r", "p", 1)
        assert sig1 != sig2


class TestBuildRegisterBody:
    """构造 DynamicRegister 请求体。"""

    def test_required_fields(self) -> None:
        body = _build_register_body(
            instance_id="inst-123",
            product_key="pk-abc",
            device_name="dev-001",
            product_secret="secret-xyz",
        )
        assert body["InstanceID"] == "inst-123"
        assert body["product_key"] == "pk-abc"
        assert body["device_name"] == "dev-001"
        assert body["auth_type"] == 1
        assert "random_num" in body
        assert "timestamp" in body
        assert "signature" in body

    def test_timestamp_is_milliseconds(self) -> None:
        before = int(time.time() * 1000)
        body = _build_register_body("i", "p", "d", "s")
        after = int(time.time() * 1000)
        assert before <= body["timestamp"] <= after

    def test_random_num_is_int(self) -> None:
        body = _build_register_body("i", "p", "d", "s")
        assert isinstance(body["random_num"], int)
        assert 0 <= body["random_num"] < 100_000_000


class TestAesCbcDecrypt:
    """AES-CBC 解密（PKCS7 填充）。"""

    def test_known_vector(self) -> None:
        """使用已知的 AES-CBC 测试向量验证解密。"""
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.primitives import padding as sym_padding

            # AES-128-CBC with PKCS7 padding
            key = b"0123456789abcdef"
            plaintext = b"hello world!!!!!"

            # Encrypt
            padder = sym_padding.PKCS7(128).padder()
            padded = padder.update(plaintext) + padder.finalize()
            cipher = Cipher(algorithms.AES(key), modes.CBC(key))
            encryptor = cipher.encryptor()
            ciphertext = encryptor.update(padded) + encryptor.finalize()

            # Decrypt with our function
            result = _aes_cbc_decrypt(key, ciphertext)
            assert result == plaintext
        except ImportError:
            pytest.skip("cryptography 库未安装，跳过 AES 解密测试")

    def test_empty_ciphertext_returns_empty(self) -> None:
        """空密文应直接返回空字节（不做解密）。"""
        result = _aes_cbc_decrypt(b"0123456789abcdef", b"")
        assert result == b""
