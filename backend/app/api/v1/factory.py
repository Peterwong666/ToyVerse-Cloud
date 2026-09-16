"""工厂端 API（P6）：生产工单 / 烧录上报 / 抽检 / 出货 / 固件 / 工作台。

为什么单独一个模块
------------------
``factory_orders`` / ``burn_reports`` / ``inspections`` 三张表随迁移
``0010_factory_production`` 建好（P4 负责建表、P6 负责端点），本模块是 P6
的落点。不往 ``platform.py`` / ``merchant.py`` 里堆，是因为工厂端既不是
平台端也不是商户端：它的数据保护手段（工厂作用域过滤 + 字段白名单脱敏）
与两者都不同，混在同一文件里迟早有人用错依赖——而用错依赖不会报错，
只会静默越权。

路由层职责边界
--------------
只做「解析参数 → 调服务 → 转响应」：不写状态机、不写工厂过滤、不做脱敏。
* 工厂作用域经 :func:`app.db.scope.factory_scoped` /
  :func:`app.db.scope.assert_factory_visible` 在服务层收口；
* 字段脱敏经 :class:`app.services.serializers.FactoryOrderSerializer` 收口。

因此**这一层拿不到**订单的客户名真名、金额与联系方式——不是「记得别用」，
而是这些值根本没被查出来，也就无法泄漏。

权限与依赖
----------
全部端点依赖 :data:`app.core.deps.FactoryAuth` + 对应的 ``factory:*`` 权限码
（见 :class:`app.core.permissions.FactoryPerm`）。工厂账号必须在 JWT 里带
``factoryId``，否则 :func:`app.core.deps.AuthContext.require_factory_id`
直接 403——宁可「登录得进来看不到东西」，也不能退化成「看到全部工单」。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request

from app.core.deps import DbSession, FactoryAuth, require_perm
from app.core.pagination import PageParams, page_params, page_response
from app.core.permissions import FactoryPerm
from app.models.enums import FactoryOrderStatus, InspectionResult
from app.schemas.common import ListResult, PageResult
from app.schemas.device import QrCodeItemResponse
from app.schemas.factory import (
    BurnReportRequest,
    FactoryOrderDetailResponse,
    FactoryOrderResponse,
    FactoryStatsResponse,
    FirmwareVersionResponse,
    InspectionCreateRequest,
    InspectionResponse,
)
from app.services import factory_service

router = APIRouter(prefix="/factory", tags=["工厂端 · 生产"])

PageQuery = Annotated[PageParams, Depends(page_params)]


# ===========================================================================
# 一、工作台
# ===========================================================================


@router.get(
    "/stats",
    response_model=FactoryStatsResponse,
    summary="工厂工作台统计",
    description=(
        "按**本厂**口径聚合工单与抽检数据（跨厂聚合会把别家的产能与在手订单"
        "展示给竞争对手，因此不存在该选项）。\n\n"
        "`burnProgressPercent` 按累计数量加权（累计烧录 / 累计委托），"
        "而不是按工单数平均——一张 5000 台的工单与一张 10 台的工单对产能的"
        "意义完全不同。"
    ),
    dependencies=[require_perm(FactoryPerm.DASHBOARD_READ)],
)
async def factory_stats(session: DbSession, auth: FactoryAuth) -> FactoryStatsResponse:
    """工厂工作台统计。"""
    return await factory_service.stats(session, auth)


# ===========================================================================
# 二、生产工单
# ===========================================================================


@router.get(
    "/orders",
    response_model=PageResult[FactoryOrderResponse],
    summary="生产工单列表",
    description=(
        "分页查询**本厂**工单。`status` 按工单状态筛选，`keyword` 同时匹配"
        "工单号与来源订单号（派单通知里给的是订单号，产线看板上贴的是工单号）。\n\n"
        "客户名已脱敏（`customerNameMasked`，如「中**动」）：工厂是跨租户角色，"
        "只需要知道「这是同一个客户的第几张单」，不需要知道客户是谁。"
    ),
    dependencies=[require_perm(FactoryPerm.ORDER_READ)],
)
async def list_factory_orders(
    session: DbSession,
    auth: FactoryAuth,
    page: PageQuery,
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
        sort_by=page.sort_by,
        order=page.order,
    )
    return page_response(records, total, page)


@router.get(
    "/orders/{factory_order_id}",
    response_model=FactoryOrderDetailResponse,
    summary="生产工单详情",
    description=(
        "含烧录上报历史、抽检记录与抽检汇总，以及订单侧状态"
        "（`orderStatus`）——工厂需要知道「这单在订单侧走到哪一步」，"
        "否则工单烧完后迟迟没有下文时无从判断卡在哪。\n\n"
        "`inspections` 数组有上限（200 条），`inspectionSummary` 是**全量**"
        "聚合口径，因此合格率不受截断影响。"
    ),
    responses={404: {"description": "工单不存在或不属于本厂"}},
    dependencies=[require_perm(FactoryPerm.ORDER_READ)],
)
async def get_factory_order_detail(
    session: DbSession,
    auth: FactoryAuth,
    factory_order_id: str,
) -> FactoryOrderDetailResponse:
    """生产工单详情。"""
    order = await factory_service.get_factory_order(session, auth, factory_order_id)
    return await factory_service.build_detail(session, auth, order)


@router.post(
    "/orders/{factory_order_id}/burn",
    response_model=FactoryOrderDetailResponse,
    summary="烧录上报",
    description=(
        "上报本次烧录数量（可分批）。累计数量达到委托数量时自动完成：\n\n"
        "- 工单 `PRODUCING → COMPLETED`（若此前还是 `PENDING`，先补一步"
        " `PENDING → PRODUCING`——状态机只有这一条路径）\n"
        "- 该订单下 `PRODUCING` 设备 `→ PRODUCED`\n"
        "- 订单 `PRODUCING → SHIPPED_TO_CLIENT`\n\n"
        "**不可超报**：超出剩余数量返回 `BURN_COUNT_EXCEEDED`，"
        "`details` 带 `remaining` / `quantity` / `burnedCount` / `currentBurnedCount`。\n\n"
        "**已完成 / 已出货的工单不再接受上报**（409）。此时数量校验必然也不通过，"
        "但「这单已经做完了」才是真正的原因——先报数量错误会让工厂以为是自己"
        "数字填错，反复重试而看不到「已完成」这个事实。"
    ),
    responses={
        400: {"description": "上报数量超出剩余数量"},
        404: {"description": "工单不存在或不属于本厂"},
        409: {"description": "工单已完成 / 已出货"},
    },
    dependencies=[require_perm(FactoryPerm.BURN_WRITE)],
)
async def report_burn(
    request: Request,
    session: DbSession,
    auth: FactoryAuth,
    factory_order_id: str,
    payload: BurnReportRequest = Body(...),
) -> FactoryOrderDetailResponse:
    """烧录上报。"""
    return await factory_service.report_burn(
        session,
        auth,
        factory_order_id=factory_order_id,
        burned_count=payload.burned_count,
        sn_from=payload.sn_from,
        sn_to=payload.sn_to,
        note=payload.note,
        request=request,
    )


@router.post(
    "/orders/{factory_order_id}/ship",
    response_model=FactoryOrderDetailResponse,
    summary="出货登记",
    description=(
        "工单 `COMPLETED → SHIPPED`，该订单下 `PRODUCED` 设备 `→ SHIPPED`。\n\n"
        "为什么设备必须走到 `SHIPPED`？因为可分配的设备状态"
        "（`ALLOCATABLE_ASSET_STATUSES`）含 `SHIPPED`——设备出货后才能被"
        "分配到客户名下。若停在 `PRODUCED`，分配单会判它「状态不可用」，"
        "业务链就断在最后一步。\n\n"
        "**仅烧录完成的工单可出货**，否则 409。"
    ),
    responses={
        404: {"description": "工单不存在或不属于本厂"},
        409: {"description": "工单未完成，不允许出货"},
    },
    dependencies=[require_perm(FactoryPerm.BURN_WRITE)],
)
async def register_shipment(
    request: Request,
    session: DbSession,
    auth: FactoryAuth,
    factory_order_id: str,
) -> FactoryOrderDetailResponse:
    """出货登记。"""
    return await factory_service.register_shipment(
        session, auth, factory_order_id=factory_order_id, request=request
    )


@router.post(
    "/orders/{factory_order_id}/inspect",
    response_model=InspectionResponse,
    status_code=201,
    summary="抽检上报",
    description=(
        "记录一次抽检（针对**具体 SN**）。\n\n"
        "**抽检不改工单状态、也不改设备状态**：抽检不合格的真实含义是"
        "「需要人决定返工 / 降级 / 让步接收」，自动把工单打回 `PENDING` 会掩盖"
        "真实问题（工厂再点一次烧录上报就「恢复」了）。处置是人的决定，"
        "系统负责把证据留全。\n\n"
        "同一 SN 允许被抽检多次：抽检是**观测**不是**状态**，留存历史才能看出"
        "「第一次不合格、返工后合格」这样的过程。\n\n"
        "SN 不存在 → 404；SN 属于别的订单 → 400（`details.field=sn`）。"
    ),
    responses={
        400: {"description": "SN 不属于本工单"},
        404: {"description": "工单不存在或设备不存在"},
    },
    dependencies=[require_perm(FactoryPerm.INSPECT_WRITE)],
)
async def create_inspection(
    request: Request,
    session: DbSession,
    auth: FactoryAuth,
    factory_order_id: str,
    payload: InspectionCreateRequest = Body(...),
) -> InspectionResponse:
    """抽检上报。"""
    return await factory_service.create_inspection(
        session,
        auth,
        factory_order_id=factory_order_id,
        sn=payload.sn,
        result=payload.result,
        defect_code=payload.defect_code,
        note=payload.note,
        request=request,
    )


@router.get(
    "/orders/{factory_order_id}/qrcodes",
    response_model=ListResult[QrCodeItemResponse],
    summary="工单二维码清单",
    description=(
        "返回工单对应订单下全部设备的二维码文本（`payload`），前端据此渲染图形。\n\n"
        "- 集贤 4G：`JX|{SN}|{IMEI}|{ICCID}|{deviceId}`\n"
        "- 京东 Wi-Fi：`JD|{tenantId}|{productId}|{sn}|{sign}`，`sign` 为 HMAC-SHA256\n\n"
        "与平台端「导出订单二维码」是**同一套生成逻辑**：工厂要打印的就是同一批"
        "设备的二维码，各写一份必然出现两边不一致。"
    ),
    responses={404: {"description": "工单不存在或不属于本厂"}},
    dependencies=[require_perm(FactoryPerm.ORDER_READ)],
)
async def export_factory_order_qrcodes(
    session: DbSession,
    auth: FactoryAuth,
    factory_order_id: str,
) -> dict[str, Any]:
    """工单二维码清单。"""
    items = await factory_service.qrcodes_for_factory_order(session, auth, factory_order_id)
    records = [
        QrCodeItemResponse(
            device_id=item.device_id,
            sn=item.sn,
            network_type=item.network_type,
            payload=item.payload,
            format=str(item.format),
        )
        for item in items
    ]
    return {"records": records, "total": len(records)}


# ===========================================================================
# 三、抽检记录
# ===========================================================================


@router.get(
    "/inspections",
    response_model=PageResult[InspectionResponse],
    summary="抽检记录列表",
    description=(
        "分页查询**本厂**抽检记录。`factoryOrderId` 按工单筛选，`sn` 精确匹配"
        "某一台的抽检历史，`result` 按合格 / 不合格筛选。\n\n"
        "抽检记录表本身没有工厂字段，作用域由「抽检 ⋈ 工单」收口，"
        "因此不会查出别家工厂的记录。"
    ),
    dependencies=[require_perm(FactoryPerm.ORDER_READ)],
)
async def list_inspections(
    session: DbSession,
    auth: FactoryAuth,
    page: PageQuery,
    factory_order_id: Annotated[
        str | None, Query(alias="factoryOrderId", description="按工单筛选")
    ] = None,
    sn: Annotated[str | None, Query(description="按 SN 精确筛选")] = None,
    result: Annotated[InspectionResult | None, Query(description="抽检结果")] = None,
) -> dict[str, Any]:
    """抽检记录列表。"""
    records, total = await factory_service.list_inspections(
        session,
        auth,
        offset=page.offset,
        limit=page.limit,
        factory_order_id=factory_order_id,
        sn=sn,
        result=str(result) if result else None,
    )
    return page_response(records, total, page)


# ===========================================================================
# 四、固件版本
# ===========================================================================


@router.get(
    "/firmwares",
    response_model=ListResult[FirmwareVersionResponse],
    summary="固件版本列表",
    description=(
        "本厂工单里出现过的固件版本聚合（版本号 / 工单数 / 委托数量 / 已烧录数量），"
        "按版本号倒序，不分页。\n\n"
        "数据源是工单而不是固件包台账：固件包管理（上传、灰度、OTA 推送）属于"
        "后续的 OTA 阶段，本阶段工厂端只需要回答「我要烧哪几个版本、各多少台」。"
        "另建一张表会立刻产生一批「有版本号但没有任何固件文件」的空记录。"
    ),
    dependencies=[require_perm(FactoryPerm.FIRMWARE_READ)],
)
async def list_firmwares(
    session: DbSession, auth: FactoryAuth
) -> dict[str, Any]:
    """固件版本列表。"""
    records = await factory_service.list_firmwares(session, auth)
    return {"records": records, "total": len(records)}


__all__ = ["router"]
