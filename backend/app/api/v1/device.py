"""设备侧 API：心跳上报（P5）+ 设备注册（Route B 后端代注册）。

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

import hashlib
import hmac

from fastapi import APIRouter, Body, Request
from sqlalchemy import select

from app.core.config import settings
from app.core.deps import DbSession
from app.core.errors import unauthenticated, vendor_unavailable
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.device import Device, DeviceCredential
from app.models.enums import CredentialType
from app.schemas.device import (
    DeviceHeartbeatRequest,
    DeviceHeartbeatResponse,
    DeviceRegisterRequest,
    DeviceRegisterResponse,
)
from app.services import device_register_service, heartbeat_service

logger = get_logger(__name__)

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


@router.post(
    "/register",
    response_model=DeviceRegisterResponse,
    summary="设备注册（后端代注册火山 device_secret）",
    description=(
        "设备首次上电时调用，用平台签发的 `sn` + `secret` 换取火山 `device_secret`。\n\n"
        "**鉴权方式**：与心跳一致，SN + 密钥摘要比对，不挂 JWT。\n\n"
        "**流程**：后端验证设备凭证 → 调用火山 `DynamicRegister` → "
        "解密得到 `device_secret` → 返回给设备。设备应将 `device_secret` "
        "持久化到 NVS，避免每次重启都重新注册。\n\n"
        "**幂等**：若设备已有 `device_secret`（NVS 缓存），建议直接使用，"
        "不必重复调用本接口。"
    ),
    responses={
        401: {"description": "设备未签发密钥 / 密钥校验失败"},
        404: {"description": "设备不存在"},
        503: {"description": "火山 IoT 凭证未配置（VOLCANO_IOT_*）"},
    },
)
async def register_device(
    request: Request,
    session: DbSession,
    payload: DeviceRegisterRequest = Body(...),
) -> DeviceRegisterResponse:
    """设备注册：用平台凭证换取火山 device_secret（Route B 后端代注册）。"""
    # 1. 查找设备
    device = (
        await session.execute(select(Device).where(Device.sn == (payload.sn or "").strip()))
    ).scalar_one_or_none()
    if device is None:
        raise unauthenticated("设备不存在")

    # 2. 校验密钥（与心跳同一逻辑）
    credential = (
        await session.execute(
            select(DeviceCredential).where(
                DeviceCredential.device_id == device.id,
                DeviceCredential.credential_type == str(CredentialType.DEVICE_SECRET),
            )
        )
    ).scalar_one_or_none()
    if credential is None:
        raise unauthenticated("设备未签发密钥")
    if credential.revoked_at is not None:
        raise unauthenticated("设备密钥已失效")
    if credential.expires_at is not None and credential.expires_at < utcnow():
        raise unauthenticated("设备密钥已失效")

    if not hmac.compare_digest(
        hashlib.sha256((payload.secret or "").encode("utf-8")).hexdigest(),
        credential.secret_hash or "",
    ):
        raise unauthenticated("设备密钥校验失败")

    # 3. 调用火山 DynamicRegister
    try:
        device_secret = await device_register_service.register_device_on_volcano(
            device_name=device.sn,
        )
    except Exception as exc:
        logger.error("设备 %s 注册失败: %s", device.sn, exc)
        raise vendor_unavailable(f"火山设备注册失败: {exc}") from exc

    # 4. 更新设备的 vendor_device_id（火山侧的 device_name）
    if not device.vendor_device_id:
        device.vendor_device_id = device.sn

    await session.flush()
    await session.commit()

    logger.info("设备 %s 注册成功，已获取 device_secret", device.sn)

    return DeviceRegisterResponse(
        device_id=device.id,
        sn=device.sn,
        device_secret=device_secret,
        instance_id=settings.VOLCANO_IOT_INSTANCE_ID,
        product_key=settings.VOLCANO_IOT_PRODUCT_KEY,
    )


__all__ = ["router"]
