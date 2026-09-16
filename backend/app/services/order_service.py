"""订单服务：下单 → 审核 → 生成设备 → 入库。

状态机（唯一权威是 :data:`app.models.enums.ORDER_TRANSITIONS`）
==============================================================

    PENDING_AUDIT ──审核通过──→ APPROVED ──→ GENERATING ──→ GENERATED ──→ IN_STOCK
                  └─审核驳回──→ REJECTED（终态）

本模块提供三个动作，分别对应上面三条边：

* :func:`create_order` —— 商户下单，落 ``PENDING_AUDIT``
* :func:`audit_order` —— 平台审核（**仅** ``PENDING_AUDIT`` 可审核）
* :func:`generate_devices` —— 生成设备（``APPROVED → GENERATING → GENERATED``）
  并随即 :func:`mark_in_stock` 入库（``GENERATED → IN_STOCK``）

所有迁移都经 :func:`transition_order` 校验，非法迁移抛
``INVALID_STATE_TRANSITION`` 且 ``details`` 带 ``current`` / ``target``——
前端据此能直接显示「当前状态不允许该操作」，而不是一句笼统的失败。

★ ``transition_order`` 是**公开**函数（P6 起）：工厂派单 / 烧录完成 / 出货
都要驱动订单状态（``IN_STOCK → PRODUCING → SHIPPED_TO_CLIENT``），
它们必须在**同一处**校验 :data:`app.models.enums.ORDER_TRANSITIONS`——
在工厂服务里复制一份迁移规则，等于给同一个状态机留了第二个权威。

生成设备为什么按联网方式分两条路
================================
* ``4G`` —— SN/IMEI/ICCID 由**集贤云端**分配，因此必须调用厂商接口。
  未配置厂商密钥时按 **ADR-07 安全失败**：把订单退回 ``APPROVED``、写失败审计、
  抛 ``VENDOR_UNAVAILABLE``。**绝不本地伪造一批设备**——
  假设备会一路流到出库与报表，代价远高于一次明确的 503。
* ``WIFI`` —— 京东 JoyInside 方案走端侧激活，SN 规则由平台自定义
  （见 :func:`build_local_sn`），本地生成即可，不依赖厂商接口。

设备 SN 的本地生成规则
======================
``SN-{YYYYMMDD}-{6 位大写字母数字}``，字符集刻意剔除 ``0/O/1/I``——
SN 会被印在机身与包装上由人工抄录，形近字符是真实的对账成本来源。
"""

from __future__ import annotations

import secrets
from datetime import datetime

from fastapi import Request
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import registry
from app.core.deps import AuthContext
from app.core.errors import (
    AppException,
    ErrorCode,
    internal_error,
    invalid_state_transition,
    not_found,
    product_not_authorized,
    validation_error,
    vendor_unavailable,
)
from app.core.ids import new_id
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.scope import assert_visible, scoped
from app.models.catalog import ClientProduct
from app.models.device import Device
from app.models.enums import (
    ORDER_STATUS_LABELS,
    ORDER_TRANSITIONS,
    AssetStatus,
    AuditAction,
    EnableStatus,
    OrderStatus,
)
from app.models.identity import Tenant
from app.models.order import Order
from app.schemas.order import GenerateResult, OrderDetailResponse, OrderResponse
from app.services import audit_service, device_service, qrcode_service

logger = get_logger(__name__)

#: 本地 SN 的随机字符集：剔除 0/O/1/I 等形近字符，便于人工抄录核对
SN_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
#: 本地 SN 随机部分长度
SN_RANDOM_LENGTH = 6
#: SN 冲突时的最大重试次数（6 位随机 + 当天日期，实际几乎不会冲突）
SN_MAX_ATTEMPTS = 5

#: 订单列表允许的排序字段
ALLOWED_SORT_FIELDS: frozenset[str] = frozenset(
    {"created_at", "updated_at", "order_no", "quantity", "status", "generated_at"}
)

#: 已经生成过设备的订单状态（再次调用 generate 时幂等返回既有结果）
_POST_GENERATION_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.GENERATED,
        OrderStatus.IN_STOCK,
        OrderStatus.PRODUCING,
        OrderStatus.SHIPPED_TO_CLIENT,
        OrderStatus.COMPLETED,
    }
)

