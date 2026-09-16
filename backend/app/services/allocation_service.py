"""分配服务：创建分配单 → 逐行执行 → 逐行留痕。

分配是什么
==========
``devices.tenant_id`` 为空表示**平台自有库存**（见 :mod:`app.models.device`）。
分配就是把一批设备从平台库存划到某个租户名下：写 ``tenant_id``、
``client_product_id``，并把 ``asset_status`` 由 ``IN_STOCK`` 推到 ``ALLOCATED``。
分配单是这次批量操作的**载体与审计凭据**。

单号与状态机
============
* 单号 ``AL-YYYYMMDD-XXXX``，随机后缀字符集剔除 ``0/O/1/I``——与订单号 /
  SN / 批次号同口径，单号同样要被人抄在纸上核对。
* 状态迁移唯一权威是 :data:`app.models.enums.ALLOCATION_TRANSITIONS`，
  本模块只经 :func:`_transition_allocation` 改状态，不散落 ``if/else``。
* ``COMPLETED`` 再调执行接口**不做状态迁移**，直接回放既有结果
  （与 P4「已入库订单再调生成接口」同一幂等语义）。

执行为什么逐行处理、不做整单回滚
================================
一张单里 300 台设备，只要有一台已属于别的租户，整单回滚就意味着
「299 台白分配一遍」——重试时还要重新校验这 299 台，代价随重试次数累积。
因此执行**逐行独立**：成功的行立刻落库并保留，失败的行只标自己
（``status=FAILED`` + ``error_message``），最后按「有没有失败」决定整单
``COMPLETED`` / ``FAILED``。这也是「重跑只补未成功的行」能成立的前提。

重跑（``FAILED → EXECUTING``）的语义
------------------------------------
* ``ALLOCATED`` 明细**保持不动**：它是上一轮成功的凭证，再走一次会重复分配；
* ``FAILED`` 明细重置为 ``PENDING`` 后重新校验——这正是状态机里
  留出 ``FAILED → EXECUTING`` 这条边的意义（「修复后重跑，只补未成功的明细」）；
* 因此计数器按**整单现状**重算（而非简单相加），运维看到的就是真实分布。

审计为什么要「整单一条」
========================
逐台设备写一条 ``ALLOCATE_DEVICE`` 会让一次分配产生几百条审计，
把真正需要人工决策的动作淹掉。逐台留痕由 ``device_events`` 承担
（:func:`app.services.device_service.transition_asset` 已自动写入），
审计只记「这次分配单执行了什么结果」。
"""

from __future__ import annotations

import secrets
from datetime import datetime

from fastapi import Request
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext
from app.core.errors import (
    AppException,
    device_frozen,
    device_not_available,
    device_not_in_tenant,
    internal_error,
    invalid_state_transition,
    not_found,
    product_not_authorized,
    tenant_disabled,
    validation_error,
)
from app.core.ids import new_id
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.scope import assert_visible, scoped
from app.models.allocation import AllocationItem, AllocationOrder
from app.models.catalog import ClientProduct, ProductAuthorization
from app.models.device import Device
from app.models.enums import (
    ALLOCATABLE_ASSET_STATUSES,
    ALLOCATION_STATUS_LABELS,
    ALLOCATION_TRANSITIONS,
    AllocationItemStatus,
    AllocationStatus,
    AssetStatus,
    AuditAction,
    EnableStatus,
    TenantStatus,
)
from app.models.identity import Tenant
from app.schemas.allocation import (
    AllocationExecuteResult,
    AllocationFailure,
    AllocationItemResponse,
    AllocationOrderDetailResponse,
    AllocationOrderResponse,
)
from app.services import audit_service, device_service

logger = get_logger(__name__)

#: 分配单号随机后缀字符集：剔除 0/O/1/I 等形近字符（与 SN / 批次号同口径）
ALLOCATION_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
#: 随机后缀长度
ALLOCATION_RANDOM_LENGTH = 4
#: 单号冲突时的最大重试次数
ALLOCATION_NO_ATTEMPTS = 5

