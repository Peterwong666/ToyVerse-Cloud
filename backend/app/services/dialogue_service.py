"""对话编排服务（P8）：会话落库 + 内容安全 + 流式帧。

为什么抽出来的是「对话编排」而不是 P7 docstring 里写的 ``ai_service``
================================================================
P7 在 ``app/api/v1/ai.py`` 的 docstring 里预告「待 P8 抽出 ``app/services/ai_service.py``」。
真正动手时发现该文件里的服务逻辑可以干净地切成**两类**，而它们的归属不同：

* **管理端视角**（供应商列表 / 健康探测 / 密钥校验的执行顺序 / 调试台）——
  它属于「运维与平台配置」，P7 的 42 条测试钉住的就是这部分行为；
* **会话编排**（谁在跟哪台设备说话、消息怎么落库、内容安全怎么过滤、
  流式帧长什么样）——它属于「对话业务」，且被 P8 的 WS 与 SSE **同时**复用。

把两者一起抽走，等于为了 P8 的功能去动 P7 已验收的路径。因此本阶段的做法是：
**新建** :mod:`app.services.dialogue_service` 承载第二类，``ai.py`` 原样保留。
P8 需要的供应商解析与安全失败验真（``ensure_vendor_capability``）也从
:mod:`app.services.miniapp_service` 复用，不重复实现一遍 ADR-07。

会话的自动补开（宽容策略）
==========================
协议约定客户端应先发 ``session.open`` 再发 ``user.text``。
真实客户端（尤其是网络抖动后重连的固件）常常直接发 ``user.text``：
此时服务端**自动补开**一个会话，而不是回一个 ``error`` 帧。

取舍理由：报错省下的那一次建会话操作，代价是「用户说了一句话却被打回」，
而玩具场景里用户不会读错误码、只会觉得设备坏了。
补开的成本极低（一次 INSERT），且 ``session.ready`` 帧仍会正常下发，
客户端拿到会话 ID 后行为完全一致。真正该拒绝的情况（未绑定设备、
厂商未接入）仍会以 ``error`` 帧明确拒绝。

内容安全为什么在开启时牺牲流式
==============================
要求是「命中即拦截，**不发送被拦截的原文**」。逐块过滤做不到这一点：
等第 5 块检出敏感词时，前 4 块已经发给客户端了，原文已经泄漏。

因此实现上采取**先聚合、再放行**：开关打开时把整段回复聚合起来判定，
命中则整段替换为安全话术，只有安全话术的分块会被发出。
代价是首字延迟从「首块耗时」变成「整段耗时」——这是为「绝不下发原文」
付出的确定性成本，而不是实现疏漏，故在此明写。

开关关闭时（``CONTENT_SAFETY_TEXT=false``）走**真流式**：边收边发，
不做任何缓存。这样「安全」与「低延迟」的取舍对使用者是显式可见的。

三个开关各自的职责
------------------
* ``CONTENT_SAFETY_TEXT`` —— 文本轮次的回复过滤开关；
* ``CONTENT_SAFETY_AUDIO`` —— **音频轮次**用它代替上面的开关
  （语音玩具的主要输入形态是音频，两者必须能独立开关，
  否则关掉文本过滤就等于关掉全部过滤）；
* ``CONTENT_SAFETY_VISUAL`` —— P8 的帧协议里**没有图像通道**
  （只有 ``user.text`` / ``user.audio``），因此本阶段读取但未使用，
  留给 P9 的视觉能力。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import registry
from app.ai.base import CAP_ASR, CAP_DIALOGUE, CAP_TTS, ChatMessage, chunk_text, provider_unavailable_error
from app.ai.mock.scenarios import detect_safety, safety_reply
from app.ai.registry import ResolvedProvider
from app.core.config import settings
from app.core.deps import EndUserContext
from app.core.errors import AppException, ErrorCode, not_found, validation_error
from app.core.ids import new_id
from app.core.logging import get_logger, get_trace_id
from app.db.base import utcnow
from app.models.ai import DialogueMessage, DialogueSession
from app.models.device import Device
from app.models.enums import (
    AuditAction,
    DialogueSessionStatus,
    MessageContentType,
    MessageRole,
)
from app.schemas.ai import encode_audio
from app.schemas.miniapp import SAFETY_FLAG_KEYWORD, ChatFrameType
from app.services import audit_service, miniapp_service

logger = get_logger(__name__)

#: 安全过滤路径下重新分块的粒度。
#:
#: 聚合后必须重新分块再发（前端要的是增量流），粒度取 24——
#: 与 ``app.ai.base.chunk_text`` 的真实厂商默认值一致：安全兜底文案很短，
#: 块太碎只会让前端渲染出「一个字一个字跳」的观感。
SAFETY_RECHUNK_SIZE = 24

#: 会话因达到消息上限而自动关闭时写入的关闭原因
CLOSE_REASON_LIMIT = "达到会话消息上限"


# ---------------------------------------------------------------------------
# 帧构造（协议的唯一落点，见 app.schemas.miniapp 的帧说明）
# ---------------------------------------------------------------------------


def frame_ready(session_id: str) -> dict[str, Any]:
    """``session.ready``：会话已就绪。

    协议明确要求带上 ``messageId: null``（该帧不产出消息），
    因此这里**不省略**空字段——与其余帧的 ``exclude_none`` 策略不同。
    """
    return {
        "type": str(ChatFrameType.SESSION_READY),
        "sessionId": session_id,
        "messageId": None,
    }


def frame_asr_partial(text: str) -> dict[str, Any]:
    """``asr.partial``：语音识别结果（仅音频输入）。

    帧名固定为 ``asr.partial``（协议约定）。离线引擎的 ASR 是一次性结果
    （``is_final=True``），因此只会发一帧；真实链路的中间结果同样复用此帧，
    前端无需为「边识别边出字」再学一套协议。
    """
    return {"type": str(ChatFrameType.ASR_PARTIAL), "text": text}


def frame_delta(delta: str, index: int) -> dict[str, Any]:
    """``assistant.delta``：回复的一个增量分块。"""
    return {"type": str(ChatFrameType.ASSISTANT_DELTA), "delta": delta, "index": index}


def frame_audio(data: str, mime_type: str) -> dict[str, Any]:
    """``assistant.audio``：回复音频（base64）。

    ``MOCK_TTS_MODE=text`` 时不上报本帧——**没有音频**与「有一段空音频」
    对设备端是两回事：后者会让固件去解码一个空 buffer。
    """
    return {"type": str(ChatFrameType.ASSISTANT_AUDIO), "data": data, "mimeType": mime_type}


def frame_done(
    *,
    message_id: str,
    session_id: str,
    latency_ms: int,
    total_latency_ms: int,
    safety_flag: str | None,
) -> dict[str, Any]:
    """``assistant.done``：本次回复结束。

    ``latencyMs`` 是**首字延迟**（验收指标之一），``totalLatencyMs`` 是端到端耗时；
    两者都给，是因为「首字快但收尾慢」与「首字慢」的优化方向完全不同。

    ``safetyFlag`` / ``blocked`` 让前端能显示「这条被安全过滤了」，
    而不是把兜底话术当成模型的正常回答（开关关闭时两者都为 null/false，
    因此「有没有过滤」在响应里是可判定的）。
    """
    return {
        "type": str(ChatFrameType.ASSISTANT_DONE),
        "messageId": message_id,
        "latencyMs": latency_ms,
        "sessionId": session_id,
        "totalLatencyMs": total_latency_ms,
        "safetyFlag": safety_flag,
        "blocked": safety_flag is not None,
    }


def frame_error(exc: AppException) -> dict[str, Any]:
    """``error``：以帧的形式报告失败。

    流式响应一旦开始就无法改 HTTP 状态码，因此厂商不可用 / 校验失败
    只能通过本帧告知（与 P7 的 NDJSON ``error`` 同一取舍）。
    ``traceId`` 让用户报障时能直接对上服务端日志。
    """
    return {
        "type": str(ChatFrameType.ERROR),
        "code": str(exc.code),
        "message": exc.message,
        "traceId": get_trace_id(),
    }


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


async def open_dialogue(
    session: AsyncSession,
    ctx: EndUserContext,
    *,
    device_id: str,
    session_id: str | None = None,
    request: Request | None = None,
) -> tuple[Device, DialogueSession]:
    """打开（或复用）一次对话会话，返回 ``(设备, 会话)``。

    复用时会校验会话确属「这台设备 + 这位用户」：``session_id`` 是客户端回传的，
    不校验就等于允许任何人拿别人的会话 ID 继续聊、
    并把消息写进别人的会话记录里。

    续聊一个已 ``CLOSED`` 的会话时把它重新置为 ``ACTIVE`` 而不是报错：
    与 P7 ``/ai/chat`` 的处理一致——「继续聊」按钮不该因为一次超时关闭就永久失效。

    Raises:
        AppException: 设备不可见（404 ``DEVICE_NOT_FOUND``）或会话不属于该设备 /
            该用户（404）。
    """
    device = await miniapp_service.assert_device_owned(session, ctx, device_id)
    tenant_id = device.tenant_id
    if tenant_id is None and device.client_product_id is None:
        # 绑定关系存在时设备必然有租户；这里只是防御「有人绕过服务层改库」
        raise not_found("设备不存在")

    if session_id:
        existing = (
            await session.execute(select(DialogueSession).where(DialogueSession.id == session_id))
        ).scalar_one_or_none()
        if (
            existing is None
            or existing.device_id != device.id
            or existing.end_user_id != ctx.user_id
        ):
            raise not_found("对话会话不存在")
        if existing.status == str(DialogueSessionStatus.CLOSED):
            existing.status = str(DialogueSessionStatus.ACTIVE)
            existing.ended_at = None
            existing.close_reason = None
        await session.commit()
        return device, existing

    dialogue = DialogueSession(
        id=new_id("dialogue_session"),
        tenant_id=tenant_id,
        client_product_id=device.client_product_id,
        device_id=device.id,
        end_user_id=ctx.user_id,
        role_preset_code=None,
        status=str(DialogueSessionStatus.ACTIVE),
        started_at=utcnow(),
    )
    session.add(dialogue)
    await session.flush()
    await session.commit()
    logger.info("终端用户 %s 在设备 %s 上开启对话会话 %s", ctx.phone, device.sn, dialogue.id)
    return device, dialogue


async def close_dialogue(
    session: AsyncSession,
    dialogue: DialogueSession,
    *,
    reason: str | None = None,
) -> None:
    """关闭会话（幂等；已关闭时直接返回）。

    ``request`` 不是本函数的参数：关闭会话**不写审计**——它是一次连接的
    正常收尾，不是需要留痕的业务动作（真正要追的是「谁把设备绑走了」，
    那由绑定 / 激活 / 解绑的审计覆盖）。签名里放一个用不到的 ``request``
    只会让人以为这里也写了审计。

    厂商侧 ``close_session`` 是**尽力而为**：它失败（未配置密钥 / 适配器不支持）
    不影响关闭本身的语义——会话已经结束这个事实由数据库固化，
    不该因为一个厂商调用而让客户端看到「关闭失败」。
    真实厂商的连接回收应另外依赖其会话超时机制，而不是指望客户端一定发
    ``session.close``（断网时它根本发不出来）。
    """
    if str(dialogue.status) == str(DialogueSessionStatus.CLOSED):
        return
    dialogue.status = str(DialogueSessionStatus.CLOSED)
    dialogue.ended_at = utcnow()
    dialogue.close_reason = reason

    registry.ensure_default_registry()
    try:
        resolved = await registry.resolve_for_product(
            session, client_product_id=dialogue.client_product_id
        )
        if resolved is not None:
            await resolved.provider.close_session(session_id=dialogue.id, reason=reason)
    except (AppException, NotImplementedError) as exc:
        logger.info("会话 %s 的厂商侧关闭被跳过：%s", dialogue.id, exc)

    await session.commit()
    logger.info("对话会话 %s 已关闭（原因：%s）", dialogue.id, reason or "客户端主动关闭")


# ---------------------------------------------------------------------------
# 消息落库
# ---------------------------------------------------------------------------


async def _next_seq(session: AsyncSession, session_id: str) -> int:
    """会话内下一个消息序号（从 1 开始）。

    ``dialogue_messages`` 上有 ``(session_id, seq)`` 唯一约束，序号必须由
    「当前最大值 + 1」推出而不是自增列：序号是**会话内**的，跨会话共用序列
    会让前端看到的 ``seq`` 出现空洞，也无法据此判断有没有漏消息。
    """
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
    """追加一条对话消息，并同步会话的聚合字段（分块数用于验证「真的分多块」）。"""
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


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


async def _record_vendor_failure(
    session: AsyncSession,
    resolved: ResolvedProvider,
    *,
    ctx: EndUserContext,
    exc: AppException,
    dialogue: DialogueSession,
    request: Request | None,
) -> None:
    """写入 ``VENDOR_CALL_FAILED`` 审计并**单独提交**。

    单独提交的理由与 P7 一致：这条审计是「安全拒绝 / 调用失败」的证据，
    若与业务事务绑在一起，业务回滚会把证据一起丢掉。
    """
    provider = resolved.provider
    await audit_service.record(
        session,
        action=AuditAction.VENDOR_CALL_FAILED,
        actor_account=ctx.phone,
        actor_role=miniapp_service.END_USER_ACTOR_ROLE,
        tenant_id=dialogue.tenant_id,
        resource_type="ai_provider",
        resource_id=provider.code,
        summary=f"终端用户对话时厂商调用失败：{exc.message}",
        detail={
            "code": str(exc.code),
            "providerCode": provider.code,
            "layer": resolved.layer,
            "sessionId": dialogue.id,
            "ok": False,
        },
        success=False,
        request=request,
    )
    await session.commit()


async def _record_content_blocked(
    session: AsyncSession,
    *,
    ctx: EndUserContext,
    device: Device,
    dialogue: DialogueSession,
    hit_provider_safety: bool,
    rewritten: bool,
    request: Request | None,
) -> None:
    """写入 ``CONTENT_BLOCKED`` 审计（与助手消息同一事务）。

    ``detail`` 里**只记命中类型与处置方式，不记具体敏感词**：
    审计日志里的敏感词本身就是风险源（运营、客服、日志聚合系统都会看到），
    排查时按 ``safetyFlag`` 定位到消息即可看到上下文。
    """
    await audit_service.record(
        session,
        action=AuditAction.CONTENT_BLOCKED,
        actor_account=ctx.phone,
        actor_role=miniapp_service.END_USER_ACTOR_ROLE,
        tenant_id=dialogue.tenant_id,
        resource_type="dialogue_session",
        resource_id=dialogue.id,
        summary="内容安全命中：已按安全话术下发，原文未出站",
        detail={
            "deviceId": device.id,
            "sn": device.sn,
            "sessionId": dialogue.id,
            "safetyFlag": SAFETY_FLAG_KEYWORD,
            "providerSafety": hit_provider_safety,
            "rewritten": rewritten,
        },
        request=request,
    )


# ---------------------------------------------------------------------------
# 一次对话轮次
# ---------------------------------------------------------------------------


async def stream_turn(
    session: AsyncSession,
    ctx: EndUserContext,
    device: Device,
    dialogue: DialogueSession,
    *,
    text: str | None = None,
    audio: bytes | None = None,
    request: Request | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """处理一次对话轮次，产出服务端帧。

    Args:
        session: 数据库会话（与调用方共用，事务边界由本函数控制）。
        ctx: 终端用户上下文。
        device: 设备对象（本函数会**重新校验一次归属**，见下）。
        dialogue: 已打开的会话。
        text: 文本输入（与 ``audio`` 二选一）。
        audio: 音频输入（走 ASR）。
        request: 用于审计里的客户端 IP / UA。

    Yields:
        服务端帧（``dict``），WS 与 SSE 直接序列化即用。

    Note:
        **每一轮都重新校验设备归属**：WS 是一条长连接，用户完全可能在连接
        存续期间解绑了设备（换了手机、把玩具转给了别人）。
        只在 ``session.open`` 时校验一次，会让这条连接在解绑后继续可用，
        那是一个真实的越权窗口。每次多一次按主键的查询，代价可接受。

    校验顺序（顺序即错误码优先级）：

    1. 设备归属（404）；
    2. 供应商能力域 + 密钥（503 ``VENDOR_UNAVAILABLE``，ADR-07）；
    3. 音频输入先过 ASR（厂商失败同样以 ``error`` 帧收尾）；
    4. 会话消息上限（``DIALOGUE_SESSION_MESSAGE_LIMIT``）；
    5. 内容安全（在 ``assistant.delta`` **之前**判定，见模块 docstring）。
    """
    try:
        device = await miniapp_service.assert_device_owned(session, ctx, device.id)
    except AppException as exc:
        yield frame_error(exc)
        return

    if text is None and audio is None:
        yield frame_error(validation_error("缺少对话输入"))
        return

    resolved = await miniapp_service.resolve_device_provider(session, device)
    provider = resolved.provider

    # ---- 2. 能力域 + 密钥：必须在产出任何 delta 之前完成 ----
    try:
        await miniapp_service.ensure_vendor_capability(
            session, resolved, capability=CAP_DIALOGUE, ctx=ctx, request=request
        )
    except AppException as exc:
        yield frame_error(exc)
        return

    # ---- 3. 音频轮次先转写 ----
    is_audio = audio is not None
    user_text = (text or "").strip()
    if audio is not None:
        try:
            if not provider.supports(CAP_ASR):
                raise provider_unavailable_error(provider, CAP_ASR)
            transcription = await provider.asr(audio=audio, context={"deviceId": device.id})
        except AppException as exc:
            await _record_vendor_failure(
                session, resolved, ctx=ctx, exc=exc, dialogue=dialogue, request=request
            )
            yield frame_error(exc)
            return
        user_text = (transcription.text or "").strip()
        yield frame_asr_partial(user_text)

    if not user_text:
        yield frame_error(validation_error("没有识别到有效内容，请再说一次"))
        return

    # ---- 4. 会话消息上限 ----
    if dialogue.message_count >= settings.DIALOGUE_SESSION_MESSAGE_LIMIT:
        # 先关会话再报错：否则客户端会一直重试并一直撞上限
        await close_dialogue(session, dialogue, reason=CLOSE_REASON_LIMIT)
        yield frame_error(
            AppException(
                ErrorCode.VALIDATION_ERROR,
                f"本次对话已达 {settings.DIALOGUE_SESSION_MESSAGE_LIMIT} 条消息上限，请重新开始会话",
                details={"limit": settings.DIALOGUE_SESSION_MESSAGE_LIMIT},
            )
        )
        return

    user_seq = await _next_seq(session, dialogue.id)
    _append_message(
        session,
        dialogue,
        seq=user_seq,
        role=MessageRole.USER,
        content=user_text,
        # 音频输入落库的是 ASR 转写文本：存 base64 音频既无检索价值，
        # 也会让消息表迅速膨胀；``contentType`` 保留了「这轮是语音」的事实
        content_type=MessageContentType.AUDIO if is_audio else MessageContentType.TEXT,
        provider_code=provider.code,
    )
    await session.commit()

    # ---- 5. 流式回复 ----
    safety_on = (
        settings.CONTENT_SAFETY_AUDIO if is_audio else settings.CONTENT_SAFETY_TEXT
    )
    started = utcnow()
    parts: list[str] = []
    emitted_chunks = 0
    first_delta_ms: int | None = None
    finish_reason: str | None = None
    vendor_message_id: str | None = None

    try:
        async for chunk in provider.chat(
            session_id=dialogue.id,
            message=ChatMessage(content=user_text, role=str(MessageRole.USER)),
            role_preset=dialogue.role_preset_code,
            context={"device_id": device.id},
        ):
            if first_delta_ms is None and chunk.delta:
                first_delta_ms = int((utcnow() - started).total_seconds() * 1000)
            parts.append(chunk.delta)
            if chunk.is_final:
                finish_reason = chunk.finish_reason
                vendor_message_id = chunk.message_id
            if not safety_on:
                # 关闭安全开关时走真流式：边收边发，不缓存
                emitted_chunks += 1
                yield frame_delta(chunk.delta, chunk.index)
    except AppException as exc:
        await _record_vendor_failure(
            session, resolved, ctx=ctx, exc=exc, dialogue=dialogue, request=request
        )
        yield frame_error(exc)
        return
    except Exception as exc:
        logger.exception("对话流式生成异常：session=%s", dialogue.id)
        yield frame_error(
            AppException(ErrorCode.INTERNAL_ERROR, f"对话中断：{type(exc).__name__}")
        )
        return

    raw_reply = "".join(parts)

    # ---- 5.1 内容安全：在 delta 之前判定（见模块 docstring） ----
    out_text = raw_reply
    safety_flag: str | None = None
    hit_provider_safety = finish_reason == "safety"
    rewritten = False
    if safety_on:
        hit_word = detect_safety(raw_reply)
        if hit_word is not None or hit_provider_safety:
            safety_flag = SAFETY_FLAG_KEYWORD
            if hit_word is not None:
                # 我们自己检出了敏感词 → 整段替换，原文绝不进入任何出站帧
                out_text = safety_reply(hit_word)
                rewritten = True
            # 仅厂商侧标记安全时保留厂商文案：它已经是安全话术，
            # 再替换一次只会丢掉更贴合的措辞（mock 引擎的兜底话术就是如此）
        for index, piece in enumerate(chunk_text(out_text, chunk_size=SAFETY_RECHUNK_SIZE)):
            emitted_chunks += 1
            yield frame_delta(piece, index)
    elif finish_reason == "safety":
        # 开关关闭：**不过滤**，但仍如实记录厂商标记出来的安全事件，
        # 否则「关了开关之后系统里发生过什么」将完全不可考
        safety_flag = SAFETY_FLAG_KEYWORD

    # ---- 5.2 回复音频（尽力而为，失败不影响文本回复） ----
    if provider.supports(CAP_TTS):
        try:
            speech = await provider.tts(text=out_text)
        except (AppException, NotImplementedError) as exc:
            logger.info("TTS 不可用，本次仅返回文本：%s", exc)
            speech = None
        if speech is not None and speech.audio:
            encoded = encode_audio(speech.audio)
            if encoded:
                yield frame_audio(encoded, speech.content_type)

    total_ms = int((utcnow() - started).total_seconds() * 1000)
    if safety_flag is not None:
        await _record_content_blocked(
            session,
            ctx=ctx,
            device=device,
            dialogue=dialogue,
            hit_provider_safety=hit_provider_safety,
            rewritten=rewritten,
            request=request,
        )
    assistant = _append_message(
        session,
        dialogue,
        seq=user_seq + 1,
        role=MessageRole.ASSISTANT,
        content=out_text,
        latency_ms=total_ms,
        provider_code=provider.code,
        provider_message_id=vendor_message_id,
        chunk_count=emitted_chunks,
        safety_flag=safety_flag,
    )
    await session.commit()

    yield frame_done(
        message_id=assistant.id,
        session_id=dialogue.id,
        latency_ms=first_delta_ms if first_delta_ms is not None else total_ms,
        total_latency_ms=total_ms,
        safety_flag=safety_flag,
    )


__all__ = [
    "CLOSE_REASON_LIMIT",
    "SAFETY_RECHUNK_SIZE",
    "close_dialogue",
    "frame_asr_partial",
    "frame_audio",
    "frame_delta",
    "frame_done",
    "frame_error",
    "frame_ready",
    "open_dialogue",
    "stream_turn",
]
