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

在线判据为什么必须有**两个**来源
---------------------------------
``devices.online_status`` 是**落库的投影**（心跳写入 / 平台模拟掉线改写），
它不会自己随时间变化；因此设备掉线后仍会停在 ``ONLINE``。
:func:`is_online` 用 ``last_heartbeat_at`` 与 ``ONLINE_WINDOW_SECONDS``
（默认 180 秒）比较得出**派生判据**，两者语义不同：

* 筛选（``onlineStatus``）按派生判据做窗口一致化，避免「掉线了还能筛出在线」；
* 展示用 :attr:`DeviceResponse.online`，落库的 ``onlineStatus`` 只用于四维标签。

:func:`count_by_status` 同样按派生判据修正——「记着 ONLINE 但已超窗」的数量
计入 ``OFFLINE``，否则统计卡会长期高估在线数。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Any

from fastapi import Request
from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import AuthContext
from app.core.errors import (
    device_not_found,
    invalid_state_transition,
    validation_error,
)
from app.core.ids import new_id
from app.core.logging import get_logger, get_trace_id
from app.db.base import utcnow
from app.db.scope import assert_visible, scoped
from app.models.device import Device, DeviceCredential, DeviceEvent
from app.models.enums import (
    ASSET_TRANSITIONS,
    DEVICE_EVENT_DIMENSIONS,
    FREEZABLE_ASSET_STATUSES,
    AssetStatus,
    AuditAction,
    CredentialType,
    OnlineStatus,
    derive_device_label,
)
from app.schemas.device import (
    DeviceBrief,
    DeviceCredentialBrief,
    DeviceCredentialIssueResult,
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
# ---- P5 追加：分配 / 绑定 / 在线三维的事件类型 ----
EVENT_ALLOCATED = "ALLOCATED"
EVENT_BOUND = "BOUND"
EVENT_UNBOUND = "UNBOUND"
EVENT_ONLINE = "ONLINE"
EVENT_OFFLINE = "OFFLINE"
EVENT_HEARTBEAT = "HEARTBEAT"

#: 平台签发设备密钥时使用的摘要算法标识
CREDENTIAL_ALGORITHM = "sha256"


# ---------------------------------------------------------------------------
# 在线判据
# ---------------------------------------------------------------------------


def online_cutoff() -> datetime:
    """在线窗口的左边界：``now - ONLINE_WINDOW_SECONDS``。

    单独抽成函数而不是在调用点各写一遍 ``utcnow() - timedelta(...)``，
    是为了让「筛选 / 统计 / 展示」三处比较的是**同一个**时间点——
    各写一遍时，同一请求内先算出的边界与后算出的会差几毫秒，
    在窗口边界上的设备就会出现「列表说在线、统计说离线」。
    """
    return utcnow() - timedelta(seconds=settings.ONLINE_WINDOW_SECONDS)


def is_online(device: Device) -> bool:
    """派生在线判据：最后一次心跳落在在线窗口内。"""
    if device.last_heartbeat_at is None:
        return False
    return device.last_heartbeat_at >= online_cutoff()


def _online_conditions(online_status: str) -> list[ColumnElement[bool]]:
    """把 ``onlineStatus`` 筛选翻译成**窗口一致**的 SQL 条件。

    * ``ONLINE`` —— 只看最后一次心跳是否落在窗口内（不要求落库投影也是
      ``ONLINE``：设备刚从 ``NEVER_ONLINE`` 上报过心跳时，
      以心跳时间为准比以投影为准更接近事实）；
    * ``OFFLINE`` —— 「投影是 OFFLINE」**或**「投影是 ONLINE 但心跳已超窗」。
      少了后半句，掉线的设备会从两个筛选里同时消失；
    * ``NEVER_ONLINE`` 及未知取值 —— 按落库投影精确匹配。
    """
    if online_status == str(OnlineStatus.ONLINE):
        return [Device.last_heartbeat_at >= online_cutoff()]
    if online_status == str(OnlineStatus.OFFLINE):
        return [
            or_(
                Device.online_status == str(OnlineStatus.OFFLINE),
                and_(
                    Device.online_status == str(OnlineStatus.ONLINE),
                    Device.last_heartbeat_at < online_cutoff(),
                ),
            )
        ]
    return [Device.online_status == online_status]


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
    response.online = is_online(device)
    return response


def to_brief(device: Device) -> DeviceBrief:
    """ORM → 摘要响应。"""
    return DeviceBrief(
        id=device.id,
        sn=device.sn,
        network_type=device.network_type,
        asset_status=device.asset_status,
        label=device_label(device),
        online=is_online(device),
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
    unallocated: bool = False,
    keyword: str | None = None,
    sort_by: str | None = None,
    order: str = "desc",
) -> tuple[list[Device], int]:
    """分页查询设备（租户过滤经 :func:`scoped`）。

    Args:
        unallocated: 只看**尚未分配给任何租户的平台库存**（``tenant_id IS NULL``）。
            平台端「可分配库存」页需要一个确定的取值来源：靠前端在内存里过滤
            ``tenantId == null`` 会在分页下算错总数（每页都少几台）。
    """
    conditions: list[ColumnElement[bool]] = []
    if tenant_id:
        conditions.append(Device.tenant_id == tenant_id)
    if unallocated:
        conditions.append(Device.tenant_id.is_(None))
    if client_product_id:
        conditions.append(Device.client_product_id == client_product_id)
    if order_id:
        conditions.append(Device.order_id == order_id)
    if asset_status:
        conditions.append(Device.asset_status == asset_status)
    if activation_status:
        conditions.append(Device.activation_status == activation_status)
    if online_status:
        conditions.extend(_online_conditions(online_status))
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

    ``byOnlineStatus`` 与 :func:`list_devices` 的筛选口径**必须一致**
    （见模块 docstring）：落库投影为 ``ONLINE`` 但心跳已超窗的设备，
    其数量要从 ``ONLINE`` 挪到 ``OFFLINE``。做法是先按投影分组，
    再用一条条件计数取出「超窗」的数量做平移——
    比在 ``GROUP BY`` 里堆 ``CASE WHEN`` 更好读，也不会让另外三个维度跟着变形。
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

    stale_online = int(
        (
            await session.execute(
                scoped(
                    select(func.count()).select_from(Device), Device, auth
                ).where(
                    Device.online_status == str(OnlineStatus.ONLINE),
                    or_(
                        Device.last_heartbeat_at.is_(None),
                        Device.last_heartbeat_at < online_cutoff(),
                    ),
                )
            )
        ).scalar_one()
    )
    if stale_online:
        by_online: dict[str, int] = result["byOnlineStatus"]
        by_online[str(OnlineStatus.ONLINE)] = (
            by_online.get(str(OnlineStatus.ONLINE), 0) - stale_online
        )
        by_online[str(OnlineStatus.OFFLINE)] = (
            by_online.get(str(OnlineStatus.OFFLINE), 0) + stale_online
        )

    return result


async def load_credentials(session: AsyncSession, device_id: str) -> list[DeviceCredential]:
    """读取设备的凭证列表（只含掩码字段，明文不存在于库中）。"""
    stmt = (
        select(DeviceCredential)
        .where(DeviceCredential.device_id == device_id)
        .order_by(DeviceCredential.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def issue_credential(
    session: AsyncSession,
    auth: AuthContext,
    device: Device,
    *,
    credential_type: str = str(CredentialType.DEVICE_SECRET),
    expires_in_days: int | None = None,
    request: Request | None = None,
) -> DeviceCredentialIssueResult:
    """签发（或轮换）设备凭证。

    为什么是**覆盖**同一 ``(device_id, credential_type)`` 行而不是新增一行？
    ``device_credentials`` 上有唯一约束，且业务上「一台设备一个设备密钥」才是
    可用的语义——保留多行会让心跳校验时不知道用哪一条。轮换即失效，
    这正是设备丢失后应有的行为（旧密钥立刻不能再用）。

    明文只在返回值里出现一次：库内只存 ``sha256`` 摘要与「前 4 位 + ****」的
    掩码提示（掩码供运维核对「手上这把是不是系统里那把」）。

    Raises:
        AppException: 凭证类型不在 :class:`app.models.enums.CredentialType` 中
            （``VALIDATION_ERROR``）——不校验的话库里会积累永远匹配不上的垃圾类型。
    """
    try:
        normalized_type = CredentialType(credential_type)
    except ValueError as exc:
        allowed = "、".join(str(item) for item in CredentialType)
        raise validation_error(
            f"不支持的凭证类型：{credential_type}（可选：{allowed}）",
            details={"field": "credentialType"},
        ) from exc

    secret = secrets.token_urlsafe(32)
    now = utcnow()

    credential = (
        await session.execute(
            select(DeviceCredential).where(
                DeviceCredential.device_id == device.id,
                DeviceCredential.credential_type == str(normalized_type),
            )
        )
    ).scalar_one_or_none()
    rotated = credential is not None
    if credential is None:
        credential = DeviceCredential(
            id=new_id("device_credential"),
            device_id=device.id,
            credential_type=str(normalized_type),
        )
        session.add(credential)

    credential.secret_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    # 掩码只暴露前 4 位：够用来核对，却不足以暴力枚举（32 字节随机串的
    # 前 4 位只泄漏约 24 bit，剩下的穷举量仍然不可行）。
    credential.secret_hint = f"{secret[:4]}****"
    credential.algorithm = CREDENTIAL_ALGORITHM
    credential.issued_at = now
    credential.expires_at = (
        now + timedelta(days=expires_in_days) if expires_in_days is not None else None
    )
    # 轮换要显式解除吊销：否则「先吊销、后重新签发」的设备会拿着新密钥
    # 仍然被心跳判定为失效，且从界面上看不出原因。
    credential.revoked_at = None
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.UPDATE,
        actor=auth,
        resource_type="device_credential",
        resource_id=credential.id,
        summary=(
            f"{'轮换' if rotated else '签发'}设备 {device.sn} 的"
            f"{normalized_type}凭证（{credential.secret_hint}）"
        ),
        detail={
            "deviceId": device.id,
            "sn": device.sn,
            "credentialType": str(normalized_type),
            "secretHint": credential.secret_hint,
            "rotated": rotated,
            "expiresAt": credential.expires_at.isoformat() if credential.expires_at else None,
        },
        request=request,
    )
    await session.commit()
    logger.info("设备 %s 已%s凭证 %s", device.sn, "轮换" if rotated else "签发", normalized_type)
    return DeviceCredentialIssueResult(
        credential=credential_to_brief(credential), secret=secret
    )


async def build_detail(session: AsyncSession, auth: AuthContext, device: Device) -> DeviceDetailResponse:
    """组装设备详情（归属名称 + 凭证掩码 + 最近事件 + 当前绑定记录）。"""
    from app.models.catalog import ClientProduct  # 局部导入，避免模块级循环依赖
    from app.models.identity import Tenant
    from app.models.order import Order

    detail = DeviceDetailResponse.model_validate(device)
    detail.label = device_label(device)
    detail.online = is_online(device)

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

    # 绑定记录：设备详情页要回答「这台现在绑在哪」。放在详情里返回，
    # 免得前端先跳「绑定管理」再按 SN 搜一次（同一份事实两处查询，
    # 迟早出现两边显示不一致）。
    # 局部导入：binding_service 在模块级依赖本模块（device_label /
    # record_event / transition_asset），模块级互相 import 会成环。
    from app.models.allocation import DeviceBinding
    from app.services import binding_service

    binding = (
        await session.execute(
            select(DeviceBinding).where(DeviceBinding.device_id == device.id)
        )
    ).scalar_one_or_none()
    if binding is not None:
        detail.binding = await binding_service.binding_to_response(
            session, binding, device=device
        )
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

    ``dimension`` 省略时按 :data:`DEVICE_EVENT_DIMENSIONS` 兜底：
    ``event_type`` 已经隐含了维度（``BOUND`` 必然属于 ``bind``），
    让调用方每次重复填一遍只会制造不一致的机会。显式传入仍然优先
    （``transition_asset`` 就靠这一点把 ``asset`` 维度钉死）。
    """
    resolved_dimension = (
        dimension if dimension is not None else DEVICE_EVENT_DIMENSIONS.get(event_type)
    )
    event = DeviceEvent(
        id=new_id("device_event"),
        device_id=device.id,
        tenant_id=device.tenant_id,
        event_type=event_type,
        dimension=resolved_dimension,
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

    常规路径是「恢复冻结前状态」。``FREEZABLE_ASSET_STATUSES``
    （P6 起为 ``{IN_STOCK, ALLOCATED, BOUND}``）与
    ``ASSET_TRANSITIONS[FROZEN]`` 的出边严格相等，因此只要是经
    :func:`transition_asset` 冻结出来的数据，``previous_asset_status``
    必然落在该集合里，原路恢复一定成立——解冻不会把一台已分配给商户的
    设备退回 ``IN_STOCK``（那等于把客户资产弄丢）。

    下面的「回落 + 调整说明」分支是**防御性代码**：它覆盖
    「冻结发生在规则变更之前」或「有人直接改库」这类脏数据，
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
    dimension: str | None = None,
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
        dimension: 事件归属的维度；留空为 ``asset``。绑定 / 解绑由调用方传
            ``bind``——它们借道资产迁移表达（``ALLOCATED ⇄ BOUND``），
            事件本身描述的是绑定维度。

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
        # 默认恒为 ``asset``：本函数是**资产状态机**的入口，事件的前后状态
        # 也确实记的是资产维度（既有单测钉死了这一点）。
        # 绑定 / 解绑这类「借道资产迁移表达」的流程，由调用方显式传 ``bind``
        # ——事件类型是 ``BOUND`` / ``UNBOUND`` 却归到资产维度，
        # 会让时间线按维度分组时把绑定记录混进资产流转里。
        dimension=dimension or "asset",
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
    "CREDENTIAL_ALGORITHM",
    "DETAIL_EVENT_LIMIT",
    "EVENT_ALLOCATED",
    "EVENT_BOUND",
    "EVENT_FROZEN",
    "EVENT_GENERATED",
    "EVENT_HEARTBEAT",
    "EVENT_IMPORTED",
    "EVENT_IN_STOCK",
    "EVENT_OFFLINE",
    "EVENT_ONLINE",
    "EVENT_RETIRED",
    "EVENT_THAWED",
    "EVENT_UNBOUND",
    "build_detail",
    "count_by_status",
    "credential_to_brief",
    "device_label",
    "event_to_response",
    "freeze_device",
    "get_device",
    "is_online",
    "issue_credential",
    "list_devices",
    "list_events",
    "load_credentials",
    "online_cutoff",
    "record_event",
    "retire_device",
    "thaw_device",
    "to_brief",
    "to_response",
    "transition_asset",
]
