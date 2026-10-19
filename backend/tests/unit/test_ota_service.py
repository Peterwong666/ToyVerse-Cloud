"""ota_service 单元测试。

覆盖口径：

* 纯函数：``allowed_ota_extensions`` / ``package_to_response``
* 固件包 CRUD：``create_package``（三种文件形态 + 全部错误分支）、
  ``list_packages`` / ``get_package_detail`` / ``update_package`` / ``delete_package``
* 推送：``push_package`` 的逐台留痕——设备不存在 / 跨产品 / 已在目标版本（均不写库）、
  包无文件 / 供应商未解析 / 推送成功 / 厂商失败 / 三类异常分支，
  以及云服务商 ``OTA_NOT_SUPPORTED`` 的 409 安全失败
* 记录查询：``list_records`` 的过滤与租户隔离

固件文件通过 monkeypatch 落到 ``tmp_path``，绝不污染仓库的 ``data/storage``；
供应商一律用注册表替身，测试可完全离线运行。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import registry
from app.ai.base import (
    CAP_DEVICE,
    AIProvider,
    DeviceActionResult,
    HealthCheckResult,
)
from app.core.config import settings
from app.core.deps import AuthContext
from app.core.errors import AppException, ErrorCode, validation_error
from app.core.ids import new_id
from app.core.storage import LocalStorage, sha256_hex
from app.db.base import utcnow
from app.models.ai import AiConfig
from app.models.audit import AuditLog
from app.models.catalog import ClientProduct, CloudProvider, ProductTemplate
from app.models.device import Device
from app.models.enums import (
    AiProviderKind,
    AssetStatus,
    AuditAction,
    CloudVendor,
    EnableStatus,
    NetworkType,
    OtaPushStatus,
    OtaSupport,
    ProviderHealth,
    RoleType,
)
from app.models.identity import Tenant
from app.models.ops import OtaPackage, OtaRecord
from app.services import ota_service

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 夹具与替身
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """每个用例前后都清空注册表，避免全局状态串台。"""
    registry.reset_registry()
    yield
    registry.reset_registry()


@pytest.fixture(autouse=True)
def _tmp_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """把 ota_service 的存储后端重定向到临时目录（返回存储根路径）。"""
    monkeypatch.setattr(ota_service, "get_storage", lambda: LocalStorage(root=tmp_path))
    return tmp_path


class _FakeOtaProvider(AIProvider):
    """推送替身：按预编程的结果或异常响应 ``push_ota``，并记录调用参数。"""

    kind = AiProviderKind.DEVICE_CLOUD
    requires_credentials = False
    capabilities = frozenset({CAP_DEVICE})

    def __init__(
        self,
        code: str = "fake-ota",
        *,
        result: DeviceActionResult | None = None,
        error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.code = code
        self.name = f"替身-{code}"
        self._result = result
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def health_check(self) -> HealthCheckResult:
        return HealthCheckResult(status=str(ProviderHealth.UP), ok=True, message="ok")

    async def push_ota(
        self, *, device_ids: Sequence[str], firmware_version: str, firmware_url: str
    ) -> DeviceActionResult:
        self.calls.append(
            {
                "device_ids": list(device_ids),
                "firmware_version": firmware_version,
                "firmware_url": firmware_url,
            }
        )
        if self._error is not None:
            raise self._error
        assert self._result is not None, "替身未预编程结果"
        return self._result


def _register_fake(
    *,
    result: DeviceActionResult | None = None,
    error: Exception | None = None,
) -> _FakeOtaProvider:
    fake = _FakeOtaProvider(result=result, error=error)
    registry.register(fake)
    return fake


# ---------------------------------------------------------------------------
# 造数辅助
# ---------------------------------------------------------------------------


def _platform_auth() -> AuthContext:
    return AuthContext(
        user_id="u-platform",
        account="admin",
        role_code="PLATFORM_ADMIN",
        role_type=RoleType.PLATFORM,
    )


def _merchant_auth(tenant: Tenant) -> AuthContext:
    return AuthContext(
        user_id="u-merchant",
        account="13812345679",
        role_code="MERCHANT_ADMIN",
        role_type=RoleType.MERCHANT,
        tenant_id=tenant.id,
        tenant_code=tenant.code,
    )


async def _make_tenant(db: AsyncSession, *, code: str = "T001") -> Tenant:
    tenant = Tenant(id=new_id("tenant"), code=code, name=f"租户{code}", status="ACTIVE")
    db.add(tenant)
    await db.flush()
    return tenant


async def _make_cloud(
    db: AsyncSession,
    *,
    code: str = "cloud-1",
    name: str = "京东云",
    vendor: str = str(CloudVendor.JOYINSIDE),
    ota_support: str = str(OtaSupport.UNSUPPORTED),
) -> CloudProvider:
    cloud = CloudProvider(
        id=new_id("cloud_provider"),
        code=code,
        name=name,
        vendor=vendor,
        ota_support=ota_support,
    )
    db.add(cloud)
    await db.flush()
    return cloud


async def _make_template(
    db: AsyncSession,
    *,
    code: str = "tpl-1",
    name: str = "故事机模板",
    cloud_provider_id: str | None = None,
) -> ProductTemplate:
    template = ProductTemplate(
        id=new_id("product_template"),
        code=code,
        name=name,
        network_type=str(NetworkType.FOUR_G),
        cloud_provider_id=cloud_provider_id,
    )
    db.add(template)
    await db.flush()
    return template


async def _make_product(
    db: AsyncSession,
    tenant: Tenant,
    *,
    code: str = "prod-1",
    name: str = "故事机",
    template: ProductTemplate | None = None,
    cloud_snapshot: bool = True,
) -> ClientProduct:
    """建客户产品；``cloud_snapshot=True`` 时按真实流程快照模板的云服务商。"""
    if template is None:
        template = await _make_template(db, code=f"tpl-{code}")
    product = ClientProduct(
        id=new_id("client_product"),
        tenant_id=tenant.id,
        template_id=template.id,
        code=code,
        name=name,
        network_type=template.network_type,
        cloud_provider_id=template.cloud_provider_id if cloud_snapshot else None,
    )
    db.add(product)
    await db.flush()
    return product


async def _make_device(
    db: AsyncSession,
    tenant: Tenant,
    product: ClientProduct,
    *,
    sn: str,
    firmware_version: str = "1.0.0",
    asset_status: str = str(AssetStatus.GENERATED),
    vendor_device_id: str | None = None,
) -> Device:
    device = Device(
        id=new_id("device"),
        tenant_id=tenant.id,
        client_product_id=product.id,
        sn=sn,
        network_type=product.network_type,
        firmware_version=firmware_version,
        asset_status=asset_status,
        vendor_device_id=vendor_device_id,
    )
    db.add(device)
    await db.flush()
    return device


async def _make_package(
    db: AsyncSession,
    template: ProductTemplate,
    *,
    version: str = "2.0.0",
    status: str = str(EnableStatus.ENABLED),
    file_name: str | None = None,
    storage_path: str | None = None,
    checksum: str | None = None,
    created_at: Any = None,
) -> OtaPackage:
    package = OtaPackage(
        id=new_id("ota_package"),
        template_id=template.id,
        version=version,
        status=status,
        file_name=file_name,
        storage_path=storage_path,
        checksum=checksum,
        published_at=utcnow(),
    )
    if created_at is not None:
        package.created_at = created_at
    db.add(package)
    await db.flush()
    return package


async def _make_record(
    db: AsyncSession,
    package: OtaPackage,
    device: Device,
    *,
    status: str = str(OtaPushStatus.SUCCESS),
    tenant_id: str | None = None,
    created_at: Any = None,
) -> OtaRecord:
    record = OtaRecord(
        id=new_id("ota_record"),
        ota_package_id=package.id,
        device_id=device.id,
        tenant_id=tenant_id or device.tenant_id,
        client_product_id=device.client_product_id,
        status=status,
        from_version="1.0.0",
        to_version=package.version,
    )
    if created_at is not None:
        record.created_at = created_at
    db.add(record)
    await db.flush()
    return record


async def _make_ai_config(
    db: AsyncSession, tenant: Tenant, product: ClientProduct, *, provider_code: str
) -> AiConfig:
    """给产品钉死供应商（解析顺序第 ① 层），让推送解析到替身。"""
    config = AiConfig(
        id=new_id("ai_config"),
        tenant_id=tenant.id,
        client_product_id=product.id,
        provider_code=provider_code,
    )
    db.add(config)
    await db.flush()
    return config


async def _audit_rows(db: AsyncSession, action: str) -> list[AuditLog]:
    return list(
        (await db.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all()
    )


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_allowed_ota_extensions_parses_settings() -> None:
    """扩展名白名单来自配置，归一化为小写、不含点。"""
    assert ota_service.allowed_ota_extensions() == {"bin", "img", "hex", "zip", "gz"}


def test_package_to_response_flags_labels_and_stats() -> None:
    """``has_file`` 由 storage_path 判定；状态展示名命中字典，未知值原样回落。"""
    package = OtaPackage(
        id="otap-x",
        template_id="pt-x",
        version="2.0.0",
        status=str(EnableStatus.ENABLED),
        storage_path="ota/pt-x/otap-x.bin",
        published_at=utcnow(),
        is_forced=False,
        created_at=utcnow(),
    )
    response = ota_service.package_to_response(package, template_name="故事机模板")

    assert response.has_file is True
    assert response.status_label == "启用"
    assert response.template_name == "故事机模板"
    assert response.push_stats.total == 0

    package.storage_path = None
    package.status = str(EnableStatus.DISABLED)
    assert ota_service.package_to_response(package).status_label == "停用"
    assert ota_service.package_to_response(package).has_file is False

    package.status = "ARCHIVED"  # 未来新增状态时不得炸掉列表页
    assert ota_service.package_to_response(package).status_label == "ARCHIVED"


# ---------------------------------------------------------------------------
# 固件包 CRUD
# ---------------------------------------------------------------------------


async def test_get_package_not_found(db: AsyncSession) -> None:
    with pytest.raises(AppException) as excinfo:
        await ota_service.get_package(db, "otap-ghost")

    assert excinfo.value.code == ErrorCode.RESOURCE_NOT_FOUND
    assert excinfo.value.status_code == 404


async def test_create_package_template_not_found(db: AsyncSession) -> None:
    with pytest.raises(AppException) as excinfo:
        await ota_service.create_package(
            db, _platform_auth(), template_id="pt-ghost", version="2.0.0"
        )

    assert excinfo.value.code == ErrorCode.RESOURCE_NOT_FOUND
    assert "产品模板不存在" in excinfo.value.message


async def test_create_package_duplicate_version_conflicts(db: AsyncSession) -> None:
    """同模板同版本属于冲突（409），``details`` 给出已存在的包 ID 供前端跳转。"""
    template = await _make_template(db)
    existing = await _make_package(db, template, version="2.0.0")

    with pytest.raises(AppException) as excinfo:
        await ota_service.create_package(
            db, _platform_auth(), template_id=template.id, version="2.0.0"
        )

    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
    assert excinfo.value.status_code == 409
    assert excinfo.value.details == {"field": "version", "existingPackageId": existing.id}


async def test_create_package_rejects_disallowed_extension(db: AsyncSession) -> None:
    template = await _make_template(db)

    with pytest.raises(AppException) as excinfo:
        await ota_service.create_package(
            db,
            _platform_auth(),
            template_id=template.id,
            version="2.0.0",
            file_name="firmware.exe",
            file_content=b"MZ",
        )

    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
    assert excinfo.value.details is not None
    assert excinfo.value.details["field"] == "file"
    assert excinfo.value.details["allowed"] == sorted(ota_service.allowed_ota_extensions())


async def test_create_package_rejects_oversized_file(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上限走配置（测试里钉小，避免真的构造 128MB 字节串）。"""
    monkeypatch.setattr(settings, "OTA_PACKAGE_MAX_BYTES", 8)
    template = await _make_template(db)

    with pytest.raises(AppException) as excinfo:
        await ota_service.create_package(
            db,
            _platform_auth(),
            template_id=template.id,
            version="2.0.0",
            file_name="firmware.bin",
            file_content=b"123456789",
        )

    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
    assert excinfo.value.details == {"field": "file", "maxBytes": 8}