#: 订单详情内嵌的设备摘要上限（避免一次拉回上千台设备）
DETAIL_DEVICE_LIMIT = 200


# ---------------------------------------------------------------------------
# 单号与 SN
# ---------------------------------------------------------------------------


def generate_order_no(now: datetime | None = None) -> str:
    """生成订单号 ``ORD-YYYYMMDD-XXXX``。

    用日期 + 随机后缀而不是自增序号：自增需要「取号表」或序列，
    在 SQLite 与 PostgreSQL 上语义不同；随机后缀在两个库上行为一致，
    且日期前缀让人一眼能看出下单时间。
    """
    stamp = (now or utcnow()).strftime("%Y%m%d")
    suffix = "".join(secrets.choice(SN_ALPHABET) for _ in range(4))
    return f"ORD-{stamp}-{suffix}"


def build_local_sn(now: datetime | None = None) -> str:
    """生成本地设备 SN：``SN-YYYYMMDD-XXXXXX``。

    适用于 Wi-Fi（京东 JoyInside）方案——该方案 SN 由平台自定义，
    设备端激活时只用 SN + 产品信息，不依赖厂商预分配。
    """
    stamp = (now or utcnow()).strftime("%Y%m%d")
    suffix = "".join(secrets.choice(SN_ALPHABET) for _ in range(SN_RANDOM_LENGTH))
    return f"SN-{stamp}-{suffix}"


async def _next_order_no(session: AsyncSession) -> str:
    """取一个未被占用的订单号。"""
    for _ in range(SN_MAX_ATTEMPTS):
        candidate = generate_order_no()
        exists = (
            await session.execute(select(Order.id).where(Order.order_no == candidate))
        ).scalar_one_or_none()
        if exists is None:
            return candidate
    raise internal_error("订单号生成失败，请重试")


async def _allocate_sns(session: AsyncSession, count: int) -> list[str]:
    """批量分配不冲突的本地 SN。

    先本地去重，再用一条 ``IN`` 查询把与库中已有 SN 冲突的剔掉后重抽——
    相比「逐个 SN 查一次库」，可以把 N 次往返压成常数次。
    """
    if count <= 0:
        return []

    accepted: list[str] = []
    for _ in range(SN_MAX_ATTEMPTS):
        needed = count - len(accepted)
        # 每次多抽一倍，减少「抽出的恰好都在库里」导致的循环次数
        candidates = list({build_local_sn() for _ in range(needed * 2)})
        if candidates:
            existing = set(
                (await session.execute(select(Device.sn).where(Device.sn.in_(candidates))))
                .scalars()
                .all()
            )
            for sn in candidates:
                if sn not in existing and sn not in accepted:
                    accepted.append(sn)
                    if len(accepted) >= count:
                        return accepted
        logger.warning("SN 分配命中冲突，重试中（已分配 %d/%d）", len(accepted), count)
    raise internal_error("设备 SN 分配失败，请重试")


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


async def get_order(session: AsyncSession, order_id: str) -> Order:
    """按 ID 取订单。

    Raises:
        AppException: 订单不存在。
    """
    order = (
        await session.execute(select(Order).where(Order.id == order_id))
    ).scalar_one_or_none()
    if order is None:
        raise not_found("订单不存在")
    return order


async def get_visible_order(session: AsyncSession, auth: AuthContext, order_id: str) -> Order:
    """取订单并校验可见性（商户越权访问返回「不存在」语义）。"""
    order = await get_order(session, order_id)
    assert_visible(order.tenant_id, auth, resource="订单")
    return order


