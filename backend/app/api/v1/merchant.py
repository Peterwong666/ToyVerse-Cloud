"""商户端 API：我的产品 / 我的订单（P4）。

租户隔离
--------
本模块**不出现任何手写的 ``tenant_id`` 过滤条件**：

* 产品列表经 :func:`app.services.catalog_service.list_client_products`
  （内部走 ``scoped()``，ADR-08 的唯一收口点）；
* 订单列表经 :func:`app.services.order_service.list_orders`（同上）；
* 两个详情查询都走 ``assert_visible``，越权返回「不存在」语义而非「无权限」——
  后者会让人靠错误码探测出其他租户的资源是否存在。

为什么下单要配套一个产品列表
----------------------------
``POST /merchant/orders`` 要求 ``clientProductId``，而 ``client_products.id``
是随机主键（``cprod-xxxxxxxx``），人不可能记住。没有列表接口，
商户端下单就只能让人手填 ID——接口能跑通，界面走不通。
因此 :func:`list_my_products` 不是「顺手加的查询」，而是下单闭环的必要一环
（P9 的商户端「我的产品」页也复用它）。

下单为什么必须支持幂等键
------------------------
下单是**有副作用的写操作**且用户重复点击很常见（网络慢时用户会连点）。
不带幂等键时，一次超时重试就可能造出两张订单、两批设备。
服务端实现三段式（见 :mod:`app.core.idempotency`）：

1. ``find_completed`` —— 命中已完成记录 → 直接回放首次响应（含状态码语义）；
2. ``reserve`` —— 占位，防止并发重复提交；
3. ``finalize`` / ``release`` —— 成功存快照，失败释放以便客户端用同一键重试。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Query, Request

from app.core.deps import DbSession, MerchantAuth, require_perm
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
from app.core.permissions import MerchantPerm
from app.db.scope import assert_visible
from app.models.enums import EnableStatus, OrderStatus
from app.schemas.catalog import ClientProductResponse
from app.schemas.common import PageResult
from app.schemas.order import MerchantOrderCreateRequest, OrderDetailResponse, OrderResponse
from app.services import catalog_service, order_service

router = APIRouter(prefix="/merchant", tags=["商户端 · 产品与订单"])

PageQuery = Annotated[PageParams, Depends(page_params)]

#: 幂等作用域用的端点标识（与路由路径一致，便于排查）
CREATE_ORDER_ENDPOINT = "POST /merchant/orders"

#: 幂等快照里存放「请求体摘要」的键。
#:
#: 为什么要把摘要塞进快照？``app.core.idempotency`` 的公开接口
#: ``find_completed`` 只回放**响应体**，不返回请求摘要，因此无法直接判断
#: 「同一个键是否被用于不同的请求体」。把摘要一并存进快照后，
#: 回放时就能比对：不一致 → ``IDEMPOTENCY_CONFLICT``，从而落实
#: 「同键不同体必须拒绝」，而不是把首次的响应错发给另一个请求。
FINGERPRINT_FIELD = "_requestHash"


def _to_response(detail: OrderDetailResponse) -> OrderResponse:
    """从详情裁剪出列表项形状（审核 / 创建后统一响应形状）。"""
    return OrderResponse(**detail.model_dump(exclude={"generation_detail", "devices"}))


# ===========================================================================
# 一、我的产品
# ===========================================================================


@router.get(
    "/products",
    response_model=PageResult[ClientProductResponse],
    summary="我的产品列表",
    description=(
        "分页查询**本租户**的客户产品（下单时 `clientProductId` 的取值来源）。\n\n"
        "租户过滤在服务层经 `scoped()` 收口（ADR-08），商户不可能看到其他租户的产品；"
        "响应复用平台端的 `ClientProductResponse`，字段与平台视角一致——"
        "少给字段会让前端误以为「没有这个数据」，而隔离本来由作用域保证，"
        "无需靠裁剪字段来加固。"
    ),
    dependencies=[require_perm(MerchantPerm.PRODUCT_READ)],
)
async def list_my_products(
    session: DbSession,
    auth: MerchantAuth,
    page: PageQuery,
    keyword: Annotated[str | None, Query(description="名称 / 编码")] = None,
    status: Annotated[EnableStatus | None, Query(description="产品状态")] = None,
) -> dict[str, Any]:
    """我的产品列表。"""
    records, total = await catalog_service.list_client_products(
        session,
        auth,
        keyword=keyword,
        status=str(status) if status else None,
        offset=page.offset,
        limit=page.limit,
        sort_by=page.sort_by,
        order=page.order,
    )
    return page_response(records, total, page)


@router.get(
    "/products/{product_id}",
    response_model=ClientProductResponse,
    summary="我的产品详情",
    description=(
        "本租户客户产品详情。访问其他租户的产品返回 **404**（不返回 403）——"
        "避免通过错误码探测他人资源是否存在。"
    ),
    responses={404: {"description": "产品不存在或不属于本租户"}},
    dependencies=[require_perm(MerchantPerm.PRODUCT_READ)],
)
async def get_my_product(
    session: DbSession,
    auth: MerchantAuth,
    product_id: str,
) -> ClientProductResponse:
    """我的产品详情。"""
    # 先取 ORM 对象做归属校验：不存在与越权都返回 404，信息量一致
    product = await catalog_service.get_client_product(session, product_id)
    assert_visible(product.tenant_id, auth, resource="产品")
    return await catalog_service.get_client_product_detail(session, product_id)


# ===========================================================================
# 二、我的订单
# ===========================================================================


@router.get(
    "/orders",
    response_model=PageResult[OrderResponse],
    summary="我的订单列表",
    description=(
        "分页查询**本租户**的订单（租户过滤在服务层经 `scoped()` 收口）。"
        "支持按状态筛选，`keyword` 匹配订单号。"
    ),
    dependencies=[require_perm(MerchantPerm.ORDER_READ)],
)
async def list_my_orders(
    session: DbSession,
    auth: MerchantAuth,
    page: PageQuery,
    status: Annotated[OrderStatus | None, Query(description="订单状态")] = None,
    client_product_id: Annotated[
        str | None,
        Query(alias="clientProductId", description="按客户产品筛选"),
    ] = None,
    keyword: Annotated[str | None, Query(description="订单号关键字")] = None,
) -> dict[str, Any]:
    """我的订单列表。"""
    records, total = await order_service.list_orders(
        session,
        auth,
        offset=page.offset,
        limit=page.limit,
        status=str(status) if status else None,
        client_product_id=client_product_id,
        keyword=keyword,
        sort_by=page.sort_by,
        order=page.order,
    )
    return page_response(records, total, page)


@router.post(
    "/orders",
    response_model=OrderResponse,
    status_code=201,
    summary="下单",
    description=(
        "为**本租户**下单，订单初始状态为 `PENDING_AUDIT`，等待平台审核。\n\n"
        "`clientProductId` 必须属于本租户且已启用，否则按「不存在」处理；"
        "联网方式会从客户产品**快照**到订单，产品后续改配置不影响本订单。\n\n"
        "**幂等**：可携带请求头 `Idempotency-Key`。重复提交同一键会回放首次响应；"
        "同一键配不同请求体会返回 `IDEMPOTENCY_CONFLICT`。"
    ),
    responses={
        404: {"description": "客户产品不存在或不属于本租户"},
        409: {"description": "产品已停用或幂等冲突"},
    },
    dependencies=[require_perm(MerchantPerm.ORDER_WRITE)],
)
async def create_order(
    request: Request,
    session: DbSession,
    auth: MerchantAuth,
    payload: MerchantOrderCreateRequest = Body(...),
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", description="幂等键（可选，建议客户端生成 UUID）"),
    ] = None,
) -> OrderResponse:
    """商户下单（支持幂等键）。"""
    tenant_id = auth.require_tenant_id()
    key = (idempotency_key or "").strip() or None
    scope = build_scope(CREATE_ORDER_ENDPOINT, tenant_id)
    fingerprint = request_fingerprint(payload.model_dump())

    if key:
        replay = await find_completed(session, key, scope)
        if replay is not None:
            stored_hash = replay.get(FINGERPRINT_FIELD)
            if stored_hash is not None and stored_hash != fingerprint:
                raise idempotency_conflict("该幂等键已用于不同的请求体，已拒绝")
            # 回放首次响应：客户端拿到同样的结果，不会重复下单
            return OrderResponse.model_validate(replay["data"])
        await reserve(
            session,
            key=key,
            scope=scope,
            tenant_id=tenant_id,
            user_id=auth.user_id,
            request_hash=fingerprint,
        )

    try:
        order = await order_service.create_order(
            session,
            auth,
            tenant_id=tenant_id,
            client_product_id=payload.client_product_id,
            quantity=payload.quantity,
            applicant_name=payload.applicant_name,
            applicant_phone=payload.applicant_phone,
            remark=payload.remark,
            request=request,
        )
        detail = await order_service.build_order_detail(session, auth, order)
        response = _to_response(detail)
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
            # 直接用 python 模式会带出 datetime 对象，写入时报不可序列化。
            body={
                FINGERPRINT_FIELD: fingerprint,
                "data": response.model_dump(mode="json", by_alias=True),
            },
        )
    await session.commit()
    return response


@router.get(
    "/orders/{order_id}",
    response_model=OrderDetailResponse,
    summary="我的订单详情",
    description=(
        "本租户订单详情。访问其他租户的订单会返回 **404**（不返回 403）——"
        "避免通过错误码探测他人资源是否存在。"
    ),
    responses={404: {"description": "订单不存在"}},
    dependencies=[require_perm(MerchantPerm.ORDER_READ)],
)
async def get_my_order(
    session: DbSession,
    auth: MerchantAuth,
    order_id: str,
) -> OrderDetailResponse:
    """我的订单详情。"""
    order = await order_service.get_visible_order(session, auth, order_id)
    return await order_service.build_order_detail(session, auth, order)


__all__ = ["router"]
