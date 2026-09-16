"""商户端 API：我的产品 / 我的订单（P4）· 我的设备 / 绑定（P5）。

租户隔离
--------
本模块**不出现任何手写的 ``tenant_id`` 过滤条件**：

* 产品列表经 :func:`app.services.catalog_service.list_client_products`
  （内部走 ``scoped()``，ADR-08 的唯一收口点）；
* 订单列表经 :func:`app.services.order_service.list_orders`（同上）；
* 设备与绑定列表分别经 ``device_service.list_devices`` /
  ``binding_service.list_bindings``（同上）；
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

绑定 / 解绑沿用同一套三段式，但多一条硬性顺序（P5）
---------------------------------------------------
``bind`` 的回放必须发生在**令牌校验之前**：确认令牌在首次绑定成功时就被
销毁（摘要置空），若先校验令牌再查快照，同一个键的第二次请求会拿到
``QR_EXPIRED``——那就不叫「重放返回同一条」了。
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
from app.models.enums import (
    ActivationStatus,
    AssetStatus,
    BindingRecordStatus,
    BindStatus,
    EnableStatus,
    OnlineStatus,
    OrderStatus,
)
from app.schemas.binding import (
    BindingConfirmRequest,
    BindingPrecheckRequest,
    BindingPrecheckResponse,
    BindingResponse,
    BindingUnbindRequest,
)
from app.schemas.catalog import ClientProductResponse
from app.schemas.common import PageResult
from app.schemas.device import DeviceDetailResponse, DeviceEventResponse, DeviceResponse
from app.schemas.order import MerchantOrderCreateRequest, OrderDetailResponse, OrderResponse
from app.services import binding_service, catalog_service, device_service, order_service

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


# ===========================================================================
# 三、我的设备（P5）
# ===========================================================================


@router.get(
    "/devices",
    response_model=PageResult[DeviceResponse],
    summary="我的设备列表",
    description=(
        "分页查询**本租户**的设备（租户过滤在服务层经 `scoped()` 收口，ADR-08）。"
        "支持按客户产品与四维状态筛选，`keyword` 匹配 SN / IMEI / MAC。\n\n"
        "`onlineStatus` 按**派生在线判据**筛选（最后一次心跳是否落在 "
        "`ONLINE_WINDOW_SECONDS` 窗口内），因此「落库还是 ONLINE 但已超窗」"
        "的设备会出现在 `OFFLINE` 里——展示字段 `online` 与它口径一致。\n\n"
        "平台自有库存（`tenantId` 为空）不会出现在这里：租户作用域天然把它挡在外面。"
    ),
    dependencies=[require_perm(MerchantPerm.DEVICE_READ)],
)
async def list_my_devices(
    session: DbSession,
    auth: MerchantAuth,
    page: PageQuery,
    client_product_id: Annotated[
        str | None, Query(alias="clientProductId", description="按客户产品筛选")
    ] = None,
    asset_status: Annotated[AssetStatus | None, Query(alias="assetStatus")] = None,
    activation_status: Annotated[ActivationStatus | None, Query(alias="activationStatus")] = None,
    online_status: Annotated[OnlineStatus | None, Query(alias="onlineStatus")] = None,
    bind_status: Annotated[BindStatus | None, Query(alias="bindStatus")] = None,
    keyword: Annotated[str | None, Query(description="SN / IMEI / MAC")] = None,
) -> dict[str, Any]:
    """我的设备列表。"""
    records, total = await device_service.list_devices(
        session,
        auth,
        offset=page.offset,
        limit=page.limit,
        client_product_id=client_product_id,
        asset_status=str(asset_status) if asset_status else None,
        activation_status=str(activation_status) if activation_status else None,
        online_status=str(online_status) if online_status else None,
        bind_status=str(bind_status) if bind_status else None,
        keyword=keyword,
        sort_by=page.sort_by,
        order=page.order,
    )
    return page_response([device_service.to_response(item) for item in records], total, page)


@router.get(
    "/devices/{device_id}",
    response_model=DeviceDetailResponse,
    summary="我的设备详情",
    description=(
        "含四维状态派生标签 `label`、派生在线判据 `online`、产品名称、"
        "**凭证掩码**（只有 `secretHint`，永不含明文密钥）、最近事件时间线，"
        "以及当前绑定记录 `binding`（未绑定过为 `null`）。\n\n"
        "访问其他租户的设备返回 **404**（不返回 403）——避免通过错误码探测。"
    ),
    responses={404: {"description": "设备不存在或不属于本租户"}},
    dependencies=[require_perm(MerchantPerm.DEVICE_READ)],
)
async def get_my_device(
    session: DbSession,
    auth: MerchantAuth,
    device_id: str,
) -> DeviceDetailResponse:
    """我的设备详情。"""
    device = await device_service.get_device(session, auth, device_id)
    return await device_service.build_detail(session, auth, device)


@router.get(
    "/devices/{device_id}/events",
    response_model=PageResult[DeviceEventResponse],
    summary="我的设备事件时间线",
    description="分页查询设备的流转事件（按时间倒序），含资产 / 激活 / 在线 / 绑定四个维度。",
    responses={404: {"description": "设备不存在或不属于本租户"}},
    dependencies=[require_perm(MerchantPerm.DEVICE_READ)],
)
async def list_my_device_events(
    session: DbSession,
    auth: MerchantAuth,
    device_id: str,
    page: PageQuery,
) -> dict[str, Any]:
    """我的设备事件时间线。"""
    records, total = await device_service.list_events(
        session, auth, device_id, offset=page.offset, limit=page.limit
    )
    return page_response(
        [device_service.event_to_response(item) for item in records], total, page
    )


# ===========================================================================
# 四、设备绑定（P5：扫码两步流程）
# ===========================================================================

#: 绑定 / 解绑的幂等作用域端点标识（用**实际路径**，含设备 ID——
#: 不同设备的绑定互不干扰，同一个键不会因为换了设备而被误解为重放）。
BIND_ENDPOINT = "POST /merchant/devices/{device_id}/bind"
UNBIND_ENDPOINT = "POST /merchant/devices/{device_id}/unbind"


@router.post(
    "/devices/bind/precheck",
    response_model=BindingPrecheckResponse,
    summary="扫码预检",
    description=(
        "提交扫码结果，判定「这台设备能不能绑、绑给谁」，并签发一次性确认令牌。\n\n"
        "判定顺序即错误码优先级：二维码格式 / 签名（`QR_INVALID`）→ 设备是否存在"
        "（`DEVICE_NOT_FOUND`）→ 载荷摘要与设备记录是否一致（`QR_INVALID`，"
        "挡住字段被改过的码）→ 租户匹配（`DEVICE_NOT_IN_TENANT`）→ 冻结"
        "（`DEVICE_FROZEN`）→ 单绑约束（`DEVICE_ALREADY_BOUND`，`details` 带原绑定"
        "ID / 时间 / 终端用户）→ 可绑状态（只有 `ALLOCATED` 可绑）→ 产品授权"
        "（`PRODUCT_NOT_AUTHORIZED`）。\n\n"
        "**令牌明文仅此一次返回**（`confirmToken`），有效期 "
        "`QR_CONFIRM_TOKEN_TTL_SECONDS`（默认 300 秒）、用一次即销毁；"
        "库内只存 SHA-256 摘要，二维码载荷也只存摘要。"
    ),
    responses={
        404: {"description": "二维码无效（QR_INVALID）或设备不存在"},
        403: {"description": "设备不属于当前租户"},
        409: {"description": "设备已绑定 / 已冻结 / 状态不可绑 / 产品未授权"},
    },
    dependencies=[require_perm(MerchantPerm.BINDING_WRITE)],
)
async def precheck_binding(
    session: DbSession,
    auth: MerchantAuth,
    payload: BindingPrecheckRequest = Body(...),
) -> BindingPrecheckResponse:
    """扫码预检（签发一次性确认令牌）。"""
    return await binding_service.precheck(session, auth, qr_payload=payload.qr_payload)


@router.post(
    "/devices/{device_id}/bind",
    response_model=BindingResponse,
    summary="确认绑定",
    description=(
        "用 `precheck` 返回的 `confirmToken` 完成绑定，设备资产状态 "
        "`ALLOCATED → BOUND`、`bindStatus` 置为 `BOUND`。\n\n"
        "**幂等**：可携带请求头 `Idempotency-Key`；同一键的重放**先于令牌校验**"
        "返回首次响应（令牌在首次绑定成功时已销毁，否则重放会误报 `QR_EXPIRED`）。"
        "同一键配不同请求体会返回 `IDEMPOTENCY_CONFLICT`。"
    ),
    responses={
        404: {"description": "设备不存在或不属于本租户"},
        409: {"description": "令牌过期 / 已使用 / 设备已绑定 / 产品未授权"},
    },
    dependencies=[require_perm(MerchantPerm.BINDING_WRITE)],
)
async def bind_device(
    request: Request,
    session: DbSession,
    auth: MerchantAuth,
    device_id: str,
    payload: BindingConfirmRequest = Body(...),
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", description="幂等键（可选，建议客户端生成 UUID）"),
    ] = None,
) -> BindingResponse:
    """确认绑定（支持幂等键）。"""
    tenant_id = auth.require_tenant_id()
    key = (idempotency_key or "").strip() or None
    scope = build_scope(BIND_ENDPOINT.format(device_id=device_id), tenant_id)
    fingerprint = request_fingerprint(payload.model_dump())

    if key:
        replay = await find_completed(session, key, scope)
        if replay is not None:
            stored_hash = replay.get(FINGERPRINT_FIELD)
            if stored_hash is not None and stored_hash != fingerprint:
                raise idempotency_conflict("该幂等键已用于不同的请求体，已拒绝")
            return BindingResponse.model_validate(replay["data"])
        await reserve(
            session,
            key=key,
            scope=scope,
            tenant_id=tenant_id,
            user_id=auth.user_id,
            request_hash=fingerprint,
        )

    try:
        # 服务层已按契约返回 BindingResponse（含设备侧快照），router 不再二次转换
        response = await binding_service.bind(
            session,
            auth,
            device_id=device_id,
            confirm_token=payload.confirm_token,
            end_user_id=payload.end_user_id,
            remark=payload.remark,
            request=request,
        )
    except AppException:
        # 业务失败 → 释放占位，允许客户端用同一幂等键重试
        if key:
            await release(session, key=key, scope=scope)
        raise

    if key:
        # 绑定服务已提交业务事务，快照落在紧随其后的事务里。
        # 万一这里失败，客户端重试会拿到 409（占位仍在）而不是完成态——
        # 最坏情况是「要求换个键重试」，绝不会出现「重放导致重复绑定」。
        await finalize(
            session,
            key=key,
            scope=scope,
            status=200,
            body={
                FINGERPRINT_FIELD: fingerprint,
                "data": response.model_dump(mode="json", by_alias=True),
            },
        )
        await session.commit()
    return response


@router.post(
    "/devices/{device_id}/unbind",
    response_model=BindingResponse,
    summary="解绑设备",
    description=(
        "解绑设备（原因必填）。设备资产状态 `BOUND → ALLOCATED`、`bindStatus` 置为 "
        "`UNBOUND`——**不退回平台库存**：设备仍属于本租户，只是不再绑定终端用户。\n\n"
        "**幂等**：与绑定同一套三段式（作用域为解绑路径）。"
    ),
    responses={
        404: {"description": "设备不存在或不属于本租户"},
        409: {"description": "设备当前未绑定"},
    },
    dependencies=[require_perm(MerchantPerm.BINDING_WRITE)],
)
async def unbind_device(
    request: Request,
    session: DbSession,
    auth: MerchantAuth,
    device_id: str,
    payload: BindingUnbindRequest = Body(...),
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", description="幂等键（可选，建议客户端生成 UUID）"),
    ] = None,
) -> BindingResponse:
    """解绑设备（支持幂等键）。"""
    tenant_id = auth.require_tenant_id()
    key = (idempotency_key or "").strip() or None
    scope = build_scope(UNBIND_ENDPOINT.format(device_id=device_id), tenant_id)
    fingerprint = request_fingerprint(payload.model_dump())

    if key:
        replay = await find_completed(session, key, scope)
        if replay is not None:
            stored_hash = replay.get(FINGERPRINT_FIELD)
            if stored_hash is not None and stored_hash != fingerprint:
                raise idempotency_conflict("该幂等键已用于不同的请求体，已拒绝")
            return BindingResponse.model_validate(replay["data"])
        await reserve(
            session,
            key=key,
            scope=scope,
            tenant_id=tenant_id,
            user_id=auth.user_id,
            request_hash=fingerprint,
        )

    try:
        response = await binding_service.unbind(
            session, auth, device_id=device_id, reason=payload.reason, request=request
        )
    except AppException:
        if key:
            await release(session, key=key, scope=scope)
        raise

    if key:
        await finalize(
            session,
            key=key,
            scope=scope,
            status=200,
            body={
                FINGERPRINT_FIELD: fingerprint,
                "data": response.model_dump(mode="json", by_alias=True),
            },
        )
        await session.commit()
    return response


@router.get(
    "/bindings",
    response_model=PageResult[BindingResponse],
    summary="绑定记录列表",
    description=(
        "分页查询**本租户**的绑定记录（租户过滤在服务层经 `scoped()` 收口）。"
        "支持按状态 / 设备筛选，`keyword` 匹配设备 SN（服务层用子查询实现）。\n\n"
        "响应带设备侧快照（`sn` / `deviceLabel` / `assetStatus` / `bindStatus` / "
        "`networkType`），列表因此可以自解释，不需要前端再逐台回查设备。"
    ),
    dependencies=[require_perm(MerchantPerm.DEVICE_READ)],
)
async def list_my_bindings(
    session: DbSession,
    auth: MerchantAuth,
    page: PageQuery,
    status: Annotated[
        BindingRecordStatus | None,
        Query(description="绑定记录状态 PENDING / BOUND / UNBOUND"),
    ] = None,
    device_id: Annotated[str | None, Query(alias="deviceId", description="按设备筛选")] = None,
    keyword: Annotated[str | None, Query(description="设备 SN 关键字")] = None,
) -> dict[str, Any]:
    """我的绑定记录列表。"""
    records, total = await binding_service.list_bindings(
        session,
        auth,
        offset=page.offset,
        limit=page.limit,
        status=str(status) if status else None,
        device_id=device_id,
        keyword=keyword,
    )
    return page_response(records, total, page)


__all__ = ["router"]