async def _label_maps(
    session: AsyncSession, orders: list[Order]
) -> tuple[dict[str, tuple[str, str | None]], dict[str, tuple[str, str | None]]]:
    """批量取租户 / 客户产品的「名称 + 编码」，避免逐条查询（N+1）。"""
    tenant_ids = {order.tenant_id for order in orders}
    product_ids = {order.client_product_id for order in orders}

    tenants: dict[str, tuple[str, str | None]] = {}
    if tenant_ids:
        rows = await session.execute(
            select(Tenant.id, Tenant.name, Tenant.code).where(Tenant.id.in_(tenant_ids))
        )
        tenants = {str(row[0]): (str(row[1]), str(row[2])) for row in rows.all()}

    products: dict[str, tuple[str, str | None]] = {}
    if product_ids:
        rows = await session.execute(
            select(ClientProduct.id, ClientProduct.name, ClientProduct.code).where(
                ClientProduct.id.in_(product_ids)
            )
        )
        products = {str(row[0]): (str(row[1]), str(row[2])) for row in rows.all()}

    return tenants, products


async def _device_counts(session: AsyncSession, order_ids: list[str]) -> dict[str, int]:
    """统计每个订单下的设备数（一次分组查询）。"""
    if not order_ids:
        return {}
    stmt = (
        select(Device.order_id, func.count())
        .where(Device.order_id.in_(order_ids))
        .group_by(Device.order_id)
    )
    return {
        str(row[0]): int(row[1]) for row in (await session.execute(stmt)).all() if row[0] is not None
    }


def _to_response(
    order: Order,
    *,
    tenant: tuple[str, str | None] | None = None,
    product: tuple[str, str | None] | None = None,
    device_count: int = 0,
) -> OrderResponse:
    """ORM → 列表项响应（附加中文状态名与归属名称）。"""
    response = OrderResponse.model_validate(order)
    response.status_label = ORDER_STATUS_LABELS.get(OrderStatus(order.status), order.status)
    if tenant:
        response.tenant_name, response.tenant_code = tenant
    if product:
        response.client_product_name, response.client_product_code = product
    response.device_count = device_count
    return response


async def list_orders(
    session: AsyncSession,
    auth: AuthContext,
    *,
    offset: int = 0,
    limit: int = 20,
    status: str | None = None,
    tenant_id: str | None = None,
    client_product_id: str | None = None,
    keyword: str | None = None,
    sort_by: str | None = None,
    order: str = "desc",
) -> tuple[list[OrderResponse], int]:
    """分页查询订单（租户过滤经 :func:`scoped`，ADR-08）。"""
    conditions: list[ColumnElement[bool]] = []
    if status:
        conditions.append(Order.status == status)
    if tenant_id:
        conditions.append(Order.tenant_id == tenant_id)
    if client_product_id:
        conditions.append(Order.client_product_id == client_product_id)
    if keyword:
        conditions.append(Order.order_no.like(f"%{keyword.strip()}%"))

    if sort_by and sort_by not in ALLOWED_SORT_FIELDS:
        raise validation_error(f"不支持的排序字段：{sort_by}")

    total = int(
        (
            await session.execute(
                scoped(select(func.count()).select_from(Order), Order, auth).where(*conditions)
            )
        ).scalar_one()
    )

    column = getattr(Order, sort_by) if sort_by else Order.created_at
    stmt = (
        scoped(select(Order), Order, auth)
        .where(*conditions)
        .order_by(column.desc() if order == "desc" else column.asc())
        .offset(offset)
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())

    tenants, products = await _label_maps(session, rows)
    counts = await _device_counts(session, [row.id for row in rows])
    return [
        _to_response(
            row,
            tenant=tenants.get(row.tenant_id),
            product=products.get(row.client_product_id),
            device_count=counts.get(row.id, 0),
        )
        for row in rows
    ], total


async def build_order_detail(session: AsyncSession, auth: AuthContext, order: Order) -> OrderDetailResponse:
    """组装订单详情（归属名称 + 设备计数 + 最近设备摘要）。"""
    tenants, products = await _label_maps(session, [order])
    counts = await _device_counts(session, [order.id])
    base = _to_response(
        order,
        tenant=tenants.get(order.tenant_id),
        product=products.get(order.client_product_id),
        device_count=counts.get(order.id, 0),
    )

    stmt = (
        scoped(select(Device), Device, auth)
        .where(Device.order_id == order.id)
        .order_by(Device.created_at.asc())
        .limit(DETAIL_DEVICE_LIMIT)
    )
    devices = list((await session.execute(stmt)).scalars().all())

    return OrderDetailResponse(
        **base.model_dump(),
        generation_detail=order.generation_detail,
        devices=[device_service.to_brief(item) for item in devices],
    )


