"""终端用户小程序端 API（P8）。

端点一览
--------
==========================================  ==========  ==============================
端点                                         权限        说明
==========================================  ==========  ==============================
``POST /miniapp/auth/code``                  公开        发送登录验证码
``POST /miniapp/auth/login``                 公开        验证码登录（回带设备列表）
``GET  /miniapp/profile``                    终端用户    个人资料
``POST /miniapp/scan/resolve``               终端用户    扫码解析（含可绑判定）
``POST /miniapp/devices/{id}/activate-4g``   终端用户    4G 激活 + 绑定
``POST /miniapp/devices/{id}/activate-wifi`` 终端用户    Wi-Fi 配网激活 + 绑定
``GET  /miniapp/devices``                    终端用户    我的设备
``GET  /miniapp/devices/{id}``               终端用户    设备详情
``POST /miniapp/devices/{id}/unbind``        终端用户    解绑
``GET  /miniapp/devices/{id}/settings``      终端用户    读取设备设置
``PUT  /miniapp/devices/{id}/settings``      终端用户    更新设备设置
``GET  /miniapp/recharge/plans``             终端用户    流量套餐（仅 4G）
``POST /miniapp/recharge/orders``            终端用户    充值下单
``POST /miniapp/recharge/orders/{id}/pay``   终端用户    支付（mock 通道）
``GET  /miniapp/recharge/orders``            终端用户    充值记录（分页）
``POST /miniapp/chat/stream``                终端用户    对话 SSE 降级流
==========================================  ==========  ==============================

WS 不在这里
-----------
``/ws/miniapp/chat`` 挂在 :mod:`app.realtime.ws_chat`：WebSocket 不属于
``/api/v1`` 前缀体系（它没有 HTTP 语义，也没有 status code / 响应模型），
塞进本文件会让「HTTP 契约」与「帧契约」混在一起。挂载方式见 ``app.main``。

鉴权
----
除 ``auth/code`` 与 ``auth/login`` 外全部依赖 :data:`app.core.deps.EndUserAuth`。
它与管理端的 ``AuthContext`` 是**两个不同的类型**：

* 终端用户令牌 ``type="end_user"``，管理端令牌 ``type="access"``，
  双向都会被解签阶段拒绝（不是靠某个业务分支记得检查）；
* 终端用户没有角色 / 权限码 / 租户，可见范围收口在
  :func:`app.services.miniapp_service.assert_device_owned`（绑定关系），
  而不是 ``app.db.scope`` 的租户过滤。理由见该模块 docstring。

为什么流式对话这里用 SSE 而不是 NDJSON
--------------------------------------
P7 的 ``/ai/chat`` 用 NDJSON（理由见 ``app.api.v1.ai`` 的模块 docstring）。
小程序端额外提供 SSE，是因为**浏览器端的 ``EventSource`` 只认 SSE**：
小程序 / H5 页面在没有 WebSocket 的降级场景下，需要一个浏览器能直接消费的
流式端点。设备固件与调试台仍走 NDJSON（结构与 WS 帧同构），三者字段同名。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.core.deps import DbSession, EndUserAuth
from app.core.logging import get_logger
from app.core.pagination import PageParams, page_params, page_response
from app.schemas.common import ListResult
from app.schemas.miniapp import (
    ActivateWifiRequest,
    ActivationResult,
    AuthCodeRequest,
    AuthCodeResponse,
    AuthLoginRequest,
    AuthLoginResponse,
    ChatStreamRequest,
    DeviceSettingsResponse,
    DeviceSettingsUpdateRequest,
    MiniappDeviceBrief,
    MiniappDeviceDetail,
    MiniappProfileResponse,
    RechargeOrderCreateRequest,
    RechargeOrderResponse,
    RechargePayResult,
    RechargePlanListResult,
    ScanResolveRequest,
    ScanResolveResponse,
    UnbindRequest,
    UnbindResult,
)
from app.services import dialogue_service, miniapp_service

logger = get_logger(__name__)

router = APIRouter(prefix="/miniapp", tags=["小程序 · 终端用户"])

#: 分页参数依赖（与其余各端同一契约）：
#: ``?page=1&pageSize=20`` → ``{records, total, page, pageSize, totalPages}``
PageQuery = Annotated[PageParams, Depends(page_params)]

#: SSE 响应头。禁用缓冲：反代（nginx）默认会攒够缓冲再下发，
#: 那样「流式」在用户看来变成「一次性出现」，与不流式毫无区别。
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
SSE_MEDIA_TYPE = "text/event-stream"

#: 路径参数中的设备 ID / 订单 ID 的长度上限（与 ``String(36)`` 主键一致）
_ID_MAX_LENGTH = 36


def _sse_event(frame: dict[str, Any]) -> bytes:
    """把一个帧序列化为 SSE 事件（``data: {json}\\n\\n``）。

    帧本身是 JSON 对象时，SSE 的 ``data:`` 前缀与空行分帧是唯一需要
    额外处理的部分——事件类型不另用 ``event:`` 字段，
    因为 ``type`` 已经在 JSON 体内（前端用一个解析路径即可）。
    """
    return f"data: {json.dumps(frame, ensure_ascii=False)}\n\n".encode()


# ===========================================================================
# 一、认证（公开）
# ===========================================================================


@router.post(
    "/auth/code",
    response_model=AuthCodeResponse,
    summary="发送登录验证码",
    description=(
        "手机号格式不合法返回 400 `VALIDATION_ERROR`。\n\n"
        "`MINIAPP_SMS_PROVIDER=none` 时返回 503 `VENDOR_UNAVAILABLE`"
        "（**安全失败**，ADR-07）——通道没接就明确拒绝，而不是「假装发了」。\n\n"
        "`mock` 通道下验证码在 `mockCode` 回显且 `mock=true`（开发 / 验收环境），"
        "生产环境禁止该取值（启动期即拒绝）。首次使用某手机号会自动创建账号（幂等）。"
    ),
    responses={
        400: {"description": "手机号格式不合法"},
        403: {"description": "账号已被禁用"},
        503: {"description": "短信通道未接入"},
    },
)
async def send_login_code(
    request: Request,
    session: DbSession,
    payload: AuthCodeRequest = Body(...),
) -> AuthCodeResponse:
    """发送登录验证码。"""
    return await miniapp_service.send_login_code(session, phone=payload.phone, request=request)


@router.post(
    "/auth/login",
    response_model=AuthLoginResponse,
    summary="验证码登录",
    description=(
        "校验顺序即错误码优先级：无待验证码 / 已过期 → 409 `QR_EXPIRED`；"
        "验证码不匹配 → 400 `VALIDATION_ERROR`（`details.attemptsLeft` 给出剩余次数）；"
        "连续错误达到 `MINIAPP_LOGIN_MAX_ATTEMPTS` → **作废该验证码**并返回 400。\n\n"
        "成功即签发终端用户令牌（`type=end_user`，与后台令牌双向隔离），"
        "并**回带该用户的设备列表**——小程序登录后直接进首页，不需要再发一次请求。"
    ),
    responses={
        400: {"description": "验证码不正确或已作废"},
        403: {"description": "账号已被禁用"},
        409: {"description": "验证码已过期，请重新获取"},
    },
)
async def login(
    request: Request,
    session: DbSession,
    payload: AuthLoginRequest = Body(...),
) -> AuthLoginResponse:
    """验证码登录。"""
    return await miniapp_service.login(
        session, phone=payload.phone, code=payload.code, request=request
    )


# ===========================================================================
# 二、资料与扫码
# ===========================================================================


@router.get(
    "/profile",
    response_model=MiniappProfileResponse,
    summary="个人资料",
)
async def get_profile(session: DbSession, ctx: EndUserAuth) -> MiniappProfileResponse:
    """终端用户资料（含已绑定设备数量）。"""
    return await miniapp_service.get_profile(session, ctx)


@router.post(
    "/scan/resolve",
    response_model=ScanResolveResponse,
    summary="扫码解析",
    description=(
        "解析二维码（`JX|SN|IMEI|ICCID|deviceId` 或 `JD|租户|产品|SN|签名`）"
        "并判定「能不能绑」。\n\n"
        "**不可绑不是错误**：已冻结 / 已报废 / 尚未分配 / 已被他人绑定都返回"
        "`bindable=false` + 中文 `reason` + 200，页面据此给出解释性文案。\n\n"
        "只有两种根本性错误返回 4xx：二维码格式非法（404 `QR_INVALID`）"
        "与 SN 不在设备库（404 `DEVICE_NOT_FOUND`）。\n\n"
        "**同一用户重复扫码** `bindable=true` 且 `reason` 提示已激活。\n\n"
        "与商户端 `precheck` 的区别：扫码者就是绑定者，因此**不需要确认令牌**。"
    ),
    responses={
        404: {"description": "二维码无效或设备不存在"},
    },
)
async def resolve_scan(
    session: DbSession,
    ctx: EndUserAuth,
    payload: ScanResolveRequest = Body(...),
) -> ScanResolveResponse:
    """扫码解析。"""
    return await miniapp_service.resolve_scan(session, ctx, payload=payload.payload)


# ===========================================================================
# 三、激活与设备
# ===========================================================================


@router.post(
    "/devices/{device_id}/activate-4g",
    response_model=ActivationResult,
    summary="4G 设备激活",
    description=(
        "`NOT_ACTIVATED → ACTIVATING →` 调集贤厂商 `activate_device` `→ ACTIVATED`，"
        "成功后**自动绑定**到当前终端用户。\n\n"
        "**厂商未配置密钥时退回 `NOT_ACTIVATED` 并返回 503 `VENDOR_UNAVAILABLE`**"
        "（ADR-07，绝不伪造成功）；退回是必要的，否则设备会永久卡在 `ACTIVATING`"
        "且用户无法重试。\n\n"
        "已激活的设备**幂等**返回既有结果并提示「已激活」，不重复调厂商、不重复绑定。\n\n"
        "设备不属于当前用户可见范围 → 404（不泄漏存在性）；不是 4G 设备 → 400。"
    ),
    responses={
        400: {"description": "该设备不是 4G 设备"},
        404: {"description": "设备不存在或不可见"},
        503: {"description": "厂商未接入或未配置密钥"},
    },
)
async def activate_4g(
    request: Request,
    session: DbSession,
    ctx: EndUserAuth,
    device_id: str,
) -> ActivationResult:
    """4G 激活。"""
    return await miniapp_service.activate_4g(session, ctx, device_id, request=request)


@router.post(
    "/devices/{device_id}/activate-wifi",
    response_model=ActivationResult,
    summary="Wi-Fi 设备配网激活",
    description=(
        "**先校验上报的 `sn` 与设备记录是否一致**，不一致即把 `activationStatus` "
        "置为 `BIND_FAILED`、写设备事件与审计，并返回 409 `BIND_FAILED`"
        "（`details` 带 `qrSn` / `reportedSn`）——这是防止"
        "「A 的二维码激活了 B 的设备」的关键一步。\n\n"
        "`mac` 与记录不符按同一口径处理；设备记录 MAC 为空时以本次上报为准写入"
        "（MAC 常在生产阶段才回填）。\n\n"
        "一致则调京东 JoyInside 激活，成功后自动绑定；已激活设备在 SN 校验**之前**"
        "幂等返回，避免把正常重试判成失败。"
    ),
    responses={
        400: {"description": "该设备不是 Wi-Fi 设备"},
        404: {"description": "设备不存在或不可见"},
        409: {"description": "上报 SN / MAC 与设备记录不一致（BIND_FAILED）"},
        503: {"description": "厂商未接入或未配置密钥"},
    },
)
async def activate_wifi(
    request: Request,
    session: DbSession,
    ctx: EndUserAuth,
    device_id: str,
    payload: ActivateWifiRequest = Body(...),
) -> ActivationResult:
    """Wi-Fi 配网激活。"""
    return await miniapp_service.activate_wifi(
        session,
        ctx,
        device_id,
        reported_sn=payload.sn,
        reported_mac=payload.mac,
        request=request,
    )


@router.get(
    "/devices",
    response_model=ListResult[MiniappDeviceBrief],
    summary="我的设备",
    description="当前终端用户已绑定的设备（含联网方式、四维状态与在线判据）。",
)
async def list_devices(
    session: DbSession, ctx: EndUserAuth
) -> ListResult[MiniappDeviceBrief]:
    """我的设备列表。"""
    rows = await miniapp_service.list_user_devices(session, ctx)
    records = [miniapp_service.to_device_brief(device, binding) for device, binding in rows]
    return ListResult[MiniappDeviceBrief](records=records, total=len(records))


@router.get(
    "/devices/{device_id}",
    response_model=MiniappDeviceDetail,
    summary="设备详情",
    description="含产品名 / 租户名 / 设备设置 / 绑定次数。设备不可见 → 404（不泄漏存在性）。",
    responses={404: {"description": "设备不存在或不属于当前用户"}},
)
async def get_device(
    session: DbSession, ctx: EndUserAuth, device_id: str
) -> MiniappDeviceDetail:
    """设备详情。"""
    device = await miniapp_service.assert_device_owned(session, ctx, device_id)
    binding = await miniapp_service.get_binding(session, device.id)
    return await miniapp_service.build_device_detail(session, device, binding)


@router.post(
    "/devices/{device_id}/unbind",
    response_model=UnbindResult,
    summary="解绑设备",
    description=(
        "解绑：`bindStatus → UNBOUND`，设备资产状态 `BOUND → ALLOCATED`"
        "（与商户端解绑同口径，**不退回平台库存**），写设备事件与审计。\n\n"
        "`activationStatus` **保持不变**：设备已经激活过，解绑只是解除归属——"
        "一起退回 `NOT_ACTIVATED` 会让下一位用户重复走厂商激活（会被厂商拒绝），"
        "造出一台「永远激活不了」的设备。\n\n"
        "`reason` 可省略（省略时写「终端用户主动解绑」）。"
    ),
    responses={
        404: {"description": "设备不存在或不属于当前用户"},
        409: {"description": "设备当前未绑定"},
    },
)
async def unbind_device(
    request: Request,
    session: DbSession,
    ctx: EndUserAuth,
    device_id: str,
    payload: UnbindRequest = Body(...),
) -> UnbindResult:
    """解绑设备。"""
    return await miniapp_service.unbind_device(
        session, ctx, device_id, reason=payload.reason, request=request
    )


# ===========================================================================
# 四、设备设置
# ===========================================================================


@router.get(
    "/devices/{device_id}/settings",
    response_model=DeviceSettingsResponse,
    summary="读取设备设置",
    description=(
        "返回 `volume` / `childMode` / `wakeWord` / `updatedAt`。\n\n"
        "`devices.settings` 为空时返回默认值（音量 60、儿童模式关、"
        "唤醒词「你好小伙伴」）——「没设置过」与「设置为默认值」对用户是同一件事。"
    ),
    responses={404: {"description": "设备不存在或不属于当前用户"}},
)
async def get_settings(
    session: DbSession, ctx: EndUserAuth, device_id: str
) -> DeviceSettingsResponse:
    """读取设备设置。"""
    device = await miniapp_service.assert_device_owned(session, ctx, device_id)
    return miniapp_service.to_settings_response(device)


@router.put(
    "/devices/{device_id}/settings",
    response_model=DeviceSettingsResponse,
    summary="更新设备设置",
    description=(
        "局部更新，未传的字段保持原值。**未知键静默忽略**（不是报错）："
        "客户端可能比服务端新，为一个它多传的字段让整次保存失败是净损失。\n\n"
        "`volume` 约束 0–100。\n\n"
        "设置项存在 `devices.settings`（JSON）而不是逐项开列：设置项会随型号与"
        "固件迭代，逐项开列意味着每加一项就要一次迁移；键白名单由服务层维护"
        "（`miniapp_service.DEVICE_SETTING_KEYS`），同样能防止客户端塞任意键进库。"
    ),
    responses={
        400: {"description": "音量超出 0–100"},
        404: {"description": "设备不存在或不属于当前用户"},
    },
)
async def update_settings(
    session: DbSession,
    ctx: EndUserAuth,
    device_id: str,
    payload: DeviceSettingsUpdateRequest = Body(...),
) -> DeviceSettingsResponse:
    """更新设备设置。"""
    # by_alias=True：白名单的键是 camelCase（与落库形状一致），
    # 这里统一在边界处转换，服务层不必同时认识两套键名
    updates = payload.model_dump(exclude_unset=True, by_alias=True)
    return await miniapp_service.update_device_settings(
        session, ctx, device_id, updates=updates
    )


# ===========================================================================
# 五、流量充值
# ===========================================================================


@router.get(
    "/recharge/plans",
    response_model=RechargePlanListResult,
    summary="流量套餐列表",
    description=(
        "**仅 4G 设备**：Wi-Fi 设备返回 `supported=false` + 说明文案 + 空数组，"
        "**不是错误**（「这台机器不需要流量充值」是正常结论，用 4xx 表达会逼前端"
        "把它写成异常分支）。\n\n"
        "套餐按 `sortOrder` 升序，只返回 `ENABLED` 且属于设备所属租户的档位。"
    ),
    responses={404: {"description": "设备不存在或不属于当前用户"}},
)
async def list_recharge_plans(
    session: DbSession,
    ctx: EndUserAuth,
    device_id: Annotated[str, Query(alias="deviceId", min_length=1, max_length=_ID_MAX_LENGTH)],
) -> RechargePlanListResult:
    """流量套餐列表。"""
    return await miniapp_service.list_recharge_plans(session, ctx, device_id=device_id)


@router.post(
    "/recharge/orders",
    response_model=RechargeOrderResponse,
    status_code=201,
    summary="充值下单",
    description=(
        "校验设备归属 / 4G / 套餐有效且属于同一租户，订单号 `RC-YYYYMMDD-XXXX`"
        "（随机后缀剔除 `0/O/1/I`，冲突重试 5 次），"
        "并把**金额与流量快照**到订单（套餐后续改价不改写历史订单），状态 `PENDING`。\n\n"
        "**同一设备已有 `PENDING` 订单时不拒绝**：用户「先下一单没付再下一单」"
        "在真实场景里很常见，拒绝只会逼他去翻未支付订单；多张未支付订单不产生资费影响"
        "（流量只在 `PAID` 后生效），真正要防的是「同一单被付两次」，那由支付接口的"
        "状态机挡住。"
    ),
    responses={
        400: {"description": "非 4G 设备或套餐已下架"},
        404: {"description": "设备不可见或套餐不存在"},
    },
)
async def create_recharge_order(
    request: Request,
    session: DbSession,
    ctx: EndUserAuth,
    payload: RechargeOrderCreateRequest = Body(...),
) -> RechargeOrderResponse:
    """充值下单。"""
    return await miniapp_service.create_recharge_order(
        session, ctx, device_id=payload.device_id, plan_id=payload.plan_id, request=request
    )


@router.post(
    "/recharge/orders/{order_id}/pay",
    response_model=RechargePayResult,
    summary="支付充值订单",
    description=(
        "`PAYMENT_PROVIDER=mock` 时把订单置为 `PAID` 并回 `mock=true`；"
        "`none` 时 503 `VENDOR_UNAVAILABLE`（安全失败，不改订单状态）。\n\n"
        "**只允许 `PENDING → PAID`**：重复支付、或对 `FAILED` / `REFUNDED` 订单支付"
        "一律 409 `INVALID_STATE_TRANSITION`——这是「同一单被扣两次钱」的最后一道闸。"
    ),
    responses={
        404: {"description": "订单不存在或不属于当前用户"},
        409: {"description": "订单状态不允许支付"},
        503: {"description": "支付通道未接入"},
    },
)
async def pay_recharge_order(
    request: Request,
    session: DbSession,
    ctx: EndUserAuth,
    order_id: str,
) -> RechargePayResult:
    """支付充值订单。"""
    return await miniapp_service.pay_recharge_order(session, ctx, order_id, request=request)


@router.get(
    "/recharge/orders",
    summary="充值记录",
    description="分页返回某台设备的充值订单（按创建时间倒序），`deviceId` 必填。",
    responses={404: {"description": "设备不存在或不属于当前用户"}},
)
async def list_recharge_orders(
    session: DbSession,
    ctx: EndUserAuth,
    page: PageQuery,
    device_id: Annotated[str, Query(alias="deviceId", min_length=1, max_length=_ID_MAX_LENGTH)],
) -> dict[str, Any]:
    """充值记录（分页）。"""
    records, total = await miniapp_service.list_recharge_orders(
        session, ctx, device_id=device_id, offset=page.offset, limit=page.limit
    )
    return page_response(records, total, page)


# ===========================================================================
# 六、对话（SSE 降级流）
# ===========================================================================


@router.post(
    "/chat/stream",
    summary="AI 对话（SSE 降级流）",
    description=(
        "与 WebSocket `/ws/miniapp/chat` 等价的降级端点："
        "返回 `text/event-stream`，每个事件形如 `data: {json}\\n\\n`，"
        "事件类型与 WS 帧同名（`session.ready` / `assistant.delta` / "
        "`assistant.audio` / `assistant.done` / `error`）。\n\n"
        "浏览器 `EventSource` 只认 SSE，因此小程序 / H5 在没有 WebSocket 时走这里；"
        "设备固件与调试台走 NDJSON 的 `/ai/chat`。三者字段同名，前端只需一套解析。\n\n"
        "会话与消息**落库**（`dialogue_sessions` / `dialogue_messages`）；"
        "内容安全在 `assistant.delta` 之前生效，命中时整段替换为安全话术，"
        "**原文不出站**，并写 `safetyFlag` 与 `CONTENT_BLOCKED` 审计。\n\n"
        "`sessionId` 为空则新开会话；续聊传入上次的会话 ID（不属于本设备 / 本人 → 404）。"
        "厂商未接入时以 `error` 帧报告（流已开始，无法再改 HTTP 状态码）。"
    ),
    responses={404: {"description": "设备不可见或会话不存在"}},
)
async def chat_stream(
    request: Request,
    session: DbSession,
    ctx: EndUserAuth,
    payload: ChatStreamRequest = Body(...),
) -> StreamingResponse:
    """AI 对话（SSE 降级流）。

    设备归属与会话的校验在**构造 ``StreamingResponse`` 之前**完成：
    那类失败可以走正常的 JSON 错误响应（404 + traceId），
    比塞进流里的 ``error`` 帧更利于前端统一处理。
    真正的流式错误（厂商不可用等）才用 ``error`` 帧。
    """
    device, dialogue = await dialogue_service.open_dialogue(
        session,
        ctx,
        device_id=payload.device_id,
        session_id=payload.session_id,
        request=request,
    )

    async def _generate() -> AsyncIterator[bytes]:
        """把服务层帧转成 SSE 事件。

        首帧固定为 ``session.ready``（与 WS 的 ``session.open`` 响应一致）：
        客户端要靠它拿到 ``sessionId`` 才能续聊；等到 ``assistant.done``
        才拿到会话 ID 意味着「第一轮无法指定会话」这种别扭的时序。
        """
        yield _sse_event(dialogue_service.frame_ready(dialogue.id))
        async for frame in dialogue_service.stream_turn(
            session, ctx, device, dialogue, text=payload.text, request=request
        ):
            yield _sse_event(frame)

    return StreamingResponse(_generate(), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS)


__all__ = ["router"]