#: 详情内嵌明细行的上限（与创建时的设备数上限一致，故正常情况下不会截断）
DETAIL_ITEM_LIMIT = 500

#: 失败原因在生产里的最大长度（``allocation_items.error_message`` 是 ``String(512)``）
MAX_ERROR_MESSAGE_LENGTH = 512


# ---------------------------------------------------------------------------
# 单号
# ---------------------------------------------------------------------------


def generate_allocation_no(now: datetime | None = None) -> str:
    """生成分配单号 ``AL-YYYYMMDD-XXXX``。"""
    stamp = (now or utcnow()).strftime("%Y%m%d")
    suffix = "".join(secrets.choice(ALLOCATION_ALPHABET) for _ in range(ALLOCATION_RANDOM_LENGTH))
    return f"AL-{stamp}-{suffix}"


async def _next_allocation_no(session: AsyncSession) -> str:
    """取一个未被占用的分配单号。"""
    for _ in range(ALLOCATION_NO_ATTEMPTS):
        candidate = generate_allocation_no()
        exists = (
            await session.execute(
                select(AllocationOrder.id).where(AllocationOrder.allocation_no == candidate)
            )
        ).scalar_one_or_none()
        if exists is None:
            return candidate
    raise internal_error("分配单号生成失败，请重试")


# ---------------------------------------------------------------------------
# 授权校验（与客户产品创建时同一口径）
# ---------------------------------------------------------------------------