async def list_order_devices(
    session: AsyncSession, auth: AuthContext, order_id: str, *, limit: int = 200
) -> list[Device]:
    """取订单下的设备（供二维码导出）。"""
    stmt = (
        scoped(select(Device), Device, auth)
        .where(Device.order_id == order_id)
        .order_by(Device.created_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def qrcodes_for_order(
    session: AsyncSession, auth: AuthContext, order_id: str
) -> list[qrcode_service.QrCodeItem]:
    """导出订单下全部设备的二维码（格式由联网方式决定）。"""
    devices = await list_order_devices(session, auth, order_id)
    return qrcode_service.build_qrcodes(devices)


# ---------------------------------------------------------------------------
# 状态迁移
# ---------------------------------------------------------------------------


def transition_order(order: Order, target: OrderStatus) -> None:
    """订单状态迁移（唯一入口）。

    Raises:
        AppException: 迁移不在 :data:`ORDER_TRANSITIONS` 中（``INVALID_STATE_TRANSITION``）。
    """
    current = OrderStatus(order.status)
    if target not in ORDER_TRANSITIONS[current]:
        raise invalid_state_transition(
            f"订单当前状态为 {current}，不允许迁移到 {target}",
            current=str(current),
            target=str(target),
        )
    order.status = str(target)


# ---------------------------------------------------------------------------
# 下单
# ---------------------------------------------------------------------------


async def create_order(
    session: AsyncSession,
    auth: AuthContext,
    *,
    tenant_id: str,
    client_product_id: str,
    quantity: int,
    applicant_name: str | None = None,
    applicant_phone: str | None = None,
    remark: str | None = None,
    request: Request | None = None,
) -> Order:
    """商户下单。

    校验（对应 P-03 的口径：客户产品必须绑定租户且已授权）：
    1. 客户产品存在且属于**本租户**——否则按「不存在」处理，避免探测他租户产品；
    2. 客户产品状态为 ``ENABLED``——停用产品不允许再下单（返回 ``PRODUCT_NOT_AUTHORIZED``）；
    3. ``networkType`` 从客户产品**快照**到订单：产品后续改配置不影响存量订单。

    Raises:
        AppException: 产品不存在 / 不属于本租户 / 已停用。
    """
    product = (
        await session.execute(
            select(ClientProduct).where(ClientProduct.id == client_product_id)
        )
    ).scalar_one_or_none()
    if product is None or product.tenant_id != tenant_id:
        raise not_found("客户产品不存在")
    if str(product.status) != str(EnableStatus.ENABLED):
        raise product_not_authorized("该客户产品已停用，无法下单")

    order = Order(
        id=new_id("order"),
        order_no=await _next_order_no(session),
        tenant_id=tenant_id,
        client_product_id=product.id,
        quantity=quantity,
        status=str(OrderStatus.PENDING_AUDIT),
        network_type=product.network_type,
        applicant_name=applicant_name,
        applicant_phone=applicant_phone,
        remark=remark,
    )
    session.add(order)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        tenant_id=tenant_id,
        actor=auth,
        resource_type="order",
        resource_id=order.id,
        summary=f"商户下单 {order.order_no}（{quantity} 台，产品 {product.code}）",
        detail={
            "orderNo": order.order_no,
            "clientProductId": product.id,
            "quantity": quantity,
            "networkType": order.network_type,
        },
        request=request,
    )
    logger.info("订单 %s 已创建（租户 %s，%d 台）", order.order_no, tenant_id, quantity)
    return order


async def audit_order(
    session: AsyncSession,
    auth: AuthContext,
    *,
    order_id: str,
    decision: str,
    remark: str | None = None,
    reject_reason: str | None = None,
    request: Request | None = None,
) -> Order:
    """审核订单（**仅 ``PENDING_AUDIT`` 可审核**）。

    ``decision`` 只接受 ``APPROVED`` / ``REJECTED``（由请求模型约束）。
    驳回必须给出 ``rejectReason``——否则商户端无法整改。

    Raises:
        AppException: 订单不是 ``PENDING_AUDIT``（``INVALID_STATE_TRANSITION``），
            或驳回未填原因（``VALIDATION_ERROR``）。
    """
    order = await get_order(session, order_id)

    target = OrderStatus(str(decision))
    if target not in (OrderStatus.APPROVED, OrderStatus.REJECTED):
        raise validation_error("审核结论只能是 APPROVED 或 REJECTED")
    if OrderStatus(order.status) is not OrderStatus.PENDING_AUDIT:
        raise invalid_state_transition(
            f"订单当前状态为 {order.status}，不允许再次审核",
            current=order.status,
            target=str(target),
        )
    if target is OrderStatus.REJECTED and not (reject_reason or "").strip():
        raise validation_error("驳回订单必须填写驳回原因", details={"field": "rejectReason"})

    transition_order(order, target)
    order.audited_by = auth.account
    order.audited_at = utcnow()
    order.audit_remark = remark
    order.reject_reason = reject_reason if target is OrderStatus.REJECTED else None
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.AUDIT_ORDER,
        actor=auth,
        resource_type="order",
        resource_id=order.id,
        summary=(
            f"审核订单 {order.order_no}：通过"
            if target is OrderStatus.APPROVED
            else f"审核订单 {order.order_no}：驳回（{reject_reason}）"
        ),
        detail={"decision": str(target), "remark": remark, "rejectReason": order.reject_reason},
        request=request,
    )
    await session.commit()
    logger.info("订单 %s 审核结论 %s", order.order_no, target)
    return order