async def test_create_package_with_file_stores_bytes(
    db: AsyncSession, _tmp_storage: Path
) -> None:
    """带文件创建：字节落存储、checksum 落库、响应 ``has_file=True``，且写审计。"""
    template = await _make_template(db)
    content = b"fake-firmware-bytes"

    response = await ota_service.create_package(
        db,
        _platform_auth(),
        template_id=template.id,
        version="2.0.0",
        release_notes="修复离线唤醒",
        min_version="1.0.0",
        is_forced=True,
        file_name="firmware.BIN",  # 扩展名大小写不敏感
        file_content=content,
    )

    assert response.has_file is True
    assert response.template_name == template.name
    assert response.file_name == "firmware.BIN"
    assert response.file_size == len(content)
    assert response.checksum == sha256_hex(content)
    assert response.min_version == "1.0.0"
    assert response.is_forced is True

    # 存储 key 由服务端生成（不含用户原始文件名），且字节真实落盘
    row = await ota_service.get_package(db, response.id)
    assert row.storage_path is not None
    assert row.storage_path.startswith(f"ota/{template.id}/")
    assert row.storage_path.endswith(".bin")  # 扩展名归一化为小写
    assert (_tmp_storage / row.storage_path).read_bytes() == content

    audits = await _audit_rows(db, str(AuditAction.CREATE))
    assert len(audits) == 1
    assert audits[0].resource_type == "ota_package"
    assert audits[0].detail == {"templateId": template.id, "hasFile": True}