async def is_product_authorized(
    session: AsyncSession, *, tenant_id: str, template_id: str | None
) -> bool:
    """判断「租户是否仍持有该模板的有效授权」。

    分配与绑定都要问同一个问题（「这个租户现在还有权用这个产品吗」），
    因此把口径收在一处：授权行存在、状态为 ``ENABLED``，且未过期
    （``expires_at`` 为空表示长期有效）。

    为什么不像创建产品那样抛异常？这里的调用方需要**同一份判定**
    既用于「整单失败」也用于「单行失败」，返回布尔值让调用方决定
    用什么错误码与文案（分配单要按行报告，绑定只需一个错误）。
    """
    if not template_id:
        return False
    stmt = select(ProductAuthorization.id).where(
        ProductAuthorization.tenant_id == tenant_id,
        ProductAuthorization.template_id == template_id,
        ProductAuthorization.status == str(EnableStatus.ENABLED),
        or_(
            ProductAuthorization.expires_at.is_(None),
            ProductAuthorization.expires_at > utcnow(),
        ),
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _product_usable(
    session: AsyncSession, order: AllocationOrder
) -> tuple[ClientProduct | None, AppException | None]:
    """校验分配单上的客户产品是否仍可用于分配。

    Returns:
        ``(产品对象或 None, 不可用时的异常或 None)``。产品对象供调用方
        在错误文案里带上产品名——只说「未授权」而不说是哪个产品，
        运维还得自己去查这张单挂的是哪个产品。
    """
    product = (
        await session.execute(
            select(ClientProduct).where(ClientProduct.id == order.client_product_id)
        )
    ).scalar_one_or_none()
    if product is None:
        return None, product_not_authorized("分配单关联的客户产品不存在")
    if str(product.status) != str(EnableStatus.ENABLED):
        return product, product_not_authorized(f"客户产品「{product.name}」已停用，无法分配设备")
    if not await is_product_authorized(
        session, tenant_id=order.tenant_id, template_id=product.template_id
    ):
        return product, product_not_authorized(
            f"客户产品「{product.name}」所属模板对租户「{order.tenant_id}」未授权或授权已过期"
        )
    return product, None


# ---------------------------------------------------------------------------
# 转换
# ---------------------------------------------------------------------------


def to_response(order: AllocationOrder) -> AllocationOrderResponse:
    """ORM → 分配单响应（附加中文状态名）。"""
    response = AllocationOrderResponse.model_validate(order)
    response.status_label = ALLOCATION_STATUS_LABELS.get(
        AllocationStatus(order.status), order.status
    )
    return response


def item_to_response(item: AllocationItem) -> AllocationItemResponse:
    """ORM → 明细行响应。"""
    return AllocationItemResponse.model_validate(item)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


async def get_allocation(
    session: AsyncSession, auth: AuthContext, allocation_id: str
) -> AllocationOrder:
    """按 ID 取分配单，并校验对当前上下文可见。

    ``tenant_id`` 在分配单上是**目标租户**（给谁），但可见性判定与其它资源
    并无二致：商户只应看到属于自己的分配单。因此这里仍走
    :func:`app.db.scope.assert_visible` 收口（ADR-08）。

    Raises:
        AppException: 分配单不存在，或不属于当前商户租户（统一「不存在」语义）。
    """
    order = (
        await session.execute(
            select(AllocationOrder).where(AllocationOrder.id == allocation_id)
        )
    ).scalar_one_or_none()
    if order is None:
        raise not_found("分配单不存在")
    assert_visible(order.tenant_id, auth, resource="分配单")
    return order


async def list_allocations(
    session: AsyncSession,
    auth: AuthContext,
    *,
    offset: int = 0,
    limit: int = 20,
    status: str | None = None,
    tenant_id: str | None = None,
    keyword: str | None = None,
) -> tuple[list[AllocationOrderResponse], int]:
    """分页查询分配单。

    平台端是**全局长**：``tenantId`` 是筛选条件（看某个租户的分配历史），
    不是作用域过滤——这正是本模块与商户端查询的区别。为保持 ADR-08 的
    「唯一收口点」不变式，仍统一经 :func:`scoped`（平台角色下它不加条件）。
    """
    conditions: list[ColumnElement[bool]] = []
    if status:
        conditions.append(AllocationOrder.status == status)
    if tenant_id:
        conditions.append(AllocationOrder.tenant_id == tenant_id)
    if keyword:
        conditions.append(AllocationOrder.allocation_no.like(f"%{keyword.strip()}%"))

    total = int(
        (
            await session.execute(
                scoped(
                    select(func.count()).select_from(AllocationOrder), AllocationOrder, auth
                ).where(*conditions)
            )
        ).scalar_one()
    )
    stmt = (
        scoped(select(AllocationOrder), AllocationOrder, auth)
        .where(*conditions)
        .order_by(AllocationOrder.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return [to_response(row) for row in rows], total


async def _load_items(session: AsyncSession, allocation_id: str) -> list[AllocationItem]:
    """按创建顺序读出分配单的全部明细行。

    不带上限：创建时已限制单张单最多 :data:`MAX_ALLOCATION_DEVICES`（500）台，
    执行时必须**看全**每一行，否则漏掉的行永远不会被处理。
    """
    stmt = (
        select(AllocationItem)
        .where(AllocationItem.allocation_order_id == allocation_id)
        .order_by(AllocationItem.created_at.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_items(
    session: AsyncSession,
    auth: AuthContext,
    allocation_id: str,
    *,
    offset: int = 0,
    limit: int = 20,
    status: str | None = None,
) -> tuple[list[AllocationItem], int]:
    """分页查询分配明细（先校验分配单可见性）。"""
    await get_allocation(session, auth, allocation_id)

    conditions: list[ColumnElement[bool]] = [
        AllocationItem.allocation_order_id == allocation_id
    ]
    if status:
        conditions.append(AllocationItem.status == status)

    total = int(
        (
            await session.execute(
                select(func.count()).select_from(AllocationItem).where(*conditions)
            )
        ).scalar_one()
    )
    stmt = (
        select(AllocationItem)
        .where(*conditions)
        .order_by(AllocationItem.created_at.asc())
        .offset(offset)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all()), total


async def build_detail(
    session: AsyncSession, auth: AuthContext, order: AllocationOrder
) -> AllocationOrderDetailResponse:
    """组装分配单详情（归属名称 + 明细行）。

    名称只在详情里出现，列表不给——与 P3/P4 的分工一致：列表靠下拉映射，
    详情页是单条记录、需要自解释。
    """
    detail = AllocationOrderDetailResponse.model_validate(order)
    detail.status_label = ALLOCATION_STATUS_LABELS.get(
        AllocationStatus(order.status), order.status
    )

    tenant_name = (
        await session.execute(select(Tenant.name).where(Tenant.id == order.tenant_id))
    ).scalar_one_or_none()
    detail.tenant_name = str(tenant_name) if tenant_name else None

    product = (
        await session.execute(
            select(ClientProduct).where(ClientProduct.id == order.client_product_id)
        )
    ).scalar_one_or_none()
    if product is not None:
        detail.client_product_name = product.name
        detail.client_product_code = product.code
        detail.network_type = product.network_type

    items = (
        await session.execute(
            select(AllocationItem)
            .where(AllocationItem.allocation_order_id == order.id)
            .order_by(AllocationItem.created_at.asc())
            .limit(DETAIL_ITEM_LIMIT)
        )
    ).scalars()
    detail.items = [item_to_response(item) for item in items]
    detail.item_total = int(
        (
            await session.execute(
                select(func.count())
                .select_from(AllocationItem)
                .where(AllocationItem.allocation_order_id == order.id)
            )
        ).scalar_one()
    )
    return detail


# ---------------------------------------------------------------------------
# 状态迁移
# ---------------------------------------------------------------------------


def _transition_allocation(order: AllocationOrder, target: AllocationStatus) -> None:
    """分配单状态迁移（唯一入口）。

    Raises:
        AppException: 迁移不在 :data:`ALLOCATION_TRANSITIONS` 中
            （``INVALID_STATE_TRANSITION``，``details`` 带 current / target）。
    """
    current = AllocationStatus(order.status)
    if target not in ALLOCATION_TRANSITIONS[current]:
        raise invalid_state_transition(
            f"分配单当前状态为 {current}，不允许执行",
            current=str(current),
            target=str(target),
        )
    order.status = str(target)


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------


async def create_allocation(
    session: AsyncSession,
    auth: AuthContext,
    *,
    tenant_id: str,
    client_product_id: str,
    device_ids: list[str],
    remark: str | None = None,
    request: Request | None = None,
) -> AllocationOrder:
    """创建分配单（``DRAFT``，只做形态校验；可用性在执行阶段逐行判定）。

    创建阶段校验两件事，且都必须是**硬失败**：

    1. 目标租户存在且 ``ACTIVE``——把设备划给一个已停用的租户，设备会
       立刻变成谁也访问不到的孤儿资产（商户登录不进来，平台也难察觉）；
    2. 客户产品存在且**属于该租户**——否则会把 A 品牌的设备挂到 B 品牌的
       产品上。产品与设备的归属不一致属于数据事故，不是「先建单后修」的问题。

    设备是否 ``IN_STOCK``、是否已属于别的租户，**留给执行阶段**逐行判定：
    那才是库存被真正扣减的时刻，创建时校验会在「建单 → 执行」之间形成
    一个不可靠的窗口（这期间设备可能被别的单分配走）。

    Raises:
        AppException: 租户不存在（``RESOURCE_NOT_FOUND``）/ 已停用
            （``TENANT_DISABLED``）/ 产品不属于该租户（``RESOURCE_NOT_FOUND``）。
    """
    if not device_ids:
        raise validation_error("分配单至少需要一台设备", details={"field": "deviceIds"})

    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant is None:
        raise not_found("租户不存在")
    if str(tenant.status) != str(TenantStatus.ACTIVE):
        raise tenant_disabled(f"租户「{tenant.name}」已被禁用，无法为其分配设备")

    product = (
        await session.execute(
            select(ClientProduct).where(ClientProduct.id == client_product_id)
        )
    ).scalar_one_or_none()
    if product is None or product.tenant_id != tenant_id:
        raise not_found("客户产品不存在或不属于该租户")

    order = AllocationOrder(
        id=new_id("allocation_order"),
        allocation_no=await _next_allocation_no(session),
        tenant_id=tenant_id,
        client_product_id=product.id,
        status=str(AllocationStatus.DRAFT),
        total_count=len(device_ids),
        allocated_count=0,
        failed_count=0,
        created_by=auth.account,
        remark=remark,
    )
    session.add(order)
    await session.flush()

    for device_id in device_ids:
        session.add(
            AllocationItem(
                id=new_id("allocation_item"),
                allocation_order_id=order.id,
                device_id=device_id,
                status=str(AllocationItemStatus.PENDING),
            )
        )
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        tenant_id=order.tenant_id,
        actor=auth,
        resource_type="allocation_order",
        resource_id=order.id,
        summary=(
            f"创建分配单 {order.allocation_no}：{order.total_count} 台设备，"
            f"目标租户「{tenant.name}」，产品「{product.name}」"
        ),
        detail={
            "allocationNo": order.allocation_no,
            "tenantId": order.tenant_id,
            "clientProductId": order.client_product_id,
            "totalCount": order.total_count,
        },
        request=request,
    )
    logger.info(
        "分配单 %s 已创建（租户 %s，%d 台）", order.allocation_no, tenant_id, order.total_count
    )
    return order


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def _mark_failed(item: AllocationItem, exc: AppException) -> AllocationFailure:
    """把一行标记为失败，并产出可读的失败记录。"""
    item.status = str(AllocationItemStatus.FAILED)
    item.error_message = exc.message[:MAX_ERROR_MESSAGE_LENGTH]
    return AllocationFailure(
        device_id=item.device_id,
        device_sn=item.device_sn,
        code=str(exc.code),
        message=exc.message,
    )


def _already_done_count(items: list[AllocationItem]) -> int:
    """统计「无需再动作」的明细行。

    ``ALLOCATED``（已分配成功）与 ``SKIPPED``（设备已处于目标状态）对调用方
    是同一件事：这一行不需要再动。分开数会让重跑的返回结果看起来
    「成功数虚高」——P5 的 ``AllocationExecuteResult`` 因此把它们合并进
    ``skipped_count``，与本次新分配的 ``allocated_count`` 分开计算。
    """
    return _count_status(items, AllocationItemStatus.ALLOCATED, AllocationItemStatus.SKIPPED)


def _count_status(items: list[AllocationItem], *statuses: AllocationItemStatus) -> int:
    """统计处于给定状态的明细行数。"""
    wanted = {str(status) for status in statuses}
    return sum(1 for item in items if item.status in wanted)


def _replay_result(
    order: AllocationOrder, items: list[AllocationItem]
) -> AllocationExecuteResult:
    """``COMPLETED`` 分配单的幂等回放：不再迁移、不再改设备，返回既有结果。

    失败列表刻意留空：分配单为 ``COMPLETED`` 时按不变式不存在 ``FAILED`` 明细
    （执行末尾只要有失败就必然落 ``FAILED``）。明细行只存了 ``error_message``
    而没有存错误码，回放时无法还原精确的错误码——返回一个编造的错误码
    比不返回更糟，逐行原因仍可通过明细接口查到。
    """
    return AllocationExecuteResult(
        allocation=to_response(order),
        allocated_count=order.allocated_count,
        skipped_count=_already_done_count(items),
        failed_count=order.failed_count,
        failures=[],
    )


async def execute_allocation(
    session: AsyncSession,
    auth: AuthContext,
    *,
    allocation_id: str,
    request: Request | None = None,
) -> AllocationExecuteResult:
    """执行分配单（逐行校验、逐行落库，不做整单回滚）。

    判定顺序（对应返回码的优先级）：**设备不存在 → 不属于本次租户 →
    已在本租户且已分配（跳过）→ 已冻结 → 不可分配状态**。
    产品授权校验是**整单一次**（产品与租户是单级决定的，逐行重复查纯属浪费），
    失败时把全部待执行行标为失败——这种情况下一台都不该分出去。

    Raises:
        AppException: 分配单可见性不通过（``RESOURCE_NOT_FOUND``）或当前状态
            不允许执行（``INVALID_STATE_TRANSITION``）。
    """
    order = await get_allocation(session, auth, allocation_id)
    items = await _load_items(session, order.id)

    if AllocationStatus(order.status) is AllocationStatus.COMPLETED:
        logger.info("分配单 %s 已完成，幂等回放既有结果", order.allocation_no)
        return _replay_result(order, items)

    # 只有 DRAFT / FAILED 可以进入 EXECUTING（FAILED → EXECUTING 是重跑边）
    _transition_allocation(order, AllocationStatus.EXECUTING)

    # 重跑：上一轮失败的明细重新排队；已成功的行保持 ALLOCATED 不动
    pending: list[AllocationItem] = []
    skipped_count = _already_done_count(items)
    for item in items:
        status = AllocationItemStatus(item.status)
        if status is AllocationItemStatus.FAILED:
            item.status = str(AllocationItemStatus.PENDING)
            item.error_message = None
            pending.append(item)
        elif status is AllocationItemStatus.PENDING:
            pending.append(item)

    _, blocked = await _product_usable(session, order)
    if blocked is not None:
        blocked_failures = [_mark_failed(item, blocked) for item in pending]
        # 整单失败：所有待执行行标失败。已成功的行不受影响——
        # 重跑时遇到「授权刚被撤销」也不能把上一轮已分配的设备退回去。
        total_allocated = _count_status(items, AllocationItemStatus.ALLOCATED)
        return await _finalize(
            session,
            auth,
            order,
            allocated_count=0,
            failed_count=len(blocked_failures),
            skipped_count=skipped_count,
            failures=blocked_failures,
            request=request,
            summary_reason=blocked.message,
            total_allocated=total_allocated,
            total_failed=_count_status(items, AllocationItemStatus.FAILED),
        )

    failures: list[AllocationFailure] = []
    allocated_count = 0

    for item in pending:
        device = (
            await session.execute(select(Device).where(Device.id == item.device_id))
        ).scalar_one_or_none()
        if device is None:
            # 用 RESOURCE_NOT_FOUND 而不是 DEVICE_NOT_FOUND：明细里存的是
            # 一个**指向不存在设备**的 ID，语义上是「这条引用找不到目标」。
            failures.append(_mark_failed(item, not_found("设备不存在")))
            continue

        # SN 快照：错误报告要能指出是「哪一台」，只有设备 ID 对人没有意义
        if not item.device_sn:
            item.device_sn = device.sn

        if device.tenant_id is not None and device.tenant_id != order.tenant_id:
            failures.append(_mark_failed(item, device_not_in_tenant("设备已分配给其它租户")))
            continue

        if device.tenant_id == order.tenant_id and str(device.asset_status) == str(
            AssetStatus.ALLOCATED
        ):
            # 重跑已成功的行：设备已在目标状态，不再迁移、不再改设备
            item.status = str(AllocationItemStatus.SKIPPED)
            item.error_message = None
            skipped_count += 1
            continue

        if str(device.asset_status) == str(AssetStatus.FROZEN):
            # FROZEN 不在 ALLOCATABLE 集合里，但值得一个**专门的**错误码：
            # 「冻结」是可解释、可解除的状态，与「状态不对」要分开告诉运维。
            failures.append(_mark_failed(item, device_frozen(f"设备 {device.sn} 已冻结，无法分配")))
            continue

        if AssetStatus(device.asset_status) not in ALLOCATABLE_ASSET_STATUSES:
            failures.append(
                _mark_failed(
                    item,
                    device_not_available(
                        f"设备 {device.sn} 当前状态为 {device.asset_status}，不允许分配"
                    ),
                )
            )
            continue

        device.tenant_id = order.tenant_id
        device.client_product_id = order.client_product_id
        await device_service.transition_asset(
            session,
            device,
            AssetStatus.ALLOCATED,
            actor=auth,
            request=request,
        )
        item.status = str(AllocationItemStatus.ALLOCATED)
        item.error_message = None
        item.allocated_at = utcnow()
        allocated_count += 1

    # 计数器按整单现状重算：重跑时「上一轮已成功 + 本轮新成功」才是这张单的
    # 真实分布，简单相加会把被重置的失败行算两遍。
    total_allocated = _count_status(items, AllocationItemStatus.ALLOCATED)
    total_failed = _count_status(items, AllocationItemStatus.FAILED)

    reason: str | None = None
    if total_failed:
        reason = f"分配未全部成功：{total_failed} 台失败，{total_allocated} 台已分配"

    return await _finalize(
        session,
        auth,
        order,
        allocated_count=allocated_count,
        failed_count=len(failures),
        skipped_count=skipped_count,
        failures=failures,
        request=request,
        summary_reason=reason,
        total_allocated=total_allocated,
        total_failed=total_failed,
    )


async def _finalize(
    session: AsyncSession,
    auth: AuthContext,
    order: AllocationOrder,
    *,
    allocated_count: int,
    failed_count: int,
    skipped_count: int,
    failures: list[AllocationFailure],
    request: Request | None,
    summary_reason: str | None,
    total_allocated: int,
    total_failed: int,
) -> AllocationExecuteResult:
    """收尾：定终态、写整单审计、提交。

    统一一处收尾，是为了让「产品未授权导致整单失败」与「逐行执行完毕」
    两条路径写出**形状一致**的审计与响应——两条路径各写一遍时，
    迟早只有一条带上 ``resource_id``，失败审计就再也按单号查不到了。

    ``allocated_count`` / ``failed_count`` 是**本次执行**的增量，
    ``total_allocated`` / ``total_failed`` 是整单现状（落在分配单上）。
    两者都要：前者回答「这次跑了什么」，后者回答「这张单现在是什么状况」。
    """
    order.allocated_count = total_allocated
    order.failed_count = total_failed
    order.executed_by = auth.account
    order.executed_at = utcnow()

    if order.failed_count:
        _transition_allocation(order, AllocationStatus.FAILED)
        order.failure_reason = summary_reason or f"分配未全部成功：{order.failed_count} 台失败"
    else:
        _transition_allocation(order, AllocationStatus.COMPLETED)
        order.failure_reason = None
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.ALLOCATE_DEVICE,
        tenant_id=order.tenant_id,
        actor=auth,
        resource_type="allocation_order",
        resource_id=order.id,
        summary=(
            f"执行分配单 {order.allocation_no}：新增 {allocated_count} 台，"
            f"跳过 {skipped_count} 台，失败 {failed_count} 台"
        ),
        detail={
            "allocationNo": order.allocation_no,
            "tenantId": order.tenant_id,
            "clientProductId": order.client_product_id,
            "allocatedCount": allocated_count,
            "failedCount": failed_count,
            "skippedCount": skipped_count,
            # 累计值单独给出：单看「本次新增」无法回答「这张单一共分了多少台」
            "totalAllocatedCount": order.allocated_count,
            "totalFailedCount": order.failed_count,
        },
        request=request,
    )
    await session.commit()
    logger.info(
        "分配单 %s 执行完成：状态 %s（本次新增 %d / 跳过 %d / 失败 %d）",
        order.allocation_no,
        order.status,
        allocated_count,
        skipped_count,
        failed_count,
    )
    return AllocationExecuteResult(
        allocation=to_response(order),
        allocated_count=allocated_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        failures=failures,
    )


__all__ = [
    "ALLOCATION_ALPHABET",
    "ALLOCATION_RANDOM_LENGTH",
    "DETAIL_ITEM_LIMIT",
    "build_detail",
    "create_allocation",
    "execute_allocation",
    "generate_allocation_no",
    "get_allocation",
    "is_product_authorized",
    "item_to_response",
    "list_allocations",
    "list_items",
    "to_response",
]