# ---------------------------------------------------------------------------
# 生成设备
# ---------------------------------------------------------------------------


async def _provision_via_vendor(
    session: AsyncSession, order: Order, product: ClientProduct
) -> tuple[list[str], str | None, bool]:
    """4G（集贤）路径：调用厂商接口生成设备。

    Returns:
        ``(厂商设备 ID 列表, 提示信息, 是否为模拟结果)``。

    Raises:
        AppException: 供应商未注册或未配置密钥（``VENDOR_UNAVAILABLE``）。
            这是 ADR-07 的落地：**宁可失败，也不伪造设备**。
    """
    registry.ensure_default_registry()
    resolved = await registry.resolve_for_product(session, client_product_id=product.id)
    if resolved is None:
        raise vendor_unavailable(
            "未找到可用的设备云供应商，请先在「云服务商」中接入并绑定到产品模板",
            vendor=None,
        )

    provider = resolved.provider
    provider.ensure_configured()

    outcome = await provider.provision_devices(
        product_code=product.code,
        count=order.quantity,
        metadata={"orderNo": order.order_no, "networkType": order.network_type},
    )
    if not outcome.success:
        raise vendor_unavailable(outcome.message, vendor=provider.vendor or provider.code)

    logger.info(
        "订单 %s 由供应商 %s（%s 层）生成 %d 台设备",
        order.order_no,
        provider.code,
        resolved.layer,
        len(outcome.vendor_device_ids),
    )
    return list(outcome.vendor_device_ids), outcome.message, bool(outcome.simulated)


async def _create_devices(
    session: AsyncSession,
    auth: AuthContext,
    order: Order,
    product: ClientProduct,
    *,
    vendor_device_ids: list[str],
    simulated: bool = False,
    request: Request | None,
) -> list[Device]:
    """为订单落设备记录（``asset_status=GENERATED``），并写生成事件。"""
    serials = await _allocate_sns(session, order.quantity)
    now = utcnow()
    created: list[Device] = []

    for index, sn in enumerate(serials):
        device = Device(
            id=new_id("device"),
            tenant_id=order.tenant_id,
            order_id=order.id,
            client_product_id=product.id,
            cloud_provider_id=product.cloud_provider_id,
            sn=sn,
            network_type=order.network_type,
            firmware_version=product.firmware_version,
            # 生成完成即 GENERATED；随后 mark_in_stock 统一入库
            asset_status=str(AssetStatus.GENERATED),
            generated_at=now,
            # 4G 方案的厂商设备 ID 由云端返回；Wi-Fi 方案则没有这一项
            vendor_device_id=(
                vendor_device_ids[index] if index < len(vendor_device_ids) else None
            ),
            remark=f"订单 {order.order_no} 生成",
        )
        session.add(device)
        created.append(device)

    await session.flush()

    for device in created:
        await device_service.record_event(
            session,
            device,
            event_type=device_service.EVENT_GENERATED,
            dimension="asset",
            from_status=str(AssetStatus.PENDING_GEN),
            to_status=str(AssetStatus.GENERATED),
            actor=auth,
            summary=f"订单 {order.order_no} 生成设备",
            detail={"orderNo": order.order_no, "simulated": simulated},
            request=request,
        )
    return created


