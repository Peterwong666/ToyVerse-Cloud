"""火山引擎智能云 · **硬件对话智能体**适配器。

为什么这个适配器最特殊
----------------------
用户明确选用火山引擎，且硬件是 **ESP32-S3**。火山为这类场景提供的不是
「一个大模型 API」，而是一整套**智能体 + 设备 + 声纹**的服务端 OpenAPI，
覆盖智能体生命周期、设备绑定与自定义音色训练。因此本适配器同时声明
``device`` 与 ``dialogue`` / ``asr`` / ``tts`` 能力——它是唯一一个能把
「智能体」直接绑到设备上的厂商。

已核实的调用形态（来自官方文档正文，已落到代码常量）
----------------------------------------------------

* 请求方式：**POST**
* 请求地址：``https://rtc.volcengineapi.com?Action=<接口名>&Version=2025-08-01``
  → 基址是 :data:`DEFAULT_API_BASE`（``https://rtc.volcengineapi.com``），
  **所有 Action 共用同一个地址**，靠 Query 的 ``Action`` + ``Version`` 区分；
  因此本模块不再有「每个动作一个路径」的概念，只有 :data:`API_PATH`。
* Query 固定参数：``Action``（见下方清单）+ ``Version=`` :data:`API_VERSION`

Action 清单（已核实名称）
-------------------------

======================  ==========================================  ==========================
分组                    Action                                      本适配器封装
======================  ==========================================  ==========================
智能体生命周期          ``AibotCreate``                             :meth:`~VolcanoProvider.aibot_create`
                        ``AibotQuery``                              :meth:`~VolcanoProvider.aibot_query`
                        ``AibotUpdate``                             :meth:`~VolcanoProvider.aibot_update`
                        ``AibotDelete``                             :meth:`~VolcanoProvider.aibot_delete`
绑定                    ``AibotBind``                               :meth:`~VolcanoProvider.aibot_bind`
                        ``AibotDescribeBinding``                    :meth:`~VolcanoProvider.aibot_describe_binding`
                        ``AibotUnbind``                             :meth:`~VolcanoProvider.aibot_unbind`
声纹                    ``IotVoicePrintRegister``                   :meth:`~VolcanoProvider.voice_print_register`
                        ``IotVoicePrintQuery``                      :meth:`~VolcanoProvider.voice_print_query`
                        ``IotVoicePrintUpdate``                     :meth:`~VolcanoProvider.voice_print_update`
                        ``IotVoicePrintDelete``                     :meth:`~VolcanoProvider.voice_print_delete`
声音复刻                ``TrainTTSVoiceType``                       :meth:`~VolcanoProvider.train_tts_voice_type`
                        ``BatchListVoiceTrainStatus``               :meth:`~VolcanoProvider.batch_list_voice_train_status`
模型列表                ``ListModels``（名称待官方最终确认）        :meth:`~VolcanoProvider.list_models`
======================  ==========================================  ==========================

``AibotCreate`` 请求体（已核实结构）
-----------------------------------

.. code-block:: json

    {
      "Name": "小狐狸助手",
      "AccessType": "private",
      "Config": {
        "ASRConfig": {
          "Provider": "volcano",
          "ProviderParams": {"ApiResourceId": "volc.bigasr.sauc.duration"}
        },
        "TTSConfig": {
          "Provider": "volcano",
          "ProviderParams": {"ApiResourceId": "volc.service_type.10029"}
        },
        "LLMConfig": {
          "ModelName": "<从模型列表接口选择>",
          "UserPrompts": [{"Role": "system", "Content": "你是温柔的睡前故事姐姐"}]
        }
      }
    }

已逐字核实：

* ``Name``（String，必选）、``AccessType``（String，必选）、``Config``（Object，必选）
* ``AccessType``：``public`` = 平台智能体（可关联到产品、控制台可管理）；
  ``private`` = 设备智能体（**只能绑设备**、控制台仅可查看不可编辑）
* ``Config.ASRConfig.Provider``：**固定取值 ``volcano``**
* ``Config.ASRConfig.ProviderParams.ApiResourceId``：流式语音识别服务版本，
  官方示例值 ``volc.bigasr.sauc.duration``
* TTS 模型取值示例：``seed-tts-1.0`` 或 ``volc.service_type.10029``（语音合成大模型 1.0 字符版）
* LLM 侧有 ``ModelName`` 与 ``UserPrompts``（数组，元素含 ``Role``），
  后者正是承载「角色预设 System Prompt」的位置

业务语义（P9 运营配置会直接依赖）
---------------------------------

控制台**只能创建平台智能体**（``public``）。因此「每个商户 / 每台设备专属人设」
**不可能靠人工在控制台完成**，必须由后端调
``AibotCreate(AccessType=private)`` + ``AibotBind`` 实现——
这就是本适配器把 ``access_type`` 默认值设为 ``private`` 并把它做成显式参数的原因：
它是「多租户商品化人设」的唯一实现入口。

实时对话的传输差异
------------------
硬件对话智能体的实时语音走 **RTC / WebSocket 长连接**（端到端语音帧），
而非 HTTP 流式。本适配器先用「HTTP 取整段 + 本地分块」把对外接口形态固定下来，
联调时只需把 :meth:`VolcanoProvider.chat` 内部替换为长连接帧解析，
对外仍是 ``AsyncIterator[ChatChunk]``——这正是抽象层的价值所在。

待校对清单（联调时必须逐项替换，勿假装已实现）
---------------------------------------------

1. **签名算法（占位）**：见 :meth:`VolcanoProvider.build_headers` 与
   :meth:`~app.ai.base.BaseHttpProvider.sign` 的注释。真实算法见官方
   《服务端 API › 调用方法》，当前用 ``HMAC-SHA256(规范化参数, secret_key)``
   的占位实现，并把结果放在 ``signature`` Query 参数上——**该位置同样是占位**。
2. **``AibotCreate`` 之外各 Action 的 Body 字段名**：官方 Body 使用 PascalCase
   （已由 ``Name`` / ``AccessType`` / ``Config`` 证实），本模块按同一惯例统一命名，
   但**未逐字核对**（例如 ``AibotId`` 还是 ``AgentId``）。
3. **``TTSConfig`` / ``LLMConfig`` 的层级**：只有 ``ASRConfig`` 逐字来自官方示例，
   TTS / LLM 按同构惯例推断。
4. **``ListModels`` 的 Action 名称**：官方文档以「模型列表和 Cluster ID」描述，
   名称以官方为准。
5. **``AibotChat`` / ``RecognizeFlash`` / ``SynthesizeSpeech``**：实时对话与
   独立语音识别 / 合成并不在本次取证范围内，Action 名称待校对。
6. **TTS 的 ``voice_type``**：仍为占位默认值，实际取值需与所选 TTS 模型配套。

**ADR-07 行为不受影响**：未配置密钥时 :meth:`health_check` 返回 ``DOWN``，
所有能力调用经 :meth:`~app.ai.base.AIProvider.ensure_configured` 抛
``VENDOR_UNAVAILABLE`` 并写审计，绝不伪造成功。
"""

