"""工厂生产服务：派单 → 烧录上报 → 抽检 → 出货登记，外加设备批量入库。

工厂端为什么需要**两层**保护
============================
工厂是**跨租户**角色（``factories`` 独立于 ``tenants``，工厂账号
``tenant_id`` 为空），因此租户作用域（:func:`app.db.scope.scoped`）对它
不起任何作用——它的第二条判据是 ``factory_scoped``，而 ``scoped`` 对工厂
角色直接放行。工厂端需要另外两层，缺一不可：

1. **工厂作用域过滤**（:func:`app.db.scope.factory_scoped` /
   :func:`app.db.scope.assert_factory_visible`）——决定「能看到哪些工单」。
   一家工厂只应看到派给自己的工单，这层保护的是 *产量、交期、在同业里的
   位置* 这类信息。
2. **字段白名单脱敏**（:mod:`app.services.serializers`）——决定「看到工单的
   哪些字段」。工单跨租户，客户名 / 金额 / 联系方式必须逐字段剔除，
   这层保护的是 *客户身份*。

只做第 2 层不做第 1 层，等于「把别家工厂的工单也发给你，只是把客户名打了
码」：A 厂据此可以算出对手的产能与在手订单量，商业价值甚至高于客户名本身。
只做第 1 层不做第 2 层，则一次字段扩散（订单新增 ``applicantPhone``）
就会把客户联系方式泄漏给工厂，且不会报错。两层分别在不同环节收口
（作用域在 SQL 上、脱敏在序列化层），因此这里查询**一律**经
``factory_scoped``，响应**一律**经 ``FactoryOrderSerializer``。

工厂环节如何驱动设备四维状态与订单状态
======================================
工厂动作同时推动**两条状态链**，这是本阶段最容易写错的地方::

    派单       订单 IN_STOCK → PRODUCING         设备 IN_STOCK  → PRODUCING
    烧录报满   订单 PRODUCING → SHIPPED_TO_CLIENT 设备 PRODUCING → PRODUCED
    出货登记   （订单不动）                       设备 PRODUCED  → SHIPPED

（工单自身还有一条 ``PENDING → PRODUCING → COMPLETED → SHIPPED`` 的小状态机，
与上面两条并行。）

设备为什么必须一路走到 ``SHIPPED`` 才停？
-----------------------------------------
因为 :data:`app.models.enums.ALLOCATABLE_ASSET_STATUSES` 现在包含
``SHIPPED``：设备**出货之后**才能被分配到客户名下。``SHIPPED → ALLOCATED``
这条边从 P4 起就存在于 :data:`app.models.enums.ASSET_TRANSITIONS`，
但 P5 把可分配的入口限死在 ``IN_STOCK``，两者互相矛盾（迁移表允许、
服务层拒绝），于是「工厂出货的设备永远分不出去」。P6 补齐 ``SHIPPED``
把这条边接通：工厂出货 → 平台分配 → 商户绑定，业务链才是完整的。
若出货后设备停在 ``PRODUCED``，分配单会判它「状态不可用」，
整条链就断在最后一步。

为什么状态迁移一律走唯一入口，禁止散落 if/else
=============================================
* 设备 —— :func:`app.services.device_service.transition_asset`
  （校验迁移 + 写 ``device_events`` 时间线）；
* 订单 —— :func:`app.services.order_service.transition_order`
  （校验 :data:`app.models.enums.ORDER_TRANSITIONS`）；
* 工单 —— 本模块的 :func:`_transition_factory_order`
  （校验 :data:`app.models.enums.FACTORY_ORDER_TRANSITIONS`）。

三者的共同要求是「迁移合法 + 事件留痕 + 时间戳」必须同时发生。一旦在本模块
手写 ``order.status = "PRODUCING"``，这次迁移就绕过了迁移表校验，
既不会有失败前的拦截、也不会有任何留痕——线上迟早出现一个「状态是自己变的」
订单。工单状态没有第二个调用方需要，因此它的入口保持模块私有。

事务边界
========
写操作末尾 ``await session.commit()``（与 ``allocation_service`` 一致）；
只读不提交。
"""

from __future__ import annotations

import secrets
from datetime import datetime

from fastapi import Request
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext
from app.core.errors import (
    ErrorCode,
    burn_count_exceeded,
    device_not_found,
    factory_order_exists,
    internal_error,
    invalid_state_transition,
    not_found,
    validation_error,
)
from app.core.ids import new_id
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.scope import assert_factory_visible, factory_scoped, scoped
from app.models.catalog import ClientProduct, ProductTemplate
from app.models.device import Device
from app.models.enums import (
    FACTORY_ORDER_TRANSITIONS,
    INSPECTION_RESULT_LABELS,
    ORDER_STATUS_LABELS,
    AssetStatus,
    AuditAction,
    EnableStatus,
    FactoryOrderStatus,
    InspectionResult,
    OrderStatus,
)
from app.models.factory import BurnReport, FactoryOrder, Inspection
from app.models.identity import Tenant
from app.models.order import Order
from app.models.org import Factory
from app.schemas.device import StockInFailure, StockInResult
from app.schemas.factory import (
    BurnReportResponse,
    FactoryOrderDetailResponse,
    FactoryOrderResponse,
    FactoryResponse,
    FactoryStatsResponse,
    FirmwareVersionResponse,
    InspectionResponse,
    InspectionSummary,
)
from app.services import audit_service, device_service, order_service, qrcode_service
from app.services.serializers import SERIALIZER, burn_progress_percent

logger = get_logger(__name__)


#: 工单号随机后缀字符集：剔除 0/O/1/I 等形近字符（与订单号 / SN / 分配单号 /
#: 批次号同口径）——单号会被抄在纸上贴到产线，形近字符是真实的对账成本。
FACTORY_ORDER_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
#: 工单号随机后缀长度
FACTORY_ORDER_RANDOM_LENGTH = 4
#: 工单号冲突时的最大重试次数
FACTORY_ORDER_NO_ATTEMPTS = 5

#: 工单列表允许的排序字段白名单。
#: 与 ``order_service`` / ``device_service`` 同一口径：拼错的字段宁可报错，
#: 也不要静默退回默认排序（用户会以为排序生效了）。
#:
#: ``factory_order_no`` 是工厂端最自然的排序键（按工单号找单），
#: 前端表格也把它标成了可排序列——白名单漏了它就会「点一下表头 400」。
ALLOWED_SORT_FIELDS: frozenset[str] = frozenset(
    {
        "created_at",
        "assigned_at",
        "due_at",
        "quantity",
        "burned_count",
        "status",
        "factory_order_no",
    }
)

#: 工单详情内嵌的烧录上报条数上限（避免一次拉回几千条）
DETAIL_BURN_REPORT_LIMIT = 200
#: 工单详情内嵌的抽检记录条数上限。截断只会影响 `inspections` 数组，
#: `inspectionSummary` 是全量聚合，因此「合格率」不受截断影响。
DETAIL_INSPECTION_LIMIT = 200


