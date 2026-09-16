"""平台端 P9 API：OTA 固件推送 + 内容库（只读）。

为什么单独一个模块
------------------
与 ``platform_orders.py`` / ``platform_factory.py`` 同一理由：P9 与其它阶段
并行开发，追加到 ``platform.py`` 必然冲突，拆开后只在 ``router.py`` 的一行
include 上相遇。

**OTA 仅平台端可见**
--------------------
商户端**不得有** OTA 端点：固件是型号级制品，一次推送影响所有租户的同型号
设备，这个决策权属于平台而不是单个商户。因此本模块只挂 ``/platform`` 前缀，
并在 ``merchant_ops.py`` 里刻意不提供任何 OTA 路径。

`file` 为什么是可选
-------------------
固件包需要「元数据登记」这个形态：演示包、内测包、即将上传的历史包都需要
先有条目（种子里的 ``otap-4g-130`` 就是如此）。无文件的包**不能真正推送**，
推送时逐台写 ``FAILED`` 并说明「固件包没有上传文件」——界面看起来能推，
但推了必须失败，这是 ADR-07 的同一口径。

内容库权限为什么复用 `platform:template:read`
-------------------------------------------
内容库是产品/内容素材的延伸（内容挂在型号/产品语境下），而 P9 不应改动既有
角色权限矩阵（``app/core/permissions.py`` 的权限集合有测试钉住）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, File, Form, Query, Request, UploadFile

from app.core.deps import DbSession, PlatformAuth, require_perm
from app.core.pagination import PageParams, page_params, page_response
from app.core.permissions import PlatformPerm
from app.models.enums import ContentItemType, EnableStatus, OtaPushStatus
from app.schemas.common import MessageResult, PageResult
from app.schemas.ops import (
    ContentItemResponse,
    OtaPackageDetailResponse,
    OtaPackageResponse,
    OtaPackageUpdateRequest,
    OtaPushRequest,
    OtaPushResult,
    OtaRecordResponse,
)
from app.services import ai_config_service, ota_service

router = APIRouter(prefix="/platform", tags=["平台端 · OTA 与内容库"])

PageQuery = Annotated[PageParams, Depends(page_params)]


# ===========================================================================
# 一、内容库（只读）
# ===========================================================================


@router.get(
    "/content-items",
    response_model=PageResult[ContentItemResponse],
    summary="内容库列表（只读）",
    description=(
        "平台侧内容库列表。`scope` 可筛 `PLATFORM`（平台公共，`tenantId` 为空）/ "
        "`TENANT`（租户自建）。\n\n"
        "本阶段只读：内容库的运营维护涉及内容审核流程，不在 P9 范围；"
        "P9 的验收项是「内容热度榜」（读）。"
    ),
    dependencies=[require_perm(PlatformPerm.TEMPLATE_READ)],
)
async def list_content_items(
    session: DbSession,
    auth: PlatformAuth,
    page: PageQuery,
    item_type: Annotated[ContentItemType | None, Query(alias="type", description="内容类型")] = None,
    keyword: Annotated[str | None, Query(description="标题 / 描述关键字")] = None,
    scope: Annotated[str | None, Query(description="PLATFORM / TENANT")] = None,
) -> dict[str, Any]:
    """内容库列表。"""
    records, total = await ai_config_service.list_content_items(
        session,
        auth,
        item_type=str(item_type) if item_type else None,
        keyword=keyword,
        scope=scope,
        offset=page.offset,
        limit=page.limit,
    )
    return page_response(records, total, page)


# ===========================================================================
# 二、OTA 固件包
# ===========================================================================


@router.get(
    "/ota/packages",
    response_model=PageResult[OtaPackageResponse],
    summary="固件包列表",
    description=(
        "分页查询固件包，含推送统计 `pushStats`（逐台记录汇总）。"
        "`hasFile=false` 表示该包只有元数据、没有上传文件，**不能真正推送**。"
    ),
    dependencies=[require_perm(PlatformPerm.OTA_READ)],
)
async def list_ota_packages(
    session: DbSession,
    auth: PlatformAuth,
    page: PageQuery,
    status: Annotated[EnableStatus | None, Query(description="固件包状态")] = None,
    template_id: Annotated[str | None, Query(alias="templateId", description="产品模板")] = None,
    keyword: Annotated[str | None, Query(description="版本号 / 文件名")] = None,
) -> dict[str, Any]:
    """固件包列表。"""
    records, total = await ota_service.list_packages(
        session,
        auth,
        status=str(status) if status else None,
        template_id=template_id,
        keyword=keyword,
        offset=page.offset,
        limit=page.limit,
    )
    return page_response(records, total, page)


@router.post(
    "/ota/packages",
    response_model=OtaPackageResponse,
    status_code=201,
    summary="登记固件包",
    description=(
        "`multipart/form-data`：`file` **可选**，表单字段 `templateId, version, "
        "releaseNotes?, minVersion?, isForced?`。\n\n"
        "无 `file` 时 `hasFile=false`，仅登记元数据（演示/内测包）——"
        "**无文件的固件包不能真正推送**，推送时会逐台写 `FAILED` 并说明原因。\n\n"
        "校验：模板存在；`(templateId, version)` 唯一（重复 → 409）；"
        "扩展名白名单 `OTA_ALLOWED_EXTENSIONS`；大小上限 `OTA_PACKAGE_MAX_BYTES`。\n\n"
        "`checksum` 为 SHA-256（由存储层计算，推送前可做完整性校验）；"
        "落盘 key = `ota/{模板ID}/{包ID}{扩展名}`。"
    ),
    responses={
        404: {"description": "产品模板不存在"},
        409: {"description": "该模板下版本号已存在"},
        400: {"description": "扩展名不允许或超过大小上限"},
    },
    dependencies=[require_perm(PlatformPerm.OTA_WRITE)],
)
async def create_ota_package(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    template_id: Annotated[str, Form(alias="templateId", description="产品模板 ID")],
    version: Annotated[str, Form(min_length=1, max_length=64, description="固件版本号")],
    release_notes: Annotated[str | None, Form(alias="releaseNotes")] = None,
    min_version: Annotated[str | None, Form(alias="minVersion")] = None,
    is_forced: Annotated[bool, Form(alias="isForced")] = False,
    file: Annotated[UploadFile | None, File(description="固件文件（可选）")] = None,
) -> OtaPackageResponse:
    """登记固件包。"""
    file_content: bytes | None = None
    file_name: str | None = None
    if file is not None and file.filename:
        raw = await file.read()
        if raw:
            file_content = raw
            file_name = file.filename

    return await ota_service.create_package(
        session,
        auth,
        template_id=template_id,
        version=version,
        release_notes=release_notes,
        min_version=min_version,
        is_forced=is_forced,
        file_name=file_name,
        file_content=file_content,
        request=request,
    )


@router.get(
    "/ota/packages/{package_id}",
    response_model=OtaPackageDetailResponse,
    summary="固件包详情",
    description="含 `pushStats` 与最近 20 条逐台推送记录。",
    responses={404: {"description": "固件包不存在"}},
    dependencies=[require_perm(PlatformPerm.OTA_READ)],
)
async def get_ota_package(
    session: DbSession,
    auth: PlatformAuth,
    package_id: str,
) -> OtaPackageDetailResponse:
    """固件包详情。"""
    return await ota_service.get_package_detail(session, package_id)


@router.put(
    "/ota/packages/{package_id}",
    response_model=OtaPackageDetailResponse,
    summary="更新固件包",
    description=(
        "部分更新 `releaseNotes` / `minVersion` / `isForced` / `status`。\n\n"
        "**文件本身不可替换**：换了字节就该发新版本号——允许覆盖文件会让"
        "「同版本号对应两份不同固件」，设备的实际固件将无法解释。"
    ),
    dependencies=[require_perm(PlatformPerm.OTA_WRITE)],
)
async def update_ota_package(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    package_id: str,
    payload: OtaPackageUpdateRequest = Body(...),
) -> OtaPackageDetailResponse:
    """更新固件包。"""
    return await ota_service.update_package(
        session,
        auth,
        package_id,
        release_notes=payload.release_notes,
        min_version=payload.min_version,
        is_forced=payload.is_forced,
        status=str(payload.status) if payload.status is not None else None,
        fields_set=set(payload.model_fields_set),
        request=request,
    )


@router.delete(
    "/ota/packages/{package_id}",
    response_model=MessageResult,
    summary="删除固件包",
    description=(
        "级联删除推送记录并删除存储对象。\n\n"
        "若业务上需要保留推送历史，请改为「停用」（`status=DISABLED`）而不是删除——"
        "记录里只有 `packageId`，包没了记录就没有解读依据。"
    ),
    dependencies=[require_perm(PlatformPerm.OTA_WRITE)],
)
async def delete_ota_package(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    package_id: str,
) -> MessageResult:
    """删除固件包。"""
    await ota_service.delete_package(session, auth, package_id, request=request)
    return MessageResult(message="固件包已删除")


@router.post(
    "/ota/packages/{package_id}/push",
    response_model=OtaPushResult,
    summary="推送固件包",
    description=(
        "推送固件给某客户产品下的设备，逐台写 `ota_records`（`PENDING → PUSHING → "
        "SUCCESS | FAILED`）。\n\n"
        "★ **先判定云服务商的 `otaSupport`**：`UNSUPPORTED`（如京东 JoyInside Wi-Fi"
        "方案仅端侧升级）→ **409 `OTA_NOT_SUPPORTED`**，`details` 带 "
        "`{otaSupport, cloudVendor, cloudProviderName}`。这是**数据驱动**的判定，"
        "不是写死的 if。\n\n"
        "`deviceIds` 省略时 = 该产品下全部非 `RETIRED` 设备；给定 ID 时逐台校验归属，"
        "不属于该产品的计入 `failed` 并写原因（**不整批拒绝**）。\n\n"
        "成功才更新设备 `firmwareVersion`；厂商未配置 / 未实现 → 该台 `FAILED`，"
        "**绝不伪造成功**。已在目标版本的设备计入 `skipped`（不重复推、不写记录）。"
    ),
    responses={
        404: {"description": "固件包或客户产品不存在"},
        409: {"description": "该联网方案不支持平台侧 OTA 推送"},
    },
    dependencies=[require_perm(PlatformPerm.OTA_WRITE)],
)
async def push_ota_package(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    package_id: str,
    payload: OtaPushRequest = Body(...),
) -> OtaPushResult:
    """推送固件包。"""
    return await ota_service.push_package(
        session,
        auth,
        package_id=package_id,
        client_product_id=payload.client_product_id,
        device_ids=payload.device_ids,
        request=request,
    )


@router.get(
    "/ota/records",
    response_model=PageResult[OtaRecordResponse],
    summary="推送记录列表",
    description="分页查询逐台推送记录（按时间倒序），可按固件包 / 状态 / 设备筛选。",
    dependencies=[require_perm(PlatformPerm.OTA_READ)],
)
async def list_ota_records(
    session: DbSession,
    auth: PlatformAuth,
    page: PageQuery,
    package_id: Annotated[str | None, Query(alias="packageId", description="固件包")] = None,
    status: Annotated[OtaPushStatus | None, Query(description="推送状态")] = None,
    device_id: Annotated[str | None, Query(alias="deviceId", description="设备")] = None,
) -> dict[str, Any]:
    """推送记录列表。"""
    records, total = await ota_service.list_records(
        session,
        auth,
        package_id=package_id,
        status=str(status) if status else None,
        device_id=device_id,
        offset=page.offset,
        limit=page.limit,
    )
    return page_response(records, total, page)


__all__ = ["router"]
