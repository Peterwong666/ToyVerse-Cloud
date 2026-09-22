#!/usr/bin/env python3
"""P-A 最小验证：注册 + WebSocket 建连，判据是收到 session.created。

用法：
    cd backend && python -m scripts.pa_verify
    # 或
    python scripts/pa_verify.py

依赖：
    pip install httpx websockets cryptography python-dotenv

环境变量（从 .env 或 shell 读取）：
    VOLCANO_IOT_INSTANCE_ID   — IoT 实例 ID
    VOLCANO_IOT_PRODUCT_KEY   — 产品标识符
    VOLCANO_IOT_PRODUCT_SECRET — 产品密钥（仅后端使用，不下发设备）
    VOLCANO_BOT_ID            — 智能体 ID（控制台获取）
"""

from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import json
import os
import secrets
import sys
import time
from pathlib import Path

# ── 加载 .env ──────────────────────────────────────────────
_BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

try:
    from dotenv import load_dotenv
    load_dotenv(_ENV_FILE)
except ImportError:
    pass  # 没装 python-dotenv 就从 shell 环境读

# ── 配置 ────────────────────────────────────────────────────
INSTANCE_ID = os.getenv("VOLCANO_IOT_INSTANCE_ID", "")
PRODUCT_KEY = os.getenv("VOLCANO_IOT_PRODUCT_KEY", "")
PRODUCT_SECRET = os.getenv("VOLCANO_IOT_PRODUCT_SECRET", "")
BOT_ID = os.getenv("VOLCANO_BOT_ID", "")

# 注册接口
REGISTER_URL = "https://iot-cn-shanghai.iot.volces.com/2021-12-14/DynamicRegister"

# WebSocket 建连
WS_URL = "wss://ai-gateway.vei.volces.com/v1/realtime"

# ── 签名与加密工具 ──────────────────────────────────────────

def _hmac_sha256(key: bytes, content: str) -> bytes:
    return hmac_mod.new(key, content.encode("utf-8"), hashlib.sha256).digest()