async def test_create_package_filename_only_is_metadata_registration(db: AsyncSession) -> None:
    """只登记文件名（无字节）：没有 checksum 可算，``has_file=False``。"""
    template = await _make_template(db)

    response = await ota_service.create_package(
        db,
        _platform_auth(),
        template_id=template.id,
        version="2.0.0",
        file_name="firmware.bin",
    )

    assert response.file_name == "firmware.bin"
    assert response.checksum is None
    assert response.has_file is False


async def test_list_packages_filters_keyword_and_paginates(db: AsyncSession) -> None:
    """状态 / 模板 / 关键字（版本号与文件名）过滤 + 分页 + 模板名与推送统计回填。"""
    auth = _platform_auth()
    template_a = await _make_template(db, code="tpl-a", name="故事机模板")
    template_b = await _make_template(db, code="tpl-b", name="儿歌机模板")
    base = utcnow()
    pkg1 = await _make_package(db, template_a, version="2.0.0", created_at=base)
    await _make_package(
        db,
        template_a,
        version="2.1.0",
        file_name="hotfix.bin",
        created_at=base.replace(microsecond=base.microsecond + 1),
    )
    await _make_package(
        db,
        template_b,
        version="3.0.0",
        status=str(EnableStatus.DISABLED),
        created_at=base.replace(microsecond=base.microsecond + 2),
    )
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant, template=template_a)
    device = await _make_device(db, tenant, product, sn="SN-1")
    await _make_record(db, pkg1, device, status=str(OtaPushStatus.SUCCESS))
    await _make_record(db, pkg1, device, status=str(OtaPushStatus.FAILED))
    await _make_record(db, pkg1, device, status=str(OtaPushStatus.PENDING))

    items, total = await ota_service.list_packages(db, auth)
    assert total == 3
    # 按 created_at 倒序
    assert [item.version for item in items] == ["3.0.0", "2.1.0", "2.0.0"]
    by_version = {item.version: item for item in items}
    assert by_version["2.0.0"].template_name == "故事机模板"
    stats = by_version["2.0.0"].push_stats
    assert (stats.total, stats.success, stats.failed, stats.pending) == (3, 1, 1, 1)
    assert by_version["3.0.0"].status_label == "停用"

    items, total = await ota_service.list_packages(db, auth, status=str(EnableStatus.DISABLED))
    assert total == 1 and items[0].version == "3.0.0"

    items, total = await ota_service.list_packages(db, auth, template_id=template_a.id)
    assert total == 2

    # 关键字同时命中版本号与文件名
    _, total = await ota_service.list_packages(db, auth, keyword="2.1")
    assert total == 1
    items, total = await ota_service.list_packages(db, auth, keyword="hotfix")
    assert total == 1 and items[0].version == "2.1.0"

    # 分页：每页 2 条，第二页只剩 1 条
    items, total = await ota_service.list_packages(db, auth, offset=2, limit=2)
    assert total == 3 and len(items) == 1


