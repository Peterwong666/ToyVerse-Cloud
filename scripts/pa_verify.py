#!/usr/bin/env python3
"""P-A / P-B 最小验证：注册 + WebSocket 建连 + 音频上行 ASR。

用法：
    # P-A（仅建连）
    VOLCANO_PA_DEVICE_SECRET=xxx VOLCANO_PA_DEVICE_NAME=xxx python scripts/pa_verify.py

    # P-B（建连 + 音频上行）
    VOLCANO_PA_DEVICE_SECRET=xxx VOLCANO_PA_DEVICE_NAME=xxx python scripts/pa_verify.py --audio test.wav

依赖：
    pip install httpx websockets cryptography python-dotenv

环境变量（从 .env 或 shell 读取）：
    VOLCANO_IOT_INSTANCE_ID   — IoT 实例 ID
    VOLCANO_IOT_PRODUCT_KEY   — 产品标识符
    VOLCANO_IOT_PRODUCT_SECRET — 产品密钥（仅后端使用，不下发设备）
    VOLCANO_BOT_ID            — 智能体 ID（控制台获取）
    VOLCANO_PA_DEVICE_SECRET  — 已注册设备的 device_secret（跳过注册）
    VOLCANO_PA_DEVICE_NAME    — 已注册设备名（默认 pa-test-6333c399）
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac as hmac_mod
import json
import math
import os
import secrets
import struct
import sys
import time
from pathlib import Path

# ── 加载 .env ──────────────────────────────────────────────
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

try:
    from dotenv import load_dotenv
    load_dotenv(_ENV_FILE)
except ImportError:
    pass

# ── 配置 ────────────────────────────────────────────────────
INSTANCE_ID = os.getenv("VOLCANO_IOT_INSTANCE_ID", "")
PRODUCT_KEY = os.getenv("VOLCANO_IOT_PRODUCT_KEY", "")
PRODUCT_SECRET = os.getenv("VOLCANO_IOT_PRODUCT_SECRET", "")
BOT_ID = os.getenv("VOLCANO_BOT_ID", "")

# 注册接口
REGISTER_URL = "https://iot-cn-shanghai.iot.volces.com/2021-12-14/DynamicRegister"

# WebSocket 建连
WS_URL = "wss://ai-gateway.vei.volces.com/v1/realtime"

# 音频参数（来自火山技术支持确认）
AUDIO_SAMPLE_RATE = 16000  # 上行 16kHz
AUDIO_CHANNELS = 1
AUDIO_BITS = 16


# ── 签名与加密工具 ──────────────────────────────────────────

def _hmac_sha256(key: bytes, content: str) -> bytes:
    return hmac_mod.new(key, content.encode("utf-8"), hashlib.sha256).digest()


def _aes_cbc_decrypt(key_16: bytes, ciphertext: bytes) -> bytes:
    from cryptography.hazmat.primitives import padding as sym_padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    cipher = Cipher(algorithms.AES(key_16), modes.CBC(key_16))
    decryptor = cipher.decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    return unpadder.update(plaintext) + unpadder.finalize()


# ── 步骤 1：注册设备 ────────────────────────────────────────

def register_device(device_name: str) -> str | None:
    """调用 DynamicRegister，返回解密后的 device_secret。失败返回 None。"""
    import httpx

    random_num = secrets.randbelow(10**8)
    timestamp_ms = int(time.time() * 1000)

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

    data = resp.json()
    result = data.get("Result", {}) or {}
    meta_error = (data.get("ResponseMetadata") or {}).get("Error") or {}
    if meta_error.get("CodeN"):
        print(f"[注册] 失败: code={meta_error.get('CodeN')}, msg={meta_error.get('Message')}")
        return None

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
    content = (
        f"auth_type=1&device_name={device_name}&random_num={random_num}"
        f"&product_key={product_key}&timestamp={timestamp_sec}"
        f"&instance_id={instance_id}"
    )
    return base64.b64encode(
        _hmac_sha256(device_secret.encode("utf-8"), content)
    ).decode("utf-8")


def load_audio(path: str) -> bytes:
    """加载音频文件，返回原始 PCM16 数据。

    支持 WAV（16kHz/16bit/mono）和其他格式（需 numpy）。
    如果是其他格式，尝试用 numpy 转换。
    """
    p = Path(path)
    if not p.exists():
        print(f"[音频] 文件不存在: {path}")
        sys.exit(1)

    if p.suffix.lower() == ".wav":
        import wave
        with wave.open(str(p), "rb") as wf:
            assert wf.getnchannels() == 1, f"需要 mono，实际 {wf.getnchannels()} 声道"
            assert wf.getsampwidth() == 2, f"需要 16bit，实际 {wf.getsampwidth() * 8}bit"
            assert wf.getframerate() == AUDIO_SAMPLE_RATE, (
                f"需要 {AUDIO_SAMPLE_RATE}Hz，实际 {wf.getframerate()}Hz"
            )
            pcm_data = wf.readframes(wf.getnframes())
        print(f"[音频] WAV: {len(pcm_data)} bytes, "
              f"{len(pcm_data) / (AUDIO_SAMPLE_RATE * 2):.2f}s")
        return pcm_data
    else:
        # 尝试用 numpy/scipy 读取其他格式
        try:
            import numpy as np
            from scipy.io import wavfile
            sr, data = wavfile.read(str(p))
            if sr != AUDIO_SAMPLE_RATE:
                print(f"[音频] 采样率 {sr}Hz，需要重采样到 {AUDIO_SAMPLE_RATE}Hz")
                # 简单线性重采样
                ratio = AUDIO_SAMPLE_RATE / sr
                new_len = int(len(data) * ratio)
                indices = np.linspace(0, len(data) - 1, new_len)
                data = np.interp(indices, np.arange(len(data)), data.astype(float))
                data = np.clip(data, -32768, 32767).astype(np.int16)
            if data.ndim > 1:
                data = data[:, 0]  # 取第一声道
            pcm_data = data.tobytes()
            print(f"[音频] 已转换: {len(pcm_data)} bytes, "
                  f"{len(pcm_data) / (AUDIO_SAMPLE_RATE * 2):.2f}s")
            return pcm_data
        except ImportError:
            print("[音频] 需要 numpy/scipy 来读取非 WAV 格式")
            sys.exit(1)


def generate_test_tone(duration_sec: float = 3.0, freq: float = 440.0) -> bytes:
    """生成一段正弦波测试音频（PCM16 mono）。"""
    n_samples = int(AUDIO_SAMPLE_RATE * duration_sec)
    samples = []
    for i in range(n_samples):
        t = i / AUDIO_SAMPLE_RATE
        # 440Hz 正弦波，带淡入淡出
        envelope = min(1.0, t * 10) * max(0, 1.0 - max(0, t - duration_sec + 0.1) * 10)
        value = int(16000 * math.sin(2 * math.pi * freq * t) * envelope)
        samples.append(max(-32768, min(32767, value)))
    pcm_data = struct.pack(f"<{len(samples)}h", *samples)
    print(f"[音频] 生成测试音: {duration_sec}s, {freq}Hz, {len(pcm_data)} bytes")
    return pcm_data


async def connect_websocket(
    device_secret: str,
    device_name: str,
    audio_path: str | None = None,
    text_message: str | None = None,
) -> None:
    import websockets

    random_num = str(secrets.randbelow(10**8))
    timestamp_sec = int(time.time())

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

    async with websockets.connect(
        ws_url,
        extra_headers=extra_headers,
        open_timeout=10,
        close_timeout=5,
    ) as ws:
        print("[建连] WebSocket 已连接，等待 session.created...")

        # 等待 session.created
        msg = await asyncio.wait_for(ws.recv(), timeout=10)
        data = json.loads(msg)
        print(f"[收到] {data.get('type')}")
        if data.get("type") != "session.created":
            print(f"[错误] 期望 session.created，实际: {json.dumps(data, indent=2, ensure_ascii=False)}")
            return

        session_info = data.get("session", {})
        print(f"[会话] model={session_info.get('model')}")
        print(f"[会话] input_audio_format={session_info.get('input_audio_format')}")
        print(f"[会话] output_audio_format={session_info.get('output_audio_format')}")
        print(f"[会话] turn_detection={session_info.get('turn_detection')}")

        # ── P-B：音频上行 ──
        if audio_path:
            print("\n--- P-B：音频上行 ---")
            pcm_data = load_audio(audio_path)
            await send_audio(ws, pcm_data)
        elif text_message:
            print("\n--- P-B：文本上行 ---")
            await send_text(ws, text_message)
        else:
            print("\n[提示] 未指定输入，跳过 P-B")
            print("[提示] 音频: python scripts/pa_verify.py --audio <file.wav>")
            print("[提示] 文本: python scripts/pa_verify.py --text '你好'")


async def send_text(ws, text: str) -> None:
    """通过 input_text 发送文本消息，验证双向通信。"""
    # 创建文本消息
    create_event = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }
    await ws.send(json.dumps(create_event))
    print(f"[发送] conversation.item.create: {text}")

    # 触发响应
    response_event = {"type": "response.create"}
    await ws.send(json.dumps(response_event))
    print("[发送] response.create")

    # 收集响应
    print("[等待] 等待响应...")
    full_response = ""
    try:
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=60)
            data = json.loads(msg)
            event_type = data.get("type", "unknown")

            if event_type == "response.text.delta":
                delta = data.get("delta", "")
                print(f"[文本] {delta}", end="", flush=True)
                full_response += delta

            elif event_type == "response.text.done":
                print("\n[文本] (完成)")
                full_response = data.get("text", full_response)

            elif event_type == "response.audio.delta":
                pass  # 忽略音频数据

            elif event_type == "response.audio_transcript.delta":
                td = data.get("delta", "")
                if td:
                    print(f"[转写] {td}", end="", flush=True)
                    full_response += td

            elif event_type == "response.done":
                print("\n[收到] response.done")
                # 从 response.done 提取最终文本
                resp = data.get("response", {})
                output = resp.get("output", [])
                for item in output:
                    if item.get("type") == "message":
                        for content in item.get("content", []):
                            if content.get("type") == "text":
                                full_response = content.get("text", full_response)
                break

            elif event_type == "error":
                print(f"\n❌ 错误: {json.dumps(data, indent=2, ensure_ascii=False)}")
                await ws.close(1000)
                return
            else:
                print(f"[收到] {event_type}")

        print(f"\n{'=' * 60}")
        print("✅ P-B 文本验证成功！")
        print(f"发送: 「{text}」")
        print(f"回复: 「{full_response}」")
        print("=" * 60)
        await ws.close(1000)

    except TimeoutError:
        print("\n❌ 等待超时（60秒）")
    except Exception as e:
        if "ConnectionClosed" in type(e).__name__ and full_response:
            print(f"\n{'=' * 60}")
            print("✅ P-B 文本验证成功！（连接被服务端关闭，但已收到回复）")
            print(f"发送: 「{text}」")
            print(f"回复: 「{full_response}」")
            print(f"关闭原因: {e}")
            print("=" * 60)
        else:
            print(f"\n❌ 异常: {type(e).__name__}: {e}")


async def send_audio(ws, pcm_data: bytes) -> None:
    """将 PCM16 音频分帧发送到 WebSocket。"""
    # 每帧 100ms 的音频（16000Hz * 2 bytes * 0.1s = 3200 bytes）
    frame_size = AUDIO_SAMPLE_RATE * 2 // 10  # 100ms
    total_frames = math.ceil(len(pcm_data) / frame_size)
    print(f"[发送] {len(pcm_data)} bytes, {total_frames} 帧, 每帧 {frame_size} bytes")

    for i in range(total_frames):
        chunk = pcm_data[i * frame_size : (i + 1) * frame_size]
        b64 = base64.b64encode(chunk).decode("utf-8")
        event = {
            "type": "input_audio_buffer.append",
            "audio": b64,
        }
        await ws.send(json.dumps(event))

        if i % 10 == 0:
            print(f"[发送] 帧 {i + 1}/{total_frames} ({(i + 1) * 100}ms)")

        # 控制发送节奏：100ms 音频用 ~50ms 发送
        await asyncio.sleep(0.05)

    print(f"[发送] 全部 {total_frames} 帧发送完毕")

    # 等待识别结果
    print("[等待] 等待 ASR 识别结果...")
    try:
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=30)
            data = json.loads(msg)
            event_type = data.get("type", "unknown")
            print(f"[收到] {event_type}")

            if event_type == "conversation.item.input_audio_transcription.completed":
                transcript = data.get("transcript", "")
                print(f"\n{'=' * 60}")
                print(f"✅ P-B 验证成功！ASR 识别结果: 「{transcript}」")
                print(json.dumps(data, indent=2, ensure_ascii=False))
                print("=" * 60)
                await ws.close(1000)
                return

            if event_type == "error":
                print(f"\n❌ 错误: {json.dumps(data, indent=2, ensure_ascii=False)}")
                await ws.close(1000)
                return

            if event_type in ("input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"):
                print(f"  → {event_type}")

    except Exception as e:
        print(f"\n❌ 等待超时: {e}")


# ── 主流程 ──────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="P-A / P-B 最小验证")
    parser.add_argument("--audio", help="PCM16 WAV 音频文件路径（跳过注册直接建连+发音频）")
    parser.add_argument("--device-secret", help="已有设备的 device_secret（跳过注册）")
    parser.add_argument("--device-name", default="pa-test-6333c399", help="设备名")
    parser.add_argument("--test-tone", type=float, metavar="SEC", dest="test_tone",
                        help="生成指定秒数的测试正弦波音频（440Hz）")
    parser.add_argument("--text", help="发送文本消息（跳过音频，直接验证双向通信）")
    args = parser.parse_args()

    print("=" * 60)
    print("P-A / P-B 最小验证")
    print("=" * 60)

    # 获取 device_secret
    device_secret = args.device_secret or os.getenv("VOLCANO_PA_DEVICE_SECRET", "")
    device_name = args.device_name or os.getenv("VOLCANO_PA_DEVICE_NAME", "pa-test-6333c399")

    if not device_secret:
        # 需要注册
        missing = []
        if not INSTANCE_ID:
            missing.append("VOLCANO_IOT_INSTANCE_ID")
        if not PRODUCT_KEY:
            missing.append("VOLCANO_IOT_PRODUCT_KEY")
        if not PRODUCT_SECRET:
            missing.append("VOLCANO_IOT_PRODUCT_SECRET")
        if missing:
            print(f"\n❌ 缺少环境变量: {', '.join(missing)}")
            sys.exit(1)

        print(f"\n设备名: {device_name}")
        print("\n--- 步骤 1：注册设备 ---")
        device_secret = register_device(device_name)
        if not device_secret:
            print("\n[失败] 无法获取 device_secret")
            sys.exit(1)
    else:
        print(f"\n[使用] 已有设备: {device_name}")

    print(f"[使用] device_secret = {device_secret[:8]}...")

    # 准备音频
    audio_path = args.audio
    if args.test_tone and not audio_path:
        # 生成测试音频
        tone_path = "/tmp/pa_test_tone.wav"
        pcm_data = generate_test_tone(duration_sec=args.test_tone)
        import wave
        with wave.open(tone_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(AUDIO_SAMPLE_RATE)
            wf.writeframes(pcm_data)
        audio_path = tone_path
        print(f"[音频] 测试音频已保存: {tone_path}")

    # 建连
    print("\n--- 步骤 2：WebSocket 建连 ---")
    asyncio.run(connect_websocket(device_secret, device_name, audio_path, args.text))


if __name__ == "__main__":
    main()