async def mark_in_stock(
    session: AsyncSession,
    auth: AuthContext,
    order: Order,
    *,
    devices: list[Device] | None = None,
    request: Request | None = None,
) -> None:
    """设备入库：订单 ``GENERATED → IN_STOCK``，设备同步迁到 ``IN_STOCK``。

    「生成」与「入库」分成两步而不是一步到位，是因为二者在现实里
    是两次动作（生成可能在厂商侧失败、入库是平台侧的仓储动作），
    拆开后 ``GENERATED`` 状态才有意义（可用于「已生成待入库」的统计）。
    """
    if OrderStatus(order.status) is not OrderStatus.GENERATED:
        raise invalid_state_transition(
            f"订单当前状态为 {order.status}，不允许入库",
            current=order.status,
            target=str(OrderStatus.IN_STOCK),
        )

    transition_order(order, OrderStatus.IN_STOCK)

    targets = devices
    if targets is None:
        stmt = select(Device).where(
            Device.order_id == order.id, Device.asset_status == str(AssetStatus.GENERATED)
        )
        targets = list((await session.execute(stmt)).scalars().all())

    for device in targets:
        if device.asset_status != str(AssetStatus.GENERATED):
            continue
        await device_service.transition_asset(
            session,
            device,
            AssetStatus.IN_STOCK,
            reason=f"订单 {order.order_no} 生成完成入库",
            actor=auth,
            request=request,
            event_type=device_service.EVENT_IN_STOCK,
        )
    await session.flush()


