"""设备注册服务：后端代注册（Route B）。

为什么需要这个服务
------------------
火山引擎「硬件对话智能体」的标准协议要求设备持有 ``device_secret`` 才能
连接 WebSocket。获取 ``device_secret`` 的方式是调用 ``DynamicRegister`` 接口，
但该接口需要 ``product_secret``——**官方明确告诫「切勿泄露或直接硬编码在客户端」**。

因此采用 **Route B（后端代注册）**：
1. 设备用平台签发的 ``DEVICE_SECRET`` 向本后端换取 ``device_secret``；
2. 后端用 ``product_secret`` 调用火山 ``DynamicRegister``；
3. 解密响应得到 ``device_secret``，加密存储后下发给设备。

签名算法（来自 docs/16 §1.2）
-------------------------------
```
content = "auth_type={auth_type}&device_name={device_name}&random_num={random_num}"
          "&product_key={product_key}&timestamp={timestamp}"
signature = base64( HMAC-SHA256(key=product_secret, content) )
```

AES-CBC 解密（来自 docs/16 §1.2）
------------------------------------
- 算法：AES-CBC，PKCS7 填充
- 密钥：``product_secret`` 的前 16 字节
- IV：同上（IV == key）
"""

from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import json
import os
import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import AppException, ErrorCode
from app.core.logging import get_logger

logger = get_logger(__name__)

# 火山 IoT DynamicRegister 接口
DYNAMIC_REGISTER_URL = (
    "https://iot-cn-shanghai.iot.volces.com/2021-12-14/DynamicRegister"
)


def _hmac_sha256(key: bytes, content: str) -> bytes:
    """HMAC-SHA256 签名。"""
    return hmac_mod.new(key, content.encode("utf-8"), hashlib.sha256).digest()


def _base64_encode(data: bytes) -> str:
    return base64.b64encode(data).decode("utf-8")


def _base64_decode(data: str) -> bytes:
    return base64.b64decode(data)


def _aes_cbc_decrypt(key_16: bytes, ciphertext: bytes) -> bytes:
    """AES-CBC 解密（PKCS7 填充），key 和 IV 均为 key_16。"""
    if not ciphertext:
        return b""

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding as sym_padding

        cipher = Cipher(algorithms.AES(key_16), modes.CBC(key_16))
        decryptor = cipher.decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()

        # Remove PKCS7 padding
        unpadder = sym_padding.PKCS7(128).unpadder()
        return unpadder.update(plaintext) + unpadder.finalize()
    except ImportError:
        # cryptography 库未安装时的简化实现（仅用于开发/测试）
        logger.warning("cryptography 库未安装，使用简化 AES 解密（不安全，仅开发用）")
        # 简化：直接返回 base64 解码后的原始数据（不做真正的 AES 解密）
        # 生产环境必须安装 cryptography
        return ciphertext


def _build_register_signature(
    product_secret: str,
    device_name: str,
    random_num: str,
    product_key: str,
    timestamp: int,
) -> str:
    """构造 DynamicRegister 的 HMAC-SHA256 签名。"""
    content = (
        f"auth_type=1&device_name={device_name}&random_num={random_num}"
        f"&product_key={product_key}&timestamp={timestamp}"
    )
    digest = _hmac_sha256(product_secret.encode("utf-8"), content)
    return _base64_encode(digest)


def _build_register_body(
    instance_id: str,
    product_key: str,
    device_name: str,
    product_secret: str,
) -> dict[str, Any]:
    """构造 DynamicRegister 请求体。"""
    random_num = str(int.from_bytes(os.urandom(4), "big") % 100000000)
    timestamp = int(time.time() * 1000)  # 毫秒

    signature = _build_register_signature(
        product_secret=product_secret,
        device_name=device_name,
        random_num=random_num,
        product_key=product_key,
        timestamp=timestamp,
    )

    return {
        "InstanceID": instance_id,
        "product_key": product_key,
        "device_name": device_name,
        "random_num": random_num,
        "timestamp": timestamp,
        "auth_type": 1,
        "signature": signature,
    }


async def register_device_on_volcano(
    *,
    device_name: str,
    instance_id: str | None = None,
    product_key: str | None = None,
    product_secret: str | None = None,
) -> str:
    """调用火山 DynamicRegister 获取 device_secret。

    Args:
        device_name: 设备名（单产品内唯一，建议用 SN）。
        instance_id: IoT 实例 ID，默认从配置读取。
        product_key: 产品标识符，默认从配置读取。
        product_secret: 产品密钥，默认从配置读取。

    Returns:
        解密后的 device_secret 字符串。

    Raises:
        AppException: 注册失败（签名错误、设备名不合规、License 过期等）。
    """
    iid = (instance_id or settings.VOLCANO_IOT_INSTANCE_ID).strip()
    pkey = (product_key or settings.VOLCANO_IOT_PRODUCT_KEY).strip()
    psecret = (product_secret or settings.VOLCANO_IOT_PRODUCT_SECRET).strip()

    if not iid or not pkey or not psecret:
        raise AppException(
            code=ErrorCode.VENDOR_UNAVAILABLE,
            message="火山 IoT 凭证未配置（VOLCANO_IOT_INSTANCE_ID / PRODUCT_KEY / PRODUCT_SECRET）",
            status_code=503,
        )

    body = _build_register_body(
        instance_id=iid,
        product_key=pkey,
        device_name=device_name,
        product_secret=psecret,
    )

    logger.info("调用火山 DynamicRegister: device_name=%s", device_name)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(DYNAMIC_REGISTER_URL, json=body)

    if resp.status_code != 200:
        logger.error("DynamicRegister HTTP %d: %s", resp.status_code, resp.text[:500])
        raise AppException(
            code=ErrorCode.VENDOR_UNAVAILABLE,
            message=f"火山 DynamicRegister 请求失败（HTTP {resp.status_code}）",
            status_code=502,
        )

    data = resp.json()
    result = data.get("Result", {})
    code = result.get("Code", data.get("Code"))

    if code and str(code) != "0":
        error_msg = result.get("Message", data.get("Message", "未知错误"))
        logger.error("DynamicRegister 失败: code=%s, msg=%s", code, error_msg)
        raise AppException(
            code=ErrorCode.VENDOR_UNAVAILABLE,
            message=f"火山 DynamicRegister 失败: {error_msg}",
            status_code=502,
            detail={"volcano_code": code, "volcano_message": error_msg},
        )

    # 解密 device_secret
    encrypted_payload = result.get("Payload", "")
    if not encrypted_payload:
        raise AppException(
            code=ErrorCode.VENDOR_UNAVAILABLE,
            message="火山 DynamicRegister 响应缺少 Payload（device_secret）",
            status_code=502,
        )

    try:
        ciphertext = _base64_decode(encrypted_payload)
        key_16 = psecret[:16].encode("utf-8")
        decrypted = _aes_cbc_decrypt(key_16, ciphertext)
        device_secret = decrypted.decode("utf-8")
    except Exception as exc:
        logger.error("解密 device_secret 失败: %s", exc)
        raise AppException(
            code=ErrorCode.VENDOR_UNAVAILABLE,
            message="解密 device_secret 失败",
            status_code=502,
        ) from exc

    logger.info("DynamicRegister 成功: device_name=%s", device_name)
    return device_secret
