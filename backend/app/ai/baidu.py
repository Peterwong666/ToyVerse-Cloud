"""百度智能云（文心 / 语音技术）适配器骨架。

定位
----
百度在这里承担与火山同类的**模型类**角色（对话 + 语音），区别在于它没有
「硬件对话智能体」那套设备绑定能力，因此 ``capabilities`` 只声明
``dialogue`` / ``asr`` / ``tts``——设备侧仍由集贤 / JoyInside 负责。

为什么保留这个「用不上」的适配器
--------------------------------
它证明了抽象层的**可替换性**：同一个 ``AIProvider`` 契约下，
平台既能接「智能体 + 设备」一体化的火山，也能接纯粹的模型 API 百度，
而对话链路的代码一行都不用改。这对作品集中的架构论证价值高于多接一家厂商本身。

鉴权流程（三段式）
------------------
百度与另两家的显著差异是**两步换 token**：

1. ``POST /oauth/2.0/token`` 用 ``api_key`` + ``secret_key`` 换 ``access_token``（有效期 30 天）
2. 业务接口带 ``?access_token=...``（或 body 里的 ``access_token``）

因此本适配器在 :meth:`BaiduProvider.ensure_access_token` 里缓存 token，
并按 :data:`TOKEN_SAFETY_WINDOW_SECONDS` 提前续期——避免「token 到期当天
整条对话链路静默失败」这类难排查的问题。

待联调时按官方文档校对
----------------------
1. 端点路径（``/rpc/2.0/ai_custom/v1/wenxinworkshop/chat/{endpoint}`` 中的 ``endpoint`` 随模型不同）
2. 语音识别是「短语音极速版」还是「实时流式」（后者需要 WebSocket）
3. TTS 的 ``per``（发音人）取值与音频编码格式
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.ai.base import (
    CAP_ASR,
    CAP_DIALOGUE,
    CAP_TTS,
    AsrResult,
    BaseHttpProvider,
    ChatChunk,
    ChatMessage,
    HttpRetryConfig,
    TtsResult,
    chunk_text,
)
from app.core.config import settings
from app.models.enums import AiProviderKind

#: 注册键
PROVIDER_CODE = "baidu"

#: 换 token 的端点（相对 ``api_base``）
PATH_TOKEN = "/oauth/2.0/token"
#: 文心一言对话（``{endpoint}`` 由模型决定，如 ``ernie-4.0-8k``）
PATH_CHAT = "/rpc/2.0/ai_custom/v1/wenxinworkshop/chat/{endpoint}"
#: 短语音识别（极速版）
PATH_ASR = "/server_api"
#: 短文本在线合成
PATH_TTS = "/text2audio"

#: token 提前续期窗口（秒）。3600 秒 = 提前 1 小时续期，
#: 远小于 30 天有效期，但足以覆盖跨时区 / 时钟漂移的边界
TOKEN_SAFETY_WINDOW_SECONDS = 3600

#: 默认发音人与音频格式
DEFAULT_VOICE = "0"
DEFAULT_AUDIO_FORMAT = "mp3"


class BaiduProvider(BaseHttpProvider):
    """百度智能云适配器（文心一言 + 语音技术）。"""

    code = PROVIDER_CODE
    name = "百度智能云（文心 / 语音技术）"
    kind = AiProviderKind.MODEL
    #: 百度不在 ``CloudVendor`` 枚举内——它不提供设备云（生成 / 激活），
    #: 只提供模型能力，因此这里用厂商名字面量而不污染云服务商枚举
    vendor = "BAIDU"
    capabilities = frozenset({CAP_DIALOGUE, CAP_ASR, CAP_TTS})
    description = "文心一言对话 + 短语音识别 + 短文本在线合成（两步换 token 鉴权）"

    def __init__(self, *, endpoint: str = "ernie-4.0-8k", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        #: 文心模型标识，拼进对话端点路径
        self.endpoint = endpoint
        #: 缓存的 access_token 与过期时间（单调时钟，避免受系统时间调整影响）
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    def missing_credentials(self) -> list[str]:
        """百度需要 ``api_base`` + ``api_key`` + ``secret_key`` 三者齐备。"""
        required = ("api_base", "api_key", "secret_key")
        return [key for key in required if not (self.credentials.get(key) or "").strip()]

    def build_headers(self) -> dict[str, str]:
        """百度业务接口默认走表单 / JSON，鉴权在查询参数或 body。"""
        return {"Accept": "application/json"}

    # ---------------- 鉴权 ----------------

    async def ensure_access_token(self) -> str:
        """确保拿到有效的 ``access_token``（带提前续期的内存缓存）。

        刻意缓存而非每次换 token：换 token 接口有调用频率限制，
        每次对话都换会在高并发下被限流，表现为「偶发不可用」——
        这类问题最难排查，所以在实现层就规避。
        """
        self.ensure_configured()
        now = time.monotonic()
        if self._access_token and now < self._token_expires_at:
            return self._access_token

        url = f"{self.api_base}{PATH_TOKEN}"
        async with httpx.AsyncClient(timeout=self.retry.timeout_seconds) as client:
            response = await client.post(
                url,
                params={
                    "grant_type": "client_credentials",
                    "client_id": self.credentials.get("api_key", ""),
                    "client_secret": self.credentials.get("secret_key", ""),
                },
            )
        if response.status_code >= 400:
            raise self._call_failed(
                f"{self.name} 换 token 失败（HTTP {response.status_code}）",
                detail={"statusCode": response.status_code},
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise self._call_failed(f"{self.name} 换 token 返回非 JSON 响应") from exc
        if not isinstance(data, dict) or not data.get("access_token"):
            raise self._call_failed(
                f"{self.name} 换 token 失败：{data.get('error_description') if isinstance(data, dict) else ''}"
            )

        expires_in = float(data.get("expires_in") or 0)
        self._access_token = str(data["access_token"])
        self._token_expires_at = now + max(0.0, expires_in - TOKEN_SAFETY_WINDOW_SECONDS)
        return self._access_token

    def invalidate_token(self) -> None:
        """丢弃缓存 token（收到 401 / 110 / 111 时可调用后重试）。"""
        self._access_token = None
        self._token_expires_at = 0.0

    # ---------------- AIProvider 接口实现 ----------------

    async def open_session(
        self,
        *,
        session_id: str,
        role_preset: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        """百度无服务端会话概念，仅预热 token 后回显会话 ID。"""
        await self.ensure_access_token()
        return session_id

    async def chat(
        self,
        *,
        session_id: str,
        message: ChatMessage,
        history: Sequence[ChatMessage] = (),
        role_preset: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> AsyncIterator[ChatChunk]:
        """流式对话：调文心一言接口后分块产出。

        **联调待办**：文心支持 ``stream=true`` 的 SSE 响应，
        届时把下面的「整段 + 本地分块」替换为 SSE 增量解析，对外接口不变。
        """
        token = await self.ensure_access_token()
        payload = await self.request_json(
            "POST",
            PATH_CHAT.format(endpoint=self.endpoint),
            params={"access_token": token},
            json_body={
                "messages": [
                    {"role": str(item.role).lower(), "content": item.content}
                    for item in [*history, message]
                ],
                "stream": False,
            },
            signed=False,
        )
        reply = str(payload.get("result") or _first_choice_content(payload) or "")
        pieces = chunk_text(reply)
        for index, delta in enumerate(pieces):
            is_final = index == len(pieces) - 1
            yield ChatChunk(
                delta=delta,
                index=index,
                is_final=is_final,
                finish_reason="stop" if is_final else None,
                message_id=str(payload.get("id") or "") or None,
            )

    async def close_session(self, *, session_id: str, reason: str | None = None) -> None:
        """百度无长连接可释放。"""
        return None

    async def asr(
        self,
        *,
        audio: bytes | None = None,
        audio_url: str | None = None,
        hint_text: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> AsrResult:
        """短语音识别。

        音频以 base64 提交（``speech`` 字段），``format`` / ``rate`` 由调用方
        通过 ``context`` 传入，缺省用 pcm / 16000（玩具设备最常见配置）。
        """
        token = await self.ensure_access_token()
        options = context or {}
        payload = await self.request_json(
            "POST",
            PATH_ASR,
            params={"access_token": token, "cuid": str(options.get("cuid") or "toyverse")},
            json_body={
                "format": str(options.get("format") or "pcm"),
                "rate": int(options.get("rate") or 16000),
                "channel": int(options.get("channel") or 1),
                "speech": base64.b64encode(audio or b"").decode("ascii"),
                "len": len(audio or b""),
            },
            signed=False,
        )
        results = payload.get("result")
        text = ""
        if isinstance(results, list) and results:
            text = str(results[0])
        return AsrResult(
            text=text,
            is_final=True,
            confidence=float(payload.get("confidence") or 0.0) or None,
            raw={"errNo": payload.get("err_no"), "note": "骨架实现，音频经 base64 提交"},
        )

    async def tts(
        self,
        *,
        text: str,
        voice_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> TtsResult:
        """短文本在线合成。

        百度返回的是**二进制音频**（非 JSON），因此这里用独立的
        ``request_bytes`` 分支而不是 ``request_json``。
        """
        token = await self.ensure_access_token()
        options = context or {}
        audio, content_type = await self._synthesize_bytes(
            token=token,
            text=text,
            voice=voice_id or DEFAULT_VOICE,
            fmt=str(options.get("format") or DEFAULT_AUDIO_FORMAT),
        )
        return TtsResult(
            text=text,
            content_type=content_type,
            audio=audio,
            voice_id=voice_id or DEFAULT_VOICE,
        )

    async def _synthesize_bytes(
        self, *, token: str, text: str, voice: str, fmt: str
    ) -> tuple[bytes, str]:
        """调用 TTS 并返回音频字节（对 JSON 错误响应做显式识别）。"""
        url = f"{self.api_base}{PATH_TTS}"
        params: dict[str, str | int] = {
            "access_token": token,
            "tex": text,
            "per": voice,
            "aue": fmt,
            "cuid": "toyverse",
            "spd": 5,
            "pit": 5,
            "vol": 5,
        }
        headers = self.build_headers()
        last_error = "未知错误"

        for attempt in range(self.retry.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.retry.timeout_seconds) as client:
                    response = await client.post(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retry.max_retries:
                    await asyncio.sleep(self.retry.backoff_seconds * (attempt + 1))
                    continue
                raise self._call_failed(f"{self.name} 语音合成失败：{last_error}") from exc

            if response.status_code in self.retry.retry_statuses and attempt < self.retry.max_retries:
                last_error = f"HTTP {response.status_code}"
                await asyncio.sleep(self.retry.backoff_seconds * (attempt + 1))
                continue

            content_type = response.headers.get("content-type", "application/octet-stream")
            if response.status_code >= 400 or "json" in content_type:
                # 百度在失败时返回 JSON（含 err_no / err_msg），必须与音频区分开，
                # 否则会把一段错误 JSON 当成音频播出去
                raise self._call_failed(
                    f"{self.name} 语音合成返回错误：{response.text[:200]}",
                    detail={"statusCode": response.status_code},
                )
            return response.content, content_type

        raise self._call_failed(f"{self.name} 语音合成失败：{last_error}")


def _first_choice_content(payload: dict[str, Any]) -> str:
    """从 OpenAI 兼容形状的响应里取第一条回复内容。"""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                return str(message.get("content") or "")
    return ""


def build_provider() -> BaiduProvider:
    """按 ``settings`` 构造百度适配器。"""
    return BaiduProvider(
        credentials=settings.ai_provider_credentials["baidu"],
        retry=HttpRetryConfig(
            timeout_seconds=float(settings.BAIDU_TIMEOUT_SECONDS),
            max_retries=2,
        ),
    )


__all__ = [
    "DEFAULT_AUDIO_FORMAT",
    "DEFAULT_VOICE",
    "PATH_ASR",
    "PATH_CHAT",
    "PATH_TOKEN",
    "PATH_TTS",
    "PROVIDER_CODE",
    "TOKEN_SAFETY_WINDOW_SECONDS",
    "BaiduProvider",
    "build_provider",
]
