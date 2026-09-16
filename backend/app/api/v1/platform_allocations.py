"""平台端 API：分配单（P5）。

为什么单独开一个模块而不往 ``platform_orders.py`` 里加？
--------------------------------------------------------
P5（设备 / 分配 / 绑定）与其它阶段是**并行开发**的两条线，追加到同一个文件
必然产生冲突（两人同时改同一文件的末尾、``__all__``、import 块）。
拆成独立模块后，两边只在 ``router.py`` 的一行导入上相遇，
冲突面从「整段代码」缩小到「一行 include」；
语义上也更清晰：``platform_orders.py`` 管订单与库存设备，
本模块管「平台把库存交给哪个租户」。

路由层职责边界
--------------
只做「解析参数 → 调服务 → 转响应」：不写状态机、不写租户过滤
（ADR-08 的唯一收口点是 :func:`app.db.scope.scoped`）。

创建一个有副作用的写操作为什么必须支持幂等键
--------------------------------------------
分配单一旦执行就会改写几百台设备的归属，而「网络慢时用户连点两次」
是线上最常见的重复提交来源。不带幂等键时，一次超时重试就会造出两张
分配单，第二张执行时才发现设备已被分走（表现为一堆莫名其妙的
``DEVICE_NOT_IN_TENANT``）。三段式实现见 :mod:`app.core.idempotency`。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Query, Request

from app.core.deps import DbSession, PlatformAuth, require_perm
from app.core.errors import AppException, idempotency_conflict
from app.core.idempotency import (
    build_scope,
    finalize,
    find_completed,
    release,
    request_fingerprint,
    reserve,
)
from app.core.pagination import PageParams, page_params, page_response
from app.core.permissions import PlatformPerm
from app.models.enums import AllocationItemStatus, AllocationStatus
from app.schemas.allocation import (
    AllocationExecuteResult,
    AllocationItemResponse,
    AllocationOrderCreateRequest,
    AllocationOrderDetailResponse,
    AllocationOrderResponse,
)
from app.schemas.common import PageResult
from app.services import allocation_service

router = APIRouter(prefix="/platform", tags=["平台端 · 分配"])

PageQuery = Annotated[PageParams, Depends(page_params)]

#: 幂等作用域用的端点标识。
#: 平台端没有租户归属，租户部分固定为 ``platform``（见 ``build_scope``）。
CREATE_ALLOCATION_ENDPOINT = "POST /platform/allocations"

#: 幂等快照里存放「请求体摘要」的键（与商户端下单同一约定）。
#:
#: 为什么要把摘要塞进快照？``find_completed`` 只回放**响应体**，
#: 不返回请求摘要，因此无法直接判断「同一个键是否被用于不同的请求体」。
#: 存进快照后，回放时即可比对：不一致 → ``IDEMPOTENCY_CONFLICT``，
#: 而不是把首次的响应错发给另一个请求。
FINGERPRINT_FIELD = "_requestHash"


# ===========================================================================
# 一、分配单
# ===========================================================================


@router.get(
    "/allocations",
    response_model=PageResult[AllocationOrderResponse],
    summary="分配单列表",
    description=(
        "分页查询分配单（平台全局长）。支持按状态 / 目标租户筛选，"
        "`keyword` 匹配分配单号。"
    ),
    dependencies=[require_perm(PlatformPerm.ALLOCATION_READ)],
)
async def list_allocations(
    session: DbSession,
    auth: PlatformAuth,
    page: PageQuery,
    status: Annotated[AllocationStatus | None, Query(description="分配单状态")] = None,
    tenant_id: Annotated[
        str | None, Query(alias="tenantId", description="按目标租户筛选")
    ] = None,
    keyword: Annotated[str | None, Query(description="分配单号关键字")] = None,
) -> dict[str, Any]:
    """分配单列表。"""
    records, total = await allocation_service.list_allocations(
        session,
        auth,
        offset=page.offset,
        limit=page.limit,
        status=str(status) if status else None,
        tenant_id=tenant_id,
        keyword=keyword,
    )
    return page_response(records, total, page)


@router.post(
    "/allocations",
    response_model=AllocationOrderResponse,
    status_code=201,
    summary="创建分配单",
    description=(
        "创建分配单（`DRAFT`），把一批设备分配给指定租户的指定客户产品。\n\n"
        "**创建阶段只做硬校验**：目标租户必须存在且 `ACTIVE`；客户产品必须存在"
        "且属于该租户（否则按「不存在」处理）。设备是否可分配留给"
        "「执行」阶段逐行判定——那才是库存真正被扣减的时刻。\n\n"
        "**幂等**：可携带请求头 `Idempotency-Key`。重复提交同一键会回放首次响应；"
        "同一键配不同请求体会返回 `IDEMPOTENCY_CONFLICT`。"
    ),
    responses={
        403: {"description": "目标租户已被禁用"},
        404: {"description": "租户或客户产品不存在"},
        409: {"description": "幂等冲突"},
    },
    dependencies=[require_perm(PlatformPerm.ALLOCATION_WRITE)],
)
async def create_allocation(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    payload: AllocationOrderCreateRequest = Body(...),
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", description="幂等键（可选，建议客户端生成 UUID）"),
    ] = None,
) -> AllocationOrderResponse:
    """创建分配单（支持幂等键）。"""
    key = (idempotency_key or "").strip() or None
    scope = build_scope(CREATE_ALLOCATION_ENDPOINT, None)
    fingerprint = request_fingerprint(payload.model_dump())

    if key:
        replay = await find_completed(session, key, scope)
        if replay is not None:
            stored_hash = replay.get(FINGERPRINT_FIELD)
            if stored_hash is not None and stored_hash != fingerprint:
                raise idempotency_conflict("该幂等键已用于不同的请求体，已拒绝")
            return AllocationOrderResponse.model_validate(replay["data"])
        await reserve(
            session,
            key=key,
            scope=scope,
            tenant_id=None,
            user_id=auth.user_id,
            request_hash=fingerprint,
        )

    try:
        order = await allocation_service.create_allocation(
            session,
            auth,
            tenant_id=payload.tenant_id,
            client_product_id=payload.client_product_id,
            device_ids=payload.device_ids,
            remark=payload.remark,
            request=request,
        )
        response = allocation_service.to_response(order)
    except AppException:
        # 业务失败 → 释放占位，允许客户端用同一幂等键重试
        if key:
            await release(session, key=key, scope=scope)
        raise

    if key:
        await finalize(
            session,
            key=key,
            scope=scope,
            status=201,
            # mode="json" 是必须的：幂等快照要落 JSON 列，
            # python 模式会带出 datetime 对象，写入时报不可序列化。
            body={
                FINGERPRINT_FIELD: fingerprint,
                "data": response.model_dump(mode="json", by_alias=True),
            },
        )
    await session.commit()
    return response


@router.get(
    "/allocations/{allocation_id}",
    response_model=AllocationOrderDetailResponse,
    summary="分配单详情",
    description=(
        "含租户 / 客户产品名称与编码、联网方式，以及**全部明细行**（逐行的成功 / "
        "失败原因）。明细只在详情里返回：列表页带 500 行明细会把列表接口拖垮。"
    ),
    responses={404: {"description": "分配单不存在"}},
    dependencies=[require_perm(PlatformPerm.ALLOCATION_READ)],
)
async def get_allocation_detail(
    session: DbSession,
    auth: PlatformAuth,
    allocation_id: str,
) -> AllocationOrderDetailResponse:
    """分配单详情。"""
    order = await allocation_service.get_allocation(session, auth, allocation_id)
    return await allocation_service.build_detail(session, auth, order)


@router.get(
    "/allocations/{allocation_id}/items",
    response_model=PageResult[AllocationItemResponse],
    summary="分配明细",
    description="分页查询分配明细行（按创建顺序），可按行状态筛选。",
    responses={404: {"description": "分配单不存在"}},
    dependencies=[require_perm(PlatformPerm.ALLOCATION_READ)],
)
async def list_allocation_items(
    session: DbSession,
    auth: PlatformAuth,
    allocation_id: str,
    page: PageQuery,
    status: Annotated[
        AllocationItemStatus | None,
        Query(description="行状态 PENDING / ALLOCATED / FAILED / SKIPPED"),
    ] = None,
) -> dict[str, Any]:
    """分配明细。"""
    records, total = await allocation_service.list_items(
        session,
        auth,
        allocation_id,
        offset=page.offset,
        limit=page.limit,
        status=str(status) if status else None,
    )
    return page_response(
        [allocation_service.item_to_response(item) for item in records], total, page
    )


@router.post(
    "/allocations/{allocation_id}/execute",
    response_model=AllocationExecuteResult,
    summary="执行分配单",
    description=(
        "把明细中的设备划到目标租户名下（`IN_STOCK → ALLOCATED`）。\n\n"
        "**逐行处理，不做整单回滚**：成功的行立刻保留，失败的行只标自己"
        "（`status=FAILED` + `errorMessage`），整单按「有没有失败」落 "
        "`COMPLETED` / `FAILED`。重跑只补未成功的明细，已成功的行记为 `SKIPPED`。\n\n"
        "**幂等**：`COMPLETED` 的分配单再次调用不会做任何迁移，直接回放既有计数。\n\n"
        "逐行判定优先级：设备不存在 → 不属于本次租户 → 已在本租户且已分配（跳过）"
        "→ 已冻结 → 不可分配状态；产品授权是**整单一次**校验，失败则整单失败。"
    ),
    responses={
        404: {"description": "分配单不存在"},
        409: {"description": "分配单当前状态不允许执行"},
    },
    dependencies=[require_perm(PlatformPerm.ALLOCATION_WRITE)],
)
async def execute_allocation(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    allocation_id: str,
) -> AllocationExecuteResult:
    """执行分配单。"""
    return await allocation_service.execute_allocation(
        session, auth, allocation_id=allocation_id, request=request
    )


__all__ = ["router"]