# ---------------------------------------------------------------------------
# 单号
# ---------------------------------------------------------------------------


def generate_factory_order_no(now: datetime | None = None) -> str:
    """生成工单号 ``FO-YYYYMMDD-XXXX``。

    与订单号 ``ORD-*`` 同样的构成（日期 + 随机后缀，字符集剔除形近字符）：
    两个单号会同时出现在对账单上，规则一致可以少一次解释成本。
    """
    stamp = (now or utcnow()).strftime("%Y%m%d")
    suffix = "".join(
        secrets.choice(FACTORY_ORDER_ALPHABET) for _ in range(FACTORY_ORDER_RANDOM_LENGTH)
    )
    return f"FO-{stamp}-{suffix}"


async def _next_factory_order_no(session: AsyncSession) -> str:
    """取一个未被占用的工单号（冲突则重抽，最多 5 次）。"""
    for _ in range(FACTORY_ORDER_NO_ATTEMPTS):
        candidate = generate_factory_order_no()
        exists = (
            await session.execute(
                select(FactoryOrder.id).where(FactoryOrder.factory_order_no == candidate)
            )
        ).scalar_one_or_none()
        if exists is None:
            return candidate
    raise internal_error("工单号生成失败，请重试")


# ---------------------------------------------------------------------------
# 转换
# ---------------------------------------------------------------------------


def to_response(
    order: FactoryOrder,
    *,
    factory_name: str | None,
    order_no: str | None,
    customer_name: str | None,
) -> FactoryOrderResponse:
    """ORM → 工单列表项（经白名单序列化器，客户名在此脱敏）。

    ``customer_name`` 传的是**未脱敏**的客户名，脱敏发生在序列化器内部——
    调用方（本模块）因此不需要记住「这里要先脱敏」，
    也就不存在「某个新端点忘了脱敏」的路径。
    """
    return SERIALIZER.to_response(
        order,
        factory_name=factory_name,
        order_no=order_no,
        customer_name=customer_name,
    )


def burn_report_to_response(report: BurnReport) -> BurnReportResponse:
    """ORM → 烧录上报响应。"""
    return BurnReportResponse.model_validate(report)


def inspection_to_response(inspection: Inspection) -> InspectionResponse:
    """ORM → 抽检响应（附加中文结果名）。"""
    response = InspectionResponse.model_validate(inspection)
    response.result_label = INSPECTION_RESULT_LABELS.get(
        InspectionResult(inspection.result), inspection.result
    )
    return response


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


async def get_factory_order(
    session: AsyncSession, auth: AuthContext, factory_order_id: str
) -> FactoryOrder:
    """按 ID 取工单，并校验对本上下文可见（工厂越权返回「不存在」语义）。

    ``factory_orders`` 上没有 ``tenant_id``（工单挂在 ``orders`` 上），
    因此这里必须用 :func:`assert_factory_visible` 而不是
    :func:`app.db.scope.assert_visible`——后者对工厂角色直接放行，
    等于不校验。

    Raises:
        AppException: 工单不存在，或不属于当前工厂账号（统一「不存在」语义，
            避免通过错误码差异探测别家工厂的工单号是否存在）。
    """
    order = (
        await session.execute(select(FactoryOrder).where(FactoryOrder.id == factory_order_id))
    ).scalar_one_or_none()
    if order is None:
        raise not_found("工单不存在")
    assert_factory_visible(order.factory_id, auth)
    return order


async def _label_maps(
    session: AsyncSession, orders: list[FactoryOrder]
) -> tuple[dict[str, str], dict[str, tuple[str, str | None]], dict[str, str]]:
    """批量取「工厂名 / 订单号+租户 / 租户名」映射，避免 N+1。

    列表接口每页 20 条，逐条查这三样会变成 60 次往返（``order_service._label_maps``
    同款问题）。这里用三条 ``IN`` 查询把往返压成常数次。

    Returns:
        ``(工厂ID → 工厂名, 订单ID → (订单号, 租户ID), 租户ID → 租户名)``。
        查不到的键不会出现在映射里，调用方用 ``.get()`` 拿到 ``None``——
        宁可少显示一个名称，也不要因为关联行缺失而让整页 500。
    """
    factory_ids = {order.factory_id for order in orders}
    factories: dict[str, str] = {}
    if factory_ids:
        rows = await session.execute(
            select(Factory.id, Factory.name).where(Factory.id.in_(factory_ids))
        )
        factories = {str(row[0]): str(row[1]) for row in rows.all()}

    order_ids = {order.order_id for order in orders}
    order_rows: dict[str, tuple[str, str | None]] = {}
    if order_ids:
        rows = await session.execute(
            select(Order.id, Order.order_no, Order.tenant_id).where(Order.id.in_(order_ids))
        )
        order_rows = {
            str(row[0]): (str(row[1]), str(row[2]) if row[2] else None) for row in rows.all()
        }

    tenant_ids = {value[1] for value in order_rows.values()}
    tenant_ids.discard(None)
    tenants: dict[str, str] = {}
    if tenant_ids:
        rows = await session.execute(
            select(Tenant.id, Tenant.name).where(Tenant.id.in_(tenant_ids))
        )
        tenants = {str(row[0]): str(row[1]) for row in rows.all()}

    return factories, order_rows, tenants


async def _context_for(
    session: AsyncSession, order: FactoryOrder
) -> tuple[str | None, str | None, str | None]:
    """取单张工单的 ``(工厂名, 订单号, 客户名)``（详情用，复用批量映射）。"""
    factories, order_rows, tenants = await _label_maps(session, [order])
    order_no, tenant_id = order_rows.get(order.order_id, (None, None))
    return (
        factories.get(order.factory_id),
        order_no,
        tenants.get(tenant_id) if tenant_id else None,
    )


def _resolve_sort_field(sort_by: str) -> str:
    """把 camelCase 排序字段归一到 ``snake_case``（``createdAt`` → ``created_at``）。

    为什么要做这层归一，而不是把 camelCase 直接塞进 ``ALLOWED_SORT_FIELDS``？

    * ``ALLOWED_SORT_FIELDS`` 是**规范字段名**，与 :class:`FactoryOrder` 的属性
      一一对应（单测据此断言），混入别名会让「白名单」同时承载两种词汇表；
    * 归一之后**仍然要过白名单**：``dueAt`` 归一到 ``due_at`` 通过，
      ``factoryName`` 归一到 ``factory_name`` 依旧被拒——放行范围没有扩大。

    这层归一不是可选项：前端表格的排序键用的是**响应里的 camelCase 字段名**
    （``sortBy=createdAt``），而 schemas 层的约定本就是
    「请求体同时接受 camelCase 与 snake_case，前端无需做字段名转换」。
    少了它，前端排序与「按状态拉工单下拉」都会被 400 挡掉。
    """
    chars: list[str] = []
    for char in sort_by.strip():
        if char.isupper():
            chars.append("_")
            chars.append(char.lower())
        else:
            chars.append(char)
    return "".join(chars)


