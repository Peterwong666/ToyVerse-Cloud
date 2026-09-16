"""平台端 API：订单 / 设备 / 批次（P4）。

为什么单独开一个模块而不往 ``app/api/v1/platform.py`` 里加？
------------------------------------------------------------
P4 与 P3 是**并行开发**的两条线，追加到同一个文件必然产生冲突
（两人同时改同一文件的末尾、``__all__`` 与 import 块）。
拆成独立模块后，两边只在 ``router.py`` 的一行导入上相遇，
冲突面从「整段代码」缩小到「一行 include」，且语义上更清晰：
``platform.py`` 管目录域（租户 / 云厂商 / 模板 / 授权 / 产品 / 小程序），
本模块管**业务主干**（订单 → 设备 → 批次）。

路由层职责边界
--------------
只做「解析参数 → 调服务 → 转响应」：不写状态机、不写租户过滤，
租户过滤统一在服务层经 :func:`app.db.scope.scoped` 收口（ADR-08）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, File, Query, Request, Response, UploadFile

from app.core.deps import DbSession, PlatformAuth, require_perm
from app.core.pagination import PageParams, page_params, page_response
from app.core.permissions import PlatformPerm
from app.db.scope import assert_visible
from app.models.enums import (
    ActivationStatus,
    AssetStatus,
    BatchStatus,
    BindStatus,
    OnlineStatus,
    OrderStatus,
)
from app.schemas.batch import BatchLineResponse, BatchResponse
from app.schemas.common import ListResult, PageResult
from app.schemas.device import (
    DeviceCredentialIssueRequest,
    DeviceCredentialIssueResult,
    DeviceDetailResponse,
    DeviceEventResponse,
    DeviceFreezeRequest,
    DeviceHeartbeatResponse,
    DeviceResponse,
    DeviceRetireRequest,
    DeviceSimulateHeartbeatRequest,
    DeviceStatsResponse,
    DeviceThawRequest,
    QrCodeItemResponse,
)
from app.schemas.order import GenerateResult, OrderAuditRequest, OrderDetailResponse, OrderResponse
from app.services import batch_service, device_service, heartbeat_service, order_service

router = APIRouter(prefix="/platform", tags=["平台端 · 订单与设备"])

PageQuery = Annotated[PageParams, Depends(page_params)]


# ===========================================================================
# 一、订单
# ===========================================================================


@router.get(
    "/orders",
    response_model=PageResult[OrderResponse],
    summary="订单列表",
    description="分页查询订单。支持按状态 / 租户 / 客户产品筛选，`keyword` 匹配订单号。",
    dependencies=[require_perm(PlatformPerm.ORDER_READ)],
)
async def list_orders(
    session: DbSession,
    auth: PlatformAuth,
    page: PageQuery,
    status: Annotated[OrderStatus | None, Query(description="订单状态")] = None,
    tenant_id: Annotated[str | None, Query(alias="tenantId", description="按租户筛选")] = None,
    client_product_id: Annotated[
        str | None, Query(alias="clientProductId", description="按客户产品筛选")
    ] = None,
    keyword: Annotated[str | None, Query(description="订单号关键字")] = None,
) -> dict[str, Any]:
    """订单列表。"""
    records, total = await order_service.list_orders(
        session,
        auth,
        offset=page.offset,
        limit=page.limit,
        status=str(status) if status else None,
        tenant_id=tenant_id,
        client_product_id=client_product_id,
        keyword=keyword,
        sort_by=page.sort_by,
        order=page.order,
    )
    return page_response(records, total, page)


@router.get(
    "/orders/{order_id}",
    response_model=OrderDetailResponse,
    summary="订单详情",
    description="含租户 / 客户产品名称与编码、联网方式、设备计数与最近生成的设备摘要。",
    dependencies=[require_perm(PlatformPerm.ORDER_READ)],
)
async def get_order_detail(
    session: DbSession,
    auth: PlatformAuth,
    order_id: str,
) -> OrderDetailResponse:
    """订单详情。"""
    order = await order_service.get_order(session, order_id)
    return await order_service.build_order_detail(session, auth, order)


@router.post(
    "/orders/{order_id}/audit",
    response_model=OrderResponse,
    summary="审核订单",
    description=(
        "审核订单（**仅 `PENDING_AUDIT` 可审核**，否则返回 `INVALID_STATE_TRANSITION`）。\n\n"
        "- `decision=APPROVED` → 订单进入 `APPROVED`\n"
        "- `decision=REJECTED` → 订单进入 `REJECTED`（终态），**必须**填写 `rejectReason`"
    ),
    responses={409: {"description": "订单当前状态不允许审核"}},
    dependencies=[require_perm(PlatformPerm.ORDER_WRITE)],
)
async def audit_order(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    order_id: str,
    payload: OrderAuditRequest = Body(...),
) -> OrderResponse:
    """审核订单。"""
    order = await order_service.audit_order(
        session,
        auth,
        order_id=order_id,
        decision=payload.decision,
        remark=payload.remark,
        reject_reason=payload.reject_reason,
        request=request,
    )
    # 审核后状态已变，重新组装以带上中文状态名与归属名称
    detail = await order_service.build_order_detail(session, auth, order)
    return OrderResponse(**detail.model_dump(exclude={"generation_detail", "devices"}))


@router.post(
    "/orders/{order_id}/generate",
    response_model=GenerateResult,
    summary="生成设备",
    description=(
        "按订单生成设备并入库：`APPROVED → GENERATING → GENERATED → IN_STOCK`。\n\n"
        "**按联网方式分支**：\n"
        "- `4G`（集贤）→ 调用厂商接口分配设备；**未配置厂商密钥时按 ADR-07 安全失败**，"
        "订单退回 `APPROVED`，返回 `VENDOR_UNAVAILABLE`（503），绝不伪造成功\n"
        "- `WIFI`（京东 JoyInside）→ 平台本地生成 SN，不依赖厂商接口\n\n"
        "**幂等**：订单已处于生成后状态时，直接返回既有结果，不会重复造设备。"
    ),
    responses={409: {"description": "订单当前状态不允许生成"}, 503: {"description": "厂商未配置"}},
    dependencies=[require_perm(PlatformPerm.ORDER_WRITE)],
)
async def generate_devices(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    order_id: str,
) -> GenerateResult:
    """生成设备并入库。"""
    return await order_service.generate_devices(
        session, auth, order_id=order_id, request=request
    )


@router.get(
    "/orders/{order_id}/qrcodes",
    response_model=ListResult[QrCodeItemResponse],
    summary="导出订单二维码",
    description=(
        "返回订单下全部设备的二维码文本（`payload`），前端据此渲染图形。\n\n"
        "- 集贤 4G：`JX|{SN}|{IMEI}|{ICCID}|{deviceId}`\n"
        "- 京东 Wi-Fi：`JD|{tenantId}|{productId}|{sn}|{sign}`，`sign` 为 HMAC-SHA256\n\n"
        "签名规范：`HMAC-SHA256(f\"{tenantId}|{productId}|{sn}\", QR_SIGN_SECRET)`。"
    ),
    dependencies=[require_perm(PlatformPerm.ORDER_READ)],
)
async def export_order_qrcodes(
    session: DbSession,
    auth: PlatformAuth,
    order_id: str,
) -> dict[str, Any]:
    """导出订单二维码。"""
    items = await order_service.qrcodes_for_order(session, auth, order_id)
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
# 二、设备
# ===========================================================================


@router.get(
    "/devices/stats",
    response_model=DeviceStatsResponse,
    summary="设备统计",
    description="按四个维度分别聚合设备数量（供统计卡与工作台）。",
    dependencies=[require_perm(PlatformPerm.DEVICE_READ)],
)
async def device_stats(session: DbSession, auth: PlatformAuth) -> DeviceStatsResponse:
    """设备统计。"""
    data = await device_service.count_by_status(session, auth)
    return DeviceStatsResponse.model_validate(data)


@router.get(
    "/devices",
    response_model=PageResult[DeviceResponse],
    summary="设备列表",
    description=(
        "分页查询设备。支持按租户 / 客户产品 / 四维状态筛选，"
        "`keyword` 匹配 SN / IMEI / MAC；`sortBy` 支持 `created_at` / `sn` / `generated_at`。\n\n"
        "`unallocated=true` 只看**尚未分配给任何租户的平台库存**（`tenantId IS NULL`），"
        "是「分配设备」页挑选目标设备的取值来源。\n\n"
        "`onlineStatus` 按**派生在线判据**（最后一次心跳是否落在 "
        "`ONLINE_WINDOW_SECONDS` 窗口内）筛选：落库仍是 `ONLINE` 但已超窗的设备"
        "会出现在 `OFFLINE` 里，与展示字段 `online` 口径一致。"
    ),
    dependencies=[require_perm(PlatformPerm.DEVICE_READ)],
)
async def list_devices(
    session: DbSession,
    auth: PlatformAuth,
    page: PageQuery,
    tenant_id: Annotated[str | None, Query(alias="tenantId")] = None,
    client_product_id: Annotated[str | None, Query(alias="clientProductId")] = None,
    order_id: Annotated[str | None, Query(alias="orderId")] = None,
    asset_status: Annotated[AssetStatus | None, Query(alias="assetStatus")] = None,
    activation_status: Annotated[ActivationStatus | None, Query(alias="activationStatus")] = None,
    online_status: Annotated[OnlineStatus | None, Query(alias="onlineStatus")] = None,
    bind_status: Annotated[BindStatus | None, Query(alias="bindStatus")] = None,
    unallocated: Annotated[
        bool, Query(description="仅看尚未分配给任何租户的平台库存")
    ] = False,
    keyword: Annotated[str | None, Query(description="SN / IMEI / MAC")] = None,
) -> dict[str, Any]:
    """设备列表。"""
    records, total = await device_service.list_devices(
        session,
        auth,
        offset=page.offset,
        limit=page.limit,
        tenant_id=tenant_id,
        client_product_id=client_product_id,
        order_id=order_id,
        asset_status=str(asset_status) if asset_status else None,
        activation_status=str(activation_status) if activation_status else None,
        online_status=str(online_status) if online_status else None,
        bind_status=str(bind_status) if bind_status else None,
        unallocated=unallocated,
        keyword=keyword,
        sort_by=page.sort_by,
        order=page.order,
    )
    return page_response([device_service.to_response(item) for item in records], total, page)


@router.get(
    "/devices/{device_id}",
    response_model=DeviceDetailResponse,
    summary="设备详情",
    description=(
        "含四维状态派生标签 `label`、归属名称、**凭证掩码**（只有 `secretHint`，"
        "永不含明文密钥）与最近事件（时间线）。"
    ),
    dependencies=[require_perm(PlatformPerm.DEVICE_READ)],
)
async def get_device_detail(
    session: DbSession,
    auth: PlatformAuth,
    device_id: str,
) -> DeviceDetailResponse:
    """设备详情。"""
    device = await device_service.get_device(session, auth, device_id)
    return await device_service.build_detail(session, auth, device)


@router.get(
    "/devices/{device_id}/events",
    response_model=PageResult[DeviceEventResponse],
    summary="设备事件时间线",
    description="分页查询设备的流转事件（按时间倒序）。",
    dependencies=[require_perm(PlatformPerm.DEVICE_READ)],
)
async def list_device_events(
    session: DbSession,
    auth: PlatformAuth,
    device_id: str,
    page: PageQuery,
) -> dict[str, Any]:
    """设备事件时间线。"""
    records, total = await device_service.list_events(
        session, auth, device_id, offset=page.offset, limit=page.limit
    )
    return page_response(
        [device_service.event_to_response(item) for item in records], total, page
    )


@router.post(
    "/devices/{device_id}/freeze",
    response_model=DeviceResponse,
    summary="冻结设备",
    description=(
        "冻结设备（原因必填）。仅 `IN_STOCK` 可冻结，"
        "冻结时把原状态记入 `previousAssetStatus`，解冻时恢复。"
    ),
    responses={409: {"description": "当前状态不允许冻结"}},
    dependencies=[require_perm(PlatformPerm.DEVICE_WRITE)],
)
async def freeze_device(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    device_id: str,
    payload: DeviceFreezeRequest = Body(...),
) -> DeviceResponse:
    """冻结设备。"""
    device = await device_service.get_device(session, auth, device_id)
    await device_service.freeze_device(session, auth, device, reason=payload.reason, request=request)
    return device_service.to_response(device)


@router.post(
    "/devices/{device_id}/thaw",
    response_model=DeviceResponse,
    summary="解冻设备",
    description="解冻设备并恢复冻结前状态（无法原路恢复时回落到 `IN_STOCK`，见事件详情）。",
    dependencies=[require_perm(PlatformPerm.DEVICE_WRITE)],
)
async def thaw_device(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    device_id: str,
    payload: DeviceThawRequest | None = Body(default=None),
) -> DeviceResponse:
    """解冻设备。"""
    device = await device_service.get_device(session, auth, device_id)
    await device_service.thaw_device(
        session,
        auth,
        device,
        reason=payload.reason if payload else None,
        request=request,
    )
    return device_service.to_response(device)


@router.post(
    "/devices/{device_id}/retire",
    response_model=DeviceResponse,
    summary="报废设备",
    description="报废设备（原因必填，写入 `retireReason`）。报废是**终态**，不可再迁移。",
    responses={409: {"description": "当前状态不允许报废"}},
    dependencies=[require_perm(PlatformPerm.DEVICE_WRITE)],
)
async def retire_device(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    device_id: str,
    payload: DeviceRetireRequest = Body(...),
) -> DeviceResponse:
    """报废设备。"""
    device = await device_service.get_device(session, auth, device_id)
    await device_service.retire_device(session, auth, device, reason=payload.reason, request=request)
    return device_service.to_response(device)


@router.post(
    "/devices/{device_id}/simulate-heartbeat",
    response_model=DeviceHeartbeatResponse,
    summary="模拟设备心跳 / 掉线",
    description=(
        "演示与验收用：模拟设备心跳上报，或把设备置为离线。\n\n"
        "- `online=true`（默认）→ 写入心跳时间并把在线状态推到 `ONLINE`\n"
        "- `online=false` → 模拟「最后一次心跳已超出在线窗口」：把心跳时间挪到窗口外"
        "（`ONLINE_WINDOW_SECONDS`，默认 180 秒）**并**把在线状态置为 `OFFLINE`，"
        "写一条 `OFFLINE` 事件\n\n"
        "为什么需要模拟掉线？若只能靠真实等待来验证「超窗即离线」，"
        "验收就必须干等三分钟，无法自动化。响应里的 `simulated=true` "
        "明确标注这不是真实设备上报，避免演示数据被误当成设备事实。\n\n"
        "审计：`SIMULATE_HEARTBEAT`（真实心跳不写审计，模拟是人工动作，必须留痕）。"
    ),
    responses={409: {"description": "设备已冻结或已报废"}},
    dependencies=[require_perm(PlatformPerm.DEVICE_WRITE)],
)
async def simulate_device_heartbeat(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    device_id: str,
    payload: DeviceSimulateHeartbeatRequest = Body(...),
) -> DeviceHeartbeatResponse:
    """模拟设备心跳 / 掉线。"""
    device = await device_service.get_device(session, auth, device_id)
    return await heartbeat_service.simulate_heartbeat(
        session,
        auth,
        device,
        online=payload.online,
        firmware_version=payload.firmware_version,
        request=request,
    )


@router.post(
    "/devices/{device_id}/credentials",
    response_model=DeviceCredentialIssueResult,
    status_code=201,
    summary="签发设备密钥",
    description=(
        "为设备签发（或轮换）设备凭证。**明文 `secret` 仅此一次返回**："
        "库内只存 `sha256` 摘要与「前 4 位 + ****」的掩码提示 `secretHint`。\n\n"
        "同 `(deviceId, credentialType)` 已有凭证时**覆盖**该行（唯一约束保证一台设备"
        "一个设备密钥）：旧密钥随即失效——这正是设备丢失时该有的行为。"
        "重新签发会同时解除吊销状态。\n\n"
        "用途：设备侧 `POST /device/heartbeat` 用该密钥鉴权（设备不持有 JWT）。"
    ),
    responses={
        400: {"description": "不支持的凭证类型"},
        404: {"description": "设备不存在"},
    },
    dependencies=[require_perm(PlatformPerm.DEVICE_WRITE)],
)
async def issue_device_credential(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    device_id: str,
    payload: DeviceCredentialIssueRequest = Body(...),
) -> DeviceCredentialIssueResult:
    """签发设备密钥（明文仅此一次返回）。"""
    device = await device_service.get_device(session, auth, device_id)
    return await device_service.issue_credential(
        session,
        auth,
        device,
        credential_type=payload.credential_type,
        expires_in_days=payload.expires_in_days,
        request=request,
    )


# ===========================================================================
# 三、设备批次（CSV 导入）
# ===========================================================================


@router.get(
    "/batches",
    response_model=PageResult[BatchResponse],
    summary="批次列表",
    description="分页查询批次（含总数 / 有效 / 无效 / 重复 / 已导入行数）。",
    dependencies=[require_perm(PlatformPerm.BATCH_READ)],
)
async def list_batches(
    session: DbSession,
    auth: PlatformAuth,
    page: PageQuery,
    status: Annotated[BatchStatus | None, Query(description="批次状态")] = None,
    keyword: Annotated[str | None, Query(description="批次号 / 文件名")] = None,
) -> dict[str, Any]:
    """批次列表。"""
    records, total = await batch_service.list_batches(
        session,
        auth,
        offset=page.offset,
        limit=page.limit,
        status=str(status) if status else None,
        keyword=keyword,
        sort_by=page.sort_by,
        order=page.order,
    )
    return page_response([batch_service.batch_to_response(item) for item in records], total, page)


@router.post(
    "/batches",
    response_model=BatchResponse,
    status_code=201,
    summary="上传批次 CSV（预检）",
    description=(
        "上传 CSV 并完成**预检**（缺 SN / SN 已存在 → `INVALID`；文件内重复 → `SKIPPED`）。\n\n"
        "**幂等**：同一份文件（SHA-256 相同）重复上传会直接返回已存在的批次，"
        "不会重复导入也不会报错。\n\n"
        "列名容错：`SN` / `序列号` / `serial`、`IMEI` / `imei`、`ICCID` / `iccid`、`MAC` / `mac`；"
        "最多 5000 行、5 MB。"
    ),
    dependencies=[require_perm(PlatformPerm.BATCH_WRITE)],
)
async def upload_batch(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    file: Annotated[UploadFile, File(description="CSV 文件")],
    order_id: Annotated[str | None, Query(alias="orderId", description="关联订单（可选）")] = None,
) -> BatchResponse:
    """上传批次 CSV 并预检。"""
    content = await file.read()
    batch = await batch_service.create_batch(
        session,
        auth,
        file_name=file.filename or "upload.csv",
        content=content,
        order_id=order_id,
        request=request,
    )
    return batch_service.batch_to_response(batch)


@router.post(
    "/batches/{batch_id}/import",
    response_model=BatchResponse,
    summary="导入批次",
    description=(
        "把预检通过的行导入为设备（`assetStatus=GENERATED`，`tenantId` 留空 = 平台库存）。\n\n"
        "**幂等**：已 `IMPORTED` 的批次直接返回；失败后重跑只补未导入的行，不会重复建设备。"
    ),
    responses={409: {"description": "批次当前状态不允许导入"}},
    dependencies=[require_perm(PlatformPerm.BATCH_WRITE)],
)
async def import_batch(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    batch_id: str,
) -> BatchResponse:
    """导入批次。"""
    batch = await batch_service.import_batch(session, auth, batch_id=batch_id, request=request)
    return batch_service.batch_to_response(batch)


@router.get(
    "/batches/{batch_id}/lines",
    response_model=PageResult[BatchLineResponse],
    summary="批次明细",
    description="分页查询批次明细行（按行号升序），可按行状态筛选。",
    dependencies=[require_perm(PlatformPerm.BATCH_READ)],
)
async def list_batch_lines(
    session: DbSession,
    auth: PlatformAuth,
    batch_id: str,
    page: PageQuery,
    status: Annotated[str | None, Query(description="行状态 VALID/INVALID/SKIPPED/IMPORTED")] = None,
) -> dict[str, Any]:
    """批次明细。"""
    records, total = await batch_service.list_lines(
        session, auth, batch_id, offset=page.offset, limit=page.limit, status=status
    )
    return page_response(
        [batch_service.line_to_response(item) for item in records], total, page
    )


@router.get(
    "/batches/{batch_id}/error-report",
    summary="下载批次错误报告",
    description="返回带 BOM 的 CSV（`text/csv`），Excel 直接双击打开不乱码。",
    responses={200: {"content": {"text/csv": {}}, "description": "错误明细 CSV"}},
    dependencies=[require_perm(PlatformPerm.BATCH_READ)],
)
async def download_error_report(
    session: DbSession,
    auth: PlatformAuth,
    batch_id: str,
) -> Response:
    """下载批次错误报告。"""
    batch = await batch_service.get_batch(session, batch_id)
    # 与 list_lines / import 保持一致：读路径统一经 assert_visible 收口（ADR-08）
    assert_visible(batch.tenant_id, auth, resource="批次")
    csv_text = batch_service.build_error_csv(batch)
    filename = f"{batch.batch_no}-errors.csv"
    return Response(
        content=csv_text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


__all__ = ["router"]
