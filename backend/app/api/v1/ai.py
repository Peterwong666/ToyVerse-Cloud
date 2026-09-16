"""AI 供应商与调试台 API（P7）。

端点一览
--------
==============================  ==========  ==============================
端点                            权限        说明
==============================  ==========  ==============================
``GET  /ai/providers``          平台读      已注册适配器 + 平台配置行的合并视图
``GET  /ai/providers/{c}/health`` 平台读     单厂商健康探测（未配置 → DOWN）
``POST /ai/chat``               登录用户    对话（默认 NDJSON 流式）
``POST /ai/asr``                登录用户    语音识别
``POST /ai/tts``                登录用户    语音合成
``GET  /ai/mock/scenarios``     登录用户    离线引擎素材与规则（调试台用）
==============================  ==========  ==============================

为什么流式用 NDJSON 而不是 SSE
------------------------------
两者都能做流式，这里选 NDJSON（``application/x-ndjson``，每行一个 JSON 对象）：

* **前端解析更简单**：一行一个对象，``JSON.parse`` 即可；SSE 需要处理
  ``data:`` 前缀、空行分帧与多行合并，
* **与 WebSocket 帧结构同构**：P8 的 ``assistant.delta`` 帧就是对象，
  同一套解析代码可以复用（前端只换传输层），
* **天然支持多字段事件**：SSE 只能传文本载荷，``type`` / ``index`` /
  ``latencyMs`` 这些结构字段都得塞进 JSON 字符串里，反而绕了一圈。

代价是不兼容浏览器的 ``EventSource``，因此 P8 会额外提供 SSE 降级端点
``/miniapp/chat/stream``——调试台与设备走 NDJSON，浏览器降级走 SSE。

未接入厂商的处理（ADR-07）
--------------------------
所有真实能力调用前都会依次校验「能力域是否支持」与「是否已配置密钥」，
任一不过即抛 ``VENDOR_UNAVAILABLE`` 并写入 ``VENDOR_CALL_FAILED`` 审计。
**流式响应一旦开始就无法改 HTTP 状态码**，因此这类校验必须在
构造 ``StreamingResponse`` **之前**完成——见 :func:`ai_chat` 的执行顺序。

分层说明
--------
按项目的分层约定，「服务层负责审计 + 提交事务」。P7 阶段 AI 域的服务逻辑
暂时与路由同文件（``app/ai`` 目录的既有模块都是无状态适配器，放不下
依赖 ORM 会话的编排逻辑），待 P8 引入 WebSocket 会话管理时再抽出
``app/services/ai_service.py``，届时本文件将只保留参数解析与响应转换。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import registry
from app.ai.base import (
    CAP_ASR,
    CAP_DIALOGUE,
    CAP_TTS,
    AsrResult,
    ChatMessage,
    TtsResult,
    provider_unavailable_error,
)
from app.ai.mock.scenarios import (
    BUILTIN_ROLE_PRESETS,
    FALLBACK_REPLIES,
    GREETING_KEYWORDS,
    QUESTION_KEYWORDS,
    SAFETY_KEYWORDS,
    SONG_KEYWORDS,
    SONGS,
    STORIES,
    STORY_KEYWORDS,
    WEATHER_KEYWORDS,
    WEATHER_REPLIES,
    ContentSnippet,
)
from app.ai.registry import ResolvedProvider
from app.core.deps import AuthContext, CurrentAuth, DbSession, PlatformAuth, require_perm
from app.core.errors import AppException, ErrorCode, not_found, validation_error
from app.core.ids import new_id
from app.core.logging import get_logger
from app.core.permissions import PlatformPerm
from app.db.base import utcnow
from app.db.scope import scoped
from app.models.ai import AiProvider as AiProviderRow, DialogueMessage, DialogueSession
from app.models.catalog import ClientProduct
from app.models.enums import (
    AiProviderKind,
    AuditAction,
    DialogueSessionStatus,
    MessageContentType,
    MessageRole,
)
from app.schemas.ai import (
    AsrRequest,
    AsrResponse,
    ChatChunkEvent,
    ChatRequest,
    ChatResponse,
    HealthResponse,
    ProviderInfo,
    ProviderListResult,
    RolePresetItem,
    ScenarioItem,
    ScenarioListResponse,
    TtsRequest,
    TtsResponse,
    decode_audio,
    encode_audio,
)
from app.services import audit_service

logger = get_logger(__name__)

router = APIRouter(prefix="/ai", tags=["AI 供应商与调试台"])

#: NDJSON 媒体类型（每行一个 JSON 对象）
NDJSON_MEDIA_TYPE = "application/x-ndjson"

#: 内容安全命中的标记值。
#: 刻意只记「命中类型」而不记具体敏感词——审计与日志里的敏感词本身就是风险源，
#: 排查时按 ``safety_flag`` 定位到消息即可看到上下文。
SAFETY_FLAG_KEYWORD = "KEYWORD"

#: 流式输出的预检能力域（避免在流开始后才发现不支持）
_STREAM_CACHE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


# ===========================================================================
# 内部工具
# ===========================================================================


def _encode_event(event: ChatChunkEvent) -> bytes:
    """把一个事件序列化为一行 NDJSON。"""
    payload = event.model_dump(by_alias=True, exclude_none=True)
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


async def _record_vendor_failure(
    session: AsyncSession,
    exc: AppException,
    *,
    provider_code: str,
    actor: AuthContext | None,
    request: Request | None,
    layer: str | None = None,
    resource_type: str = "ai_provider",
    resource_id: str | None = None,
) -> None:
    """写入一条 ``VENDOR_CALL_FAILED`` 审计并提交。

    为什么单独提交？因为这条审计是「安全拒绝」的**证据**：
    若与业务事务绑在一起，业务回滚会把证据一起丢掉，
    运维就再也无法证明「当时确实拒绝了，而不是悄悄成功了」。
    """
    await audit_service.record(
        session,
        action=AuditAction.VENDOR_CALL_FAILED,
        actor=actor,
        resource_type=resource_type,
        resource_id=resource_id or provider_code,
        summary=f"厂商调用被安全拒绝：{exc.message}",
        detail={
            "code": str(exc.code),
            "providerCode": provider_code,
            "layer": layer,
            "ok": False,
        },
        success=False,
        request=request,
    )
    await session.commit()


async def _resolve_provider(
    session: AsyncSession,
    *,
    request_code: str | None,
    client_product_id: str | None,
) -> ResolvedProvider:
    """按四层解析顺序选定供应商。

    Raises:
        AppException: 四层全部未命中已注册供应商。
    """
    registry.ensure_default_registry()
    resolved = await registry.resolve_for_product(
        session, client_product_id=client_product_id, request_code=request_code
    )
    if resolved is None:
        raise AppException(
            ErrorCode.VENDOR_UNAVAILABLE,
            "未找到可用的 AI 供应商（请检查 AI 配置、模板云厂商或 AI_DEFAULT_PROVIDER）",
            details={"ok": False, "layer": "none", "reason": "NO_PROVIDER_RESOLVED"},
        )
    return resolved


async def _guard_capability(
    session: AsyncSession,
    resolved: ResolvedProvider,
    *,
    capability: str,
    actor: AuthContext | None,
    request: Request | None,
) -> None:
    """校验「能力域支持」与「已配置密钥」，任一不过即安全失败并写审计。"""
    provider = resolved.provider
    try:
        if not provider.supports(capability):
            raise provider_unavailable_error(provider, capability)
        provider.ensure_configured()
    except AppException as exc:
        await _record_vendor_failure(
            session,
            exc,
            provider_code=provider.code,
            actor=actor,
            request=request,
            layer=resolved.layer,
        )
        raise


async def _resolve_tenant_id(
    session: AsyncSession, auth: AuthContext, client_product_id: str | None
) -> str:
    """确定本次调用归属的租户。

    * 商户 / 工厂账号：直接用自身租户；若指定了产品，校验产品确属该租户
      （不通过时返回「不存在」而非「无权限」——避免探测其他租户的资源是否存在）
    * 平台账号：必须指定 ``clientProductId``，由产品反查租户
    """
    if auth.tenant_id:
        if client_product_id:
            owner = (
                await session.execute(
                    select(ClientProduct.tenant_id).where(ClientProduct.id == client_product_id)
                )
            ).scalar_one_or_none()
            if owner is None or owner != auth.tenant_id:
                raise not_found("客户产品不存在")
        return auth.tenant_id

    if not client_product_id:
        raise validation_error("平台账号调用 AI 能力需指定 clientProductId")
    owner = (
        await session.execute(
            select(ClientProduct.tenant_id).where(ClientProduct.id == client_product_id)
        )
    ).scalar_one_or_none()
    if owner is None:
        raise not_found("客户产品不存在")
    return str(owner)


async def _load_or_create_session(
    session: AsyncSession,
    auth: AuthContext,
    payload: ChatRequest,
    *,
    tenant_id: str,
    provider_code: str,
) -> tuple[DialogueSession, bool]:
    """取会话或新建会话。

    Returns:
        ``(会话, 是否新建)``。
    """
    if payload.session_id:
        stmt = scoped(
            select(DialogueSession).where(DialogueSession.id == payload.session_id),
            DialogueSession,
            auth,
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            raise not_found("对话会话不存在")
        if existing.status == DialogueSessionStatus.CLOSED:
            # 续聊已结束的会话：重新置为进行中而不是报错——
            # 前端「继续聊」按钮不应该因为一次超时关闭就永久失效
            existing.status = str(DialogueSessionStatus.ACTIVE)
            existing.ended_at = None
            existing.close_reason = None
        return existing, False

    created = DialogueSession(
        id=new_id("dialogue_session"),
        tenant_id=tenant_id,
        client_product_id=payload.client_product_id,
        device_id=payload.device_id,
        end_user_id=payload.end_user_id,
        provider_code=provider_code,
        role_preset_code=payload.role_preset,
        status=str(DialogueSessionStatus.ACTIVE),
        started_at=utcnow(),
    )
    session.add(created)
    await session.flush()
    return created, True


async def _next_seq(session: AsyncSession, session_id: str) -> int:
    """会话内下一个消息序号（从 1 开始）。"""
    current = (
        await session.execute(
            select(func.max(DialogueMessage.seq)).where(DialogueMessage.session_id == session_id)
        )
    ).scalar_one_or_none()
    return int(current or 0) + 1


def _append_message(
    session: AsyncSession,
    dialogue: DialogueSession,
    *,
    seq: int,
    role: MessageRole,
    content: str,
    content_type: MessageContentType = MessageContentType.TEXT,
    latency_ms: int | None = None,
    provider_code: str | None = None,
    provider_message_id: str | None = None,
    chunk_count: int = 0,
    safety_flag: str | None = None,
) -> DialogueMessage:
    """追加一条对话消息并同步会话聚合字段。"""
    message = DialogueMessage(
        id=new_id("dialogue_message"),
        session_id=dialogue.id,
        tenant_id=dialogue.tenant_id,
        seq=seq,
        role=str(role),
        content_type=str(content_type),
        content=content,
        latency_ms=latency_ms,
        provider_code=provider_code,
        provider_message_id=provider_message_id,
        chunk_count=chunk_count,
        safety_flag=safety_flag,
    )
    session.add(message)
    dialogue.message_count += 1
    if latency_ms:
        dialogue.total_latency_ms += latency_ms
    return message


async def _collect_chat(
    provider_resolved: ResolvedProvider,
    *,
    session_id: str,
    payload: ChatRequest,
) -> tuple[str, int, int | None, str | None, str | None]:
    """非流式收集：把流式分块拼成整段。

    Returns:
        ``(全文, 分块数, 首字延迟毫秒, 结束原因, 厂商消息 ID)``
    """
    started = utcnow()
    parts: list[str] = []
    first_delta_ms: int | None = None
    finish_reason: str | None = None
    vendor_message_id: str | None = None

    async for chunk in provider_resolved.provider.chat(
        session_id=session_id,
        message=ChatMessage(content=payload.message, role=str(MessageRole.USER)),
        role_preset=payload.role_preset,
        context={"device_id": payload.device_id},
    ):
        if first_delta_ms is None and chunk.delta:
            first_delta_ms = int((utcnow() - started).total_seconds() * 1000)
        parts.append(chunk.delta)
        if chunk.is_final:
            finish_reason = chunk.finish_reason
            vendor_message_id = chunk.message_id

    return "".join(parts), len(parts), first_delta_ms, finish_reason, vendor_message_id


# ===========================================================================
# 一、供应商
# ===========================================================================


@router.get(
    "/providers",
    response_model=ProviderListResult,
    summary="AI 供应商列表",
    description=(
        "返回已注册的适配器（mock / 集贤 / JoyInside / 火山 / 百度），"
        "并附带平台配置行状态与最近一次健康探测结果。\n\n"
        "``configured`` 表示**是否已录入密钥**，``registered`` 表示**代码里是否有适配器**——"
        "两者分开才能区分「没实现」与「没配置」。响应**不含任何密钥**。"
    ),
    dependencies=[require_perm(PlatformPerm.CLOUD_READ)],
)
async def list_providers(session: DbSession, _auth: PlatformAuth) -> ProviderListResult:
    """AI 供应商列表。"""
    registry.ensure_default_registry()
    rows = list((await session.execute(select(AiProviderRow))).scalars().all())
    row_by_code = {row.code: row for row in rows}

    records: list[ProviderInfo] = []
    for descriptor in registry.describe_providers():
        row = row_by_code.get(descriptor.code)
        records.append(
            ProviderInfo(
                code=descriptor.code,
                name=row.name if row else descriptor.name,
                kind=row.kind if row else descriptor.kind,
                vendor=descriptor.vendor or (row.vendor if row else None),
                capabilities=descriptor.capabilities,
                configured=descriptor.configured,
                registered=True,
                requires_credentials=descriptor.requires_credentials,
                description=descriptor.description,
                health=row.last_health_status if row else None,
            )
        )

    # 平台登记了、但代码里没有适配器的供应商也列出来——这通常是「配错了 code」，
    # 静默忽略会让运维以为配置生效了
    for row in rows:
        if row.code in registry.provider_codes():
            continue
        records.append(
            ProviderInfo(
                code=row.code,
                name=row.name,
                kind=row.kind,
                vendor=row.vendor,
                capabilities=[],
                configured=row.has_credentials,
                registered=False,
                requires_credentials=True,
                description="平台已登记但无对应适配器（请检查注册键是否正确）",
                health=row.last_health_status,
            )
        )

    return ProviderListResult(
        records=records, total=len(records), default_code=registry.default_provider_code()
    )


@router.get(
    "/providers/{code}/health",
    response_model=HealthResponse,
    summary="供应商健康探测",
    description=(
        "对单个供应商发起健康探测。\n\n"
        "**安全语义（ADR-07）**：未配置密钥时返回 ``status=DOWN``、``ok=false``，"
        "**不发起任何真实网络调用**，同时写入 ``VENDOR_CALL_FAILED`` 审计。\n\n"
        "为什么这里返回 200 而不是 503？探测接口的职责就是**报告**状态；"
        "若探测失败也返回错误状态码，运维就无法区分「厂商不可用」与「探测接口本身坏了」。"
        "真正的能力调用（``/ai/chat`` 等）未接入时一律返回 503。"
    ),
    dependencies=[require_perm(PlatformPerm.CLOUD_READ)],
)
async def provider_health(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    code: str,
) -> HealthResponse:
    """供应商健康探测。"""
    registry.ensure_default_registry()
    provider = registry.get(code)
    if provider is None:
        exc = AppException(
            ErrorCode.VENDOR_UNAVAILABLE,
            f"供应商 {code} 未注册，无法探测",
            details={"vendor": code, "ok": False, "reason": "PROVIDER_NOT_REGISTERED"},
        )
        await _record_vendor_failure(
            session, exc, provider_code=code, actor=auth, request=request, layer=registry.LAYER_NONE
        )
        raise exc

    result = await provider.health_check()

    row = (
        await session.execute(select(AiProviderRow).where(AiProviderRow.code == code))
    ).scalar_one_or_none()
    if row is not None:
        row.last_health_at = result.checked_at
        row.last_health_status = result.status
        row.last_health_message = result.message

    if result.ok:
        await audit_service.record(
            session,
            action=AuditAction.UPDATE,
            actor=auth,
            resource_type="ai_provider",
            resource_id=code,
            summary=f"健康探测：{result.status}——{result.message}",
            detail={"status": result.status, "latencyMs": result.latency_ms},
            success=True,
            request=request,
        )
        await session.commit()
    else:
        await _record_vendor_failure(
            session,
            AppException(ErrorCode.VENDOR_UNAVAILABLE, result.message),
            provider_code=code,
            actor=auth,
            request=request,
            layer=registry.LAYER_NONE,
        )

    return HealthResponse(
        code=code,
        name=provider.name or code,
        status=result.status,
        ok=result.ok,
        message=result.message,
        vendor=result.vendor,
        latency_ms=result.latency_ms,
        checked_at=result.checked_at,
        detail=result.detail,
    )


# ===========================================================================
# 二、对话
# ===========================================================================


@router.post(
    "/chat",
    response_model=None,
    summary="AI 对话（默认流式 NDJSON）",
    description=(
        "发起一次对话。``stream=true``（默认）返回 ``application/x-ndjson``，"
        "每行一个事件：``session`` → 若干 ``delta`` → ``done``；\n"
        "``stream=false`` 返回整段 :class:`ChatResponse`。\n\n"
        "会话与消息都会落库（``dialogue_sessions`` / ``dialogue_messages``），"
        "包括分块数 ``chunkCount``、耗时 ``latencyMs`` 与安全过滤标记 ``safetyFlag``，"
        "为 P8 的 WebSocket 对话与 P9 的运营看板提供数据。\n\n"
        "**未接入的供应商返回 503 ``VENDOR_UNAVAILABLE`` 并写审计，绝不伪造成功。**"
    ),
    responses={503: {"description": "供应商未接入或不可用"}},
)
async def ai_chat(
    request: Request,
    session: DbSession,
    auth: CurrentAuth,
    payload: ChatRequest = Body(...),
) -> Response:
    """AI 对话。

    执行顺序很重要：**能力校验 → 供应商配置校验 → 会话落库 → 才开始流式输出**。
    流式响应一旦开始就不能再改 HTTP 状态码，因此所有会失败的前置检查
    都必须在构造 ``StreamingResponse`` 之前完成。
    """
    resolved = await _resolve_provider(
        session, request_code=payload.provider_code, client_product_id=payload.client_product_id
    )
    await _guard_capability(
        session, resolved, capability=CAP_DIALOGUE, actor=auth, request=request
    )

    tenant_id = await _resolve_tenant_id(session, auth, payload.client_product_id)
    dialogue, _created = await _load_or_create_session(
        session, auth, payload, tenant_id=tenant_id, provider_code=resolved.provider.code
    )
    await resolved.provider.open_session(
        session_id=dialogue.id,
        role_preset=payload.role_preset,
        context={"device_id": payload.device_id},
    )

    user_seq = await _next_seq(session, dialogue.id)
    _append_message(
        session,
        dialogue,
        seq=user_seq,
        role=MessageRole.USER,
        content=payload.message,
        provider_code=resolved.provider.code,
    )
    await session.commit()

    if payload.stream:
        return StreamingResponse(
            _chat_stream(
                session,
                dialogue=dialogue,
                resolved=resolved,
                payload=payload,
                auth=auth,
                request=request,
                user_seq=user_seq,
            ),
            media_type=NDJSON_MEDIA_TYPE,
            headers=_STREAM_CACHE_HEADERS,
        )

    started = utcnow()
    try:
        reply, chunk_count, first_delta_ms, finish_reason, vendor_message_id = await _collect_chat(
            resolved, session_id=dialogue.id, payload=payload
        )
    except AppException as exc:
        await _record_vendor_failure(
            session,
            exc,
            provider_code=resolved.provider.code,
            actor=auth,
            request=request,
            layer=resolved.layer,
            resource_id=dialogue.id,
        )
        raise

    total_ms = int((utcnow() - started).total_seconds() * 1000)
    safety_flag = SAFETY_FLAG_KEYWORD if finish_reason == "safety" else None
    assistant = _append_message(
        session,
        dialogue,
        seq=user_seq + 1,
        role=MessageRole.ASSISTANT,
        content=reply,
        latency_ms=total_ms,
        provider_code=resolved.provider.code,
        provider_message_id=vendor_message_id,
        chunk_count=chunk_count,
        safety_flag=safety_flag,
    )
    await session.commit()

    body = ChatResponse(
        session_id=dialogue.id,
        message_id=assistant.id,
        reply=reply,
        chunks=chunk_count,
        latency_ms=total_ms,
        provider=resolved.provider.code,
        provider_layer=resolved.layer,
        role_preset=payload.role_preset,
        safety_flag=safety_flag,
        simulated=resolved.provider.kind == AiProviderKind.LOCAL,
    )
    return JSONResponse(content=body.model_dump(by_alias=True))


async def _chat_stream(
    session: AsyncSession,
    *,
    dialogue: DialogueSession,
    resolved: ResolvedProvider,
    payload: ChatRequest,
    auth: AuthContext,
    request: Request | None,
    user_seq: int,
) -> AsyncIterator[bytes]:
    """流式输出生成器。

    注意：本生成器在响应体发送过程中仍使用请求级会话，因此**必须在
    产出 ``done`` / ``error`` 事件之前完成落库与提交**，否则客户端断开时
    会话可能被提前关闭。真实音频链路上 P8 会改用独立会话管理。
    """
    provider = resolved.provider
    started = utcnow()
    parts: list[str] = []
    chunk_count = 0
    first_delta_ms: int | None = None
    finish_reason: str | None = None
    vendor_message_id: str | None = None

    yield _encode_event(
        ChatChunkEvent(
            type="session",
            session_id=dialogue.id,
            provider=provider.code,
            provider_layer=resolved.layer,
            is_final=False,
        )
    )

    try:
        async for chunk in provider.chat(
            session_id=dialogue.id,
            message=ChatMessage(content=payload.message, role=str(MessageRole.USER)),
            role_preset=payload.role_preset,
            context={"device_id": payload.device_id},
        ):
            if first_delta_ms is None and chunk.delta:
                first_delta_ms = int((utcnow() - started).total_seconds() * 1000)
            parts.append(chunk.delta)
            chunk_count += 1
            if chunk.is_final:
                finish_reason = chunk.finish_reason
                vendor_message_id = chunk.message_id
            yield _encode_event(
                ChatChunkEvent(
                    type="delta",
                    session_id=dialogue.id,
                    delta=chunk.delta,
                    index=chunk.index,
                    is_final=chunk.is_final,
                    finish_reason=chunk.finish_reason,
                    provider=provider.code,
                    provider_layer=resolved.layer,
                    latency_ms=first_delta_ms if chunk.index == 0 else None,
                )
            )
    except AppException as exc:
        await _record_vendor_failure(
            session,
            exc,
            provider_code=provider.code,
            actor=auth,
            request=request,
            layer=resolved.layer,
            resource_id=dialogue.id,
        )
        yield _encode_event(
            ChatChunkEvent(
                type="error",
                session_id=dialogue.id,
                provider=provider.code,
                provider_layer=resolved.layer,
                is_final=True,
                error_code=str(exc.code),
                error_message=exc.message,
            )
        )
        return
    except Exception as exc:
        logger.exception("流式对话异常：provider=%s session=%s", provider.code, dialogue.id)
        yield _encode_event(
            ChatChunkEvent(
                type="error",
                session_id=dialogue.id,
                provider=provider.code,
                provider_layer=resolved.layer,
                is_final=True,
                error_code=str(ErrorCode.INTERNAL_ERROR),
                error_message=f"流式对话中断：{type(exc).__name__}",
            )
        )
        return

    total_ms = int((utcnow() - started).total_seconds() * 1000)
    safety_flag = SAFETY_FLAG_KEYWORD if finish_reason == "safety" else None
    assistant = _append_message(
        session,
        dialogue,
        seq=user_seq + 1,
        role=MessageRole.ASSISTANT,
        content="".join(parts),
        latency_ms=total_ms,
        provider_code=provider.code,
        provider_message_id=vendor_message_id,
        chunk_count=chunk_count,
        safety_flag=safety_flag,
    )
    await session.commit()

    yield _encode_event(
        ChatChunkEvent(
            type="done",
            session_id=dialogue.id,
            message_id=assistant.id,
            is_final=True,
            finish_reason=finish_reason,
            provider=provider.code,
            provider_layer=resolved.layer,
            latency_ms=first_delta_ms,
            total_latency_ms=total_ms,
            chunk_count=chunk_count,
            safety_flag=safety_flag,
        )
    )


# ===========================================================================
# 三、语音
# ===========================================================================


@router.post(
    "/asr",
    response_model=AsrResponse,
    summary="语音识别（ASR）",
    description=(
        "把音频转成文字。离线环境下可不传音频、只传 ``hintText``，"
        "由 mock 的 ``echo`` 模式复读，从而在无音频素材时也能联调语音链路。\n\n"
        "**未接入的供应商返回 503 ``VENDOR_UNAVAILABLE`` 并写审计。**"
    ),
    responses={503: {"description": "供应商未接入或不可用"}},
)
async def ai_asr(
    request: Request,
    session: DbSession,
    auth: CurrentAuth,
    payload: AsrRequest = Body(...),
) -> AsrResponse:
    """语音识别。"""
    resolved = await _resolve_provider(
        session, request_code=payload.provider_code, client_product_id=payload.client_product_id
    )
    await _guard_capability(session, resolved, capability=CAP_ASR, actor=auth, request=request)

    try:
        result: AsrResult = await resolved.provider.asr(
            audio=decode_audio(payload.audio_base64),
            hint_text=payload.hint_text,
            context={"format": payload.format, "sample_rate": payload.sample_rate},
        )
    except AppException as exc:
        await _record_vendor_failure(
            session,
            exc,
            provider_code=resolved.provider.code,
            actor=auth,
            request=request,
            layer=resolved.layer,
        )
        raise

    return AsrResponse(
        text=result.text,
        is_final=result.is_final,
        confidence=result.confidence,
        duration_ms=result.duration_ms,
        provider=resolved.provider.code,
        provider_layer=resolved.layer,
        simulated=result.simulated,
    )


@router.post(
    "/tts",
    response_model=TtsResponse,
    summary="语音合成（TTS）",
    description=(
        "把文字转成语音。``MOCK_TTS_MODE=text`` 时 ``audioBase64`` 为空，"
        "表示**本次未产出音频**（调用方应回退为文本播报），不是错误；"
        "``MOCK_TTS_MODE=wav`` 时返回结构合法的静音 WAV，用于验证音频播放链路。\n\n"
        "**未接入的供应商返回 503 ``VENDOR_UNAVAILABLE`` 并写审计。**"
    ),
    responses={503: {"description": "供应商未接入或不可用"}},
)
async def ai_tts(
    request: Request,
    session: DbSession,
    auth: CurrentAuth,
    payload: TtsRequest = Body(...),
) -> TtsResponse:
    """语音合成。"""
    resolved = await _resolve_provider(
        session, request_code=payload.provider_code, client_product_id=payload.client_product_id
    )
    await _guard_capability(session, resolved, capability=CAP_TTS, actor=auth, request=request)

    try:
        result: TtsResult = await resolved.provider.tts(
            text=payload.text, voice_id=payload.voice_id
        )
    except AppException as exc:
        await _record_vendor_failure(
            session,
            exc,
            provider_code=resolved.provider.code,
            actor=auth,
            request=request,
            layer=resolved.layer,
        )
        raise

    return TtsResponse(
        text=result.text,
        content_type=result.content_type,
        audio_base64=encode_audio(result.audio),
        duration_ms=result.duration_ms,
        voice_id=result.voice_id,
        provider=resolved.provider.code,
        provider_layer=resolved.layer,
        simulated=result.simulated,
    )


# ===========================================================================
# 四、离线引擎素材（调试台）
# ===========================================================================


def _to_scenario_item(snippet: ContentSnippet) -> ScenarioItem:
    """把素材转为响应项（列表页只需预览，避免整段正文传回）。"""
    preview = snippet.body[:40] + ("…" if len(snippet.body) > 40 else "")
    return ScenarioItem(
        kind=snippet.kind, title=snippet.title, keywords=list(snippet.keywords), preview=preview
    )


@router.get(
    "/mock/scenarios",
    response_model=ScenarioListResponse,
    summary="离线模拟引擎素材与规则",
    description=(
        "返回 mock 引擎内置的故事 / 儿歌 / 天气话术、意图关键词、安全关键词与角色预设。\n\n"
        "把离线引擎的**能力边界摊开**给调试台，是为了让「为什么这样回答」可被解释——"
        "规则对话的命中依据必须可见，否则它和黑盒模型没有区别。"
    ),
)
async def mock_scenarios(_auth: CurrentAuth) -> ScenarioListResponse:
    """离线模拟引擎素材与规则。"""
    return ScenarioListResponse(
        stories=[_to_scenario_item(item) for item in STORIES],
        songs=[_to_scenario_item(item) for item in SONGS],
        intent_keywords={
            "story": list(STORY_KEYWORDS),
            "song": list(SONG_KEYWORDS),
            "weather": list(WEATHER_KEYWORDS),
            "greeting": list(GREETING_KEYWORDS),
            "question": list(QUESTION_KEYWORDS),
        },
        weather_replies=list(WEATHER_REPLIES),
        fallback_replies=list(FALLBACK_REPLIES),
        safety_keywords=list(SAFETY_KEYWORDS),
        role_presets=[
            RolePresetItem(
                code=code, name=str(preset["name"]), greeting=str(preset["greeting"])
            )
            for code, preset in BUILTIN_ROLE_PRESETS.items()
        ],
    )


__all__ = ["router"]