async def test_get_package_detail_returns_recent_records_with_sn(db: AsyncSession) -> None:
    """详情附最近推送记录（按时间倒序），并回填设备 SN。"""
    tenant = await _make_tenant(db)
    template = await _make_template(db)
    product = await _make_product(db, tenant, template=template)
    package = await _make_package(db, template)
    device_old = await _make_device(db, tenant, product, sn="SN-OLD")
    device_new = await _make_device(db, tenant, product, sn="SN-NEW")
    base = utcnow()
    await _make_record(db, package, device_old, created_at=base)
    await _make_record(
        db,
        package,
        device_new,
        status=str(OtaPushStatus.FAILED),
        created_at=base.replace(microsecond=base.microsecond + 1),
    )

    detail = await ota_service.get_package_detail(db, package.id)

    assert detail.id == package.id
    assert detail.template_name == template.name
    assert detail.push_stats.total == 2
    assert len(detail.records) == 2
    # 按 created_at 倒序
    assert detail.records[0].sn == "SN-NEW"
    assert detail.records[1].sn == "SN-OLD"
    by_sn = {record.sn: record for record in detail.records}
    assert by_sn["SN-NEW"].status == str(OtaPushStatus.FAILED)
    assert by_sn["SN-OLD"].to_version == package.version


async def test_update_package_applies_only_fields_set(db: AsyncSession) -> None:
    """部分更新只认 ``fields_set`` 里的字段；未列入的字段一律不动。"""
    template = await _make_template(db)
    package = await _make_package(db, template, version="2.0.0")

    detail = await ota_service.update_package(
        db,
        _platform_auth(),
        package.id,
        release_notes="新说明",
        min_version="9.9.9",  # 不在 fields_set 里，必须被忽略
        is_forced=True,
        status=str(EnableStatus.DISABLED),
        fields_set={"release_notes", "is_forced", "status"},
    )

    assert detail.release_notes == "新说明"
    assert detail.min_version is None
    assert detail.is_forced is True
    assert detail.status == str(EnableStatus.DISABLED)

    audits = await _audit_rows(db, str(AuditAction.UPDATE))
    assert len(audits) == 1
    assert audits[0].detail == {"fields": ["release_notes", "is_forced", "status"]}


