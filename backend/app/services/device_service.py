"""设备服务：四维状态迁移、查询、事件时间线。

三条贯穿本模块的规则
--------------------
1. **状态迁移只经 :data:`app.models.enums.ASSET_TRANSITIONS` 校验**。
   服务层不写 ``if/else`` 判断「能不能改」，因为那样规则会散落各处、
   前端与文档无法对齐；一张表就是唯一权威。
2. **每次变更都写一条 :class:`DeviceEvent`**（含维度、前后状态、操作者、
   ``traceId``）。设备详情的时间线直接读这张表，因此「忘了写事件」
   等价于「这次变更在系统里没发生过」——所以事件写入与状态变更在
   同一个事务里，不做「事后补记」。
3. **租户过滤一律经 :func:`app.db.scope.scoped`**（ADR-08），
   本模块不出现手写的 ``WHERE tenant_id = ...``。
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext
from app.core.errors import device_not_found, invalid_state_transition, validation_error
from app.core.ids import new_id
from app.core.logging import get_logger, get_trace_id
from app.db.base import utcnow
from app.db.scope import assert_visible, scoped
from app.models.device import Device, DeviceCredential, DeviceEvent
from app.models.enums import (
    ASSET_TRANSITIONS,
    FREEZABLE_ASSET_STATUSES,
    AssetStatus,
    AuditAction,
    derive_device_label,
)
from app.schemas.device import (
    DeviceBrief,
    DeviceCredentialBrief,
    DeviceDetailResponse,
    DeviceEventResponse,
    DeviceResponse,
)
from app.services import audit_service

logger = get_logger(__name__)

#: 设备列表允许的排序字段白名单。
#: 为什么要白名单而不是直接 ``getattr(model, sort_by)``？
#: ``sortBy`` 来自 query string，虽然 ``getattr`` 天然挡掉了 SQL 注入，
#: 但拼错字段会静默退回默认排序（用户以为排序生效了），报错反而更好。
ALLOWED_SORT_FIELDS: frozenset[str] = frozenset({"created_at", "sn", "generated_at"})

#: 设备详情里返回的最近事件条数
DETAIL_EVENT_LIMIT = 20

#: 事件类型常量（与 ``DeviceEvent.event_type`` 的取值词汇表一致）
EVENT_GENERATED = "GENERATED"
EVENT_IN_STOCK = "IN_STOCK"
EVENT_FROZEN = "FROZEN"
EVENT_THAWED = "THAWED"
EVENT_RETIRED = "RETIRED"
EVENT_IMPORTED = "IMPORTED"


# ---------------------------------------------------------------------------
# 转换
# ---------------------------------------------------------------------------


def device_label(device: Device) -> str:
    """派生设备展示标签（仅供展示，不参与业务判断）。"""
    return derive_device_label(
        device.asset_status,
        device.activation_status,
        device.online_status,
        device.bind_status,
    )


def to_response(device: Device) -> DeviceResponse:
    """ORM → 列表项响应。"""
    response = DeviceResponse.model_validate(device)
    response.label = device_label(device)
    return response


def to_brief(device: Device) -> DeviceBrief:
    """ORM → 摘要响应。"""
    return DeviceBrief(
        id=device.id,
        sn=device.sn,
        network_type=device.network_type,
        asset_status=device.asset_status,
        label=device_label(device),
    )


def event_to_response(event: DeviceEvent) -> DeviceEventResponse:
    """ORM → 事件响应。"""
    return DeviceEventResponse.model_validate(event)


def credential_to_brief(credential: DeviceCredential) -> DeviceCredentialBrief:
    """ORM → 凭证摘要（只有掩码）。"""
    return DeviceCredentialBrief.model_validate(credential)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


async def get_device(session: AsyncSession, auth: AuthContext, device_id: str) -> Device:
    """按 ID 取设备，并校验对当前上下文可见。

    Raises:
        AppException: 设备不存在，或属于其他租户（统一返回「不存在」语义，
            避免通过错误信息探测他人设备是否存在）。
    """
    device = (
        await session.execute(select(Device).where(Device.id == device_id))
    ).scalar_one_or_none()
    if device is None:
        raise device_not_found("设备不存在")
    assert_visible(device.tenant_id, auth, resource="设备")
    return device


async def list_devices(
    session: AsyncSession,
    auth: AuthContext,
    *,
    offset: int = 0,
    limit: int = 20,
    tenant_id: str | None = None,
    client_product_id: str | None = None,
    order_id: str | None = None,
    asset_status: str | None = None,
    activation_status: str | None = None,
    online_status: str | None = None,
    bind_status: str | None = None,
    keyword: str | None = None,
    sort_by: str | None = None,
    order: str = "desc",
) -> tuple[list[Device], int]:
    """分页查询设备（租户过滤经 :func:`scoped`）。"""
    conditions: list[ColumnElement[bool]] = []
    if tenant_id:
        conditions.append(Device.tenant_id == tenant_id)
    if client_product_id:
        conditions.append(Device.client_product_id == client_product_id)
    if order_id:
        conditions.append(Device.order_id == order_id)
    if asset_status:
        conditions.append(Device.asset_status == asset_status)
    if activation_status:
        conditions.append(Device.activation_status == activation_status)
    if online_status:
        conditions.append(Device.online_status == online_status)
    if bind_status:
        conditions.append(Device.bind_status == bind_status)
    if keyword:
        pattern = f"%{keyword.strip()}%"
        conditions.append(
            Device.sn.like(pattern) | Device.imei.like(pattern) | Device.mac.like(pattern)
        )

    if sort_by and sort_by not in ALLOWED_SORT_FIELDS:
        raise validation_error(f"不支持的排序字段：{sort_by}")

    total = int(
        (
            await session.execute(
                scoped(select(func.count()).select_from(Device), Device, auth).where(*conditions)
            )
        ).scalar_one()
    )

    column = getattr(Device, sort_by) if sort_by else Device.created_at
    stmt = (
        scoped(select(Device), Device, auth)
        .where(*conditions)
        .order_by(column.desc() if order == "desc" else column.asc())
        .offset(offset)
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return rows, total


async def list_events(
    session: AsyncSession,
    auth: AuthContext,
    device_id: str,
    *,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[DeviceEvent], int]:
    """分页查询设备事件（先校验设备可见性，再查事件）。"""
    await get_device(session, auth, device_id)

    total = int(
        (
            await session.execute(
                scoped(
                    select(func.count()).select_from(DeviceEvent), DeviceEvent, auth
                ).where(DeviceEvent.device_id == device_id)
            )
        ).scalar_one()
    )
    stmt = (
        scoped(select(DeviceEvent), DeviceEvent, auth)
        .where(DeviceEvent.device_id == device_id)
        .order_by(DeviceEvent.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return rows, total


async def count_by_status(session: AsyncSession, auth: AuthContext) -> dict[str, Any]:
    """按维度聚合设备数量（供前端统计卡与工作台）。

    用三条 ``GROUP BY`` 而不是把全部设备拉回内存再统计：
    设备量级是「万」级别，拉回内存是 O(n) 的内存与网络开销，
    而聚合在数据库里是索引扫描。
    """
    result: dict[str, Any] = {"total": 0, "byAssetStatus": {}, "byOnlineStatus": {},
                             "byActivationStatus": {}, "byBindStatus": {}}

    def _collect(rows: list[Any]) -> dict[str, int]:
        return {str(row[0]): int(row[1]) for row in rows}

    total = int(
        (
            await session.execute(scoped(select(func.count()).select_from(Device), Device, auth))
        ).scalar_one()
    )
    result["total"] = total

    for key, column in (
        ("byAssetStatus", Device.asset_status),
        ("byOnlineStatus", Device.online_status),
        ("byActivationStatus", Device.activation_status),
        ("byBindStatus", Device.bind_status),
    ):
        stmt = scoped(
            select(column, func.count()).select_from(Device), Device, auth
        ).group_by(column)
        result[key] = _collect(list((await session.execute(stmt)).all()))

    return result


async def load_credentials(session: AsyncSession, device_id: str) -> list[DeviceCredential]:
    """读取设备的凭证列表（只含掩码字段，明文不存在于库中）。"""
    stmt = (
        select(DeviceCredential)
        .where(DeviceCredential.device_id == device_id)
        .order_by(DeviceCredential.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def build_detail(session: AsyncSession, auth: AuthContext, device: Device) -> DeviceDetailResponse:
    """组装设备详情（归属名称 + 凭证掩码 + 最近事件）。"""
    from app.models.catalog import ClientProduct  # 局部导入，避免模块级循环依赖
    from app.models.identity import Tenant
    from app.models.order import Order

    detail = DeviceDetailResponse.model_validate(device)
    detail.label = device_label(device)

    if device.tenant_id:
        tenant_name = (
            await session.execute(select(Tenant.name).where(Tenant.id == device.tenant_id))
        ).scalar_one_or_none()
        detail.tenant_name = str(tenant_name) if tenant_name else None
    if device.client_product_id:
        product_name = (
            await session.execute(
                select(ClientProduct.name).where(ClientProduct.id == device.client_product_id)
            )
        ).scalar_one_or_none()
        detail.client_product_name = str(product_name) if product_name else None
    if device.order_id:
        order_no = (
            await session.execute(select(Order.order_no).where(Order.id == device.order_id))
        ).scalar_one_or_none()
        detail.order_no = str(order_no) if order_no else None

    detail.credentials = [
        credential_to_brief(item) for item in await load_credentials(session, device.id)
    ]

    events, event_total = await list_events(
        session, auth, device.id, offset=0, limit=DETAIL_EVENT_LIMIT
    )
    detail.events = [event_to_response(item) for item in events]
    detail.event_total = event_total
    return detail


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------


async def record_event(
    session: AsyncSession,
    device: Device,
    *,
    event_type: str,
    dimension: str | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    actor: AuthContext | None = None,
    summary: str | None = None,
    detail: dict[str, Any] | None = None,
    request: Request | None = None,
) -> DeviceEvent:
    """写入一条设备事件（不提交，由调用方决定事务边界）。

    为什么 ``trace_id`` 从日志上下文取而不是由调用方传？
    一次请求内的所有事件应共享同一个 ``traceId``，由上下文统一提供
    可以避免「有的调用方传了、有的忘了传」导致时间线断链。
    """
    event = DeviceEvent(
        id=new_id("device_event"),
        device_id=device.id,
        tenant_id=device.tenant_id,
        event_type=event_type,
        dimension=dimension,
        from_status=from_status,
        to_status=to_status,
        actor_id=actor.user_id if actor else None,
        actor_account=actor.account if actor else None,
        summary=summary,
        detail=detail,
        trace_id=get_trace_id(),
        client_ip=request.client.host if request and request.client else None,
    )
    session.add(event)
    return event


# ---------------------------------------------------------------------------
# 状态迁移
# ---------------------------------------------------------------------------


def _resolve_thaw_target(device: Device) -> tuple[AssetStatus, str | None]:
    """决定解冻后恢复到哪个资产状态。

    常规路径是「恢复冻结前状态」。由于 ``FREEZABLE_ASSET_STATUSES`` 只允许
    冻结 ``IN_STOCK``，正常情况下 ``previous_asset_status`` 必然是
    ``IN_STOCK``，直接原路恢复。

    下面的「回落 + 调整说明」分支是**防御性代码**：它覆盖
    「冻结发生在本规则收紧之前」或「有人直接改库」这类脏数据，
    避免因为历史数据而抛异常，同时在事件详情里留下原因。

    Returns:
        ``(目标状态, 调整说明或 None)``。
    """
    previous = device.previous_asset_status
    if previous:
        try:
            candidate = AssetStatus(previous)
        except ValueError:
            candidate = None
        if candidate is not None and candidate in ASSET_TRANSITIONS[AssetStatus.FROZEN]:
            return candidate, None
        return (
            AssetStatus.IN_STOCK,
            f"冻结前状态为 {previous}，不能由 FROZEN 直接恢复，已回落到 IN_STOCK",
        )
    return AssetStatus.IN_STOCK, None


async def transition_asset(
    session: AsyncSession,
    device: Device,
    target: AssetStatus | str,
    *,
    reason: str | None = None,
    actor: AuthContext | None = None,
    request: Request | None = None,
    event_type: str | None = None,
) -> Device:
    """资产状态统一迁移入口。

    所有资产状态变更（入库 / 分配 / 绑定 / 冻结 / 解冻 / 报废）都必须走这里，
    以保证「迁移合法 + 事件留痕 + 时间戳正确」三件事同时发生。

    Args:
        session: 数据库会话。
        device: 设备 ORM 对象。
        target: 目标资产状态。
        reason: 变更原因（冻结 / 报废时必填，写入事件详情）。
        actor: 操作者。
        request: 用于提取客户端 IP。
        event_type: 事件类型；留空时按目标状态推断。

    Returns:
        迁移后的设备对象（未提交）。

    Raises:
        AppException: 迁移不被 :data:`ASSET_TRANSITIONS` 允许（``INVALID_STATE_TRANSITION``，
            ``details`` 带 ``current`` / ``target``）。
    """
    current = AssetStatus(device.asset_status)
    desired = AssetStatus(str(target))

    if desired not in ASSET_TRANSITIONS[current]:
        raise invalid_state_transition(
            f"设备当前状态为 {current}，不允许迁移到 {desired}",
            current=str(current),
            target=str(desired),
        )

    now = utcnow()
    detail: dict[str, Any] = {}
    if reason:
        detail["reason"] = reason

    if desired is AssetStatus.FROZEN:
        # 只有 FREEZABLE_ASSET_STATUSES 里的状态才允许冻结。
        # 这里单独判断而不是靠迁移表：迁移表回答「FROZEN 是否是合法终点」，
        # 而冻结还要额外回答「从哪些状态进入有意义」（冻结一个已报废的设备是荒唐的）。
        if current not in FREEZABLE_ASSET_STATUSES:
            raise invalid_state_transition(
                f"设备当前状态为 {current}，不允许冻结",
                current=str(current),
                target=str(desired),
            )
        device.previous_asset_status = str(current)
        device.frozen_at = now
        device.freeze_reason = reason
        event_type = event_type or EVENT_FROZEN
    elif current is AssetStatus.FROZEN:
        # 解冻：真实目标由「冻结前状态」决定，而不是调用方传来的占位值
        restored, adjustment = _resolve_thaw_target(device)
        detail["restoredFrom"] = device.previous_asset_status
        if adjustment:
            detail["adjustment"] = adjustment
        device.previous_asset_status = None
        device.frozen_at = None
        device.freeze_reason = None
        desired = restored
        event_type = event_type or EVENT_THAWED
    elif desired is AssetStatus.RETIRED:
        device.retired_at = now
        device.retire_reason = reason
        event_type = event_type or EVENT_RETIRED

    device.asset_status = str(desired)
    await session.flush()

    await record_event(
        session,
        device,
        event_type=event_type or str(desired),
        dimension="asset",
        from_status=str(current),
        to_status=str(desired),
        actor=actor,
        summary=reason,
        detail=detail or None,
        request=request,
    )
    return device


async def freeze_device(
    session: AsyncSession,
    auth: AuthContext,
    device: Device,
    *,
    reason: str,
    request: Request | None = None,
) -> Device:
    """冻结设备（写审计）。"""
    await transition_asset(session, device, AssetStatus.FROZEN, reason=reason, actor=auth, request=request)
    await audit_service.record(
        session,
        action=AuditAction.FREEZE_DEVICE,
        actor=auth,
        resource_type="device",
        resource_id=device.id,
        summary=f"冻结设备 {device.sn}：{reason}",
        detail={"reason": reason, "previousStatus": device.previous_asset_status},
        request=request,
    )
    await session.commit()
    logger.info("设备 %s 已冻结：%s", device.sn, reason)
    return device


async def thaw_device(
    session: AsyncSession,
    auth: AuthContext,
    device: Device,
    *,
    reason: str | None = None,
    request: Request | None = None,
) -> Device:
    """解冻设备（恢复到冻结前状态；无法原路恢复时回落 IN_STOCK）。"""
    await transition_asset(session, device, AssetStatus.IN_STOCK, reason=reason, actor=auth, request=request)
    await audit_service.record(
        session,
        action=AuditAction.THAW_DEVICE,
        actor=auth,
        resource_type="device",
        resource_id=device.id,
        summary=f"解冻设备 {device.sn}（恢复为 {device.asset_status}）",
        detail={"restoredTo": device.asset_status, "reason": reason},
        request=request,
    )
    await session.commit()
    logger.info("设备 %s 已解冻，恢复为 %s", device.sn, device.asset_status)
    return device


async def retire_device(
    session: AsyncSession,
    auth: AuthContext,
    device: Device,
    *,
    reason: str,
    request: Request | None = None,
) -> Device:
    """报废设备（写 ``retire_reason`` 与审计）。"""
    await transition_asset(session, device, AssetStatus.RETIRED, reason=reason, actor=auth, request=request)
    await audit_service.record(
        session,
        action=AuditAction.RETIRE_DEVICE,
        actor=auth,
        resource_type="device",
        resource_id=device.id,
        summary=f"报废设备 {device.sn}：{reason}",
        detail={"reason": reason},
        request=request,
    )
    await session.commit()
    logger.info("设备 %s 已报废：%s", device.sn, reason)
    return device


__all__ = [
    "ALLOWED_SORT_FIELDS",
    "DETAIL_EVENT_LIMIT",
    "EVENT_FROZEN",
    "EVENT_GENERATED",
    "EVENT_IMPORTED",
    "EVENT_IN_STOCK",
    "EVENT_RETIRED",
    "EVENT_THAWED",
    "build_detail",
    "count_by_status",
    "credential_to_brief",
    "device_label",
    "event_to_response",
    "freeze_device",
    "get_device",
    "list_devices",
    "list_events",
    "load_credentials",
    "record_event",
    "retire_device",
    "thaw_device",
    "to_brief",
    "to_response",
    "transition_asset",
]