from __future__ import annotations

import datetime
import hashlib
import hmac as hmac_mod
from collections.abc import AsyncIterator, Sequence
from typing import Any
from urllib.parse import urlencode

import httpx

from app.ai.base import (
    CAP_ASR,
    CAP_DEVICE,
    CAP_DIALOGUE,
    CAP_TTS,
    AsrResult,
    BaseHttpProvider,
    ChatChunk,
    ChatMessage,
    DeviceActionResult,
    DeviceProvisionResult,
    HttpRetryConfig,
    TtsResult,
    chunk_text,
)
from app.core.config import settings
from app.models.enums import AiProviderKind, CloudVendor, VoiceTrainStatus

# ===========================================================================
# 一、调用形态常量（已核实）
# ===========================================================================

#: 注册键
PROVIDER_CODE = "volcano"

#: 服务端 OpenAPI 基址（**所有 Action 共用**）
DEFAULT_API_BASE = "https://rtc.volcengineapi.com"

#: 所有 Action 共用的请求路径（真实区分靠 Query 的 ``Action``）
API_PATH = "/"

#: API 版本（Query 参数 ``Version``）
API_VERSION = "2025-08-01"

# ---- Action 常量（已核实名称）----
ACTION_AIBOT_CREATE = "AibotCreate"
ACTION_AIBOT_QUERY = "AibotQuery"
ACTION_AIBOT_UPDATE = "AibotUpdate"
ACTION_AIBOT_DELETE = "AibotDelete"
ACTION_AIBOT_BIND = "AibotBind"
ACTION_AIBOT_DESCRIBE_BINDING = "AibotDescribeBinding"
ACTION_AIBOT_UNBIND = "AibotUnbind"

ACTION_VOICE_PRINT_REGISTER = "IotVoicePrintRegister"
ACTION_VOICE_PRINT_QUERY = "IotVoicePrintQuery"
ACTION_VOICE_PRINT_UPDATE = "IotVoicePrintUpdate"
ACTION_VOICE_PRINT_DELETE = "IotVoicePrintDelete"

ACTION_TRAIN_TTS_VOICE = "TrainTTSVoiceType"
ACTION_BATCH_VOICE_TRAIN_STATUS = "BatchListVoiceTrainStatus"

#: 待校对：官方以「模型列表和 Cluster ID」描述，Action 名称以官方为准
ACTION_LIST_MODELS = "ListModels"

#: 待校对：实时对话与独立语音识别 / 合成不在本次取证范围
ACTION_AIBOT_CHAT = "AibotChat"
ACTION_ASR_RECOGNIZE = "RecognizeFlash"
ACTION_TTS_SYNTHESIZE = "SynthesizeSpeech"

