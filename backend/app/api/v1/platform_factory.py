"""平台端 API：工厂生产（P6）。

为什么单独一个模块而不往 ``platform_orders.py`` 里加？
------------------------------------------------------
P6 与其它阶段是**并行开发**的两条线，而 ``platform_orders.py`` 已经是
「订单 + 设备 + 批次」三大块的大文件。第三个阶段再往里追加，冲突面就不再是
「一行 include」而是「同一段代码」——两人同时改同一文件的末尾、``__all__``
与 import 块，合并时必然要人工逐行对账。拆开之后，本模块管「订单交给工厂
之后的事」：工厂下拉、工单查询、派单、设备批量入库。

**设备入库端点（``POST /platform/devices/stock-in``）刻意放在这里**：
它是 P5 的遗留项②，而 ``platform_orders.py`` 正是 P4/P5 并行开发时的共享
文件——把 P6 的改动写进去，等于同时改动两个阶段正在维护的文件。
放在本模块后，``router.py`` 只多一行 include，冲突概率降到最低。

依赖与作用域
------------
全部端点用 :data:`app.core.deps.PlatformAuth`（平台全局长），因此这里
**不套** ``factory_scoped``——那是工厂端的作用域（「只看本厂工单」），
平台端本来就要看所有工厂。

但响应仍然走同一套白名单序列化器：``FactoryOrderResponse`` 只有
``customerNameMasked`` 字段，所以平台端的工单详情也拿不到未脱敏的客户名。
需要真名时走订单接口（``GET /platform/orders/{id}``），那里有订单权限码把关。
这不是遗漏，而是刻意的——工单接口不应该成为绕过脱敏的旁路。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request

from app.core.deps import DbSession, PlatformAuth, require_perm
from app.core.pagination import PageParams, page_params, page_response
from app.core.permissions import PlatformPerm
from app.models.enums import FactoryOrderStatus
from app.schemas.common import ListResult, PageResult
from app.schemas.device import StockInRequest, StockInResult
from app.schemas.factory import (
    FactoryDispatchRequest,
    FactoryOrderDetailResponse,
    FactoryOrderResponse,
    FactoryResponse,
)
from app.services import factory_service

router = APIRouter(prefix="/platform", tags=["平台端 · 工厂生产"])

PageQuery = Annotated[PageParams, Depends(page_params)]


# ===========================================================================
# 一、工厂
# ===========================================================================


@router.get(
    "/factories",
    response_model=ListResult[FactoryResponse],
    summary="工厂列表",
    description=(
        "派单下拉的取值来源（不分页）。**含已停用工厂**并带上 `status`："
        "前端可将其置灰并说明原因，比后端静默隐藏更好——隐藏会让人分不清"
        "「没建这家厂」与「这家厂被停用了」。后端派单时还会再校验一次状态。"
    ),
    dependencies=[require_perm(PlatformPerm.ORG_READ)],
)
async def list_factories(session: DbSession, auth: PlatformAuth) -> dict[str, Any]:
    """工厂列表。"""
    records = await factory_service.list_factories(session, auth)
    return {"records": records, "total": len(records)}


# ===========================================================================
# 二、生产工单
# ===========================================================================


@router.get(
    "/factory-orders",
    response_model=PageResult[FactoryOrderResponse],
    summary="生产工单列表",
    description=(
        "分页查询全部工厂的工单（平台全局长）。`factoryId` 按工厂筛选，"
        "`status` 按工单状态筛选，`keyword` 同时匹配工单号与订单号。\n\n"
        "客户名为**脱敏**值（`customerNameMasked`）：本模块与工厂端共用同一套"
        "响应模型，工单接口因此不提供客户真名——需要真名走订单接口。"
    ),
    dependencies=[require_perm(PlatformPerm.FACTORY_ORDER_READ)],
)
async def list_factory_orders(
    session: DbSession,
    auth: PlatformAuth,
    page: PageQuery,
    factory_id: Annotated[str | None, Query(alias="factoryId", description="按工厂筛选")] = None,
    status: Annotated[FactoryOrderStatus | None, Query(description="工单状态")] = None,
    keyword: Annotated[str | None, Query(description="工单号 / 订单号关键字")] = None,
) -> dict[str, Any]:
    """生产工单列表。"""
    records, total = await factory_service.list_factory_orders(
        session,
        auth,
        offset=page.offset,
        limit=page.limit,
        status=str(status) if status else None,
        keyword=keyword,
        factory_id=factory_id,
        sort_by=page.sort_by,
        order=page.order,
    )
    return page_response(records, total, page)


@router.get(
    "/factory-orders/{factory_order_id}",
    response_model=FactoryOrderDetailResponse,
    summary="生产工单详情",
    description=(
        "含烧录上报历史、抽检记录与抽检汇总、订单侧状态。"
        "平台端与工厂端共用同一响应模型，因此客户名同样是脱敏值。"
    ),
    responses={404: {"description": "工单不存在"}},
    dependencies=[require_perm(PlatformPerm.FACTORY_ORDER_READ)],
)
async def get_factory_order_detail(
    session: DbSession,
    auth: PlatformAuth,
    factory_order_id: str,
) -> FactoryOrderDetailResponse:
    """生产工单详情。"""
    order = await factory_service.get_factory_order(session, auth, factory_order_id)
    return await factory_service.build_detail(session, auth, order)


@router.post(
    "/orders/{order_id}/dispatch",
    response_model=FactoryOrderDetailResponse,
    status_code=201,
    summary="派单给工厂",
    description=(
        "把已入库的订单派给工厂生产，创建工单并推着两条状态链一起走：\n\n"
        "- 订单 `IN_STOCK → PRODUCING`\n"
        "- 该订单下 `IN_STOCK` 设备全部 `→ PRODUCING`（冻结中的设备天然被排除）\n"
        "- 工单 `PENDING`（待工厂开始烧录）\n\n"
        "工单上的型号取模板 `model → chip → name`，固件版本取**客户产品快照**"
        "（模板可能已被改到下一代，拿模板值会让已下单的工单烧错固件）。\n\n"
        "**校验顺序**（顺序即出错时的提示优先级）：订单存在 → 订单为 `IN_STOCK` "
        "→ 订单下至少 1 台 `IN_STOCK` 设备 → 工厂存在且启用 → 同一订单无既有工单。"
        "重复派单返回 `FACTORY_ORDER_EXISTS`，`details` 带 `existingFactoryOrderNo`，"
        "前端可直接跳转到既有工单。"
    ),
    responses={
        404: {"description": "订单或工厂不存在"},
        409: {"description": "订单状态不允许派单 / 工厂已停用 / 重复派单"},
    },
    dependencies=[require_perm(PlatformPerm.FACTORY_ORDER_WRITE)],
)
async def dispatch_order(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    order_id: str,
    payload: FactoryDispatchRequest = Body(...),
) -> FactoryOrderDetailResponse:
    """派单给工厂。"""
    return await factory_service.dispatch_order(
        session,
        auth,
        order_id=order_id,
        factory_id=payload.factory_id,
        due_at=payload.due_at,
        production_note=payload.production_note,
        request=request,
    )


# ===========================================================================
# 三、设备批量入库（P5 遗留②）
# ===========================================================================


@router.post(
    "/devices/stock-in",
    response_model=StockInResult,
    summary="批量入库设备",
    description=(
        "把一批设备推进 `IN_STOCK`（`GENERATED → IN_STOCK`）：批次导入或厂商"
        "生成出来的设备停在 `GENERATED`，必须入库才算平台库存，"
        "否则分配单永远选不到它们。\n\n"
        "**逐台处理，部分失败不整单回滚**：成功的立刻保留，失败只标自己，"
        "按 `failures[]` 逐条给出 `{deviceId, sn, code, message}`——"
        "整单回滚意味着「几十台白入库一遍」，重试代价随次数累积。\n\n"
        "**幂等**：已是 `IN_STOCK` 的设备记为 `skipped`（重复点击不报错、"
        "也不会产生第二次迁移）；其余状态记为 `failed`（`DEVICE_NOT_AVAILABLE`，"
        "文案带当前状态）；设备不存在记为 `failed`（`RESOURCE_NOT_FOUND`）。\n\n"
        "逐台的迁移留痕在设备事件时间线上（`IN_STOCK` 事件），审计只记一条汇总。"
    ),
    responses={422: {"description": "deviceIds 为空或超过 500 台"}},
    dependencies=[require_perm(PlatformPerm.DEVICE_WRITE)],
)
async def stock_in_devices(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    payload: StockInRequest = Body(...),
) -> StockInResult:
    """批量入库设备。"""
    return await factory_service.stock_in_devices(
        session, auth, device_ids=payload.device_ids, request=request
    )


__all__ = ["router"]
