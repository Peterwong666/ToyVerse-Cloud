"""目录域服务：云服务商 / 产品模板 / 产品授权 / 客户产品 / 小程序配置。

三条贯穿本模块的规则
--------------------
1. **密钥只进不出**：``access_key`` / ``secret_key`` / ``app_secret`` 明文入参
   经 :mod:`app.core.crypto` 加密后落库；响应只回 ``*_hint`` 掩码。
   本模块任何函数的返回值都不含明文密钥。
2. **授权是创建客户产品的前置条件**：未授权的组合一律
   ``PRODUCT_NOT_AUTHORIZED``（见 :func:`create_client_product`）。
3. **删除前先校验关联**（P-06）：有下游数据时返回 ``CASCADE_CONFLICT``
   并说明「还有什么挡着」，而不是把错误抛给数据库外键。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import Request
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret, secret_hint
from app.core.deps import AuthContext
from app.core.errors import (
    cascade_conflict,
    cloud_code_exists,
    not_found,
    product_code_exists,
    product_not_authorized,
    template_code_exists,
    validation_error,
)
from app.core.ids import new_id
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.scope import scoped
from app.models.catalog import (
    ClientProduct,
    CloudProvider,
    MiniAppConfig,
    ProductAuthorization,
    ProductTemplate,
)
from app.models.enums import (
    AuditAction,
    CloudProviderStatus,
    CloudVendor,
    EnableStatus,
    NetworkType,
    TenantStatus,
    TestResult,
)
from app.models.identity import Tenant
from app.schemas.catalog import (
    AuthorizationResponse,
    ClientProductBrief,
    ClientProductResponse,
    CloudProviderResponse,
    CloudTestResponse,
    MiniAppConfigResponse,
    TemplateResponse,
)
from app.services import audit_service

logger = get_logger(__name__)

#: 连通性探测的超时（秒）。刻意设短——检测接口不应长时间占用请求
CONNECT_TIMEOUT_SECONDS = 5.0


# ===========================================================================
# 一、云服务商
# ===========================================================================


def cloud_to_response(cloud: CloudProvider, template_count: int = 0) -> CloudProviderResponse:
    """把云服务商 ORM 对象转为响应对象（只带掩码，不带任何密钥）。"""
    return CloudProviderResponse(
        id=cloud.id,
        code=cloud.code,
        name=cloud.name,
        vendor=cloud.vendor,
        network_type=cloud.network_type,
        api_base=cloud.api_base,
        access_key_hint=cloud.access_key_hint,
        secret_key_hint=cloud.secret_key_hint,
        extra_config=cloud.extra_config,
        status=cloud.status,
        ota_support=cloud.ota_support,
        has_credentials=cloud.has_credentials,
        last_tested_at=cloud.last_tested_at,
        last_test_ok=cloud.last_test_ok,
        last_test_message=cloud.last_test_message,
        template_count=template_count,
        remark=cloud.remark,
        created_at=cloud.created_at,
        updated_at=cloud.updated_at,
    )


async def get_cloud(session: AsyncSession, cloud_id: str) -> CloudProvider:
    """按 ID 取云服务商。

    Raises:
        AppException: 不存在。
    """
    cloud = (
        await session.execute(select(CloudProvider).where(CloudProvider.id == cloud_id))
    ).scalar_one_or_none()
    if cloud is None:
        raise not_found("云服务商不存在")
    return cloud


async def list_clouds(
    session: AsyncSession,
    *,
    offset: int = 0,
    limit: int = 20,
    sort_by: str | None = None,
    order: str = "desc",
    keyword: str | None = None,
    vendor: str | None = None,
    status: str | None = None,
) -> tuple[list[CloudProviderResponse], int]:
    """分页查询云服务商（附带「被多少模板引用」计数）。"""
    conditions: list[ColumnElement[bool]] = []
    if keyword:
        pattern = f"%{keyword.strip()}%"
        conditions.append(CloudProvider.name.like(pattern) | CloudProvider.code.like(pattern))
    if vendor:
        conditions.append(CloudProvider.vendor == vendor)
    if status:
        conditions.append(CloudProvider.status == status)

    total = int(
        (
            await session.execute(
                select(func.count()).select_from(CloudProvider).where(*conditions)
            )
        ).scalar_one()
    )

    stmt = select(CloudProvider).where(*conditions)
    column = getattr(CloudProvider, sort_by, None) if sort_by else None
    if column is None or not hasattr(column, "asc"):
        column = CloudProvider.created_at
    stmt = stmt.order_by(column.desc() if order == "desc" else column.asc()).offset(offset).limit(limit)

    rows = list((await session.execute(stmt)).scalars().all())
    counts = await _template_counts(session, [row.id for row in rows])
    return [cloud_to_response(row, counts.get(row.id, 0)) for row in rows], total


async def _template_counts(session: AsyncSession, cloud_ids: list[str]) -> dict[str, int]:
    """统计每个云服务商被多少产品模板引用（一次分组查询，避免 N+1）。"""
    if not cloud_ids:
        return {}
    stmt = (
        select(ProductTemplate.cloud_provider_id, func.count())
        .where(ProductTemplate.cloud_provider_id.in_(cloud_ids))
        .group_by(ProductTemplate.cloud_provider_id)
    )
    return {
        str(row[0]): int(row[1]) for row in (await session.execute(stmt)).all() if row[0] is not None
    }


async def create_cloud(
    session: AsyncSession,
    *,
    payload: dict[str, Any],
    actor: AuthContext | None = None,
    request: Request | None = None,
) -> CloudProvider:
    """创建云服务商（密钥加密落库）。

    Raises:
        AppException: 编码已存在。
    """
    code = str(payload["code"])
    exists = (
        await session.execute(select(CloudProvider).where(CloudProvider.code == code))
    ).scalar_one_or_none()
    if exists is not None:
        raise cloud_code_exists(
            f"云服务商编码「{code}」已存在",
            details={"field": "code", "message": "编码已存在"},
        )

    access_key = (payload.get("access_key") or "").strip()
    secret_key = (payload.get("secret_key") or "").strip()

    cloud = CloudProvider(
        id=new_id("cloud_provider"),
        code=code,
        name=payload["name"],
        vendor=str(payload.get("vendor") or CloudVendor.JIXIAN),
        network_type=str(payload.get("network_type") or NetworkType.FOUR_G),
        api_base=(payload.get("api_base") or None),
        access_key_enc=encrypt_secret(access_key) if access_key else None,
        secret_key_enc=encrypt_secret(secret_key) if secret_key else None,
        access_key_hint=secret_hint(access_key) if access_key else None,
        secret_key_hint=secret_hint(secret_key) if secret_key else None,
        extra_config=payload.get("extra_config"),
        ota_support=str(payload.get("ota_support") or "SUPPORTED"),
        status=CloudProviderStatus.NOT_CONNECTED,
        remark=payload.get("remark"),
    )
    session.add(cloud)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        actor=actor,
        resource_type="cloud_provider",
        resource_id=cloud.id,
        summary=f"新增云服务商「{cloud.name}」（{cloud.code}）",
        # 审计只记录「是否配置了密钥」，绝不记录密钥内容
        detail={"vendor": cloud.vendor, "credentialsConfigured": cloud.has_credentials},
        request=request,
    )
    await session.commit()
    logger.info("已新增云服务商 %s（%s）", cloud.code, cloud.vendor)
    return cloud


async def update_cloud(
    session: AsyncSession,
    *,
    cloud_id: str,
    payload: dict[str, Any],
    actor: AuthContext | None = None,
    request: Request | None = None,
) -> CloudProvider:
    """更新云服务商。密钥字段留空表示保持原值。"""
    cloud = await get_cloud(session, cloud_id)
    changed: list[str] = []

    for field_name in ("name", "api_base", "extra_config", "ota_support", "remark"):
        if field_name not in payload:
            continue
        value = payload[field_name]
        if value is None:
            continue
        if getattr(cloud, field_name) != value:
            setattr(cloud, field_name, value)
            changed.append(field_name)

    access_key = (payload.get("access_key") or "").strip()
    if access_key:
        cloud.access_key_enc = encrypt_secret(access_key)
        cloud.access_key_hint = secret_hint(access_key)
        changed.append("access_key")

    secret_key = (payload.get("secret_key") or "").strip()
    if secret_key:
        cloud.secret_key_enc = encrypt_secret(secret_key)
        cloud.secret_key_hint = secret_hint(secret_key)
        changed.append("secret_key")

    if not changed:
        return cloud

    # 密钥变更后原检测结论失效，强制回到「未连接」
    if "access_key" in changed or "secret_key" in changed:
        cloud.status = CloudProviderStatus.NOT_CONNECTED
        cloud.last_test_ok = None
        cloud.last_test_message = "密钥已更新，请重新执行连通性检测"

    await audit_service.record(
        session,
        action=AuditAction.UPDATE,
        actor=actor,
        resource_type="cloud_provider",
        resource_id=cloud.id,
        summary=f"更新云服务商「{cloud.name}」",
        detail={"fields": changed},
        request=request,
    )
    await session.commit()
    return cloud


async def delete_cloud(
    session: AsyncSession,
    *,
    cloud_id: str,
    actor: AuthContext | None = None,
    request: Request | None = None,
) -> None:
    """删除云服务商。

    ★ P-06：被产品模板或客户产品引用时返回 ``CASCADE_CONFLICT``。

    Raises:
        AppException: 存在关联数据。
    """
    cloud = await get_cloud(session, cloud_id)

    template_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ProductTemplate)
                .where(ProductTemplate.cloud_provider_id == cloud.id)
            )
        ).scalar_one()
    )
    product_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ClientProduct)
                .where(ClientProduct.cloud_provider_id == cloud.id)
            )
        ).scalar_one()
    )

    if template_count or product_count:
        parts = []
        if template_count:
            parts.append(f"{template_count} 个产品模板")
        if product_count:
            parts.append(f"{product_count} 个客户产品")
        raise cascade_conflict(
            f"云服务商「{cloud.name}」仍被 {'、'.join(parts)} 引用，无法删除。",
            details={"templates": template_count, "clientProducts": product_count},
        )

    await session.delete(cloud)
    await audit_service.record(
        session,
        action=AuditAction.DELETE,
        actor=actor,
        resource_type="cloud_provider",
        resource_id=cloud.id,
        summary=f"删除云服务商「{cloud.name}」（{cloud.code}）",
        request=request,
    )
    await session.commit()
    logger.warning("已删除云服务商 %s", cloud.code)


async def test_cloud_connectivity(
    session: AsyncSession,
    *,
    cloud_id: str,
    actor: AuthContext | None = None,
    request: Request | None = None,
) -> CloudTestResponse:
    """云服务商连通性检测。

    语义边界（重要，避免「伪造成功」）
    --------------------------------
    * 未配置 ``api_base`` 或密钥 → ``result=NOT_CONFIGURED``、``ok=false``，
      **不发起任何真实调用**，并把状态置为「未连接」
    * 已配置 → 对 ``api_base`` 发起一次 HTTP 探测：
      只要拿到 HTTP 响应即视为「网络可达」（``ok=true``）；
      连接失败 / 超时 / 5xx 视为 ``FAILED``
    * 无论结果如何，**都不宣称「密钥有效」**——密钥有效性只有在真正调用
      厂商业务接口（P4 生成设备、P8 激活）时才能确认，本接口不越权承诺
    """
    cloud = await get_cloud(session, cloud_id)
    tested_at = utcnow()

    if not cloud.api_base or not cloud.has_credentials:
        missing = []
        if not cloud.api_base:
            missing.append("接口地址")
        if not cloud.has_credentials:
            missing.append("AccessKey / SecretKey")
        result = CloudTestResponse(
            result=TestResult.NOT_CONFIGURED,
            ok=False,
            message=f"尚未配置 {'、'.join(missing)}，已按安全策略跳过真实调用",
            vendor=cloud.vendor,
            tested_at=tested_at,
        )
    else:
        ok, message, latency = await _probe(cloud.api_base)
        result = CloudTestResponse(
            result=TestResult.SUCCESS if ok else TestResult.FAILED,
            ok=ok,
            message=message,
            latency_ms=latency,
            vendor=cloud.vendor,
            tested_at=tested_at,
        )

    # 回写检测结论（无论成功失败都记录，便于运维看到「上次探测时间」）
    cloud.last_tested_at = tested_at
    cloud.last_test_ok = result.ok
    cloud.last_test_message = result.message
    cloud.status = (
        CloudProviderStatus.CONNECTED if result.ok else CloudProviderStatus.NOT_CONNECTED
    )

    await audit_service.record(
        session,
        action=AuditAction.UPDATE,
        actor=actor,
        resource_type="cloud_provider",
        resource_id=cloud.id,
        summary=f"连通性检测：{result.result}——{result.message}",
        detail={"ok": result.ok, "latencyMs": result.latency_ms},
        success=result.ok,
        request=request,
    )
    await session.commit()
    return result


async def _probe(api_base: str) -> tuple[bool, str, int | None]:
    """对厂商接口基址发起一次轻量 HTTP 探测。

    Returns:
        ``(是否可达, 中文说明, 耗时毫秒)``
    """
    started = utcnow()
    try:
        async with httpx.AsyncClient(timeout=CONNECT_TIMEOUT_SECONDS) as client:
            response = await client.get(api_base)
    except httpx.HTTPError as exc:
        elapsed = int((utcnow() - started).total_seconds() * 1000)
        return False, f"无法连通厂商接口：{type(exc).__name__}", elapsed

    elapsed = int((utcnow() - started).total_seconds() * 1000)
    if response.status_code >= 500:
        return False, f"厂商接口返回 HTTP {response.status_code}（服务异常）", elapsed
    return (
        True,
        f"网络可达（HTTP {response.status_code}）。该结论仅代表网络连通，"
        "密钥有效性将在实际调用厂商接口时验证",
        elapsed,
    )


# ===========================================================================
# 二、产品模板
# ===========================================================================


def template_to_response(
    template: ProductTemplate,
    *,
    cloud_name: str | None = None,
    authorization_count: int = 0,
    client_product_count: int = 0,
) -> TemplateResponse:
    """把模板 ORM 对象转为响应对象。"""
    return TemplateResponse(
        id=template.id,
        code=template.code,
        name=template.name,
        category=template.category,
        model=template.model,
        chip=template.chip,
        network_type=template.network_type,
        cloud_provider_id=template.cloud_provider_id,
        cloud_provider_name=cloud_name,
        firmware_version=template.firmware_version,
        reference_price=float(template.reference_price) if template.reference_price is not None else None,
        ai_features=template.ai_features,
        specs=template.specs,
        status=template.status,
        description=template.description,
        authorization_count=authorization_count,
        client_product_count=client_product_count,
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


async def get_template(session: AsyncSession, template_id: str) -> ProductTemplate:
    """按 ID 取产品模板。

    Raises:
        AppException: 不存在。
    """
    template = (
        await session.execute(select(ProductTemplate).where(ProductTemplate.id == template_id))
    ).scalar_one_or_none()
    if template is None:
        raise not_found("产品模板不存在")
    return template


async def get_template_detail(session: AsyncSession, template_id: str) -> TemplateResponse:
    """取产品模板详情（补齐云服务商名称与两个计数）。

    为什么详情也要补计数：列表接口会填 ``cloudProviderName`` /
    ``authorizationCount`` / ``clientProductCount``，若详情接口不填，
    前端从列表点进详情会出现「同一字段时有时无」的错觉（P3 验收中发现）。
    """
    template = await get_template(session, template_id)
    cloud_names = await _cloud_names(
        session, {template.cloud_provider_id} if template.cloud_provider_id else set()
    )
    auth_counts = await _authorization_counts(session, [template.id])
    product_counts = await _template_product_counts(session, [template.id])
    return template_to_response(
        template,
        cloud_name=cloud_names.get(template.cloud_provider_id or ""),
        authorization_count=auth_counts.get(template.id, 0),
        client_product_count=product_counts.get(template.id, 0),
    )


async def list_templates(
    session: AsyncSession,
    *,
    offset: int = 0,
    limit: int = 20,
    sort_by: str | None = None,
    order: str = "desc",
    keyword: str | None = None,
    category: str | None = None,
    network_type: str | None = None,
    status: str | None = None,
) -> tuple[list[TemplateResponse], int]:
    """分页查询产品模板（附授权数 / 客户产品数）。"""
    conditions: list[ColumnElement[bool]] = []
    if keyword:
        pattern = f"%{keyword.strip()}%"
        conditions.append(
            ProductTemplate.name.like(pattern)
            | ProductTemplate.code.like(pattern)
            | ProductTemplate.model.like(pattern)
        )
    if category:
        conditions.append(ProductTemplate.category == category)
    if network_type:
        conditions.append(ProductTemplate.network_type == network_type)
    if status:
        conditions.append(ProductTemplate.status == status)

    total = int(
        (
            await session.execute(
                select(func.count()).select_from(ProductTemplate).where(*conditions)
            )
        ).scalar_one()
    )

    stmt = select(ProductTemplate).where(*conditions)
    column = getattr(ProductTemplate, sort_by, None) if sort_by else None
    if column is None or not hasattr(column, "asc"):
        column = ProductTemplate.created_at
    stmt = stmt.order_by(column.desc() if order == "desc" else column.asc()).offset(offset).limit(limit)

    rows = list((await session.execute(stmt)).scalars().all())
    ids = [row.id for row in rows]
    cloud_names = await _cloud_names(session, {row.cloud_provider_id for row in rows if row.cloud_provider_id})
    auth_counts = await _authorization_counts(session, ids)
    product_counts = await _template_product_counts(session, ids)

    return (
        [
            template_to_response(
                row,
                cloud_name=cloud_names.get(row.cloud_provider_id or ""),
                authorization_count=auth_counts.get(row.id, 0),
                client_product_count=product_counts.get(row.id, 0),
            )
            for row in rows
        ],
        total,
    )


async def _cloud_names(session: AsyncSession, cloud_ids: set[str]) -> dict[str, str]:
    """批量取云服务商名称。"""
    if not cloud_ids:
        return {}
    stmt = select(CloudProvider.id, CloudProvider.name).where(CloudProvider.id.in_(cloud_ids))
    return {str(row[0]): str(row[1]) for row in (await session.execute(stmt)).all()}


async def _authorization_counts(session: AsyncSession, template_ids: list[str]) -> dict[str, int]:
    """统计每个模板被授权给多少租户。"""
    if not template_ids:
        return {}
    stmt = (
        select(ProductAuthorization.template_id, func.count())
        .where(ProductAuthorization.template_id.in_(template_ids))
        .group_by(ProductAuthorization.template_id)
    )
    return {str(row[0]): int(row[1]) for row in (await session.execute(stmt)).all()}


async def _template_product_counts(session: AsyncSession, template_ids: list[str]) -> dict[str, int]:
    """统计每个模板派生出多少客户产品。"""
    if not template_ids:
        return {}
    stmt = (
        select(ClientProduct.template_id, func.count())
        .where(ClientProduct.template_id.in_(template_ids))
        .group_by(ClientProduct.template_id)
    )
    return {str(row[0]): int(row[1]) for row in (await session.execute(stmt)).all()}


async def create_template(
    session: AsyncSession,
    *,
    payload: dict[str, Any],
    actor: AuthContext | None = None,
    request: Request | None = None,
) -> ProductTemplate:
    """创建产品模板。

    Raises:
        AppException: 编码已存在，或关联的云服务商不存在。
    """
    code = str(payload["code"])
    exists = (
        await session.execute(select(ProductTemplate).where(ProductTemplate.code == code))
    ).scalar_one_or_none()
    if exists is not None:
        raise template_code_exists(
            f"产品模板编码「{code}」已存在",
            details={"field": "code", "message": "编码已存在"},
        )

    cloud_id = payload.get("cloud_provider_id")
    if cloud_id:
        await get_cloud(session, str(cloud_id))  # 不存在则抛 RESOURCE_NOT_FOUND

    template = ProductTemplate(
        id=new_id("product_template"),
        code=code,
        name=payload["name"],
        category=payload.get("category"),
        model=payload.get("model"),
        chip=payload.get("chip"),
        network_type=str(payload.get("network_type") or NetworkType.WIFI),
        cloud_provider_id=str(cloud_id) if cloud_id else None,
        firmware_version=payload.get("firmware_version"),
        reference_price=payload.get("reference_price"),
        ai_features=payload.get("ai_features"),
        specs=payload.get("specs"),
        status=str(payload.get("status") or EnableStatus.ENABLED),
        description=payload.get("description"),
    )
    session.add(template)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        actor=actor,
        resource_type="product_template",
        resource_id=template.id,
        summary=f"新增产品模板「{template.name}」（{template.code}）",
        detail={"networkType": template.network_type, "cloudProviderId": template.cloud_provider_id},
        request=request,
    )
    await session.commit()
    logger.info("已新增产品模板 %s（%s）", template.code, template.network_type)
    return template


async def update_template(
    session: AsyncSession,
    *,
    template_id: str,
    payload: dict[str, Any],
    actor: AuthContext | None = None,
    request: Request | None = None,
) -> ProductTemplate:
    """更新产品模板。

    注意：模板变更**不追溯**已创建的客户产品（后者持有快照字段）。
    """
    template = await get_template(session, template_id)

    if payload.get("cloud_provider_id"):
        await get_cloud(session, str(payload["cloud_provider_id"]))

    changed: list[str] = []
    for field_name in (
        "name",
        "category",
        "model",
        "chip",
        "network_type",
        "cloud_provider_id",
        "firmware_version",
        "reference_price",
        "ai_features",
        "specs",
        "status",
        "description",
    ):
        if field_name not in payload or payload[field_name] is None:
            continue
        value = payload[field_name]
        if hasattr(value, "value"):  # StrEnum → 存字符串
            value = value.value
        if getattr(template, field_name) != value:
            setattr(template, field_name, value)
            changed.append(field_name)

    if not changed:
        return template

    await audit_service.record(
        session,
        action=AuditAction.UPDATE,
        actor=actor,
        resource_type="product_template",
        resource_id=template.id,
        summary=f"更新产品模板「{template.name}」",
        detail={"fields": changed},
        request=request,
    )
    await session.commit()
    return template


async def delete_template(
    session: AsyncSession,
    *,
    template_id: str,
    actor: AuthContext | None = None,
    request: Request | None = None,
) -> None:
    """删除产品模板。

    ★ P-06：存在授权或客户产品时返回 ``CASCADE_CONFLICT``。

    Raises:
        AppException: 存在关联数据。
    """
    template = await get_template(session, template_id)

    auth_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ProductAuthorization)
                .where(ProductAuthorization.template_id == template.id)
            )
        ).scalar_one()
    )
    product_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ClientProduct)
                .where(ClientProduct.template_id == template.id)
            )
        ).scalar_one()
    )

    if auth_count or product_count:
        parts = []
        if auth_count:
            parts.append(f"{auth_count} 条租户授权")
        if product_count:
            parts.append(f"{product_count} 个客户产品")
        raise cascade_conflict(
            f"产品模板「{template.name}」仍存在 {'、'.join(parts)}，无法删除。"
            "请先撤销授权并处理客户产品。",
            details={"authorizations": auth_count, "clientProducts": product_count},
        )

    await session.delete(template)
    await audit_service.record(
        session,
        action=AuditAction.DELETE,
        actor=actor,
        resource_type="product_template",
        resource_id=template.id,
        summary=f"删除产品模板「{template.name}」（{template.code}）",
        request=request,
    )
    await session.commit()
    logger.warning("已删除产品模板 %s", template.code)


# ===========================================================================
# 三、产品授权
# ===========================================================================


@dataclass(slots=True)
class AuthorizeOutcome:
    """授权结果。"""

    created: list[str]
    skipped: list[str]
    denied: list[str]
    total_authorized: int


async def authorize_template(
    session: AsyncSession,
    *,
    template_id: str,
    tenant_ids: list[str],
    max_devices: int | None = None,
    expires_at: Any = None,
    remark: str | None = None,
    actor: AuthContext | None = None,
    request: Request | None = None,
) -> AuthorizeOutcome:
    """把模板授权给若干租户（**幂等**）。

    已授权的租户标记为 ``skipped`` 而不是报错——批量授权时
    「有几个已存在」是常态，不该让整批失败。

    Raises:
        AppException: 模板不存在。
    """
    template = await get_template(session, template_id)

    existing = set(
        (
            await session.execute(
                select(ProductAuthorization.tenant_id).where(
                    ProductAuthorization.template_id == template.id
                )
            )
        )
        .scalars()
        .all()
    )

    found = set(
        (
            await session.execute(select(Tenant.id).where(Tenant.id.in_(tenant_ids)))
        )
        .scalars()
        .all()
    )

    created: list[str] = []
    skipped: list[str] = []
    denied: list[str] = []

    for tenant_id in tenant_ids:
        if tenant_id not in found:
            denied.append(tenant_id)
            continue
        if tenant_id in existing:
            skipped.append(tenant_id)
            continue

        session.add(
            ProductAuthorization(
                id=new_id("product_authorization"),
                tenant_id=tenant_id,
                template_id=template.id,
                status=EnableStatus.ENABLED,
                authorized_at=utcnow(),
                authorized_by=actor.account if actor else None,
                expires_at=expires_at,
                max_devices=max_devices,
                remark=remark,
            )
        )
        created.append(tenant_id)

    await session.flush()

    total = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ProductAuthorization)
                .where(ProductAuthorization.template_id == template.id)
            )
        ).scalar_one()
    )

    await audit_service.record(
        session,
        action=AuditAction.AUTHORIZE,
        actor=actor,
        resource_type="product_template",
        resource_id=template.id,
        summary=f"授权模板「{template.name}」给 {len(created)} 个租户",
        detail={
            "created": created,
            "skipped": skipped,
            "denied": denied,
            "maxDevices": max_devices,
        },
        request=request,
    )
    await session.commit()

    logger.info("模板 %s 授权：新增 %d，跳过 %d，无效 %d", template.code, len(created), len(skipped), len(denied))
    return AuthorizeOutcome(
        created=created, skipped=skipped, denied=denied, total_authorized=total
    )


async def revoke_authorization(
    session: AsyncSession,
    *,
    authorization_id: str,
    actor: AuthContext | None = None,
    request: Request | None = None,
) -> None:
    """撤销授权。

    ★ P-06：该授权下已有客户产品时拒绝撤销——否则产品会失去授权依据。

    Raises:
        AppException: 授权不存在或存在关联产品。
    """
    authorization = (
        await session.execute(
            select(ProductAuthorization).where(ProductAuthorization.id == authorization_id)
        )
    ).scalar_one_or_none()
    if authorization is None:
        raise not_found("授权记录不存在")

    product_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ClientProduct)
                .where(
                    ClientProduct.tenant_id == authorization.tenant_id,
                    ClientProduct.template_id == authorization.template_id,
                )
            )
        ).scalar_one()
    )
    if product_count:
        raise cascade_conflict(
            f"该授权下已存在 {product_count} 个客户产品，无法撤销。"
            "请先停用或删除这些产品。",
            details={"clientProducts": product_count},
        )

    await session.delete(authorization)
    await audit_service.record(
        session,
        action=AuditAction.DELETE,
        actor=actor,
        tenant_id=authorization.tenant_id,
        resource_type="product_authorization",
        resource_id=authorization.id,
        summary="撤销模板授权",
        request=request,
    )
    await session.commit()


async def list_authorizations(
    session: AsyncSession,
    auth: AuthContext,
    *,
    tenant_id: str | None = None,
    template_id: str | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[AuthorizationResponse], int]:
    """查询授权列表。

    ★ ADR-08：租户过滤经由 :func:`app.db.scope.scoped` 收口，
    商户角色只能看到自己的授权，平台角色可见全部。
    """
    conditions: list[ColumnElement[bool]] = []
    if tenant_id:
        conditions.append(ProductAuthorization.tenant_id == tenant_id)
    if template_id:
        conditions.append(ProductAuthorization.template_id == template_id)
    if status:
        conditions.append(ProductAuthorization.status == status)

    base = scoped(select(ProductAuthorization), ProductAuthorization, auth).where(*conditions)
    total = int(
        (
            await session.execute(
                scoped(
                    select(func.count()).select_from(ProductAuthorization),
                    ProductAuthorization,
                    auth,
                ).where(*conditions)
            )
        ).scalar_one()
    )

    stmt = base.order_by(ProductAuthorization.created_at.desc()).offset(offset).limit(limit)
    rows = list((await session.execute(stmt)).scalars().all())

    tenant_names = await _tenant_labels(session, {row.tenant_id for row in rows})
    templates = await _template_labels(session, {row.template_id for row in rows})
    product_counts = await _authorization_product_counts(
        session, [(row.tenant_id, row.template_id) for row in rows]
    )

    return (
        [
            _authorization_response(
                row,
                tenant_names=tenant_names,
                templates=templates,
                product_count=product_counts.get((row.tenant_id, row.template_id), 0),
            )
            for row in rows
        ],
        total,
    )


def _authorization_response(
    row: ProductAuthorization,
    *,
    tenant_names: dict[str, tuple[str, str]],
    templates: dict[str, tuple[str, str]],
    product_count: int = 0,
) -> AuthorizationResponse:
    """把授权 ORM 对象转为响应对象（补齐租户与模板名称）。"""
    tenant_label = tenant_names.get(row.tenant_id)
    template_label = templates.get(row.template_id)
    return AuthorizationResponse(
        id=row.id,
        tenant_id=row.tenant_id,
        tenant_code=tenant_label[0] if tenant_label else None,
        tenant_name=tenant_label[1] if tenant_label else None,
        template_id=row.template_id,
        template_code=template_label[0] if template_label else None,
        template_name=template_label[1] if template_label else None,
        status=row.status,
        authorized_at=row.authorized_at,
        authorized_by=row.authorized_by,
        expires_at=row.expires_at,
        max_devices=row.max_devices,
        client_product_count=product_count,
        remark=row.remark,
        created_at=row.created_at,
    )


async def _tenant_labels(session: AsyncSession, tenant_ids: set[str]) -> dict[str, tuple[str, str]]:
    """批量取租户的 (code, name)。"""
    if not tenant_ids:
        return {}
    stmt = select(Tenant.id, Tenant.code, Tenant.name).where(Tenant.id.in_(tenant_ids))
    return {str(row[0]): (str(row[1]), str(row[2])) for row in (await session.execute(stmt)).all()}


async def _template_labels(
    session: AsyncSession, template_ids: set[str]
) -> dict[str, tuple[str, str]]:
    """批量取模板的 (code, name)。"""
    if not template_ids:
        return {}
    stmt = select(ProductTemplate.id, ProductTemplate.code, ProductTemplate.name).where(
        ProductTemplate.id.in_(template_ids)
    )
    return {str(row[0]): (str(row[1]), str(row[2])) for row in (await session.execute(stmt)).all()}


async def _authorization_product_counts(
    session: AsyncSession, pairs: list[tuple[str, str]]
) -> dict[tuple[str, str], int]:
    """统计「租户 × 模板」组合下已有多少客户产品。"""
    if not pairs:
        return {}
    tenant_ids = {pair[0] for pair in pairs}
    template_ids = {pair[1] for pair in pairs}
    stmt = (
        select(ClientProduct.tenant_id, ClientProduct.template_id, func.count())
        .where(
            ClientProduct.tenant_id.in_(tenant_ids),
            ClientProduct.template_id.in_(template_ids),
        )
        .group_by(ClientProduct.tenant_id, ClientProduct.template_id)
    )
    return {(str(row[0]), str(row[1])): int(row[2]) for row in (await session.execute(stmt)).all()}


# ===========================================================================
# 四、客户产品
# ===========================================================================


def _client_product_brief(
    product: ClientProduct,
    *,
    tenant_labels: dict[str, tuple[str, str]],
    template_labels: dict[str, tuple[str, str]],
    cloud_labels: dict[str, str],
) -> ClientProductResponse:
    """把客户产品 ORM 对象转为响应对象（补齐租户 / 模板 / 云服务商名称）。"""
    tenant_label = tenant_labels.get(product.tenant_id)
    template_label = template_labels.get(product.template_id)
    return ClientProductResponse(
        id=product.id,
        code=product.code,
        name=product.name,
        tenant_id=product.tenant_id,
        tenant_code=tenant_label[0] if tenant_label else None,
        tenant_name=tenant_label[1] if tenant_label else None,
        template_id=product.template_id,
        template_code=template_label[0] if template_label else None,
        template_name=template_label[1] if template_label else None,
        network_type=product.network_type,
        cloud_provider_id=product.cloud_provider_id,
        cloud_provider_name=cloud_labels.get(product.cloud_provider_id or ""),
        firmware_version=product.firmware_version,
        status=product.status,
        ai_enabled=product.ai_enabled,
        has_miniapp_config=product.miniapp_config is not None,
        miniapp_config=miniapp_to_response(product.miniapp_config)
        if product.miniapp_config
        else None,
        remark=product.remark,
        created_at=product.created_at,
        updated_at=product.updated_at,
    )


def miniapp_to_response(config: MiniAppConfig) -> MiniAppConfigResponse:
    """把小程序配置 ORM 对象转为响应对象（只带掩码）。"""
    return MiniAppConfigResponse(
        id=config.id,
        client_product_id=config.client_product_id,
        tenant_id=config.tenant_id,
        app_name=config.app_name,
        app_id=config.app_id,
        app_secret_hint=config.app_secret_hint,
        has_app_secret=bool(config.app_secret_enc),
        original_id=config.original_id,
        theme_color=config.theme_color,
        logo_url=config.logo_url,
        share_title=config.share_title,
        share_desc=config.share_desc,
        service_phone=config.service_phone,
        service_qr_url=config.service_qr_url,
        version=config.version,
        status=config.status,
        published_at=config.published_at,
        remark=config.remark,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


async def get_client_product(session: AsyncSession, product_id: str) -> ClientProduct:
    """按 ID 取客户产品。

    Raises:
        AppException: 不存在。
    """
    product = (
        await session.execute(select(ClientProduct).where(ClientProduct.id == product_id))
    ).scalar_one_or_none()
    if product is None:
        raise not_found("客户产品不存在")
    return product


async def get_client_product_detail(
    session: AsyncSession, product_id: str
) -> ClientProductResponse:
    """取客户产品详情（含小程序配置与各层名称）。"""
    product = await get_client_product(session, product_id)
    tenant_labels = await _tenant_labels(session, {product.tenant_id})
    template_labels = await _template_labels(session, {product.template_id})
    cloud_labels = await _cloud_names(
        session, {product.cloud_provider_id} if product.cloud_provider_id else set()
    )
    return _client_product_brief(
        product,
        tenant_labels=tenant_labels,
        template_labels=template_labels,
        cloud_labels=cloud_labels,
    )


async def list_client_products(
    session: AsyncSession,
    auth: AuthContext,
    *,
    tenant_id: str | None = None,
    template_id: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
    offset: int = 0,
    limit: int = 20,
    sort_by: str | None = None,
    order: str = "desc",
) -> tuple[list[ClientProductResponse], int]:
    """查询客户产品列表。

    ★ ADR-08：租户过滤经 :func:`app.db.scope.scoped` 收口——
    商户角色只能看到自己的产品，平台角色可见全部。
    """
    conditions: list[ColumnElement[bool]] = []
    if tenant_id:
        conditions.append(ClientProduct.tenant_id == tenant_id)
    if template_id:
        conditions.append(ClientProduct.template_id == template_id)
    if status:
        conditions.append(ClientProduct.status == status)
    if keyword:
        pattern = f"%{keyword.strip()}%"
        conditions.append(ClientProduct.name.like(pattern) | ClientProduct.code.like(pattern))

    total = int(
        (
            await session.execute(
                scoped(
                    select(func.count()).select_from(ClientProduct), ClientProduct, auth
                ).where(*conditions)
            )
        ).scalar_one()
    )

    stmt = scoped(select(ClientProduct), ClientProduct, auth).where(*conditions)
    column = getattr(ClientProduct, sort_by, None) if sort_by else None
    if column is None or not hasattr(column, "asc"):
        column = ClientProduct.created_at
    stmt = stmt.order_by(column.desc() if order == "desc" else column.asc()).offset(offset).limit(limit)

    rows = list((await session.execute(stmt)).scalars().all())
    tenant_labels = await _tenant_labels(session, {row.tenant_id for row in rows})
    template_labels = await _template_labels(session, {row.template_id for row in rows})
    cloud_labels = await _cloud_names(
        session, {row.cloud_provider_id for row in rows if row.cloud_provider_id}
    )

    return (
        [
            _client_product_brief(
                row,
                tenant_labels=tenant_labels,
                template_labels=template_labels,
                cloud_labels=cloud_labels,
            )
            for row in rows
        ],
        total,
    )


async def list_client_product_options(
    session: AsyncSession, auth: AuthContext, *, tenant_id: str | None = None
) -> list[ClientProductBrief]:
    """客户产品下拉选项（不分页，用于关联选择）。"""
    stmt = scoped(select(ClientProduct), ClientProduct, auth)
    if tenant_id:
        stmt = stmt.where(ClientProduct.tenant_id == tenant_id)
    stmt = stmt.order_by(ClientProduct.name).limit(200)
    rows = list((await session.execute(stmt)).scalars().all())
    return [
        ClientProductBrief(
            id=row.id,
            code=row.code,
            name=row.name,
            tenant_id=row.tenant_id,
            template_id=row.template_id,
            network_type=row.network_type,
            status=row.status,
            ai_enabled=row.ai_enabled,
            created_at=row.created_at,
        )
        for row in rows
    ]


async def create_client_product(
    session: AsyncSession,
    *,
    payload: dict[str, Any],
    actor: AuthContext | None = None,
    request: Request | None = None,
) -> ClientProduct:
    """创建客户产品。

    ★ P-03 修复：创建时必须同时给出 ``tenant_id`` 与 ``template_id``，
    且校验三件事，任何一项不满足都不会留下「半成品」数据：

    1. 租户存在且未被禁用
    2. 模板存在且处于启用状态
    3. **该租户已获得该模板的有效授权**（否则 ``PRODUCT_NOT_AUTHORIZED``）

    Raises:
        AppException: 编码已存在 / 租户或模板不存在 / 未授权。
    """
    tenant_id = str(payload["tenant_id"])
    template_id = str(payload["template_id"])
    code = str(payload["code"])

    exists = (
        await session.execute(select(ClientProduct).where(ClientProduct.code == code))
    ).scalar_one_or_none()
    if exists is not None:
        raise product_code_exists(
            f"产品编码「{code}」已存在",
            details={"field": "code", "message": "编码已存在"},
        )

    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant is None:
        raise not_found("租户不存在")
    if tenant.status != TenantStatus.ACTIVE:
        raise validation_error(
            f"租户「{tenant.name}」已被禁用，无法为其创建产品",
            details={"field": "tenantId", "message": "请先启用该租户"},
        )

    template = await get_template(session, template_id)
    if template.status != EnableStatus.ENABLED:
        raise validation_error(
            f"产品模板「{template.name}」已停用，无法用于创建产品",
            details={"field": "templateId", "message": "请先启用该模板"},
        )

    authorization = (
        await session.execute(
            select(ProductAuthorization).where(
                ProductAuthorization.tenant_id == tenant.id,
                ProductAuthorization.template_id == template.id,
                ProductAuthorization.status == EnableStatus.ENABLED,
            )
        )
    ).scalar_one_or_none()
    if authorization is None:
        raise product_not_authorized(
            f"租户「{tenant.name}」尚未获得模板「{template.name}」的授权，"
            "请先在「产品模板」页完成授权",
            details={"tenantId": tenant.id, "templateId": template.id},
        )
    if authorization.expires_at is not None and authorization.expires_at <= utcnow():
        raise product_not_authorized(
            f"模板「{template.name}」对该租户的授权已于 "
            f"{authorization.expires_at:%Y-%m-%d} 到期",
            details={"tenantId": tenant.id, "templateId": template.id},
        )

    product = ClientProduct(
        id=new_id("client_product"),
        tenant_id=tenant.id,
        template_id=template.id,
        code=code,
        name=payload.get("name") or template.name,
        # ---- 快照字段：模板后续变更不追溯影响已交付产品 ----
        network_type=template.network_type,
        cloud_provider_id=template.cloud_provider_id,
        firmware_version=payload.get("firmware_version") or template.firmware_version,
        status=str(payload.get("status") or EnableStatus.ENABLED),
        ai_enabled=False,
        remark=payload.get("remark"),
    )
    session.add(product)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        actor=actor,
        tenant_id=tenant.id,
        resource_type="client_product",
        resource_id=product.id,
        summary=f"为租户「{tenant.name}」创建客户产品「{product.name}」（{product.code}）",
        detail={"templateId": template.id, "networkType": product.network_type},
        request=request,
    )
    await session.commit()

    logger.info("已创建客户产品 %s（租户 %s）", product.code, tenant.code)
    return product


async def update_client_product(
    session: AsyncSession,
    *,
    product_id: str,
    payload: dict[str, Any],
    actor: AuthContext | None = None,
    request: Request | None = None,
) -> ClientProduct:
    """更新客户产品（归属租户与模板不可变更）。"""
    product = await get_client_product(session, product_id)
    changed: list[str] = []

    for field_name in ("name", "firmware_version", "status", "ai_enabled", "remark"):
        if field_name not in payload or payload[field_name] is None:
            continue
        value = payload[field_name]
        if hasattr(value, "value"):
            value = value.value
        if getattr(product, field_name) != value:
            setattr(product, field_name, value)
            changed.append(field_name)

    if not changed:
        return product

    await audit_service.record(
        session,
        action=AuditAction.UPDATE,
        actor=actor,
        tenant_id=product.tenant_id,
        resource_type="client_product",
        resource_id=product.id,
        summary=f"更新客户产品「{product.name}」",
        detail={"fields": changed},
        request=request,
    )
    await session.commit()
    return product


async def delete_client_product(
    session: AsyncSession,
    *,
    product_id: str,
    actor: AuthContext | None = None,
    request: Request | None = None,
) -> None:
    """删除客户产品。

    小程序配置随产品一并删除（1:1 从属关系，DB 层已设 ``ON DELETE CASCADE``，
    这里显式删除以便审计记录清楚）。
    设备关联校验在 P4 设备表落地后接入——
    届时此处会先查 ``devices``，有设备则返回 ``CASCADE_CONFLICT``。

    Raises:
        AppException: 产品不存在。
    """
    product = await get_client_product(session, product_id)

    if product.miniapp_config is not None:
        await session.delete(product.miniapp_config)

    await session.delete(product)
    await audit_service.record(
        session,
        action=AuditAction.DELETE,
        actor=actor,
        tenant_id=product.tenant_id,
        resource_type="client_product",
        resource_id=product.id,
        summary=f"删除客户产品「{product.name}」（{product.code}）",
        detail={"hadMiniappConfig": product.miniapp_config is not None},
        request=request,
    )
    await session.commit()
    logger.warning("已删除客户产品 %s", product.code)


# ===========================================================================
# 五、小程序配置
# ===========================================================================


async def get_miniapp_config(
    session: AsyncSession, *, product_id: str
) -> MiniAppConfig | None:
    """取客户产品的小程序配置（不存在返回 ``None``）。"""
    product = await get_client_product(session, product_id)
    return (
        await session.execute(
            select(MiniAppConfig).where(MiniAppConfig.client_product_id == product.id)
        )
    ).scalar_one_or_none()


async def upsert_miniapp_config(
    session: AsyncSession,
    *,
    product_id: str,
    payload: dict[str, Any],
    actor: AuthContext | None = None,
    request: Request | None = None,
) -> MiniAppConfig:
    """创建或更新小程序配置（upsert）。

    ``app_secret`` 留空表示保持原值；一旦提供则加密落库并刷新掩码。

    Raises:
        AppException: 客户产品不存在。
    """
    product = await get_client_product(session, product_id)
    config = (
        await session.execute(
            select(MiniAppConfig).where(MiniAppConfig.client_product_id == product.id)
        )
    ).scalar_one_or_none()

    app_secret = (payload.get("app_secret") or "").strip()
    created = config is None
    if config is None:
        config = MiniAppConfig(
            id=new_id("miniapp_config"),
            client_product_id=product.id,
            tenant_id=product.tenant_id,
            app_name=payload.get("app_name") or product.name,
            status=str(payload.get("status") or EnableStatus.DISABLED),
        )
        session.add(config)

    for field_name in (
        "app_name",
        "app_id",
        "original_id",
        "theme_color",
        "logo_url",
        "share_title",
        "share_desc",
        "service_phone",
        "service_qr_url",
        "version",
        "remark",
    ):
        if field_name in payload and payload[field_name] is not None:
            setattr(config, field_name, payload[field_name])

    if "status" in payload and payload["status"] is not None:
        status_value = payload["status"]
        config.status = status_value.value if hasattr(status_value, "value") else str(status_value)
        if config.status == EnableStatus.ENABLED and config.published_at is None:
            config.published_at = utcnow()

    if app_secret:
        config.app_secret_enc = encrypt_secret(app_secret)
        config.app_secret_hint = secret_hint(app_secret)

    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE if created else AuditAction.UPDATE,
        actor=actor,
        tenant_id=product.tenant_id,
        resource_type="miniapp_config",
        resource_id=config.id,
        summary=f"{'创建' if created else '更新'}客户产品「{product.name}」的小程序配置",
        detail={"appSecretUpdated": bool(app_secret)},
        request=request,
    )
    await session.commit()
    logger.info("已%s小程序配置：%s", "创建" if created else "更新", config.app_name)
    return config


__all__ = [
    "AuthorizeOutcome",
    "authorize_template",
    "cloud_to_response",
    "create_client_product",
    "create_cloud",
    "create_template",
    "delete_client_product",
    "delete_cloud",
    "delete_template",
    "get_client_product",
    "get_client_product_detail",
    "get_cloud",
    "get_miniapp_config",
    "get_template",
    "get_template_detail",
    "list_authorizations",
    "list_client_product_options",
    "list_client_products",
    "list_clouds",
    "list_templates",
    "miniapp_to_response",
    "revoke_authorization",
    "template_to_response",
    "test_cloud_connectivity",
    "update_client_product",
    "update_cloud",
    "update_template",
    "upsert_miniapp_config",
]