async def test_update_package_noop_skips_audit_and_commit(db: AsyncSession) -> None:
    """没有任何字段变化：直接返回详情，不写审计（避免刷屏）。"""
    template = await _make_template(db)
    package = await _make_package(db, template, version="2.0.0")

    detail = await ota_service.update_package(
        db, _platform_auth(), package.id, release_notes="x", fields_set=set()
    )
    assert detail.version == "2.0.0"
    assert await _audit_rows(db, str(AuditAction.UPDATE)) == []

    # status 与原值相同也不算变化
    detail = await ota_service.update_package(
        db,
        _platform_auth(),
        package.id,
        status=str(EnableStatus.ENABLED),
        fields_set={"status"},
    )
    assert detail.status == str(EnableStatus.ENABLED)
    assert await _audit_rows(db, str(AuditAction.UPDATE)) == []


async def test_delete_package_cascades_records_and_storage(
    db: AsyncSession, _tmp_storage: Path
) -> None:
    """删除 = 级联删推送记录 + 尽力删存储对象 + 写审计。"""
    tenant = await _make_tenant(db)
    template = await _make_template(db)
    product = await _make_product(db, tenant, template=template)
    package = await ota_service.create_package(
        db,
        _platform_auth(),
        template_id=template.id,
        version="2.0.0",
        file_name="firmware.bin",
        file_content=b"bytes",
    )
    device = await _make_device(db, tenant, product, sn="SN-1")
    row = await ota_service.get_package(db, package.id)
    assert row.storage_path is not None
    await _make_record(db, row, device)
    stored_file = _tmp_storage / row.storage_path
    assert stored_file.is_file()

    await ota_service.delete_package(db, _platform_auth(), package.id)

    assert (await db.execute(select(func.count()).select_from(OtaPackage))).scalar_one() == 0
    assert (await db.execute(select(func.count()).select_from(OtaRecord))).scalar_one() == 0
    assert not stored_file.exists()
    audits = await _audit_rows(db, str(AuditAction.DELETE))
    assert len(audits) == 1
    assert audits[0].resource_id == package.id


async def test_delete_package_not_found(db: AsyncSession) -> None:
    with pytest.raises(AppException) as excinfo:
        await ota_service.delete_package(db, _platform_auth(), "otap-ghost")

    assert excinfo.value.code == ErrorCode.RESOURCE_NOT_FOUND


# ---------------------------------------------------------------------------
# 推送：云服务商门禁
# ---------------------------------------------------------------------------


async def test_push_rejects_unsupported_cloud_provider(db: AsyncSession) -> None:
    """Wi-Fi 方案（京东 JoyInside）仅支持端侧升级：平台推送必须 409 安全失败。"""
    tenant = await _make_tenant(db)
    cloud = await _make_cloud(db, ota_support=str(OtaSupport.UNSUPPORTED))
    template = await _make_template(db, cloud_provider_id=cloud.id)
    product = await _make_product(db, tenant, template=template)
    package = await _make_package(db, template)

    with pytest.raises(AppException) as excinfo:
        await ota_service.push_package(
            db, _platform_auth(), package_id=package.id, client_product_id=product.id
        )

    assert excinfo.value.code == ErrorCode.OTA_NOT_SUPPORTED
    assert excinfo.value.status_code == 409
    assert excinfo.value.details == {
        "otaSupport": str(OtaSupport.UNSUPPORTED),
        "cloudVendor": str(CloudVendor.JOYINSIDE),
        "cloudProviderName": cloud.name,
    }


async def test_push_cloud_falls_back_to_template_when_snapshot_missing(db: AsyncSession) -> None:
    """产品快照缺云服务商时回落到模板绑定——回落路径同样要过 OTA 门禁。"""
    tenant = await _make_tenant(db)
    cloud = await _make_cloud(db, ota_support=str(OtaSupport.UNSUPPORTED))
    template = await _make_template(db, cloud_provider_id=cloud.id)
    product = await _make_product(db, tenant, template=template, cloud_snapshot=False)
    package = await _make_package(db, template)

    with pytest.raises(AppException) as excinfo:
        await ota_service.push_package(
            db, _platform_auth(), package_id=package.id, client_product_id=product.id
        )

    assert excinfo.value.code == ErrorCode.OTA_NOT_SUPPORTED