async def list_factory_orders(
    session: AsyncSession,
    auth: AuthContext,
    *,
    offset: int = 0,
    limit: int = 20,
    status: str | None = None,
    keyword: str | None = None,
    factory_id: str | None = None,
    sort_by: str | None = None,
    order: str = "desc",
) -> tuple[list[FactoryOrderResponse], int]:
    """分页查询工单。

    ``factoryId`` 是**筛选条件**而不是作用域：平台端是全局长（看某家工厂的
    产能），工厂端的作用域由 :func:`factory_scoped` 强制注入、不接受参数——
    两者正交，把作用域做成参数就等于给了越权的入口。

    ``keyword`` 同时匹配工单号与来源订单号：工厂对账时手上拿到的可能是
    任意一个（平台派单通知里给的是订单号，产线看板上贴的是工单号）。
    """
    conditions: list[ColumnElement[bool]] = []
    if status:
        conditions.append(FactoryOrder.status == status)
    if factory_id:
        conditions.append(FactoryOrder.factory_id == factory_id)
    if keyword:
        pattern = f"%{keyword.strip()}%"
        conditions.append(
            or_(
                FactoryOrder.factory_order_no.like(pattern),
                # 订单号在 orders 表上，用子查询而不是 join：join 会让
                # 「按工厂过滤」与「按订单过滤」两个条件耦合在一起，
                # 后续再加筛选条件时容易写出笛卡尔积。
                FactoryOrder.order_id.in_(select(Order.id).where(Order.order_no.like(pattern))),
            )
        )

    # 归一后再校验白名单：camelCase 写法（前端表格的排序键）与 snake_case 都接受，
    # 但白名单之外的字段名（``factoryName`` / ``remark`` 等）一律拒绝。
    sort_field = _resolve_sort_field(sort_by) if sort_by else None
    if sort_field is not None and sort_field not in ALLOWED_SORT_FIELDS:
        raise validation_error(f"不支持的排序字段：{sort_by}")

    total = int(
        (
            await session.execute(
                factory_scoped(
                    select(func.count()).select_from(FactoryOrder), FactoryOrder, auth
                ).where(*conditions)
            )
        ).scalar_one()
    )

    column = getattr(FactoryOrder, sort_field) if sort_field else FactoryOrder.created_at
    stmt = (
        factory_scoped(select(FactoryOrder), FactoryOrder, auth)
        .where(*conditions)
        .order_by(column.desc() if order == "desc" else column.asc())
        .offset(offset)
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())

    factories, order_rows, tenants = await _label_maps(session, rows)
    records: list[FactoryOrderResponse] = []
    for row in rows:
        order_no, tenant_id = order_rows.get(row.order_id, (None, None))
        records.append(
            to_response(
                row,
                factory_name=factories.get(row.factory_id),
                order_no=order_no,
                customer_name=tenants.get(tenant_id) if tenant_id else None,
            )
        )
    return records, total


async def _inspection_summary(
    session: AsyncSession, factory_order_id: str
) -> InspectionSummary:
    """统计某工单的抽检合格 / 不合格数（全量口径）。"""
    stmt = (
        select(Inspection.result, func.count())
        .where(Inspection.factory_order_id == factory_order_id)
        .group_by(Inspection.result)
    )
    counts = {str(row[0]): int(row[1]) for row in (await session.execute(stmt)).all()}
    pass_count = counts.get(str(InspectionResult.PASS), 0)
    fail_count = counts.get(str(InspectionResult.FAIL), 0)
    return InspectionSummary(
        pass_count=pass_count, fail_count=fail_count, total=pass_count + fail_count
    )


