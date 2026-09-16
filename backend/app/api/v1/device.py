"""设备侧 API：心跳上报（P5）。

为什么不挂任何 JWT 依赖
----------------------
本模块的调用方是**玩具本体**，不是人：设备出厂时只有平台签发的一次性
``DEVICE_SECRET``，没有账号、登不了录，自然也不可能有平台 / 商户令牌。
因此这里**没有** ``require_platform`` / ``require_merchant`` / ``require_perm``——
一旦挂上 JWT 依赖，真实设备将永远上报不了心跳（这是「接口能跑通、
设备用不了」的典型形态）。

鉴权由 :func:`app.services.heartbeat_service.record_heartbeat` 里的
**密钥摘要比对**（常量时间 ``hmac.compare_digest``）完成；库内只存摘要，
明文仅在签发响应里出现一次。作为代价，该端点必须自己承担防爆破责任：
失败一律回 401 ``UNAUTHENTICATED``，不通过错误细节泄漏设备是否存在之外的信息。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Request

from app.core.deps import DbSession
from app.schemas.device import DeviceHeartbeatRequest, DeviceHeartbeatResponse
from app.services import heartbeat_service

router = APIRouter(prefix="/device", tags=["设备侧"])


@router.post(
    "/heartbeat",
    response_model=DeviceHeartbeatResponse,
    summary="设备心跳上报",
    description=(
        "设备定期上报心跳（建议间隔见响应里的 `heartbeatIntervalSeconds`）。\n\n"
        "**鉴权方式**：请求体携带 `sn` + `secret`，服务端比对 "
        "`sha256(secret)` 与库内摘要——设备不持有 JWT，因此本端点**不挂认证依赖**。"
        "密钥由平台在「设备详情 → 签发设备密钥」处一次性下发。\n\n"
        "**在线判定**：心跳时间与 `onlineWindowSeconds`（默认 180 秒）比较，"
        "超出窗口即视为离线；只有在线状态**真的变化**时才写一条设备事件，"
        "避免每 30 秒一条把时间线淹掉。\n\n"
        "**幂等**：心跳天然可重复，不需要幂等键。"
    ),
    responses={
        401: {"description": "设备未签发密钥 / 密钥已失效 / 密钥校验失败"},
        404: {"description": "设备不存在"},
        409: {"description": "设备已冻结（DEVICE_FROZEN）或已报废（DEVICE_NOT_AVAILABLE）"},
    },
)
async def report_heartbeat(
    request: Request,
    session: DbSession,
    payload: DeviceHeartbeatRequest = Body(...),
) -> DeviceHeartbeatResponse:
    """设备心跳上报（密钥鉴权，无 JWT）。"""
    return await heartbeat_service.record_heartbeat(
        session,
        sn=payload.sn,
        secret=payload.secret,
        firmware_version=payload.firmware_version,
        request=request,
    )


__all__ = ["router"]