# ---- AibotCreate 的取值常量（已核实）----

#: ``AccessType=public``：平台智能体，可关联到产品、控制台可管理
ACCESS_TYPE_PUBLIC = "public"
#: ``AccessType=private``：设备智能体，**只能绑设备**、控制台仅可查看
ACCESS_TYPE_PRIVATE = "private"

#: ``Config.ASRConfig.Provider`` 的固定取值
ASR_PROVIDER_VOLCANO = "volcano"

#: ``Config.ASRConfig.ProviderParams.ApiResourceId`` 的官方示例值
DEFAULT_ASR_RESOURCE_ID = "volc.bigasr.sauc.duration"

#: TTS 模型取值示例（官方给出两个：``seed-tts-1.0`` 与字符版 ``volc.service_type.10029``）
DEFAULT_TTS_MODEL = "volc.service_type.10029"
TTS_MODEL_SEED = "seed-tts-1.0"

#: 待校对：TTS 合成动作的 cluster 与音色取值需与所选模型配套
DEFAULT_TTS_CLUSTER = "volcano_tts"
DEFAULT_VOICE_TYPE = "BV700_streaming"

#: ``Config.LLMConfig.ModelName`` 的取值须从模型列表接口选择，留空表示暂不提交该字段
DEFAULT_LLM_MODEL_NAME = ""