async def _load_inspections(
    session: AsyncSession, factory_order_id: str
) -> list[InspectionResponse]:
    """读出某工单的抽检记录（最新在前，带上限）。"""
    stmt = (
        select(Inspection)
        .where(Inspection.factory_order_id == factory_order_id)
        .order_by(Inspection.created_at.desc())
        .limit(DETAIL_INSPECTION_LIMIT)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return [inspection_to_response(item) for item in rows]


async def build_detail(
    session: AsyncSession, auth: AuthContext, order: FactoryOrder
) -> FactoryOrderDetailResponse:
    """组装工单详情（归属名称 + 订单侧状态 + 烧录 / 抽检明细）。

    ``inspectionSummary`` 单独用聚合查询算全量，而 ``inspections`` 数组带上限：
    抽检记录会随生产持续增长，若用数组长度当汇总，「合格率」会在记录变多
    之后逐渐偏低（被截断的部分全部不计入分母）。
    """
    factory_name, order_no, customer_name = await _context_for(session, order)
    detail = SERIALIZER.to_detail(
        order,
        factory_name=factory_name,
        order_no=order_no,
        customer_name=customer_name,
    )

    order_row = (
        await session.execute(select(Order).where(Order.id == order.order_id))
    ).scalar_one_or_none()
    if order_row is not None:
        detail.order_quantity = order_row.quantity
        detail.order_status = str(order_row.status)
        detail.order_status_label = ORDER_STATUS_LABELS.get(
            OrderStatus(order_row.status), order_row.status
        )

    reports = (
        await session.execute(
            select(BurnReport)
            .where(BurnReport.factory_order_id == order.id)
            .order_by(BurnReport.created_at.asc())
            .limit(DETAIL_BURN_REPORT_LIMIT)
        )
    ).scalars()
    detail.burn_reports = [burn_report_to_response(item) for item in reports]

    detail.inspections = await _load_inspections(session, order.id)
    detail.inspection_summary = await _inspection_summary(session, order.id)
    return detail


# ---------------------------------------------------------------------------
# 派单
# ---------------------------------------------------------------------------


def _resolve_product_model(template: ProductTemplate | None) -> str | None:
    """决定工单上的型号：``template.model`` → ``template.chip`` → ``template.name``。

    工厂要据此备料、选烧录夹具与测试治具，所以宁可回落也要给出点什么。
    平台维护的模板常常只填了芯片方案（``chip``，如 ``ESP32-S3``）——
    那对工厂同样可用；三级都空才留 ``None``。
    """
    if template is None:
        return None
    return template.model or template.chip or template.name


def _resolve_firmware_version(
    product: ClientProduct | None, template: ProductTemplate | None
) -> str | None:
    """决定工单上要烧的固件版本：客户产品快照优先，模板兜底。

    客户产品上的 ``firmware_version`` 是创建时的模板快照（见
    ``models/catalog.py`` 的「快照字段」说明），代表**这条产品线实际要烧的
    版本**；模板可能已经被平台改到下一代，拿模板值会让已经下单的工单烧错固件。
    """
    if product is not None and product.firmware_version:
        return product.firmware_version
    return template.firmware_version if template is not None else None


async def dispatch_order(
    session: AsyncSession,
    auth: AuthContext,
    *,
    order_id: str,
    factory_id: str,
    due_at: datetime | None,
    production_note: str | None,
    request: Request | None = None,
) -> FactoryOrderDetailResponse:
    """平台派单：创建工单，并把订单与设备推进到生产环节。

    校验顺序刻意固定，每一条都对应一种真实误操作（顺序即提示优先级）：

    1. 订单存在 —— 否则 404（先有订单才谈得上派单）；
    2. **同一订单不能有第二张工单** —— 409 ``FACTORY_ORDER_EXISTS``，
       ``details`` 带上既有工单号供前端直接跳转（比让人反复重试有用）。
       这条刻意排在状态校验**之前**：重复派单是这个端点最高频的误操作，
       而用户第二次点「派单」时订单早已被上一次推进到 ``PRODUCING``，
       若先校验状态，返回的会是笼统的 ``INVALID_STATE_TRANSITION``
       ——用户看到「状态不允许」，永远不知道真正原因是「你已经派过了」。
       （P6 的独立验证代理实测出这一点：按原顺序该错误码在正常流程下不可达。）
    3. 订单状态 ``IN_STOCK`` —— 否则 409。已在生产 / 已出货的订单再派一次，
       会造出两张工单各自烧录同一批设备；
    4. 订单下至少 1 台 ``IN_STOCK`` 设备 —— 否则 409。订单「已入库」但设备
       全被单独冻结 / 报废时，派单会造出一张工厂无活可干的工单；
    5. 工厂存在且 ``ENABLED`` —— 不存在 404、已停用 409。派给停用工厂的任务
       会永远停在 ``PENDING``，且没人会收到提醒。

    动作（同一个事务）：建工单 → 订单 ``IN_STOCK → PRODUCING`` →
    该订单下 ``IN_STOCK`` 设备全部 ``→ PRODUCING`` → 写审计 → 提交。

    设备状态条件带 ``IN_STOCK`` 而不是「订单下所有设备」：冻结中的设备
    ``asset_status`` 是 ``FROZEN``，天然被排除——冻结的语义就是「别动它」。

    工单数量为什么取「实际可派工设备数」而不是订单的合同数量
    --------------------------------------------------------
    取 ``order.quantity`` 会造出对不上账的工单：订单 2 台、其中 1 台已被
    单独冻结 → 工单写着 2 台、却只有 1 台会流转到 ``PRODUCING``；工厂报满
    2 台后工单变 ``COMPLETED``，而 ``burned_count(2) > 出货设备数(1)``，
    「烧录台数」与「设备台数」从此永久对不上（P6 的独立验证代理实测出该缺口）。

    **工单是「委托工厂生产多少台」的凭证**，所以它描述的是**实际交付给工厂
    的台数**；订单的合同数量仍原样保存在 ``orders.quantity``，并通过
    ``FactoryOrderDetailResponse.orderQuantity`` 一并下发——两个数字都给出，
    差异（例如有设备被冻结、或已先分配给了客户）就能被对账发现，
    而不是被一个「看起来正确」的数字掩盖。
    """
    order = await order_service.get_order(session, order_id)

    existing = (
        await session.execute(select(FactoryOrder).where(FactoryOrder.order_id == order.id))
    ).scalar_one_or_none()
    if existing is not None:
        raise factory_order_exists(
            f"订单 {order.order_no} 已派发（工单 {existing.factory_order_no}），请勿重复派单",
            details={"existingFactoryOrderNo": existing.factory_order_no},
        )

    if OrderStatus(order.status) is not OrderStatus.IN_STOCK:
        raise invalid_state_transition(
            f"订单当前状态为 {order.status}，不允许派单",
            current=order.status,
            target=str(OrderStatus.PRODUCING),
        )

    pending_devices = int(
        (
            await session.execute(
                scoped(select(func.count()).select_from(Device), Device, auth).where(
                    Device.order_id == order.id,
                    Device.asset_status == str(AssetStatus.IN_STOCK),
                )
            )
        ).scalar_one()
    )
    if not pending_devices:
        raise invalid_state_transition(
            f"订单 {order.order_no} 下没有已入库的设备，无法派单"
        )

    factory = (
        await session.execute(select(Factory).where(Factory.id == factory_id))
    ).scalar_one_or_none()
    if factory is None:
        raise not_found("工厂不存在")
    if str(factory.status) != str(EnableStatus.ENABLED):
        raise invalid_state_transition(f"工厂「{factory.name}」已停用，无法派单")

    product = (
        await session.execute(
            select(ClientProduct).where(ClientProduct.id == order.client_product_id)
        )
    ).scalar_one_or_none()
    template: ProductTemplate | None = None
    if product is not None:
        template = (
            await session.execute(
                select(ProductTemplate).where(ProductTemplate.id == product.template_id)
            )
        ).scalar_one_or_none()

    factory_order = FactoryOrder(
        id=new_id("factory_order"),
        factory_order_no=await _next_factory_order_no(session),
        order_id=order.id,
        factory_id=factory.id,
        product_model=_resolve_product_model(template),
        firmware_version=_resolve_firmware_version(product, template),
        # 委托台数 = 实际会被推进到 PRODUCING 的设备数（见上方 docstring）。
        # 它保证「工单进度」与「设备流转」互相印证，不会出现
        # burned_count 大于实际出货设备数的对不上账。
        quantity=pending_devices,
        burned_count=0,
        status=str(FactoryOrderStatus.PENDING),
        assigned_at=utcnow(),
        due_at=due_at,
        remark=production_note,
    )
    session.add(factory_order)
    await session.flush()

    order_service.transition_order(order, OrderStatus.PRODUCING)
    await _mark_order_devices(
        session,
        auth,
        factory_order,
        AssetStatus.IN_STOCK,
        AssetStatus.PRODUCING,
        reason=f"订单 {order.order_no} 派发工厂生产",
        # 派单是设备与工单归属关系的**唯一诞生点**：此处按「订单 + IN_STOCK」
        # 挑选并认领，之后所有工单维度的查询都以 factory_order_id 为准。
        claim_scope=True,
        request=request,
    )

    await audit_service.record(
        session,
        action=AuditAction.DISPATCH_FACTORY_ORDER,
        tenant_id=order.tenant_id,
        actor=auth,
        resource_type="factory_order",
        resource_id=factory_order.id,
        summary=(
            f"订单 {order.order_no} 派发工厂 {factory.code}，"
            f"工单 {factory_order.factory_order_no}（{factory_order.quantity} 台）"
        ),
        detail={
            "factoryOrderNo": factory_order.factory_order_no,
            "orderNo": order.order_no,
            "factoryId": factory.id,
            # 合同数量与实际委托数量都记进审计：两者的差异正是需要被追查的信号
            "quantity": factory_order.quantity,
            "orderQuantity": order.quantity,
        },
        request=request,
    )
    await session.commit()
    logger.info(
        "订单 %s 已派发工厂 %s（工单 %s，%d 台；合同 %d 台）",
        order.order_no,
        factory.code,
        factory_order.factory_order_no,
        factory_order.quantity,
        order.quantity,
    )
    return await build_detail(session, auth, factory_order)


# ---------------------------------------------------------------------------
# 状态迁移与设备联动
# ---------------------------------------------------------------------------


def _transition_factory_order(order: FactoryOrder, target: FactoryOrderStatus) -> None:
    """工单状态迁移（唯一入口）。

    保持模块私有：只有本模块的「烧录上报」与「出货登记」两个动作会用到，
    对外暴露只会诱使别处直接改工单状态、绕过迁移表。

    Raises:
        AppException: 迁移不在 :data:`FACTORY_ORDER_TRANSITIONS` 中
            （``INVALID_STATE_TRANSITION``，``details`` 带 current / target）。
    """
    current = FactoryOrderStatus(order.status)
    if target not in FACTORY_ORDER_TRANSITIONS[current]:
        raise invalid_state_transition(
            f"工单当前状态为 {current}，不允许迁移到 {target}",
            current=str(current),
            target=str(target),
        )
    order.status = str(target)


async def _mark_order_devices(
    session: AsyncSession,
    auth: AuthContext,
    factory_order: FactoryOrder,
    source: AssetStatus,
    target: AssetStatus,
    *,
    reason: str,
    claim_scope: bool = False,
    request: Request | None = None,
) -> int:
    """把本工单名下处于 ``source`` 的设备统一迁到 ``target``。

    为什么需要 ``claim_scope`` 这个开关
    ----------------------------------
    设备与工单的归属（``devices.factory_order_id``）是**事实**，不是
    「订单 + 状态」的函数：同一个订单下可能有被单独冻结、或已先分配给客户的
    设备，它们不属于这张工单。于是这里有两种语义，必须显式区分：

    * ``claim_scope=True``（**派单时唯一一次**）：按「订单 + ``source`` 状态」
      挑选设备，并把它们认领到本工单（写 ``factory_order_id``）。
      这是归属关系的诞生点，只有这一刻才允许这么挑。
    * ``claim_scope=False``（烧录 / 出货）：只用
      ``factory_order_id == 本工单`` 筛选。若仍按状态挑，出货时会把后来
      被解冻或新导入的同订单设备一起卷进去——那不是这张工单的活。

    Returns:
        实际迁移的设备台数（供审计与响应展示）。
    """
    conditions = [
        Device.order_id == factory_order.order_id,
        Device.asset_status == str(source),
    ]
    if claim_scope:
        # 认领：按订单 + 状态挑，挑中即写归属
        pass
    else:
        # 已认领过的设备：严格按工单归属筛
        conditions = [
            Device.factory_order_id == factory_order.id,
            Device.asset_status == str(source),
        ]

    stmt = scoped(select(Device), Device, auth).where(*conditions)
    devices = list((await session.execute(stmt)).scalars().all())
    for device in devices:
        if claim_scope:
            device.factory_order_id = factory_order.id
        await device_service.transition_asset(
            session, device, target, reason=reason, actor=auth, request=request
        )
    if claim_scope and devices:
        await session.flush()
    return len(devices)


# ---------------------------------------------------------------------------
# 烧录上报
# ---------------------------------------------------------------------------


async def report_burn(
    session: AsyncSession,
    auth: AuthContext,
    *,
    factory_order_id: str,
    burned_count: int,
    sn_from: str | None,
    sn_to: str | None,
    note: str | None,
    request: Request | None = None,
) -> FactoryOrderDetailResponse:
    """烧录上报（累计烧录数达标时联动推进订单与设备）。

    校验顺序：**先判工单状态、再判数量**。这两个条件在「已完成的工单又报
    一批」时会同时不满足，但「这单已经做完了」才是真正的原因——
    先报 ``BURN_COUNT_EXCEEDED`` 会让工厂以为是自己数字填错，反复重试而
    始终看不到「已完成」这个事实。终态 ``SHIPPED`` 同理。

    报满时（``burned_count == quantity``）的动作链：
    工单 ``（按需 PENDING→PRODUCING）→ COMPLETED``；
    **本工单名下**（``devices.factory_order_id``）``PRODUCING`` 设备
    ``→ PRODUCED``；订单 ``PRODUCING → SHIPPED_TO_CLIENT``
    （订单侧先走一步，因为「烧录完成」对商户就意味着「即将出货」）。
    未报满时工单按需从 ``PENDING → PRODUCING``。

    一台都没被推进时不为空转：``quantity`` 在派单时已等于被认领的设备数，
    因此「报满」必然对应同样多的设备（不变量由
    ``tests/integration/test_factory_flow.py`` 钉住）。

    Raises:
        AppException: 工单不在本厂（``RESOURCE_NOT_FOUND``）、工单已完成 /
            已出货（``INVALID_STATE_TRANSITION``）、上报数量超出剩余
            （``BURN_COUNT_EXCEEDED``，``details`` 带 remaining / quantity /
            burnedCount / currentBurnedCount）。
    """
    order = await get_factory_order(session, auth, factory_order_id)
    status = FactoryOrderStatus(order.status)

    if status not in (FactoryOrderStatus.PENDING, FactoryOrderStatus.PRODUCING):
        raise invalid_state_transition(
            f"工单当前状态为 {status}，不允许再上报烧录",
            current=str(status),
            target=str(FactoryOrderStatus.PRODUCING),
        )

    # ``remaining`` 必须在累加**之前**取：累加后再算会把本次数量算两次，
    # 报错信息里的「剩余」就变成了负数。
    remaining = order.remaining
    if order.burned_count + burned_count > order.quantity:
        raise burn_count_exceeded(
            f"上报数量超出工单剩余数量：剩余 {remaining} 台，本次上报 {burned_count} 台",
            details={
                "remaining": remaining,
                "quantity": order.quantity,
                "burnedCount": burned_count,
                "currentBurnedCount": order.burned_count,
            },
        )

    order_row = await order_service.get_order(session, order.order_id)
    report = BurnReport(
        id=new_id("burn_report"),
        factory_order_id=order.id,
        burned_count=burned_count,
        sn_from=sn_from,
        sn_to=sn_to,
        operator=auth.account,
        note=note,
        reported_at=utcnow(),
    )
    session.add(report)
    order.burned_count += burned_count
    await session.flush()

    completed = order.burned_count == order.quantity
    if completed:
        if status is FactoryOrderStatus.PENDING:
            # 一次报满时现实中并不存在「先生产一段时间」这段经历，但状态机
            # 只有 PENDING → PRODUCING → COMPLETED 这条唯一路径。照旧补这一步，
            # 而不是给它开一条 PENDING → COMPLETED 的捷径——捷径一旦存在，
            # 「工单必然经历过生产中」这个不变式就没了，统计口径随之失效。
            _transition_factory_order(order, FactoryOrderStatus.PRODUCING)
        _transition_factory_order(order, FactoryOrderStatus.COMPLETED)

        await _mark_order_devices(
            session,
            auth,
            order,
            AssetStatus.PRODUCING,
            AssetStatus.PRODUCED,
            reason=f"工单 {order.factory_order_no} 烧录完成",
            request=request,
        )
        order_service.transition_order(order_row, OrderStatus.SHIPPED_TO_CLIENT)
    elif status is FactoryOrderStatus.PENDING:
        _transition_factory_order(order, FactoryOrderStatus.PRODUCING)

    await audit_service.record(
        session,
        action=AuditAction.BURN_REPORT,
        tenant_id=order_row.tenant_id,
        actor=auth,
        resource_type="factory_order",
        resource_id=order.id,
        summary=(
            f"工单 {order.factory_order_no} 烧录上报 {burned_count} 台"
            f"（累计 {order.burned_count}/{order.quantity}）"
            f"{'，烧录完成' if completed else ''}"
        ),
        detail={
            "factoryOrderNo": order.factory_order_no,
            "orderNo": order_row.order_no,
            "burnReportId": report.id,
            "burnedCount": burned_count,
            "totalBurnedCount": order.burned_count,
            "quantity": order.quantity,
            "snFrom": sn_from,
            "snTo": sn_to,
        },
        request=request,
    )
    await session.commit()
    logger.info(
        "工单 %s 烧录上报 %d 台（累计 %d/%d）",
        order.factory_order_no,
        burned_count,
        order.burned_count,
        order.quantity,
    )
    return await build_detail(session, auth, order)


# ---------------------------------------------------------------------------
# 出货登记
# ---------------------------------------------------------------------------


async def register_shipment(
    session: AsyncSession,
    auth: AuthContext,
    *,
    factory_order_id: str,
    request: Request | None = None,
) -> FactoryOrderDetailResponse:
    """出货登记：工单 ``COMPLETED → SHIPPED``，设备 ``PRODUCED → SHIPPED``。

    设备必须走到 ``SHIPPED`` 而不是停在 ``PRODUCED``：
    :data:`app.models.enums.ALLOCATABLE_ASSET_STATUSES` 含 ``SHIPPED``，
    出货后的设备才能被分配单划到客户名下（见模块 docstring）。

    Raises:
        AppException: 工单不在本厂（``RESOURCE_NOT_FOUND``）、工单未完成
            （``INVALID_STATE_TRANSITION``）。
    """
    order = await get_factory_order(session, auth, factory_order_id)
    status = FactoryOrderStatus(order.status)
    if status is not FactoryOrderStatus.COMPLETED:
        raise invalid_state_transition(
            f"工单当前状态为 {status}，不允许出货登记",
            current=str(status),
            target=str(FactoryOrderStatus.SHIPPED),
        )

    _transition_factory_order(order, FactoryOrderStatus.SHIPPED)
    order.shipped_at = utcnow()
    await session.flush()

    moved = await _mark_order_devices(
        session,
        auth,
        order,
        AssetStatus.PRODUCED,
        AssetStatus.SHIPPED,
        reason=f"工单 {order.factory_order_no} 已出货",
        request=request,
    )
    order_row = await order_service.get_order(session, order.order_id)

    await audit_service.record(
        session,
        action=AuditAction.SHIP_FACTORY_ORDER,
        tenant_id=order_row.tenant_id,
        actor=auth,
        resource_type="factory_order",
        resource_id=order.id,
        summary=(
            f"工单 {order.factory_order_no} 出货登记：{moved} 台"
            f"（订单 {order_row.order_no}）"
        ),
        detail={
            "factoryOrderNo": order.factory_order_no,
            "orderNo": order_row.order_no,
            "shippedDeviceCount": moved,
            "quantity": order.quantity,
        },
        request=request,
    )
    await session.commit()
    logger.info("工单 %s 已出货登记（%d 台）", order.factory_order_no, moved)
    return await build_detail(session, auth, order)


# ---------------------------------------------------------------------------
# 抽检
# ---------------------------------------------------------------------------


async def create_inspection(
    session: AsyncSession,
    auth: AuthContext,
    *,
    factory_order_id: str,
    sn: str,
    result: InspectionResult,
    defect_code: str | None,
    note: str | None,
    request: Request | None = None,
) -> InspectionResponse:
    """记录一次抽检。

    **抽检不改工单状态，也不改设备状态**——这是刻意的。抽检不合格的真实
    含义是「需要人决定返工 / 降级 / 让步接收」，而让抽检自动把工单打回
    ``PENDING`` 会把这条重要信息淹没成一个普通的状态变化：工厂只要再点一次
    烧录上报就「恢复」了，质量问题反而被掩盖。处置是人的决定，
    系统负责把证据留全。

    同一 SN 允许被抽检多次：抽检是**观测**不是**状态**，保留历史才能看出
    「第一次不合格、返工后合格」这样的过程。

    SN 必须真实存在且属于本工单：
    * 不存在 → 404 ``DEVICE_NOT_FOUND``（记一条空记录会污染合格率）；
    * 不属于本工单 → 400 ``VALIDATION_ERROR``（``details.field=sn``）。

    「属于本工单」的判据是 ``devices.factory_order_id``，**不是**
    ``order_id`` 相等。这一点在 P6 验收时被实测暴露：同一订单下可能有
    被单独冻结或已先分配给客户的设备，它们没被派工、也不该被这张工单抽检
    ——只比对 ``order_id`` 会让一台从未进入生产的设备计进合格率。
    也不需要比对设备归属租户：工厂本来就不该知道设备属于哪个租户。

    Raises:
        AppException: 工单不在本厂（``RESOURCE_NOT_FOUND``）、SN 不存在
            （``DEVICE_NOT_FOUND``）、SN 不属于本工单（``VALIDATION_ERROR``）。
    """
    order = await get_factory_order(session, auth, factory_order_id)

    device = (
        await session.execute(select(Device).where(Device.sn == sn))
    ).scalar_one_or_none()
    if device is None:
        raise device_not_found(f"设备 {sn} 不存在")
    if device.factory_order_id != order.id:
        raise validation_error(f"SN {sn} 不属于本工单", details={"field": "sn"})

    inspection = Inspection(
        id=new_id("inspection"),
        factory_order_id=order.id,
        device_id=device.id,
        sn=sn,
        result=str(result),
        inspector=auth.account,
        defect_code=defect_code,
        note=note,
        inspected_at=utcnow(),
    )
    session.add(inspection)
    await session.flush()

    order_row = await order_service.get_order(session, order.order_id)
    await audit_service.record(
        session,
        action=AuditAction.INSPECT,
        tenant_id=order_row.tenant_id,
        actor=auth,
        resource_type="inspection",
        resource_id=inspection.id,
        summary=(
            f"工单 {order.factory_order_no} 抽检 {sn}："
            f"{INSPECTION_RESULT_LABELS.get(result, str(result))}"
        ),
        detail={
            "factoryOrderNo": order.factory_order_no,
            "orderNo": order_row.order_no,
            "deviceId": device.id,
            "sn": sn,
            "result": str(result),
            "defectCode": defect_code,
        },
        request=request,
    )
    await session.commit()
    logger.info(
        "工单 %s 抽检 %s：%s",
        order.factory_order_no,
        sn,
        INSPECTION_RESULT_LABELS.get(result, result),
    )
    return inspection_to_response(inspection)


async def list_inspections(
    session: AsyncSession,
    auth: AuthContext,
    *,
    offset: int = 0,
    limit: int = 20,
    factory_order_id: str | None = None,
    sn: str | None = None,
    result: str | None = None,
) -> tuple[list[InspectionResponse], int]:
    """分页查询抽检记录。

    ``inspections`` 表上**没有** ``factory_id``（它只指向工单），因此必须
    ``JOIN factory_orders`` 之后再套 :func:`factory_scoped`——少了这一步，
    工厂端能查出别家工厂的抽检记录（含 SN 与不良代码）。平台角色下
    ``factory_scoped`` 不加条件，同一份代码在两端都成立。

    ``sn`` 用**精确匹配**而不是模糊匹配：这里筛选的是「某一台的抽检历史」，
    输入框里填的就是从设备上抄下来的完整 SN；模糊匹配会让短前缀命中一堆
    无关记录，反而看不出历史。
    """
    conditions: list[ColumnElement[bool]] = []
    if factory_order_id:
        conditions.append(Inspection.factory_order_id == factory_order_id)
    if sn:
        conditions.append(Inspection.sn == sn.strip())
    if result:
        conditions.append(Inspection.result == result)

    join_on = Inspection.factory_order_id == FactoryOrder.id

    total = int(
        (
            await session.execute(
                factory_scoped(
                    select(func.count()).select_from(Inspection).join(FactoryOrder, join_on),
                    FactoryOrder,
                    auth,
                ).where(*conditions)
            )
        ).scalar_one()
    )
    stmt = (
        factory_scoped(
            select(Inspection).join(FactoryOrder, join_on), FactoryOrder, auth
        )
        .where(*conditions)
        .order_by(Inspection.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return [inspection_to_response(item) for item in rows], total


# ---------------------------------------------------------------------------
# 固件 / 统计 / 二维码
# ---------------------------------------------------------------------------


async def list_firmwares(
    session: AsyncSession, auth: AuthContext
) -> list[FirmwareVersionResponse]:
    """本厂固件版本聚合（不是固件包台账）。

    数据源是**本厂工单里出现过的** ``firmware_version`` 分组，而不是另建一张
    ``firmwares`` 表：固件包管理（上传、灰度、OTA 推送）属于 P9 的 OTA，
    本阶段工厂端只需要回答一个问题——「我要烧哪几个版本、各多少台」。
    现在建表会立刻产生一批「有版本号但没有任何固件文件」的空记录，
    而且列表与工单两处版本号迟早不一致。

    按版本号**字符串倒序**：同一产品线的版本号前缀一致（``1.0.0`` / ``1.2.0``），
    因此字符串倒序即发布顺序倒序。
    """
    stmt = (
        factory_scoped(
            select(
                FactoryOrder.firmware_version,
                func.count(),
                func.sum(FactoryOrder.quantity),
                func.sum(FactoryOrder.burned_count),
            ),
            FactoryOrder,
            auth,
        )
        .where(
            FactoryOrder.firmware_version.is_not(None),
            # 空串也要排除：迁移或导入把版本号写成 "" 时，分组里会出现一行
            # 没有版本号的「(空)」，在固件页面上毫无意义。
            FactoryOrder.firmware_version != "",
        )
        .group_by(FactoryOrder.firmware_version)
        .order_by(FactoryOrder.firmware_version.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [
        FirmwareVersionResponse(
            firmware_version=str(row[0]),
            order_count=int(row[1]),
            total_quantity=int(row[2] or 0),
            burned_count=int(row[3] or 0),
        )
        for row in rows
    ]


async def stats(session: AsyncSession, auth: AuthContext) -> FactoryStatsResponse:
    """工厂工作台统计（**严格本厂口径**）。

    所有聚合都经 :func:`factory_scoped`——统计是最容易被忽略作用域的地方：
    一个「跨厂汇总」的工作台数字更漂亮，却把别家的产能与在手订单量直接
    展示给了竞争对手，而这正是工厂作用域要保护的信息。

    ``burnProgressPercent`` 按**累计数量**而不是工单数加权（见
    :func:`app.services.serializers.burn_progress_percent`）。
    """
    rows = (
        await session.execute(
            factory_scoped(
                select(
                    FactoryOrder.status,
                    func.count(),
                    func.sum(FactoryOrder.quantity),
                    func.sum(FactoryOrder.burned_count),
                ),
                FactoryOrder,
                auth,
            ).group_by(FactoryOrder.status)
        )
    ).all()
    by_status = {str(row[0]): int(row[1]) for row in rows}
    total_quantity = sum(int(row[2] or 0) for row in rows)
    burned_quantity = sum(int(row[3] or 0) for row in rows)

    firmware_count = int(
        (
            await session.execute(
                factory_scoped(
                    select(func.count(func.distinct(FactoryOrder.firmware_version))),
                    FactoryOrder,
                    auth,
                ).where(
                    FactoryOrder.firmware_version.is_not(None),
                    FactoryOrder.firmware_version != "",
                )
            )
        ).scalar_one()
    )

    inspection_rows = (
        await session.execute(
            factory_scoped(
                select(Inspection.result, func.count())
                .select_from(Inspection)
                .join(FactoryOrder, Inspection.factory_order_id == FactoryOrder.id),
                FactoryOrder,
                auth,
            ).group_by(Inspection.result)
        )
    ).all()
    inspection_counts = {str(row[0]): int(row[1]) for row in inspection_rows}
    pass_count = inspection_counts.get(str(InspectionResult.PASS), 0)
    fail_count = inspection_counts.get(str(InspectionResult.FAIL), 0)

    return FactoryStatsResponse(
        pending_orders=by_status.get(str(FactoryOrderStatus.PENDING), 0),
        producing_orders=by_status.get(str(FactoryOrderStatus.PRODUCING), 0),
        completed_orders=by_status.get(str(FactoryOrderStatus.COMPLETED), 0),
        shipped_orders=by_status.get(str(FactoryOrderStatus.SHIPPED), 0),
        total_orders=sum(by_status.values()),
        total_quantity=total_quantity,
        burned_quantity=burned_quantity,
        burn_progress_percent=burn_progress_percent(burned_quantity, total_quantity),
        firmware_count=firmware_count,
        # 汇总口径取全部结果的合计（而不仅是 PASS + FAIL）：万一库里有
        # 第三种结果值（历史数据），合计才不会「比明细少」而对不上账。
        inspection_total=sum(inspection_counts.values()),
        inspection_pass=pass_count,
        inspection_fail=fail_count,
    )


async def qrcodes_for_factory_order(
    session: AsyncSession, auth: AuthContext, factory_order_id: str
) -> list[qrcode_service.QrCodeItem]:
    """本工单**认领**设备的二维码清单（贴码环节）。

    只返回 ``devices.factory_order_id == 本工单`` 的设备，而**不是**
    「订单下的全部设备」：工单委托 3 台、订单里有 5 台（1 台被冻结、1 台已先
    分配给客户）时，按订单取会让工厂多打 2 张标签——标签一旦贴上就很难挽回，
    而多打的那两张对应的设备根本不在这一批货里。这条差异由 P6 验收实测发现。

    二维码格式复用 :func:`app.services.order_service.qrcodes_for_order` 的
    编码器（``qrcode_service.build_qrcodes``）：工厂要打印的是**同一批设备**
    的二维码，自己再拼一次格式必然会与平台端导出结果不一致
    （两种格式的字段顺序与签名规则见 ``qrcode_service`` 的模块文档）。

    可见性先由 :func:`get_factory_order` 收口——工厂只能导自己工单的设备。
    """
    order = await get_factory_order(session, auth, factory_order_id)
    devices = list(
        (
            await session.execute(
                select(Device)
                .where(Device.factory_order_id == order.id)
                .order_by(Device.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return qrcode_service.build_qrcodes(devices)


async def list_factories(session: AsyncSession, auth: AuthContext) -> list[FactoryResponse]:
    """工厂列表（派单下拉用）。

    刻意**不过滤** ``DISABLED``：响应里带 ``status``，前端可以把已停用的工厂
    置灰并说明原因；后端派单时还会再校验一次状态，因此不依赖前端过滤。
    若这里静默隐藏停用工厂，运维看到的只是「下拉里没有这家厂」，
    无法区分「没建」与「被停用」。

    ``auth`` 目前不参与过滤（平台端是全局长），保留形参是为了与其它查询函数
    的签名一致——将来若出现「工厂可见范围」这类配置，收口点就在这里，
    不必再改一遍所有调用方。
    """
    rows = list(
        (await session.execute(select(Factory).order_by(Factory.code.asc()))).scalars().all()
    )
    return [FactoryResponse.model_validate(item) for item in rows]


# ---------------------------------------------------------------------------
# 设备批量入库（P5 遗留②）
# ---------------------------------------------------------------------------


async def stock_in_devices(
    session: AsyncSession,
    auth: AuthContext,
    *,
    device_ids: list[str],
    request: Request | None = None,
) -> StockInResult:
    """批量入库：``GENERATED → IN_STOCK``，逐台处理、部分失败不整单回滚。

    为什么需要这个端点？批次导入与厂商生成出来的设备停在 ``GENERATED``，
    必须有人把它推进 ``IN_STOCK`` 才算「平台库存」，否则分配单永远选不到它们
    （``ALLOCATABLE_ASSET_STATUSES`` 不含 ``GENERATED``）。

    与分配单执行同一套取舍（见 ``allocation_service`` 模块 docstring）：
    一次请求 100 台里有 3 台状态不对时，整单回滚意味着「97 台白入库一遍」，
    重试还要重新校验这 97 台，代价随重试次数累积。逐台独立后成功的立刻保留，
    失败只标自己，调用方按 ``failures[]`` 逐条处理。

    **幂等**：已是 ``IN_STOCK`` 的设备记为 ``skipped`` 而不是错误——
    重复点击「入库」不该报错，也不该产生第二次迁移（第二次迁移本来也会被
    ``ASSET_TRANSITIONS`` 拒绝，报错只会让重跑变得不可用）。

    逐台的迁移留痕由 :func:`app.services.device_service.transition_asset` 写入
    ``device_events``（``event_type=IN_STOCK``）；审计只记一条**汇总**，
    否则一次 500 台的入库会淹掉审计列表里真正需要人看的动作。

    Raises: 无。逐台失败通过返回值表达——抛出会让整批的既成结果一起丢掉。
    """
    devices: dict[str, Device] = {}
    if device_ids:
        rows = (
            await session.execute(select(Device).where(Device.id.in_(device_ids)))
        ).scalars().all()
        devices = {str(item.id): item for item in rows}

    moved = 0
    skipped = 0
    failures: list[StockInFailure] = []

    for device_id in device_ids:
        device = devices.get(device_id)
        if device is None:
            # 用 RESOURCE_NOT_FOUND 而不是 DEVICE_NOT_FOUND：语义上是
            # 「这条引用找不到目标」，与分配单明细对不存在设备的处理同一口径。
            failures.append(
                StockInFailure(
                    device_id=device_id,
                    sn=None,
                    code=str(ErrorCode.RESOURCE_NOT_FOUND),
                    message="设备不存在",
                )
            )
            continue

        status = AssetStatus(device.asset_status)
        if status is AssetStatus.IN_STOCK:
            skipped += 1
            continue
        if status is not AssetStatus.GENERATED:
            failures.append(
                StockInFailure(
                    device_id=device.id,
                    sn=device.sn,
                    code=str(ErrorCode.DEVICE_NOT_AVAILABLE),
                    message=f"设备 {device.sn} 当前状态为 {status}，不允许入库",
                )
            )
            continue

        await device_service.transition_asset(
            session,
            device,
            AssetStatus.IN_STOCK,
            reason=f"平台批量入库（{auth.account}）",
            actor=auth,
            request=request,
            event_type=device_service.EVENT_IN_STOCK,
        )
        moved += 1

    result = StockInResult(
        requested=len(device_ids),
        moved=moved,
        skipped=skipped,
        failed=len(failures),
        failures=failures,
    )
    await audit_service.record(
        session,
        action=AuditAction.STOCK_IN_DEVICE,
        actor=auth,
        resource_type="device",
        # 一次请求可能涉及几百台设备，没有**单一** resource_id 可填。
        # 逐台留痕由 device_events 承担，这里只记「这次批量动作的结果」。
        resource_id=None,
        summary=(
            f"批量入库：请求 {len(device_ids)} 台，入库 {moved} 台，"
            f"跳过 {skipped} 台，失败 {len(failures)} 台"
        ),
        detail={
            "requested": len(device_ids),
            "moved": moved,
            "skipped": skipped,
            "failed": len(failures),
            # 只带 ID 与错误码：审计详情落 JSON 列，逐条 message 会把
            # 一次 500 台的失败变成几百 KB 的审计记录。
            "failures": [{"deviceId": item.device_id, "code": item.code} for item in failures],
        },
        request=request,
    )
    await session.commit()
    logger.info(
        "批量入库：请求 %d 台，入库 %d 台，跳过 %d 台，失败 %d 台",
        len(device_ids),
        moved,
        skipped,
        len(failures),
    )
    return result


__all__ = [
    "ALLOWED_SORT_FIELDS",
    "DETAIL_BURN_REPORT_LIMIT",
    "DETAIL_INSPECTION_LIMIT",
    "FACTORY_ORDER_ALPHABET",
    "FACTORY_ORDER_NO_ATTEMPTS",
    "FACTORY_ORDER_RANDOM_LENGTH",
    "build_detail",
    "burn_report_to_response",
    "create_inspection",
    "dispatch_order",
    "generate_factory_order_no",
    "get_factory_order",
    "inspection_to_response",
    "list_factories",
    "list_factory_orders",
    "list_firmwares",
    "list_inspections",
    "qrcodes_for_factory_order",
    "register_shipment",
    "report_burn",
    "stats",
    "stock_in_devices",
    "to_response",
]