async def test_push_product_not_found_and_merchant_cross_tenant(db: AsyncSession) -> None:
    """产品不存在 → 404；商户推别人租户的产品同样 404（越权不暴露存在性）。"""
    template = await _make_template(db)
    package = await _make_package(db, template)

    with pytest.raises(AppException) as excinfo:
        await ota_service.push_package(
            db, _platform_auth(), package_id=package.id, client_product_id="cprod-ghost"
        )
    assert excinfo.value.code == ErrorCode.RESOURCE_NOT_FOUND

    tenant_a = await _make_tenant(db, code="T00A")
    tenant_b = await _make_tenant(db, code="T00B")
    product = await _make_product(db, tenant_a, template=template)
    with pytest.raises(AppException) as excinfo:
        await ota_service.push_package(
            db, _merchant_auth(tenant_b), package_id=package.id, client_product_id=product.id
        )
    assert excinfo.value.code == ErrorCode.RESOURCE_NOT_FOUND


# ---------------------------------------------------------------------------
# 推送：逐台留痕
# ---------------------------------------------------------------------------


async def test_push_unknown_cross_product_and_same_version_devices_skip_db_writes(
    db: AsyncSession,
) -> None:
    """三类「不推送」都只进响应、不写库：设备不存在 / 不属于该产品 / 已在目标版本。"""
    tenant = await _make_tenant(db)
    template = await _make_template(db)
    product = await _make_product(db, tenant, template=template)
    other_product = await _make_product(db, tenant, code="prod-2", template=template)
    package = await _make_package(db, template, version="2.0.0")
    foreign = await _make_device(db, tenant, other_product, sn="SN-FOREIGN")
    up_to_date = await _make_device(db, tenant, product, sn="SN-DONE", firmware_version="2.0.0")

    result = await ota_service.push_package(
        db,
        _platform_auth(),
        package_id=package.id,
        client_product_id=product.id,
        device_ids=["d-ghost", foreign.id, up_to_date.id],
    )

    assert (result.requested, result.pushed, result.failed, result.skipped) == (3, 0, 2, 1)
    by_id = {record.device_id: record for record in result.records}
    assert by_id["d-ghost"].status == str(OtaPushStatus.FAILED)
    assert by_id["d-ghost"].error_message == "设备不存在"
    assert by_id["d-ghost"].sn is None
    assert by_id[foreign.id].error_message == "设备不属于该客户产品"
    assert by_id[foreign.id].sn == "SN-FOREIGN"
    assert by_id[up_to_date.id].status == ota_service.PUSH_STATUS_SKIPPED
    assert by_id[up_to_date.id].from_version == "2.0.0"

    # 一台都没真正推送：不写 ota_records，也不写审计
    assert (await db.execute(select(func.count()).select_from(OtaRecord))).scalar_one() == 0
    assert await _audit_rows(db, str(AuditAction.OTA_PUSH)) == []


async def test_push_fails_when_package_has_no_file(db: AsyncSession) -> None:
    """元数据登记的包（无文件）逐台写 FAILED 并说明原因——绝不伪造成功。"""
    tenant = await _make_tenant(db)
    template = await _make_template(db)
    product = await _make_product(db, tenant, template=template)
    package = await _make_package(db, template)  # 无 storage_path
    await _make_device(db, tenant, product, sn="SN-1")

    result = await ota_service.push_package(
        db, _platform_auth(), package_id=package.id, client_product_id=product.id
    )

    assert (result.requested, result.pushed, result.failed) == (1, 0, 1)
    item = result.records[0]
    assert item.status == str(OtaPushStatus.FAILED)
    assert item.error_message == "固件包没有上传文件，无法推送"

    record = (await db.execute(select(OtaRecord))).scalar_one()
    assert record.status == str(OtaPushStatus.FAILED)
    assert record.error_message == "固件包没有上传文件，无法推送"
    assert record.finished_at is not None
    assert record.tenant_id == tenant.id