class VolcanoProvider(BaseHttpProvider):
    """火山引擎硬件对话智能体适配器。"""

    code = PROVIDER_CODE
    name = "火山引擎（硬件对话智能体）"
    kind = AiProviderKind.MODEL
    vendor = str(CloudVendor.VOLCANO)
    #: 唯一同时具备设备与对话能力的厂商（智能体可绑定到设备）
    capabilities = frozenset({CAP_DEVICE, CAP_DIALOGUE, CAP_ASR, CAP_TTS})
    description = "硬件对话智能体：智能体生命周期 / 设备绑定 / 声纹 / 自定义音色 / 实时语音对话"

    def __init__(
        self,
        *,
        access_type: str = ACCESS_TYPE_PRIVATE,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        #: 默认创建**设备智能体**：多租户「一设备一人设」必须走后端 private，
        #: 因为控制台只能创建平台智能体（见模块 docstring 的业务语义小节）
        self.access_type = access_type
        self.asr_resource_id = (
            self.credentials.get("asr_resource_id") or ""
        ).strip() or DEFAULT_ASR_RESOURCE_ID
        self.tts_model = (self.credentials.get("tts_model") or "").strip() or DEFAULT_TTS_MODEL
        self.cluster_id = (self.credentials.get("cluster_id") or "").strip() or DEFAULT_TTS_CLUSTER
        self.voice_type = (self.credentials.get("voice_type") or "").strip() or DEFAULT_VOICE_TYPE
        self.llm_model_name = (
            self.credentials.get("llm_model_name") or ""
        ).strip() or DEFAULT_LLM_MODEL_NAME

    # ---------------- 基址与凭据 ----------------

    @property
    def api_base(self) -> str:
        """基址：优先用平台配置，未配置时回落到已核实的官方基址。

        之所以敢回落：``https://rtc.volcengineapi.com`` 是官方文档给出的**固定**
        地址，不是环境相关的可变值；把「未配置基址」也算作「未接入」会让
        ADR-07 的安全失败语义失真（缺的其实是密钥，不是地址）。
        """
        configured = (self.credentials.get("api_base") or "").strip().rstrip("/")
        return configured or DEFAULT_API_BASE

    def missing_credentials(self) -> list[str]:
        """火山需要 ``access_key`` + ``secret_key``（AK/SK 签名）。"""
        required = ("access_key", "secret_key")
        return [key for key in required if not (self.credentials.get(key) or "").strip()]

    # ---------------- AK/SK HMAC-SHA256 签名（已实现） ----------------

    @staticmethod
    def _hash_sha256(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _hmac_sha256(key: bytes, content: str) -> bytes:
        return hmac_mod.new(key, content.encode("utf-8"), hashlib.sha256).digest()

    @classmethod
    def _sign_request(
        cls,
        *,
        ak: str,
        sk: str,
        method: str,
        host: str,
        uri: str,
        query_string: str,
        body: str,
    ) -> tuple[str, str]:
        """火山引擎 AK/SK HMAC-SHA256 签名（来自官方 demo RtcApiRequester.py）。

        Returns:
            (x_date, authorization_header) 二元组。
        """
        now = datetime.datetime.utcnow()
        x_date = now.strftime("%Y%m%dT%H%M%SZ")
        x_content_sha256 = cls._hash_sha256(body)
        content_type = "application/json"

        signed_headers_vec = (
            ("content-type", content_type),
            ("host", host),
            ("x-content-sha256", x_content_sha256),
            ("x-date", x_date),
        )
        canonical_headers = "\n".join(":".join(x) for x in signed_headers_vec) + "\n"
        signed_headers = ";".join(x[0] for x in signed_headers_vec)

        # 步骤 1：规范请求
        canonical_request = "\n".join([
            method, uri, query_string, canonical_headers, signed_headers, x_content_sha256,
        ])

        # 步骤 2：待签字符串
        credential_scope = f"{x_date[:8]}/cn-north-1/rtc/request"
        string_to_sign = "\n".join([
            "HMAC-SHA256", x_date, credential_scope, cls._hash_sha256(canonical_request),
        ])

        # 步骤 3：HMAC 链签名
        hmac_parts = [*credential_scope.split("/"), string_to_sign]
        signature = sk.encode("utf-8")
        for part in hmac_parts:
            signature = cls._hmac_sha256(signature, part)
        signature_hex = signature.hex()

        # 步骤 4：Authorization header
        authorization = (
            f"HMAC-SHA256 Credential={ak}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature_hex}"
        )
        return x_date, authorization

    def _build_action_headers(self, action: str, body: dict[str, Any]) -> dict[str, str]:
        """为一次 Action 调用构造完整请求头（含 HMAC 签名）。"""
        ak = (self.credentials.get("access_key") or "").strip()
        sk = (self.credentials.get("secret_key") or "").strip()
        if not ak or not sk:
            return {"Content-Type": "application/json"}

        import json as json_mod
        body_str = json_mod.dumps(body, separators=(",", ":"), ensure_ascii=False)
        query_string = urlencode({"Action": action, "Version": API_VERSION})

        from urllib.parse import urlparse
        parsed = urlparse(self.api_base)
        host = parsed.hostname or "rtc.volcengineapi.com"

        x_date, authorization = self._sign_request(
            ak=ak, sk=sk,
            method="POST", host=host, uri=API_PATH,
            query_string=query_string, body=body_str,
        )
        return {
            "Content-Type": "application/json",
            "X-Date": x_date,
            "Authorization": authorization,
        }

    def _action_params(self, action: str) -> dict[str, Any]:
        """构造 Query 固定参数：``Action`` + ``Version``。"""
        return {"Action": action, "Version": API_VERSION}

    async def _call_action(
        self, action: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """调用一个 Action（带 AK/SK HMAC 签名）。

        所有 Action 都 POST 到**同一个地址** :data:`DEFAULT_API_BASE` +
        :data:`API_PATH`，靠 Query 的 ``Action`` / ``Version`` 区分。

        重写此方法而非依赖 ``request_json`` 的 ``build_headers()``：
        火山签名需要请求体参与计算，必须在知道 body 之后才能构造 Authorization header。
        但仍复用基类的重试与错误处理逻辑。
        """
        self.ensure_configured()
        effective_body = body or {}
        query = self._action_params(action)
        url = f"{self.api_base}{API_PATH}"
        headers = self._build_action_headers(action, effective_body)

        import asyncio
        last_error = "未知错误"

        for attempt in range(self.retry.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.retry.timeout_seconds) as client:
                    response = await client.request(
                        "POST", url, params=query, json=effective_body, headers=headers,
                    )
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retry.max_retries:
                    await asyncio.sleep(self.retry.backoff_seconds * (attempt + 1))
                    continue
                raise self._call_failed(
                    f"{self.name} 调用失败（已重试 {self.retry.max_retries} 次）：{last_error}"
                ) from exc

            if response.status_code in self.retry.retry_statuses and attempt < self.retry.max_retries:
                last_error = f"HTTP {response.status_code}"
                await asyncio.sleep(self.retry.backoff_seconds * (attempt + 1))
                continue

            if response.status_code >= 400:
                raise self._call_failed(
                    f"{self.name} 返回 HTTP {response.status_code}",
                    detail={"statusCode": response.status_code},
                )

            try:
                data = response.json()
            except ValueError as exc:
                raise self._call_failed(f"{self.name} 返回了非 JSON 响应") from exc
            if isinstance(data, dict):
                return data
            return {"data": data}

        raise self._call_failed(f"{self.name} 调用失败：{last_error}")

    # ===========================================================
    # 二、智能体生命周期（AibotCreate / Query / Update / Delete）
    # ===========================================================

    @staticmethod
    def build_create_body(
        *,
        name: str,
        access_type: str = ACCESS_TYPE_PRIVATE,
        asr_resource_id: str = DEFAULT_ASR_RESOURCE_ID,
        tts_model: str = DEFAULT_TTS_MODEL,
        llm_model_name: str = "",
        system_prompt: str | None = None,
        greeting: str | None = None,
        extra_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """构造 ``AibotCreate`` 的请求体（纯函数，便于单元测试）。

        刻意只覆盖**最小可用集**：官方字段有数百个，全量建模既无必要也无法维护。
        其余字段通过 ``extra_config`` 浅合并进 ``Config``，让联调时不必改代码。

        Args:
            name: 智能体名称（``Name``）。
            access_type: ``public`` / ``private``（默认 ``private``）。
            asr_resource_id: ``Config.ASRConfig.ProviderParams.ApiResourceId``。
            tts_model: ``Config.TTSConfig.ProviderParams.ApiResourceId``。
            llm_model_name: ``Config.LLMConfig.ModelName``；留空则不提交该字段。
            system_prompt: 角色预设的系统提示，写入 ``UserPrompts``（``Role=system``）。
            greeting: 开场白。官方文档未见独立字段，暂拼入系统提示，
                联调时确认是否有专门字段。
            extra_config: 厂商专属补充字段，浅合并进 ``Config``。

        Returns:
            可直接作为 JSON Body 的字典。
        """
        asr_config: dict[str, Any] = {
            "Provider": ASR_PROVIDER_VOLCANO,
            "ProviderParams": {"ApiResourceId": asr_resource_id},
        }
        # 待校对：TTS / LLM 的层级按 ASRConfig 的同构惯例推断
        tts_config: dict[str, Any] = {
            "Provider": ASR_PROVIDER_VOLCANO,
            "ProviderParams": {"ApiResourceId": tts_model},
        }
        llm_config: dict[str, Any] = {}
        if llm_model_name:
            llm_config["ModelName"] = llm_model_name

        prompt_parts: list[str] = []
        if system_prompt:
            prompt_parts.append(system_prompt)
        if greeting:
            prompt_parts.append(f"开场白请使用：「{greeting}」")
        if prompt_parts:
            llm_config["UserPrompts"] = [
                {"Role": "system", "Content": "\n".join(prompt_parts)}
            ]

        config: dict[str, Any] = {
            "ASRConfig": asr_config,
            "TTSConfig": tts_config,
            "LLMConfig": llm_config,
        }
        if extra_config:
            config.update(extra_config)

        return {
            "Name": name,
            "AccessType": access_type,
            "Config": config,
        }

    async def aibot_create(
        self,
        *,
        name: str,
        access_type: str | None = None,
        system_prompt: str | None = None,
        greeting: str | None = None,
        extra_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``AibotCreate`` 创建智能体。

        ``AccessType`` 的语义见模块 docstring：
        ``public`` 只能由控制台创建（平台智能体），后端要创建
        **商户 / 设备专属人设**必须用 ``private``。
        """
        body = self.build_create_body(
            name=name,
            access_type=access_type or self.access_type,
            asr_resource_id=self.asr_resource_id,
            tts_model=self.tts_model,
            llm_model_name=self.llm_model_name,
            system_prompt=system_prompt,
            greeting=greeting,
            extra_config=extra_config,
        )
        return await self._call_action(ACTION_AIBOT_CREATE, body)

    async def aibot_query(self, *, aibot_id: str) -> dict[str, Any]:
        """``AibotQuery`` 查询智能体。"""
        return await self._call_action(ACTION_AIBOT_QUERY, {"AibotId": aibot_id})

    async def aibot_update(self, *, aibot_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """``AibotUpdate`` 更新智能体（人设 / 模型 / 音色等）。

        ``changes`` 的键同样使用官方 PascalCase 命名（如 ``Name``、
        ``Config``、``AccessType``）。
        """
        return await self._call_action(
            ACTION_AIBOT_UPDATE, {"AibotId": aibot_id, **changes}
        )

    async def aibot_delete(self, *, aibot_id: str) -> dict[str, Any]:
        """``AibotDelete`` 删除智能体。"""
        return await self._call_action(ACTION_AIBOT_DELETE, {"AibotId": aibot_id})

    # ===========================================================
    # 三、智能体与设备 / 产品的绑定
    # ===========================================================

    async def aibot_bind(
        self, *, aibot_id: str, product_id: str | None = None, device_id: str | None = None
    ) -> dict[str, Any]:
        """``AibotBind`` 关联智能体到**产品**或**设备**（两者至少给一个）。

        产品级绑定 = 该产品所有设备共用一个人设；
        设备级绑定 = 单台设备专属人设（``AccessType=private`` 时只能这样绑）。
        """
        if not product_id and not device_id:
            raise self._call_failed(
                "AibotBind 需要 ProductId 或 DeviceId 至少一个",
                detail={"reason": "MISSING_TARGET", "ok": False},
            )
        body: dict[str, Any] = {"AibotId": aibot_id}
        if product_id:
            body["ProductId"] = product_id
        if device_id:
            body["DeviceId"] = device_id
        return await self._call_action(ACTION_AIBOT_BIND, body)

    async def aibot_describe_binding(
        self, *, aibot_id: str | None = None, device_id: str | None = None
    ) -> dict[str, Any]:
        """``AibotDescribeBinding`` 查询绑定关系。"""
        body: dict[str, Any] = {}
        if aibot_id:
            body["AibotId"] = aibot_id
        if device_id:
            body["DeviceId"] = device_id
        return await self._call_action(ACTION_AIBOT_DESCRIBE_BINDING, body)

    async def aibot_unbind(self, *, aibot_id: str, device_id: str | None = None) -> dict[str, Any]:
        """``AibotUnbind`` 取消关联。"""
        body: dict[str, Any] = {"AibotId": aibot_id}
        if device_id:
            body["DeviceId"] = device_id
        return await self._call_action(ACTION_AIBOT_UNBIND, body)

    # ===========================================================
    # 四、声纹（IotVoicePrint*）
    # ===========================================================

    async def voice_print_register(
        self, *, device_id: str, audio_url: str, user_name: str | None = None
    ) -> dict[str, Any]:
        """``IotVoicePrintRegister`` 注册声纹（让设备认得家庭成员）。"""
        body: dict[str, Any] = {"DeviceId": device_id, "AudioUrl": audio_url}
        if user_name:
            body["UserName"] = user_name
        return await self._call_action(ACTION_VOICE_PRINT_REGISTER, body)

    async def voice_print_query(
        self, *, device_id: str, voice_print_id: str | None = None
    ) -> dict[str, Any]:
        """``IotVoicePrintQuery`` 查询声纹。"""
        body: dict[str, Any] = {"DeviceId": device_id}
        if voice_print_id:
            body["VoicePrintId"] = voice_print_id
        return await self._call_action(ACTION_VOICE_PRINT_QUERY, body)

    async def voice_print_update(
        self, *, device_id: str, voice_print_id: str, audio_url: str
    ) -> dict[str, Any]:
        """``IotVoicePrintUpdate`` 更新声纹样本。"""
        return await self._call_action(
            ACTION_VOICE_PRINT_UPDATE,
            {"DeviceId": device_id, "VoicePrintId": voice_print_id, "AudioUrl": audio_url},
        )

    async def voice_print_delete(self, *, device_id: str, voice_print_id: str) -> dict[str, Any]:
        """``IotVoicePrintDelete`` 删除声纹。"""
        return await self._call_action(
            ACTION_VOICE_PRINT_DELETE,
            {"DeviceId": device_id, "VoicePrintId": voice_print_id},
        )

    # ===========================================================
    # 五、音色训练与模型列表
    # ===========================================================

    async def train_tts_voice_type(
        self, *, speaker_id: str, audio_urls: Sequence[str], language: int = 0
    ) -> dict[str, Any]:
        """``TrainTTSVoiceType`` 训练自定义音色（声音复刻）。

        Returns:
            含 ``status`` 与 ``task_id`` 的响应；``status`` 会被补成
            :class:`~app.models.enums.VoiceTrainStatus` 的取值，
            便于落库到 ``voice_profiles.train_status``。
        """
        payload = await self._call_action(
            ACTION_TRAIN_TTS_VOICE,
            {"SpeakerId": speaker_id, "Audios": list(audio_urls), "Language": language},
        )
        payload.setdefault("status", str(VoiceTrainStatus.PENDING))
        return payload

    async def batch_list_voice_train_status(self, *, task_ids: Sequence[str]) -> dict[str, Any]:
        """``BatchListVoiceTrainStatus`` 批量查询声音复刻训练任务状态。"""
        return await self._call_action(
            ACTION_BATCH_VOICE_TRAIN_STATUS, {"TaskIds": list(task_ids)}
        )

    async def list_models(self, *, cluster_id: str | None = None) -> dict[str, Any]:
        """``ListModels`` 查询「模型列表和 Cluster ID」。

        为什么这个接口重要：``ModelName``（LLM）、``TTSConfig`` 的模型与
        ``voice_type`` 都必须从模型列表里选，否则会「配了却调用失败」。
        联调时应把结果写入 ``ai_providers.config`` 作为后续调用的取值来源。
        """
        body: dict[str, Any] = {}
        chosen_cluster = (cluster_id or self.cluster_id).strip()
        if chosen_cluster:
            body["ClusterId"] = chosen_cluster
        return await self._call_action(ACTION_LIST_MODELS, body)

    # ===========================================================
    # 六、AIProvider 接口实现
    # ===========================================================

    async def provision_devices(
        self,
        *,
        product_code: str,
        count: int,
        metadata: dict[str, Any] | None = None,
    ) -> DeviceProvisionResult:
        """生成设备 = 逐个 ``AibotCreate(private)`` 并 ``AibotBind`` 到产品。

        火山的「设备」在服务端体现为「绑定到某产品的智能体实例」，
        因此生成动作天然是两次调用——这也是本适配器与集贤 / 京东云最大的不同。
        """
        extra_config = (metadata or {}).get("Config") if metadata else None
        ids: list[str] = []
        for index in range(max(0, count)):
            created = await self.aibot_create(
                name=f"{product_code}-{index + 1:04d}",
                extra_config=extra_config if isinstance(extra_config, dict) else None,
            )
            aibot_id = _extract_aibot_id(created)
            if not aibot_id:
                continue
            await self.aibot_bind(aibot_id=aibot_id, product_id=product_code)
            ids.append(aibot_id)
        return DeviceProvisionResult(
            success=bool(ids) or count == 0,
            message=f"火山智能体绑定完成：{len(ids)}/{count}",
            vendor_device_ids=ids,
        )

    async def activate_device(
        self, *, device_id: str, sn: str, extra: dict[str, Any] | None = None
    ) -> DeviceActionResult:
        """激活 = 把智能体绑定到设备（``AibotBind`` + ``DeviceId``）。"""
        aibot_id = str((extra or {}).get("aibot_id") or sn)
        payload = await self.aibot_bind(aibot_id=aibot_id, device_id=device_id)
        ok = _is_success(payload)
        return DeviceActionResult(
            success=ok,
            message=str(payload.get("message") or ("绑定成功" if ok else "绑定失败")),
            device_id=device_id,
            activated=ok,
        )

    async def deactivate_device(self, *, device_id: str, reason: str | None = None) -> DeviceActionResult:
        """停用 = ``AibotUnbind`` 取消智能体与设备的关联。"""
        payload = await self.aibot_unbind(aibot_id=device_id, device_id=device_id)
        ok = _is_success(payload)
        return DeviceActionResult(
            success=ok,
            message=str(payload.get("message") or ("解绑成功" if ok else "解绑失败")),
            device_id=device_id,
            activated=False,
        )

    async def push_ota(
        self, *, device_ids: Sequence[str], firmware_version: str, firmware_url: str
    ) -> DeviceActionResult:
        """火山 OpenAPI 未提供固件分发能力（OTA 需由设备侧 / 自建通道完成）。"""
        raise self._call_failed(
            "火山引擎硬件对话智能体未提供平台侧 OTA 接口，固件升级需由设备侧完成",
            detail={"reason": "OTA_NOT_SUPPORTED", "firmwareVersion": firmware_version, "ok": False},
        )

    async def open_session(
        self,
        *,
        session_id: str,
        role_preset: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        """确认绑定关系后返回会话 ID。

        真实链路中这里会建立 RTC / WebSocket 长连接；当前先以
        ``AibotDescribeBinding`` 验证「智能体确实绑在该设备上」，
        避免在未绑定的设备上开启对话（P8 会复用这一校验）。
        """
        device_id = str((context or {}).get("device_id") or "")
        if device_id:
            await self.aibot_describe_binding(device_id=device_id)
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
        """流式对话（**骨架**）。

        **联调待办**：真实链路是 RTC / WebSocket 长连接，本方法需替换为帧解析；
        对外仍是 ``AsyncIterator[ChatChunk]``，上层无需改动。
        当前实现为「HTTP 取整段 → 本地分块」，让流式管线先跑通。
        """
        payload = await self._call_action(
            ACTION_AIBOT_CHAT,
            {
                "AibotId": str((context or {}).get("aibot_id") or ""),
                "DeviceId": str((context or {}).get("device_id") or ""),
                "SessionId": session_id,
                "Messages": [
                    {"Role": str(item.role).lower(), "Content": item.content}
                    for item in [*history, message]
                ],
                "Stream": False,
            },
        )
        reply = _extract_reply_text(payload)
        pieces = chunk_text(reply)
        for index, delta in enumerate(pieces):
            is_final = index == len(pieces) - 1
            yield ChatChunk(
                delta=delta,
                index=index,
                is_final=is_final,
                finish_reason="stop" if is_final else None,
                message_id=str(payload.get("request_id") or "") or None,
            )

    async def close_session(self, *, session_id: str, reason: str | None = None) -> None:
        """关闭会话（真实链路中关闭长连接）。"""
        return None

    async def asr(
        self,
        *,
        audio: bytes | None = None,
        audio_url: str | None = None,
        hint_text: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> AsrResult:
        """语音识别（**骨架**；实时链路在长连接上做流式 ASR）。

        ``ApiResourceId`` 用 :data:`DEFAULT_ASR_RESOURCE_ID`（官方示例的服务版本），
        与 ``AibotCreate`` 里配置的 ASR 版本保持一致。
        """
        payload = await self._call_action(
            ACTION_ASR_RECOGNIZE,
            {
                "ResourceId": self.asr_resource_id,
                "AudioUrl": audio_url or "",
                "AudioSize": len(audio or b""),
            },
        )
        text = str(payload.get("text") or _nested(payload, "result", "text") or "")
        return AsrResult(
            text=text,
            is_final=True,
            confidence=float(payload.get("confidence") or 0.0) or None,
            duration_ms=int(payload.get("duration") or 0) or None,
        )

    async def tts(
        self,
        *,
        text: str,
        voice_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> TtsResult:
        """语音合成（**骨架**）。

        模型与音色必须配套：``ResourceId`` 取 :data:`DEFAULT_TTS_MODEL`
        （或 ``seed-tts-1.0``），``voice_type`` 取音色档案里登记的取值。
        只传其中一个会直接报参数错误——这比静默用默认音色更好排查。
        """
        chosen_voice = voice_id or self.voice_type
        payload = await self._call_action(
            ACTION_TTS_SYNTHESIZE,
            {
                "ClusterId": self.cluster_id,
                "ResourceId": self.tts_model,
                "VoiceType": chosen_voice,
                "Text": text,
                "Encoding": "mp3",
            },
        )
        return TtsResult(
            text=text,
            content_type="audio/mpeg",
            audio=None,  # 真实音频为 base64 大字段，联调时按文档解析后回填
            duration_ms=int(payload.get("duration") or 0) or None,
            voice_id=chosen_voice,
            raw={"requestId": payload.get("request_id"), "note": "骨架实现，未解析音频字节"},
        )


# ===========================================================================
# 三、响应解析助手（厂商响应嵌套深度不一，在此收口）
# ===========================================================================


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    """按路径取值，任一层不是 dict 即返回 ``None``。"""
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _is_success(payload: dict[str, Any]) -> bool:
    """成功判定：``code`` 缺省或为 ``0`` / ``200`` / ``SUCCESS``。

    刻意「缺省视为成功」：这类 OpenAPI 常在成功时不回 ``code``，
    若把缺省当作失败，会把正常的成功响应误报为失败。
    """
    code = payload.get("code")
    if code is None:
        return True
    return str(code) in {"0", "200", "SUCCESS", ""}


def _extract_aibot_id(payload: dict[str, Any]) -> str:
    """从创建响应里取智能体 ID（键名大小写与嵌套位置待校对，故多路兜底）。"""
    for candidate in (
        payload.get("AibotId"),
        payload.get("aibot_id"),
        _nested(payload, "data", "AibotId"),
        _nested(payload, "data", "aibot_id"),
        _nested(payload, "Result", "AibotId"),
    ):
        if candidate:
            return str(candidate)
    return ""


def _extract_reply_text(payload: dict[str, Any]) -> str:
    """从多种可能的响应形状中取出回复文本。

    真实厂商的响应嵌套层级常有变化（``data.content`` / ``result.text`` /
    ``choices[0].message.content``），这里做一次收敛，避免上层到处写 ``.get`` 链。
    """
    for key in ("content", "text", "reply"):
        found = _nested(payload, "data", key)
        if found:
            return str(found)
    found = _nested(payload, "result", "text")
    if found:
        return str(found)
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict) and message.get("content"):
                return str(message["content"])
    return ""


def build_provider() -> VolcanoProvider:
    """按 ``settings`` 构造火山适配器。"""
    return VolcanoProvider(
        credentials=settings.ai_provider_credentials["volcano"],
        retry=HttpRetryConfig(
            timeout_seconds=float(settings.VOLCANO_TIMEOUT_SECONDS),
            max_retries=2,
        ),
    )


__all__ = [
    "ACCESS_TYPE_PRIVATE",
    "ACCESS_TYPE_PUBLIC",
    "ACTION_AIBOT_BIND",
    "ACTION_AIBOT_CHAT",
    "ACTION_AIBOT_CREATE",
    "ACTION_AIBOT_DELETE",
    "ACTION_AIBOT_DESCRIBE_BINDING",
    "ACTION_AIBOT_QUERY",
    "ACTION_AIBOT_UNBIND",
    "ACTION_AIBOT_UPDATE",
    "ACTION_ASR_RECOGNIZE",
    "ACTION_BATCH_VOICE_TRAIN_STATUS",
    "ACTION_LIST_MODELS",
    "ACTION_TRAIN_TTS_VOICE",
    "ACTION_TTS_SYNTHESIZE",
    "ACTION_VOICE_PRINT_DELETE",
    "ACTION_VOICE_PRINT_QUERY",
    "ACTION_VOICE_PRINT_REGISTER",
    "ACTION_VOICE_PRINT_UPDATE",
    "API_PATH",
    "API_VERSION",
    "ASR_PROVIDER_VOLCANO",
    "DEFAULT_API_BASE",
    "DEFAULT_ASR_RESOURCE_ID",
    "DEFAULT_LLM_MODEL_NAME",
    "DEFAULT_TTS_CLUSTER",
    "DEFAULT_TTS_MODEL",
    "DEFAULT_VOICE_TYPE",
    "PROVIDER_CODE",
    "TTS_MODEL_SEED",
    "VolcanoProvider",
    "build_provider",
]