def _aes_cbc_decrypt(key_16: bytes, ciphertext: bytes) -> bytes:
    """AES-CBC 解密（PKCS7 填充），key == IV。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_padding

    cipher = Cipher(algorithms.AES(key_16), modes.CBC(key_16))
    decryptor = cipher.decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    return unpadder.update(plaintext) + unpadder.finalize()


# ── 步骤 1：注册设备 ────────────────────────────────────────

def register_device(device_name: str) -> str | None:
    """调用 DynamicRegister，返回解密后的 device_secret。失败返回 None。"""
    import httpx

    random_num = secrets.randbelow(10**8)  # ★ int，不是 string
    timestamp_ms = int(time.time() * 1000)  # ★ 毫秒

    # 签名
    content = (
        f"auth_type=1&device_name={device_name}&random_num={random_num}"
        f"&product_key={PRODUCT_KEY}&timestamp={timestamp_ms}"
    )
    signature = base64.b64encode(
        _hmac_sha256(PRODUCT_SECRET.encode("utf-8"), content)
    ).decode("utf-8")

    body = {
        "InstanceID": INSTANCE_ID,
        "product_key": PRODUCT_KEY,
        "device_name": device_name,
        "random_num": random_num,
        "timestamp": timestamp_ms,
        "auth_type": 1,
        "signature": signature,
    }

    print(f"[注册] POST {REGISTER_URL}")
    print(f"[注册] device_name={device_name}")
    resp = httpx.post(
        REGISTER_URL,
        params={"Action": "DynamicRegister", "Version": "2021-12-14"},
        json=body,
        timeout=30,
    )
    print(f"[注册] HTTP {resp.status_code}")
    print(f"[注册] 响应: {resp.text[:500]}")

    data = resp.json()
    result = data.get("Result", {}) or {}
    meta_error = (data.get("ResponseMetadata") or {}).get("Error") or {}
    if meta_error.get("CodeN"):
        error_msg = meta_error.get("Message", "未知错误")
        print(f"[注册] 失败: code={meta_error.get('CodeN')}, msg={error_msg}")
        return None

    # 解密 device_secret（字段名小写 payload）
    payload = result.get("payload") or result.get("Payload", "")
    if not payload:
        print("[注册] 响应缺少 Payload")
        return None

    ciphertext = base64.b64decode(payload)
    key_16 = PRODUCT_SECRET[:16].encode("utf-8")
    device_secret = _aes_cbc_decrypt(key_16, ciphertext).decode("utf-8")
    print(f"[注册] device_secret = {device_secret[:8]}...（已解密）")
    return device_secret


# ── 步骤 2：WebSocket 建连 ─────────────────────────────────

def build_ws_signature(
    device_secret: str,
    device_name: str,
    random_num: str,
    product_key: str,
    timestamp_sec: int,
    instance_id: str,
) -> str:
    """构造 WebSocket 建连签名（HMAC-SHA256 + Base64）。"""
    content = (
        f"auth_type=1&device_name={device_name}&random_num={random_num}"
        f"&product_key={product_key}&timestamp={timestamp_sec}"
        f"&instance_id={instance_id}"
    )
    return base64.b64encode(
        _hmac_sha256(device_secret.encode("utf-8"), content)
    ).decode("utf-8")


async def connect_websocket(device_secret: str, device_name: str) -> None:
    """连接 WebSocket 并等待 session.created。"""
    import websockets

    random_num = str(secrets.randbelow(10**8))
    timestamp_sec = int(time.time())  # ★ 秒（与注册的毫秒不同）

    signature = build_ws_signature(
        device_secret=device_secret,
        device_name=device_name,
        random_num=random_num,
        product_key=PRODUCT_KEY,
        timestamp_sec=timestamp_sec,
        instance_id=INSTANCE_ID,
    )

    ws_url = f"{WS_URL}?bot={BOT_ID}"
    extra_headers = {
        "X-Auth-Type": "1",
        "X-Product-Key": PRODUCT_KEY,
        "X-Device-Name": device_name,
        "X-Random-Num": random_num,
        "X-Timestamp": str(timestamp_sec),
        "X-Instance-Id": INSTANCE_ID,
        "X-Signature": signature,
        "X-Hardware-Id": f"pa-test-{device_name}",
    }

    print(f"\n[建连] WS {ws_url}")
    print(f"[建连] X-Timestamp={timestamp_sec} (秒)")
    print(f"[建连] X-Signature={signature[:20]}...")

    async with websockets.connect(
        ws_url,
        extra_headers=extra_headers,
        open_timeout=10,
        close_timeout=5,
    ) as ws:
        print("[建连] WebSocket 已连接，等待消息...")

        # 等待最多 10 秒，收集所有收到的消息
        import asyncio
        messages = []
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=10)
                messages.append(msg)
                data = json.loads(msg)
                event_type = data.get("type", "unknown")
                print(f"[收到] {event_type}")

                if event_type == "session.created":
                    print("\n" + "=" * 60)
                    print("✅ P-A 验证成功！收到 session.created")
                    print(json.dumps(data, indent=2, ensure_ascii=False))
                    print("=" * 60)

                    # 发送关闭帧
                    await ws.close(1000)
                    return

                if event_type == "error":
                    print(f"\n❌ 错误事件: {json.dumps(data, indent=2, ensure_ascii=False)}")
                    await ws.close(1000)
                    return

        except Exception as e:
            print(f"\n❌ 等待超时或出错: {e}")
            if messages:
                print(f"   已收到 {len(messages)} 条消息:")
                for m in messages:
                    print(f"   - {m[:200]}")
            return

        print("\n❌ 未收到 session.created")
        print(f"   已收到 {len(messages)} 条消息")
        for m in messages:
            print(f"   - {m[:200]}")


# ── 主流程 ──────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("P-A 最小验证：注册 + WebSocket 建连")
    print("=" * 60)

    # 检查配置
    missing = []
    if not INSTANCE_ID:
        missing.append("VOLCANO_IOT_INSTANCE_ID")
    if not PRODUCT_KEY:
        missing.append("VOLCANO_IOT_PRODUCT_KEY")
    if not PRODUCT_SECRET:
        missing.append("VOLCANO_IOT_PRODUCT_SECRET")
    if not BOT_ID:
        missing.append("VOLCANO_BOT_ID")

    if missing:
        print(f"\n❌ 缺少环境变量: {', '.join(missing)}")
        print("请在 .env 或 shell 中设置后重试")
        sys.exit(1)

    # 用固定设备名（可重复注册，火山会覆盖旧的）
    device_name = os.getenv("VOLCANO_PA_DEVICE_NAME", "pa-test-6333c399")
    device_secret = os.getenv("VOLCANO_PA_DEVICE_SECRET", "")
    print(f"\n设备名: {device_name}")

    # 步骤 1：注册（若无已有 device_secret）
    if not device_secret:
        print("\n--- 步骤 1：注册设备 ---")
        device_secret = register_device(device_name)

    if not device_secret:
        print("\n[失败] 无法获取 device_secret")
        sys.exit(1)
    print(f"[使用] device_secret = {device_secret[:8]}...")

    # 步骤 2：建连
    print("\n--- 步骤 2：WebSocket 建连 ---")
    import asyncio
    asyncio.run(connect_websocket(device_secret, device_name))


if __name__ == "__main__":
    main()
