"""设备心跳服务：在线窗口的**写入侧**。

在线判据的两个来源（与 :mod:`app.services.device_service` 的读取侧配套）
----------------------------------------------------------------------
``devices.online_status`` 是落库的**投影**，``devices.last_heartbeat_at`` 是
事实。展示 / 筛选一律以「心跳是否落在 ``ONLINE_WINDOW_SECONDS``（默认 180 秒）
窗口内」为准（:func:`app.services.device_service.is_online`），
本模块负责把这两个字段维护成**互相一致**的状态：

* 真实心跳 → 心跳时间置为当前，投影从 ``NEVER_ONLINE`` / ``OFFLINE`` 推到 ``ONLINE``；
* 平台模拟掉线 → 把心跳时间挪到窗口之外**并**把投影置为 ``OFFLINE``。
  只改投影不改心跳时间，会出现「``onlineStatus=OFFLINE`` 但 ``online=true``」
  的自相矛盾（派生判据只看心跳时间），这正是模块 docstring 里说的
  「两个来源必须一致」。

为什么不写审计、不加幂等键
--------------------------
心跳是**每 30 秒一次**的高频、无人工决策的写入：写审计会把真正的操作淹掉
（审计的价值在于「谁在什么时候做了什么决定」），而幂等键对天然可重复的
心跳没有任何意义——同一秒内重发两次的结果完全一样。
逐次留痕由 ``device_events`` 承担，但**只在在线状态真的变化时**写一条，
否则时间线会被「仍然是 ONLINE」的噪声填满。

设备侧鉴权
----------
设备不持有平台 / 商户的 JWT，它持有的是平台签发的一次性
``DEVICE_SECRET``（见 ``POST /platform/devices/{id}/credentials``）。
库内只存摘要，因此校验走 :func:`hmac.compare_digest`（常量时间）。
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import AuthContext
from app.core.errors import (
    device_frozen,
    device_not_available,
    device_not_found,
    unauthenticated,
)
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.device import Device, DeviceCredential
from app.models.enums import AssetStatus, AuditAction, CredentialType, OnlineStatus
from app.schemas.device import DeviceHeartbeatResponse
from app.services import audit_service, device_service

logger = get_logger(__name__)


def _digest(secret: str) -> str:
    """设备密钥摘要（与签发侧同一算法：SHA-256 十六进制）。"""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _to_response(
    device: Device, *, simulated: bool, now: datetime
) -> DeviceHeartbeatResponse:
    """组装心跳响应（含在线窗口参数，便于固件侧自适应上报间隔）。"""
    return DeviceHeartbeatResponse(
        device_id=device.id,
        sn=device.sn,
        online=device_service.is_online(device),
        online_status=device.online_status,
        # 两个分支都会写入心跳时间，``or now`` 只是给静态类型一个确定的取值
        last_heartbeat_at=device.last_heartbeat_at or now,
        online_window_seconds=settings.ONLINE_WINDOW_SECONDS,
        heartbeat_interval_seconds=settings.HEARTBEAT_INTERVAL_SECONDS,
        server_time=now,
        simulated=simulated,
    )


async def _load_credential(session: AsyncSession, device: Device) -> DeviceCredential:
    """取出设备的 ``DEVICE_SECRET`` 并校验其可用性（不含密钥比对）。

    Raises:
        AppException: 未签发 / 已吊销 / 已过期（``UNAUTHENTICATED``）。
            统一用 401：对设备而言「密钥不可用」就是未通过认证，
            分成多个码只会让固件侧多写几个分支却无能为力。
    """
    credential = (
        await session.execute(
            select(DeviceCredential).where(
                DeviceCredential.device_id == device.id,
                DeviceCredential.credential_type == str(CredentialType.DEVICE_SECRET),
            )
        )
    ).scalar_one_or_none()
    if credential is None:
        raise unauthenticated("设备未签发密钥，请联系平台管理员签发设备密钥后重试")
    if credential.revoked_at is not None:
        raise unauthenticated("设备密钥已失效")
    if credential.expires_at is not None and credential.expires_at < utcnow():
        raise unauthenticated("设备密钥已失效")
    return credential


async def record_heartbeat(
    session: AsyncSession,
    *,
    sn: str,
    secret: str,
    firmware_version: str | None = None,
    actor: AuthContext | None = None,
    request: Request | None = None,
    simulated: bool = False,
) -> DeviceHeartbeatResponse:
    """记录一次心跳（真实设备上报；``simulated=True`` 表示平台代为上报）。

    判定顺序：设备存在 → 已签发密钥且未失效 → 密钥摘要匹配 →
    未报废 → 未冻结。**先认证再判状态**：状态信息（是否冻结 / 报废）
    对未通过认证的调用方也是一种泄漏。

    ``firmware_version`` 非空时回写设备——固件版本只有设备自己知道，
    心跳是唯一不需要额外接口就能同步它的时机。

    Raises:
        AppException: ``DEVICE_NOT_FOUND`` / ``UNAUTHENTICATED`` /
            ``DEVICE_NOT_AVAILABLE``（已报废）/ ``DEVICE_FROZEN``。
    """
    device = (
        await session.execute(select(Device).where(Device.sn == (sn or "").strip()))
    ).scalar_one_or_none()
    if device is None:
        raise device_not_found("设备不存在")

    credential = await _load_credential(session, device)
    if not hmac.compare_digest(_digest(secret), credential.secret_hash or ""):
        raise unauthenticated("设备密钥校验失败")

    if str(device.asset_status) == str(AssetStatus.RETIRED):
        raise device_not_available(f"设备 {device.sn} 已报废，不再接受心跳")
    if str(device.asset_status) == str(AssetStatus.FROZEN):
        raise device_frozen(f"设备 {device.sn} 已冻结")

    now = utcnow()
    from_status = device.online_status
    device.last_heartbeat_at = now
    if device.online_status != str(OnlineStatus.ONLINE):
        device.online_status = str(OnlineStatus.ONLINE)
    credential.last_used_at = now
    if firmware_version:
        device.firmware_version = firmware_version

    if from_status != device.online_status:
        await device_service.record_event(
            session,
            device,
            event_type=device_service.EVENT_ONLINE,
            from_status=from_status,
            to_status=device.online_status,
            actor=actor,
            summary="设备心跳上报，转为在线",
            request=request,
        )
    await session.flush()
    await session.commit()
    logger.info("设备 %s 心跳已记录（%s → %s）", device.sn, from_status, device.online_status)
    return _to_response(device, simulated=simulated, now=now)


async def simulate_heartbeat(
    session: AsyncSession,
    auth: AuthContext,
    device: Device,
    *,
    online: bool = True,
    firmware_version: str | None = None,
    request: Request | None = None,
) -> DeviceHeartbeatResponse:
    """平台模拟心跳 / 模拟掉线（演示与验收用，`simulated=true`）。

    为什么需要它？「超过 180 秒窗口即判定离线」如果只能靠真实等待来验证，
    验收就无法自动化（等三分钟才能看到一个状态变化）。

    ``online=False`` 时把 ``last_heartbeat_at`` 挪到窗口之外（``cutoff - 1s``），
    而不是只改 ``online_status``：派生判据（:func:`device_service.is_online`）
    只看心跳时间，只改投影会留下「``onlineStatus=OFFLINE`` 但 ``online=true``」
    的矛盾状态。把心跳时间退到窗口外，等价于「模拟时间流逝」，
    两个来源随之自然一致。

    Raises:
        AppException: 设备已报废 / 已冻结（``DEVICE_NOT_AVAILABLE`` / ``DEVICE_FROZEN``）。
    """
    if str(device.asset_status) == str(AssetStatus.RETIRED):
        raise device_not_available(f"设备 {device.sn} 已报废，不再接受心跳")
    if str(device.asset_status) == str(AssetStatus.FROZEN):
        raise device_frozen(f"设备 {device.sn} 已冻结")

    now = utcnow()
    from_status = device.online_status
    if online:
        device.last_heartbeat_at = now
        device.online_status = str(OnlineStatus.ONLINE)
        event_type = device_service.EVENT_ONLINE
        summary = "平台模拟心跳上报"
    else:
        device.last_heartbeat_at = device_service.online_cutoff() - timedelta(seconds=1)
        device.online_status = str(OnlineStatus.OFFLINE)
        event_type = device_service.EVENT_OFFLINE
        summary = "平台模拟掉线（最后一次心跳已超出在线窗口）"
    if firmware_version:
        device.firmware_version = firmware_version

    if from_status != device.online_status:
        await device_service.record_event(
            session,
            device,
            event_type=event_type,
            from_status=from_status,
            to_status=device.online_status,
            actor=auth,
            summary=summary,
            request=request,
        )
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.SIMULATE_HEARTBEAT,
        actor=auth,
        resource_type="device",
        resource_id=device.id,
        summary=(
            f"模拟设备 {device.sn} 心跳：{'在线' if online else '离线'}"
            f"（{from_status} → {device.online_status}）"
        ),
        detail={
            "deviceId": device.id,
            "sn": device.sn,
            "online": online,
            "fromStatus": from_status,
            "toStatus": device.online_status,
            "simulated": True,
        },
        request=request,
    )
    await session.commit()
    logger.info(
        "已模拟设备 %s 心跳：%s（%s → %s）",
        device.sn,
        "在线" if online else "离线",
        from_status,
        device.online_status,
    )
    return _to_response(device, simulated=True, now=now)


__all__ = [
    "record_heartbeat",
    "simulate_heartbeat",
]