async def test_push_fails_when_provider_unresolved(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """四层解析全部落空（平台默认指向未注册键）→ 逐台 FAILED 并说明原因。"""
    monkeypatch.setattr(settings, "AI_DEFAULT_PROVIDER", "ghost")
    tenant = await _make_tenant(db)
    template = await _make_template(db)  # 无云服务商
    product = await _make_product(db, tenant, template=template)
    package = await _make_package(db, template, storage_path="ota/x/fw.bin")
    await _make_device(db, tenant, product, sn="SN-1")

    result = await ota_service.push_package(
        db, _platform_auth(), package_id=package.id, client_product_id=product.id
    )

    assert result.failed == 1
    assert result.records[0].error_message == "未解析到可用的云服务商，无法推送"


async def test_push_success_updates_firmware_and_audits(db: AsyncSession) -> None:
    """成功推送：写 SUCCESS 记录、回写设备固件版本、按 vendor_device_id 寻址、写审计。"""
    fake = _register_fake(result=DeviceActionResult(success=True, message="已下发"))
    tenant = await _make_tenant(db)
    template = await _make_template(db)
    product = await _make_product(db, tenant, template=template)
    await _make_ai_config(db, tenant, product, provider_code=fake.code)
    package = await _make_package(db, template, version="2.0.0", storage_path="ota/x/fw.bin")
    device = await _make_device(db, tenant, product, sn="SN-1", vendor_device_id="vd-1")

    result = await ota_service.push_package(
        db, _platform_auth(), package_id=package.id, client_product_id=product.id
    )

    assert (result.requested, result.pushed, result.failed, result.skipped) == (1, 1, 0, 0)
    assert result.ota_support == str(OtaSupport.SUPPORTED)  # 无云服务商视为可推送
    item = result.records[0]
    assert item.status == str(OtaPushStatus.SUCCESS)
    assert item.vendor_message == "已下发"
    assert item.from_version == "1.0.0" and item.to_version == "2.0.0"

    # 按厂商设备 ID 寻址（没有 vendor_device_id 才回落平台 ID）
    assert fake.calls == [
        {"device_ids": ["vd-1"], "firmware_version": "2.0.0", "firmware_url": "ota/x/fw.bin"}
    ]

    record = (await db.execute(select(OtaRecord))).scalar_one()
    assert record.status == str(OtaPushStatus.SUCCESS)
    assert record.pushed_at is not None and record.finished_at is not None

    await db.refresh(device)
    assert device.firmware_version == "2.0.0"

    audits = await _audit_rows(db, str(AuditAction.OTA_PUSH))
    assert len(audits) == 1
    assert audits[0].detail == {
        "clientProductId": product.id,
        "requested": 1,
        "pushed": 1,
        "failed": 0,
        "skipped": 0,
    }


async def test_push_vendor_failure_keeps_firmware_version(db: AsyncSession) -> None:
    """厂商返回失败：版本号坚决不回写（失败却改版本会让设备固件永久说谎）。"""
    tenant = await _make_tenant(db)
    template = await _make_template(db)
    product = await _make_product(db, tenant, template=template)
    package = await _make_package(db, template, version="2.0.0", storage_path="ota/x/fw.bin")
    device = await _make_device(db, tenant, product, sn="SN-1")
    config = await _make_ai_config(db, tenant, product, provider_code="fake-ota")

    # ① 厂商给出失败原因
    _register_fake(result=DeviceActionResult(success=False, message="厂商限流"))
    result = await ota_service.push_package(
        db, _platform_auth(), package_id=package.id, client_product_id=product.id
    )
    assert result.failed == 1
    assert result.records[0].error_message == "厂商限流"
    assert result.records[0].vendor_message == "厂商限流"

    # ② 厂商连原因都没给：兜底文案（client_product_id 唯一，换供应商走更新）
    config.provider_code = "fake-ota-2"
    await db.flush()
    registry.register(
        _FakeOtaProvider(code="fake-ota-2", result=DeviceActionResult(success=False, message=""))
    )
    result = await ota_service.push_package(
        db, _platform_auth(), package_id=package.id, client_product_id=product.id
    )
    assert result.records[0].error_message == "厂商返回推送失败"

    await db.refresh(device)
    assert device.firmware_version == "1.0.0"

    records = list((await db.execute(select(OtaRecord))).scalars().all())
    assert len(records) == 2
    assert all(record.status == str(OtaPushStatus.FAILED) for record in records)


async def test_push_exception_branches_are_per_device(db: AsyncSession) -> None:
    """适配器的任何异常都不让整批中断，且按异常类型给出可解读的错误文案。"""
    tenant = await _make_tenant(db)
    template = await _make_template(db)
    product = await _make_product(db, tenant, template=template)
    package = await _make_package(db, template, version="2.0.0", storage_path="ota/x/fw.bin")
    await _make_device(db, tenant, product, sn="SN-1")
    # client_product_id 唯一：配置建一次，每轮只换注册表里的替身
    await _make_ai_config(db, tenant, product, provider_code="fake-ota")

    async def _push_with(error: Exception) -> str | None:
        registry.reset_registry()
        _register_fake(error=error)
        result = await ota_service.push_package(
            db, _platform_auth(), package_id=package.id, client_product_id=product.id
        )
        return result.records[0].error_message

    # NotImplementedError：能力未实现，报供应商名
    message = await _push_with(NotImplementedError())
    assert message == "供应商「替身-fake-ota」未实现 OTA 推送能力"

    # AppException：透传业务文案（如厂商限流）
    message = await _push_with(validation_error("请求过于频繁"))
    assert message == "请求过于频繁"

    # 未知异常：只暴露异常类型名，不把内部细节泄漏给调用方
    message = await _push_with(RuntimeError("boom"))
    assert message == "推送中断：RuntimeError"

    # 三次全部失败但都留了痕
    assert (await db.execute(select(func.count()).select_from(OtaRecord))).scalar_one() == 3


async def test_push_defaults_to_non_retired_devices_and_dedupes_ids(db: AsyncSession) -> None:
    """缺省推全部非 RETIRED 设备；显式 ID 列表去重。"""
    tenant = await _make_tenant(db)
    template = await _make_template(db)
    product = await _make_product(db, tenant, template=template)
    package = await _make_package(db, template)  # 无文件 → 逐台 FAILED，便于只核对计数
    device1 = await _make_device(db, tenant, product, sn="SN-1")
    await _make_device(db, tenant, product, sn="SN-2")
    await _make_device(db, tenant, product, sn="SN-3", asset_status=str(AssetStatus.RETIRED))

    # 显式列表：重复的 ID 只推一次
    result = await ota_service.push_package(
        db,
        _platform_auth(),
        package_id=package.id,
        client_product_id=product.id,
        device_ids=[device1.id, device1.id, device1.id],
    )
    assert result.requested == 1

    # 缺省：RETIRED 之外的全部设备
    result = await ota_service.push_package(
        db, _platform_auth(), package_id=package.id, client_product_id=product.id
    )
    assert result.requested == 2
    assert result.failed == 2
    assert {record.sn for record in result.records} == {"SN-1", "SN-2"}

    assert (await db.execute(select(func.count()).select_from(OtaRecord))).scalar_one() == 3


# ---------------------------------------------------------------------------
# 推送记录查询
# ---------------------------------------------------------------------------


async def test_list_records_filters_by_package_status_and_device(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    template = await _make_template(db)
    product = await _make_product(db, tenant, template=template)
    package_a = await _make_package(db, template, version="2.0.0")
    package_b = await _make_package(db, template, version="3.0.0")
    device1 = await _make_device(db, tenant, product, sn="SN-1")
    device2 = await _make_device(db, tenant, product, sn="SN-2")
    await _make_record(db, package_a, device1, status=str(OtaPushStatus.SUCCESS))
    await _make_record(db, package_a, device2, status=str(OtaPushStatus.FAILED))
    await _make_record(db, package_b, device1, status=str(OtaPushStatus.SUCCESS))

    auth = _platform_auth()
    items, total = await ota_service.list_records(db, auth)
    assert total == 3
    assert {item.sn for item in items} == {"SN-1", "SN-2"}

    _, total = await ota_service.list_records(db, auth, package_id=package_a.id)
    assert total == 2

    items, total = await ota_service.list_records(db, auth, status=str(OtaPushStatus.FAILED))
    assert total == 1 and items[0].sn == "SN-2"
    assert items[0].to_version == "2.0.0"

    _, total = await ota_service.list_records(
        db, auth, package_id=package_b.id, device_id=device1.id
    )
    assert total == 1

    items, total = await ota_service.list_records(db, auth, offset=2, limit=2)
    assert total == 3 and len(items) == 1


async def test_list_records_scoped_for_merchant(db: AsyncSession) -> None:
    """商户只能看到自己租户的推送记录（scope 强制 tenant_id 过滤）。"""
    tenant_a = await _make_tenant(db, code="T00A")
    tenant_b = await _make_tenant(db, code="T00B")
    template = await _make_template(db)
    product_a = await _make_product(db, tenant_a, code="prod-a", template=template)
    product_b = await _make_product(db, tenant_b, code="prod-b", template=template)
    package = await _make_package(db, template)
    device_a = await _make_device(db, tenant_a, product_a, sn="SN-A")
    device_b = await _make_device(db, tenant_b, product_b, sn="SN-B")
    await _make_record(db, package, device_a)
    await _make_record(db, package, device_b)

    items, total = await ota_service.list_records(db, _merchant_auth(tenant_a))
    assert total == 1
    assert items[0].sn == "SN-A"