async def generate_devices(
    session: AsyncSession,
    auth: AuthContext,
    *,
    order_id: str,
    request: Request | None = None,
) -> GenerateResult:
    """生成设备并入库（``APPROVED → GENERATING → GENERATED → IN_STOCK``）。

    幂等：订单已处于生成后状态时直接返回既有结果，不会重复造设备。

    Raises:
        AppException: 状态不允许（``INVALID_STATE_TRANSITION``），
            或 4G 方案厂商未配置密钥（``VENDOR_UNAVAILABLE``，ADR-07）。
    """
    order = await get_order(session, order_id)

    if OrderStatus(order.status) in _POST_GENERATION_STATUSES:
        return await _existing_result(session, auth, order)

    if OrderStatus(order.status) not in (OrderStatus.APPROVED, OrderStatus.GENERATING):
        raise invalid_state_transition(
            f"订单当前状态为 {order.status}，不允许生成设备",
            current=order.status,
            target=str(OrderStatus.GENERATING),
        )

    product = (
        await session.execute(
            select(ClientProduct).where(ClientProduct.id == order.client_product_id)
        )
    ).scalar_one_or_none()
    if product is None:
        raise not_found("订单关联的客户产品不存在")

    if OrderStatus(order.status) is OrderStatus.APPROVED:
        transition_order(order, OrderStatus.GENERATING)
        await session.flush()

    fmt = qrcode_service.format_of_network(order.network_type)
    vendor_device_ids: list[str] = []
    vendor_message: str | None = None
    simulated = False

    if fmt is qrcode_service.QrFormat.JX:
        try:
            vendor_device_ids, vendor_message, simulated = await _provision_via_vendor(
                session, order, product
            )
        except AppException as exc:
            # ADR-07：安全失败。把订单退回 APPROVED（GENERATING → APPROVED 是合法边），
            # 留下失败审计与生成摘要，再抛出——先提交再抛，保证失败证据落库。
            if OrderStatus(order.status) is OrderStatus.GENERATING:
                transition_order(order, OrderStatus.APPROVED)
            order.generation_detail = {
                "ok": False,
                "reason": str(exc.code),
                "message": exc.message,
                "vendor": (exc.details or {}).get("vendor") if isinstance(exc.details, dict) else None,
            }
            # 审计口径必须按**失败根因**分类，否则运维查不全：
            #   * 厂商不可用 / 厂商调用失败 → VENDOR_CALL_FAILED
            #     （与 P7 适配器同一口径，ADR-07 的安全拒绝可被同一条检索命中）
            #   * 其它原因 → GENERATE_DEVICES(success=False)
            # 两种情况都带 resource_type/resource_id，保证能按订单反查。
            vendor = (
                (exc.details or {}).get("vendor") if isinstance(exc.details, dict) else None
            )
            is_vendor_failure = exc.code == ErrorCode.VENDOR_UNAVAILABLE or vendor is not None
            await audit_service.record_failure(
                session,
                action=AuditAction.VENDOR_CALL_FAILED if is_vendor_failure else AuditAction.GENERATE_DEVICES,
                actor=auth,
                resource_type="order",
                resource_id=order.id,
                summary=f"订单 {order.order_no} 生成设备失败：{exc.message}",
                detail={
                    "orderNo": order.order_no,
                    "reason": str(exc.code),
                    "vendor": vendor,
                },
                request=request,
            )
            await session.commit()
            logger.warning("订单 %s 生成设备被安全拒绝：%s", order.order_no, exc.message)
            raise

    created = await _create_devices(
        session,
        auth,
        order,
        product,
        vendor_device_ids=vendor_device_ids,
        simulated=simulated,
        request=request,
    )

    order.generated_count = (order.generated_count or 0) + len(created)
    order.generated_at = utcnow()
    order.generation_detail = {
        "ok": True,
        "requested": order.quantity,
        "generated": len(created),
        "failed": max(0, order.quantity - len(created)),
        "format": str(fmt),
        "vendor": (vendor_message or None),
        "simulated": simulated,
        "deviceIds": [device.id for device in created[:50]],
    }
    transition_order(order, OrderStatus.GENERATED)
    await session.flush()

    # 生成即入库：订单与设备同步迁到 IN_STOCK
    await mark_in_stock(session, auth, order, devices=created, request=request)

    await audit_service.record(
        session,
        action=AuditAction.GENERATE_DEVICES,
        actor=auth,
        resource_type="order",
        resource_id=order.id,
        summary=f"订单 {order.order_no} 生成 {len(created)} 台设备并入库（{fmt}）",
        detail={
            "orderNo": order.order_no,
            "generated": len(created),
            "format": str(fmt),
            "simulated": simulated,
        },
        request=request,
    )
    await session.commit()
    logger.info("订单 %s 生成 %d 台设备并入库（格式 %s）", order.order_no, len(created), fmt)

    return GenerateResult(
        order_id=order.id,
        requested=order.quantity,
        generated=len(created),
        failed=max(0, order.quantity - len(created)),
        devices=[device_service.to_brief(device) for device in created],
        vendor_message=vendor_message,
    )


async def _existing_result(session: AsyncSession, auth: AuthContext, order: Order) -> GenerateResult:
    """已生成订单的幂等回放：从既有设备构造同样的响应形状。"""
    devices = await list_order_devices(session, auth, order.id, limit=DETAIL_DEVICE_LIMIT)
    detail = order.generation_detail or {}
    return GenerateResult(
        order_id=order.id,
        requested=int(detail.get("requested") or order.quantity),
        generated=len(devices),
        failed=max(0, order.quantity - len(devices)),
        devices=[device_service.to_brief(device) for device in devices],
        vendor_message=str(detail.get("vendor")) if detail.get("vendor") else "订单已生成，返回既有结果",
    )


__all__ = [
    "ALLOWED_SORT_FIELDS",
    "DETAIL_DEVICE_LIMIT",
    "build_local_sn",
    "build_order_detail",
    "create_order",
    "audit_order",
    "generate_devices",
    "generate_order_no",
    "get_order",
    "get_visible_order",
    "list_order_devices",
    "list_orders",
    "mark_in_stock",
    "qrcodes_for_order",
    "transition_order",
]
