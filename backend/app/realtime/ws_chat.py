"""终端用户 WebSocket 对话（P8）：帧协议的服务端实现。

挂载方式
--------
WebSocket **不属于** ``/api/v1`` 前缀体系：它没有 HTTP 状态码、没有响应模型、
没有 OpenAPI 文档，塞进 ``api_router`` 只会让「HTTP 契约」与「帧契约」混在一起。
因此本模块自带完整路径 ``/ws/miniapp/chat``，由 :mod:`app.main` 以
``app.include_router(router)`` 的方式直接挂上（与 ``api_router`` 挂载方式一致）。

令牌为什么走 query 而不是 header
--------------------------------
浏览器的 ``WebSocket`` 构造函数**不允许自定义请求头**（标准里只支持
``Sec-WebSocket-Protocol`` 与已废弃的 cookie 途径），因此
``Authorization: Bearer ...`` 这条既有通路在这里走不通。
可选的替代方案与取舍：

* **子协议携带**（``Sec-WebSocket-Protocol``）看起来更「正规」，
  但它必须出现在握手的响应头里，且部分反代/网关会改写成自己的一组子协议——
  在「浏览器 / 小程序 / 反代」三方都对齐前，故障面比 query 大得多；
* **首帧鉴权**（先连上再发 token）需要服务端接受未认证连接并维护超时清理，
  给了一个无鉴权连接占用资源的窗口；
* **query 参数**：与既有 ``/miniapp/auth/login`` 返回的令牌同一形态，
  前端一行就能拼出 URL，且令牌在 URL 里只出现在**服务端访问日志**中
  （本项目的日志不记录 query string 原文，见 ``app.core.logging``）。

结论：走 query。代价明确——令牌会进入反代 / 网关的访问日志，
因此生产环境应配合「日志脱敏 + 短有效期（``END_USER_TOKEN_EXPIRE_MINUTES``）」。

close code 语义（前端据此决定跳哪个页面）
----------------------------------------
* ``4401`` —— 令牌缺失 / 无效 / 过期 → 小程序应跳回登录页；
* ``4403`` —— 令牌有效但设备不属于该用户 → 提示「设备不属于当前账号」；
* ``4400`` —— 客户端发来了非法帧（未知 ``type`` / 非 JSON）→ 协议错误。

为什么先 ``accept()`` 再校验 token
----------------------------------
在 ``accept()`` **之前** ``close(code=4401)``，ASGI 服务器通常会把整个握手
拒绝掉（客户端只看到「连接失败」，拿不到我们想传达的 close code）。
先完成握手再按语义关闭，客户端才能读到 4401/4403 并做正确的跳转。
代价是多了一次极短的握手，可以忽略——连接建立后立即校验并关闭。

会话的生命周期
--------------
``session.open`` 建立会话（``session.ready`` 返回会话 ID），
``session.close`` 或**连接断开**时关闭会话并写 ``ended_at`` / ``close_reason``。
断开时也关会话，是因为玩具场景里客户端断线往往就是「孩子把玩具放下了」，
留着 ``ACTIVE`` 的会话会让「会话时长」这个指标长期虚高。

每一轮对话都会重新校验设备归属（见
:func:`app.services.dialogue_service.stream_turn` 的说明）：
长连接存续期间用户完全可能解绑设备，只在握手时校验会留下越权窗口。
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.deps import DbSession, EndUserContext
from app.core.errors import AppException, validation_error
from app.core.logging import get_logger
from app.core.security import decode_end_user_token
from app.models.ai import DialogueSession
from app.models.device import Device
from app.schemas.ai import decode_audio
from app.schemas.miniapp import ChatClientFrameType
from app.services import dialogue_service, miniapp_service

logger = get_logger(__name__)

#: WebSocket 子协议 / 路由不带前缀，路径即完整路径（见模块 docstring）
router = APIRouter(tags=["小程序 · 终端用户"])

#: 令牌缺失 / 无效 / 过期
WS_CLOSE_UNAUTHORIZED = 4401
#: 设备不属于当前终端用户
WS_CLOSE_FORBIDDEN = 4403
#: 客户端帧不符合协议（未知 type / 非 JSON 对象）
WS_CLOSE_PROTOCOL_ERROR = 4400

#: 连接断开时写入会话的关闭原因
CLOSE_REASON_DISCONNECTED = "客户端连接断开"
CLOSE_REASON_CLIENT = "客户端主动结束会话"


@router.websocket("/ws/miniapp/chat")
async def miniapp_chat(websocket: WebSocket, session: DbSession) -> None:
    """``/ws/miniapp/chat?token=<end_user_token>&deviceId=<id>``。

    帧协议见 :mod:`app.schemas.miniapp`（``SERVER_FRAME_FIELDS`` 是字段的唯一来源）
    与 :mod:`app.services.dialogue_service`（帧的实际构造）。

    Note:
        审计里的 ``clientIp`` 在本端点为 ``null``：``audit_service`` 从
        ``Request`` 取 IP，而 WebSocket 握手没有 ``Request`` 对象
        （``websocket.client`` 只有 host/port，没有 ``x-forwarded-for``）。
        与其伪造一个 ``Request``，不如让这个字段如实为空。
    """
    # 先握手再校验，否则客户端拿不到 4401 / 4403（见模块 docstring）
    await websocket.accept()

    token = (websocket.query_params.get("token") or "").strip()
    device_id = (websocket.query_params.get("deviceId") or "").strip()
    if not token or not device_id:
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return

    try:
        payload = decode_end_user_token(token)
    except AppException:
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return

    ctx = EndUserContext(user_id=payload.subject, phone=payload.phone)

    try:
        device: Device = await miniapp_service.assert_device_owned(session, ctx, device_id)
    except AppException:
        await websocket.close(code=WS_CLOSE_FORBIDDEN)
        return

    dialogue: DialogueSession | None = None
    logger.info("终端用户 %s 已连接对话 WS（设备 %s）", ctx.phone, device.sn)

    try:
        while True:
            try:
                raw_frame = await websocket.receive_json()
            except ValueError:
                # 非 JSON 文本：协议错误，但不断连接——客户端可能只是发了一帧脏数据，
                # 断开会让一次手滑毁掉整轮对话
                await websocket.send_json(
                    dialogue_service.frame_error(validation_error("帧不是合法的 JSON 对象"))
                )
                continue

            if not isinstance(raw_frame, dict):
                await websocket.send_json(
                    dialogue_service.frame_error(validation_error("帧必须是 JSON 对象"))
                )
                continue

            frame_type = str(raw_frame.get("type") or "")

            if frame_type == str(ChatClientFrameType.SESSION_CLOSE):
                if dialogue is not None:
                    await dialogue_service.close_dialogue(
                        session, dialogue, reason=CLOSE_REASON_CLIENT
                    )
                    dialogue = None
                break

            if frame_type == str(ChatClientFrameType.SESSION_OPEN):
                requested = raw_frame.get("sessionId")
                try:
                    device, dialogue = await dialogue_service.open_dialogue(
                        session,
                        ctx,
                        device_id=device_id,
                        session_id=str(requested) if requested else None,
                    )
                except AppException as exc:
                    await websocket.send_json(dialogue_service.frame_error(exc))
                    continue
                await websocket.send_json(dialogue_service.frame_ready(dialogue.id))
                continue

            if frame_type in (
                str(ChatClientFrameType.USER_TEXT),
                str(ChatClientFrameType.USER_AUDIO),
            ):
                # 宽容策略：客户端未先发 session.open 时自动补开
                # （理由见 dialogue_service 模块 docstring）
                if dialogue is None:
                    try:
                        device, dialogue = await dialogue_service.open_dialogue(
                            session, ctx, device_id=device_id
                        )
                    except AppException as exc:
                        await websocket.send_json(dialogue_service.frame_error(exc))
                        continue
                    await websocket.send_json(dialogue_service.frame_ready(dialogue.id))

                if frame_type == str(ChatClientFrameType.USER_TEXT):
                    text = str(raw_frame.get("text") or "").strip()
                    if not text:
                        await websocket.send_json(
                            dialogue_service.frame_error(validation_error("text 不能为空"))
                        )
                        continue
                    async for frame in dialogue_service.stream_turn(
                        session, ctx, device, dialogue, text=text
                    ):
                        await websocket.send_json(frame)
                else:
                    audio = decode_audio(str(raw_frame.get("data") or ""))
                    if audio is None:
                        await websocket.send_json(
                            dialogue_service.frame_error(
                                validation_error("audio.data 不是合法的 base64 音频")
                            )
                        )
                        continue
                    async for frame in dialogue_service.stream_turn(
                        session, ctx, device, dialogue, audio=audio
                    ):
                        await websocket.send_json(frame)
                continue

            await websocket.send_json(
                dialogue_service.frame_error(
                    validation_error(
                        f"不支持的帧类型：{frame_type or '空'}",
                        details={"supported": [str(item) for item in ChatClientFrameType]},
                    )
                )
            )
    except WebSocketDisconnect:
        logger.info("终端用户 %s 的对话 WS 已断开", ctx.phone)
    finally:
        if dialogue is not None:
            try:
                await dialogue_service.close_dialogue(
                    session, dialogue, reason=CLOSE_REASON_DISCONNECTED
                )
            except Exception:
                # 关闭失败不影响连接已经结束这一事实；记录后继续，
                # 不让清理阶段的异常盖掉真正的业务异常
                logger.exception("关闭对话会话 %s 失败", dialogue.id)


__all__: list[str] = ["router"]
