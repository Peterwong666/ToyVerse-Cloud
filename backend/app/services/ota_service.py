"""OTA 固件服务（P9，仅平台端）。

推送能力是**数据驱动**的
------------------------
``cloud_providers.ota_support`` 是权威字段：集贤 4G = ``SUPPORTED``（平台可推），
京东 JoyInside Wi-Fi = ``UNSUPPORTED``（仅端侧升级）。推送前先按设备所属客户
产品判定，不支持时返回 409 ``OTA_NOT_SUPPORTED``，``details`` 带
``{otaSupport, cloudVendor, cloudProviderName}`` 让前端能直接告诉用户
「该找谁升级」。**这不是写死的 if**——把云服务商的取值改掉，行为随之改变。

逐台记录，绝不整批失败
----------------------
OTA 的现实是「大部分成功、少数失败」。因此：

* 记录按**设备**逐条写（``ota_records``），``PENDING → PUSHING →
  SUCCESS | FAILED``，每台失败都留下原因；
* 厂商调用失败只影响那一台，其余继续推；
* 调用方传入的 ``deviceIds`` 若有不属于该产品的，只把那一台记 ``failed``，
  **不整批拒绝**（99 台正确设备不该因 1 个错 ID 而全部推不了）。

三条诚实性约束（ADR-07）
------------------------
1. **没有文件的包不能推**：逐台 ``FAILED`` + 「固件包没有上传文件，无法推送」。
   种子里的演示包就是这种元数据登记，界面看起来能推，但推了必须失败。
2. **厂商未配置 / 未实现 → 该台 FAILED**，绝不伪造成功。
3. **已在目标版本的设备不做重复推送**（``skipped``），**也不写
   ``ota_records``**——那张表记的是「推送」，被跳过的一台从未被推送。因此
   ``SKIPPED`` 只出现在响应里，不进入数据库状态词表。

为什么 ``(template_id, version)`` 重复用 409
--------------------------------------------
固件包是「型号 + 版本」唯一确定的制品（同版本两次上传必然字节不同，
说明版本号没升）。项目约定「资源撞唯一键 → 409 冲突而非 400 校验失败」
（见 ``app.core.errors`` 中目录域注释），因此这里复用 ``VALIDATION_ERROR``
**错误码**但显式指定 409 状态码——契约也明确要求这个组合，且不新造错误码。
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import Request
from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import registry
from app.core.config import settings
from app.core.deps import AuthContext
from app.core.errors import (
    AppException,
    ErrorCode,
    not_found,
    ota_not_supported,
    validation_error,
)
from app.core.ids import new_id
from app.core.logging import get_logger
from app.core.storage import build_object_key, get_storage
from app.db.base import utcnow
from app.db.scope import scoped
from app.models.catalog import ClientProduct, CloudProvider, ProductTemplate
from app.models.device import Device
from app.models.enums import (
    AssetStatus,
    AuditAction,
    EnableStatus,
    OtaPushStatus,
    OtaSupport,
)
from app.models.ops import OtaPackage, OtaRecord
from app.schemas.ops import (
    OtaPackageDetailResponse,
    OtaPackageResponse,
    OtaPushRecordItem,
    OtaPushResult,
    OtaPushStats,
    OtaRecordResponse,
)
from app.services import ai_config_service, audit_service

logger = get_logger(__name__)

#: 固件包状态展示名
OTA_PACKAGE_STATUS_LABELS: dict[str, str] = {
    str(EnableStatus.ENABLED): "启用",
    str(EnableStatus.DISABLED): "停用",
}

#: 详情页返回的最近推送记录条数
PACKAGE_DETAIL_RECORD_LIMIT = 20

#: 推送记录里「设备不存在 / 不属于该产品」的响应态（不落库）
PUSH_STATUS_UNKNOWN_DEVICE = "FAILED"

#: 已在目标版本、无需重复推送（只出现在响应里，不写库）
PUSH_STATUS_SKIPPED = "SKIPPED"


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------


def allowed_ota_extensions() -> set[str]:
    """解析 ``settings.OTA_ALLOWED_EXTENSIONS`` 为小写扩展名集合（不含点）。"""
    return {
        item.strip().lower().lstrip(".")
        for item in settings.OTA_ALLOWED_EXTENSIONS.split(",")
        if item.strip()
    }


def _delete_storage_object(key: str | None) -> None:
    """尽力删除固件文件：删不掉只记 warning，不阻断业务删除（与知识库同一取舍）。"""
    if not key:
        return
    try:
        get_storage().delete(key)
    except AppException as exc:
        logger.warning("删除固件存储对象失败（已跳过）：key=%s err=%s", key, exc.message)


async def _template_names(session: AsyncSession, template_ids: set[str]) -> dict[str, str]:
    """批量取模板名称（避免列表 N+1）。"""
    if not template_ids:
        return {}
    return {
        str(row[0]): str(row[1])
        for row in (
            await session.execute(
                select(ProductTemplate.id, ProductTemplate.name).where(
                    ProductTemplate.id.in_(template_ids)
                )
            )
        ).all()
    }


async def _push_stats(session: AsyncSession, package_ids: list[str]) -> dict[str, OtaPushStats]:
    """统计每个固件包的推送状态分布（一次分组查询）。"""
    if not package_ids:
        return {}
    rows = (
        await session.execute(
            select(OtaRecord.ota_package_id, OtaRecord.status, func.count())
            .where(OtaRecord.ota_package_id.in_(package_ids))
            .group_by(OtaRecord.ota_package_id, OtaRecord.status)
        )
    ).all()
    stats: dict[str, OtaPushStats] = {}
    for row in rows:
        package_id = str(row[0])
        status = str(row[1])
        count = int(row[2])
        item = stats.setdefault(package_id, OtaPushStats())
        item.total += count
        if status == str(OtaPushStatus.SUCCESS):
            item.success += count
        elif status == str(OtaPushStatus.FAILED):
            item.failed += count
        else:
            # PENDING / PUSHING / PARTIAL_FAILED 都归入「未完成」（含批次态）
            item.pending += count
    return stats


def package_to_response(
    package: OtaPackage, *, template_name: str | None = None, stats: OtaPushStats | None = None
) -> OtaPackageResponse:
    """ORM → 固件包响应。``hasFile`` 由 ``storage_path`` 是否为空判定。"""
    return OtaPackageResponse(
        id=package.id,
        template_id=package.template_id,
        template_name=template_name,
        version=package.version,
        release_notes=package.release_notes,
        file_name=package.file_name,
        file_size=package.file_size,
        checksum=package.checksum,
        min_version=package.min_version,
        is_forced=package.is_forced,
        status=package.status,
        status_label=OTA_PACKAGE_STATUS_LABELS.get(package.status, package.status),
        published_at=package.published_at,
        has_file=bool(package.storage_path),
        push_stats=stats or OtaPushStats(),
        created_at=package.created_at,
    )


def _record_to_response(record: OtaRecord, *, sn: str | None = None) -> OtaRecordResponse:
    """ORM → 推送记录响应。"""
    return OtaRecordResponse(
        id=record.id,
        package_id=record.ota_package_id,
        device_id=record.device_id,
        sn=sn,
        status=record.status,
        from_version=record.from_version,
        to_version=record.to_version,
        error_message=record.error_message,
        vendor_message=record.vendor_message,
        pushed_at=record.pushed_at,
        finished_at=record.finished_at,
        created_at=record.created_at,
    )


# ---------------------------------------------------------------------------
# 一、固件包 CRUD
# ---------------------------------------------------------------------------


async def get_package(session: AsyncSession, package_id: str) -> OtaPackage:
    """按 ID 取固件包。

    Raises:
        AppException: 不存在。
    """
    package = (
        await session.execute(select(OtaPackage).where(OtaPackage.id == package_id))
    ).scalar_one_or_none()
    if package is None:
        raise not_found("固件包不存在")
    return package


async def list_packages(
    session: AsyncSession,
    auth: AuthContext,
    *,
    status: str | None = None,
    template_id: str | None = None,
    keyword: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[OtaPackageResponse], int]:
    """分页查询固件包（平台端全局长）。"""
    conditions: list[ColumnElement[bool]] = []
    if status:
        conditions.append(OtaPackage.status == status)
    if template_id:
        conditions.append(OtaPackage.template_id == template_id)
    if keyword:
        pattern = f"%{keyword.strip()}%"
        conditions.append(
            OtaPackage.version.like(pattern) | OtaPackage.file_name.like(pattern)
        )

    total = int(
        (
            await session.execute(
                select(func.count()).select_from(OtaPackage).where(*conditions)
            )
        ).scalar_one()
    )
    rows = list(
        (
            await session.execute(
                select(OtaPackage)
                .where(*conditions)
                .order_by(OtaPackage.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    names = await _template_names(session, {row.template_id for row in rows})
    stats = await _push_stats(session, [row.id for row in rows])
    return (
        [
            package_to_response(
                row,
                template_name=names.get(row.template_id),
                stats=stats.get(row.id),
            )
            for row in rows
        ],
        total,
    )


async def get_package_detail(
    session: AsyncSession, package_id: str
) -> OtaPackageDetailResponse:
    """固件包详情（含最近 20 条逐台推送记录）。"""
    package = await get_package(session, package_id)
    names = await _template_names(session, {package.template_id})
    stats = (await _push_stats(session, [package.id])).get(package.id)
    base = package_to_response(
        package, template_name=names.get(package.template_id), stats=stats
    )

    records = list(
        (
            await session.execute(
                select(OtaRecord)
                .where(OtaRecord.ota_package_id == package.id)
                .order_by(OtaRecord.created_at.desc())
                .limit(PACKAGE_DETAIL_RECORD_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    sn_map = await _device_sns(session, {record.device_id for record in records})
    return OtaPackageDetailResponse(
        **base.model_dump(),
        records=[_record_to_response(item, sn=sn_map.get(item.device_id)) for item in records],
    )


async def _device_sns(session: AsyncSession, device_ids: set[str]) -> dict[str, str]:
    """批量取设备 SN。"""
    if not device_ids:
        return {}
    return {
        str(row[0]): str(row[1])
        for row in (
            await session.execute(
                select(Device.id, Device.sn).where(Device.id.in_(device_ids))
            )
        ).all()
    }


async def create_package(
    session: AsyncSession,
    auth: AuthContext,
    *,
    template_id: str,
    version: str,
    release_notes: str | None = None,
    min_version: str | None = None,
    is_forced: bool = False,
    file_name: str | None = None,
    file_content: bytes | None = None,
    request: Request | None = None,
) -> OtaPackageResponse:
    """创建固件包（``file`` 可选：无文件即元数据登记）。

    Raises:
        AppException: 模板不存在 / 版本重复 / 扩展名不在白名单 / 超过大小上限。
    """
    template = (
        await session.execute(
            select(ProductTemplate).where(ProductTemplate.id == template_id)
        )
    ).scalar_one_or_none()
    if template is None:
        raise not_found("产品模板不存在")

    duplicate = (
        await session.execute(
            select(OtaPackage).where(
                OtaPackage.template_id == template_id, OtaPackage.version == version
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"模板「{template.name}」已存在版本 {version} 的固件包",
            details={"field": "version", "existingPackageId": duplicate.id},
            # 撞唯一键属于冲突而非参数错误（项目约定），契约亦明确要求 409
            status_code=409,
        )

    package = OtaPackage(
        id=new_id("ota_package"),
        template_id=template.id,
        tenant_id=None,
        version=version,
        release_notes=release_notes,
        min_version=min_version,
        is_forced=is_forced,
        status=str(EnableStatus.ENABLED),
        published_at=utcnow(),
        created_by=auth.account,
    )

    if file_content is not None:
        extension = ai_config_service.file_extension(file_name or "")
        if extension not in allowed_ota_extensions():
            allowed = "、".join(sorted(allowed_ota_extensions())) or "（未配置）"
            raise validation_error(
                f"不支持的固件包格式「.{extension or '未知'}」，允许的扩展名：{allowed}",
                details={"field": "file", "allowed": sorted(allowed_ota_extensions())},
            )
        if len(file_content) > settings.OTA_PACKAGE_MAX_BYTES:
            raise validation_error(
                f"固件包超过大小上限（{settings.OTA_PACKAGE_MAX_BYTES // (1024 * 1024)} MB）",
                details={"field": "file", "maxBytes": settings.OTA_PACKAGE_MAX_BYTES},
            )
        key = build_object_key(
            prefix="ota",
            owner=template.id,
            object_id=package.id,
            filename=file_name or "firmware.bin",
        )
        stored = get_storage().save(key, file_content)
        package.file_name = file_name or "firmware.bin"
        package.file_size = stored.size
        package.storage_path = stored.key
        package.checksum = stored.checksum
    elif file_name:
        # 只登记文件名（种子里的演示包就是这种形态）：没有文件字节就没有
        # checksum，不能凭空算一个——那会让「完整性校验」变成摆设。
        package.file_name = file_name

    session.add(package)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        actor=auth,
        resource_type="ota_package",
        resource_id=package.id,
        summary=f"登记固件包「{template.name}」v{version}",
        detail={"templateId": template.id, "hasFile": bool(package.storage_path)},
        request=request,
    )
    await session.commit()
    logger.info("已登记固件包 %s v%s（hasFile=%s）", template.code, version, bool(package.storage_path))
    return package_to_response(package, template_name=template.name)


async def update_package(
    session: AsyncSession,
    auth: AuthContext,
    package_id: str,
    *,
    release_notes: str | None = None,
    min_version: str | None = None,
    is_forced: bool | None = None,
    status: str | None = None,
    fields_set: set[str] | None = None,
    request: Request | None = None,
) -> OtaPackageDetailResponse:
    """部分更新固件包（文件本身不可替换——换了字节就该升版本号）。"""
    package = await get_package(session, package_id)
    present = fields_set if fields_set is not None else set()
    changed: list[str] = []

    if "release_notes" in present:
        package.release_notes = release_notes
        changed.append("release_notes")
    if "min_version" in present:
        package.min_version = min_version
        changed.append("min_version")
    if "is_forced" in present and is_forced is not None:
        package.is_forced = is_forced
        changed.append("is_forced")
    if "status" in present and status is not None and package.status != status:
        package.status = status
        changed.append("status")

    if not changed:
        return await get_package_detail(session, package_id)

    await audit_service.record(
        session,
        action=AuditAction.UPDATE,
        actor=auth,
        resource_type="ota_package",
        resource_id=package.id,
        summary=f"更新固件包「{package.version}」",
        detail={"fields": changed},
        request=request,
    )
    await session.commit()
    return await get_package_detail(session, package_id)


async def delete_package(
    session: AsyncSession, auth: AuthContext, package_id: str, *, request: Request | None = None
) -> None:
    """删除固件包（级联删推送记录 + 删存储对象）。

    推送记录一并删除是**有意的**：固件包没了，指向它的记录就没有解读依据
    （记录里只有 package_id）。若业务上需要保留推送历史，应改为「停用
    （``status=DISABLED``）」而不是删除。

    Raises:
        AppException: 固件包不存在。
    """
    package = await get_package(session, package_id)
    await session.execute(delete(OtaRecord).where(OtaRecord.ota_package_id == package.id))
    _delete_storage_object(package.storage_path)
    await session.delete(package)
    await audit_service.record(
        session,
        action=AuditAction.DELETE,
        actor=auth,
        resource_type="ota_package",
        resource_id=package.id,
        summary=f"删除固件包 v{package.version}（含推送记录）",
        detail={"storagePath": package.storage_path},
        request=request,
    )
    await session.commit()
    logger.warning("已删除固件包 %s v%s", package.template_id, package.version)


# ---------------------------------------------------------------------------
# 二、推送
# ---------------------------------------------------------------------------


async def _cloud_provider_of(
    session: AsyncSession, product: ClientProduct
) -> CloudProvider | None:
    """取设备所属产品的云服务商（产品快照优先，回落到模板）。"""
    cloud_id = product.cloud_provider_id
    if cloud_id is None:
        cloud_id = (
            await session.execute(
                select(ProductTemplate.cloud_provider_id).where(
                    ProductTemplate.id == product.template_id
                )
            )
        ).scalar_one_or_none()
    if not cloud_id:
        return None
    return (
        await session.execute(select(CloudProvider).where(CloudProvider.id == cloud_id))
    ).scalar_one_or_none()


async def push_package(
    session: AsyncSession,
    auth: AuthContext,
    *,
    package_id: str,
    client_product_id: str,
    device_ids: Sequence[str] | None = None,
    request: Request | None = None,
) -> OtaPushResult:
    """把固件包推送给某客户产品下的设备（逐台留痕）。

    Raises:
        AppException: 固件包 / 客户产品不存在，或云服务商不支持平台侧 OTA
            （``OTA_NOT_SUPPORTED``，409）。
    """
    package = await get_package(session, package_id)
    product = await ai_config_service.get_product(session, auth, client_product_id)

    cloud = await _cloud_provider_of(session, product)
    ota_support = str(cloud.ota_support) if cloud is not None else str(OtaSupport.SUPPORTED)
    if ota_support == str(OtaSupport.UNSUPPORTED):
        raise ota_not_supported(
            "该联网方案仅支持端侧升级，平台无法推送",
            details={
                "otaSupport": ota_support,
                "cloudVendor": str(cloud.vendor) if cloud is not None else None,
                "cloudProviderName": cloud.name if cloud is not None else None,
            },
        )

    # ---- 目标设备清单 ----
    if device_ids:
        requested_ids = list(dict.fromkeys(str(item) for item in device_ids))
    else:
        requested_ids = [
            str(row)
            for row in (
                await session.execute(
                    select(Device.id).where(
                        Device.client_product_id == product.id,
                        Device.asset_status != str(AssetStatus.RETIRED),
                    )
                )
            )
            .scalars()
            .all()
        ]

    devices: dict[str, Device] = {}
    if requested_ids:
        devices = {
            row.id: row
            for row in (
                await session.execute(select(Device).where(Device.id.in_(requested_ids)))
            )
            .scalars()
            .all()
        }

    # ---- 解析供应商（设备侧 OTA 能力）----
    registry.ensure_default_registry()
    resolved = await registry.resolve_for_product(session, client_product_id=product.id)
    provider = resolved.provider if resolved is not None else None
    file_missing_message = "固件包没有上传文件，无法推送"

    results: list[OtaPushRecordItem] = []
    records_written = 0
    pushed = failed = skipped = 0

    for device_id in requested_ids:
        device = devices.get(device_id)
        if device is None or device.client_product_id != product.id:
            # 不属于该产品（或根本不存在）：只进响应、不写库——
            # ota_records.device_id 是指向 devices 的外键，写一条不存在的设备会直接违反约束。
            failed += 1
            results.append(
                OtaPushRecordItem(
                    device_id=device_id,
                    sn=device.sn if device is not None else None,
                    status=PUSH_STATUS_UNKNOWN_DEVICE,
                    from_version=device.firmware_version if device is not None else None,
                    to_version=package.version,
                    error_message=(
                        "设备不属于该客户产品" if device is not None else "设备不存在"
                    ),
                )
            )
            continue

        if device.firmware_version == package.version:
            # 幂等：已在目标版本，不重复推送（也不写 ota_records，见模块 docstring）
            skipped += 1
            results.append(
                OtaPushRecordItem(
                    device_id=device.id,
                    sn=device.sn,
                    status=PUSH_STATUS_SKIPPED,
                    from_version=device.firmware_version,
                    to_version=package.version,
                )
            )
            continue

        record = OtaRecord(
            id=new_id("ota_record"),
            ota_package_id=package.id,
            device_id=device.id,
            tenant_id=device.tenant_id or product.tenant_id,
            client_product_id=product.id,
            status=str(OtaPushStatus.PENDING),
            from_version=device.firmware_version,
            to_version=package.version,
        )
        session.add(record)
        await session.flush()
        records_written += 1

        if not package.storage_path:
            record.status = str(OtaPushStatus.FAILED)
            record.error_message = file_missing_message
            record.finished_at = utcnow()
            await session.flush()
            failed += 1
            results.append(
                OtaPushRecordItem(
                    device_id=device.id,
                    sn=device.sn,
                    status=str(OtaPushStatus.FAILED),
                    from_version=device.firmware_version,
                    to_version=package.version,
                    error_message=file_missing_message,
                )
            )
            continue

        record.status = str(OtaPushStatus.PUSHING)
        record.pushed_at = utcnow()
        await session.flush()

        vendor_message: str | None = None
        error_message: str | None = None
        success = False
        if provider is None:
            error_message = "未解析到可用的云服务商，无法推送"
        else:
            try:
                action = await provider.push_ota(
                    device_ids=[device.vendor_device_id or device.id],
                    firmware_version=package.version,
                    firmware_url=package.storage_path,
                )
                success = action.success
                vendor_message = action.message
                if not success:
                    error_message = action.message or "厂商返回推送失败"
            except NotImplementedError:
                error_message = f"供应商「{provider.name or provider.code}」未实现 OTA 推送能力"
            except AppException as exc:
                error_message = exc.message
            except Exception as exc:  # 厂商适配器的任何异常都不该让整批中断
                logger.exception("OTA 推送异常：package=%s device=%s", package.id, device.id)
                error_message = f"推送中断：{type(exc).__name__}"

        record.status = str(OtaPushStatus.SUCCESS if success else OtaPushStatus.FAILED)
        record.vendor_message = vendor_message
        record.error_message = error_message
        record.finished_at = utcnow()
        if success:
            # 成功才更新设备版本：失败却改版本号会让「设备实际固件」永久说谎
            device.firmware_version = package.version
            pushed += 1
        else:
            failed += 1
        await session.flush()

        results.append(
            OtaPushRecordItem(
                device_id=device.id,
                sn=device.sn,
                status=record.status,
                from_version=record.from_version,
                to_version=record.to_version,
                error_message=error_message,
                vendor_message=vendor_message,
            )
        )

    if records_written:
        await audit_service.record(
            session,
            action=AuditAction.OTA_PUSH,
            actor=auth,
            resource_type="ota_package",
            resource_id=package.id,
            summary=(
                f"推送固件 v{package.version} 至客户产品「{product.name}」："
                f"成功 {pushed}、失败 {failed}、跳过 {skipped}"
            ),
            detail={
                "clientProductId": product.id,
                "requested": len(requested_ids),
                "pushed": pushed,
                "failed": failed,
                "skipped": skipped,
            },
            request=request,
        )
    await session.commit()

    logger.info(
        "OTA 推送完成：package=%s product=%s requested=%d pushed=%d failed=%d skipped=%d",
        package.id,
        product.code,
        len(requested_ids),
        pushed,
        failed,
        skipped,
    )
    return OtaPushResult(
        package_id=package.id,
        client_product_id=product.id,
        ota_support=ota_support,
        requested=len(requested_ids),
        pushed=pushed,
        failed=failed,
        skipped=skipped,
        records=results,
    )


# ---------------------------------------------------------------------------
# 三、推送记录查询
# ---------------------------------------------------------------------------


async def list_records(
    session: AsyncSession,
    auth: AuthContext,
    *,
    package_id: str | None = None,
    status: str | None = None,
    device_id: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[OtaRecordResponse], int]:
    """分页查询推送记录（平台端，按时间倒序）。"""
    conditions: list[ColumnElement[bool]] = []
    if package_id:
        conditions.append(OtaRecord.ota_package_id == package_id)
    if status:
        conditions.append(OtaRecord.status == status)
    if device_id:
        conditions.append(OtaRecord.device_id == device_id)

    total = int(
        (
            await session.execute(
                scoped(select(func.count()).select_from(OtaRecord), OtaRecord, auth).where(
                    *conditions
                )
            )
        ).scalar_one()
    )
    rows = list(
        (
            await session.execute(
                scoped(select(OtaRecord), OtaRecord, auth)
                .where(*conditions)
                .order_by(OtaRecord.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    sn_map = await _device_sns(session, {row.device_id for row in rows})
    return (
        [_record_to_response(row, sn=sn_map.get(row.device_id)) for row in rows],
        total,
    )


__all__ = [
    "OTA_PACKAGE_STATUS_LABELS",
    "PACKAGE_DETAIL_RECORD_LIMIT",
    "allowed_ota_extensions",
    "create_package",
    "delete_package",
    "get_package",
    "get_package_detail",
    "list_packages",
    "list_records",
    "package_to_response",
    "push_package",
    "update_package",
]
