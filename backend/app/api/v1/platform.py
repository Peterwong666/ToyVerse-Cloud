"""平台端 API：租户 / 云服务商 / 产品模板 / 授权 / 客户产品 / 小程序配置。

设计约定
--------
* 所有端点都要求**平台角色**（``PlatformAuth``），并按读写分别校验权限码
  （平台运营只有读权限，天然只能查看不能改）。
* 路由层只做「参数解析 → 调服务 → 转响应」，不写业务规则、不写租户过滤
  （ADR-08：租户过滤统一在 repository 层经 ``app.db.scope.scoped`` 收口）。
* **任何响应都不含明文密钥**：响应模型里根本没有该字段（见 schemas/catalog.py）。

与 P-03 / P-06 的对应关系
-------------------------
* P-03「新建产品未绑定客户」→ ``POST /platform/client-products`` 强制要求
  ``tenantId`` + ``templateId`` 且校验授权；租户详情会返回其客户产品清单。
* P-06「删除无级联校验」→ 租户 / 云服务商 / 模板 / 授权的删除端点都会
  先校验关联数据，返回 ``CASCADE_CONFLICT`` 并说明还剩什么。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request

from app.core.deps import DbSession, PlatformAuth, require_perm
from app.core.pagination import PageParams, page_params, page_response
from app.core.permissions import PlatformPerm
from app.models.enums import CloudVendor, EnableStatus, NetworkType, TenantStatus
from app.schemas.catalog import (
    AccountBrief,
    AccountCreateRequest,
    AccountCreateResult,
    AuthorizationResponse,
    AuthorizeRequest,
    AuthorizeResult,
    ClientProductCreateRequest,
    ClientProductResponse,
    ClientProductUpdateRequest,
    CloudProviderCreateRequest,
    CloudProviderResponse,
    CloudProviderUpdateRequest,
    CloudTestResponse,
    MiniAppConfigRequest,
    MiniAppConfigResponse,
    OptionItem,
    PasswordResetResult,
    ResetPasswordRequest,
    TemplateCreateRequest,
    TemplateResponse,
    TemplateUpdateRequest,
    TenantCreateRequest,
    TenantDetailResponse,
    TenantResponse,
    TenantStatusRequest,
    TenantUpdateRequest,
)
from app.schemas.common import ListResult, MessageResult, PageResult
from app.services import catalog_service, tenant_service
from app.services.tenant_service import IssuedAccount

router = APIRouter(prefix="/platform", tags=["平台端"])

PageQuery = Annotated[PageParams, Depends(page_params)]


# ===========================================================================
# 内部转换：ORM → 响应对象
# ===========================================================================


def _account_brief(user: Any) -> AccountBrief:
    """账号 ORM → 摘要对象（模型里没有密码字段，不可能泄漏）。"""
    return AccountBrief.model_validate(user)


def _account_result(issued: IssuedAccount) -> AccountCreateResult:
    """新建账号结果 → 响应对象。"""
    return AccountCreateResult(
        account=_account_brief(issued.account),
        password=issued.password,
        generated=issued.password is not None,
    )


# ===========================================================================
# 一、租户（客户开通）
# ===========================================================================


@router.get(
    "/tenants",
    response_model=PageResult[TenantResponse],
    summary="租户列表",
    description="分页查询租户（客户）。支持按名称 / 编码 / 联系人关键字、状态与行业筛选。",
    dependencies=[require_perm(PlatformPerm.TENANT_READ)],
)
async def list_tenants(
    session: DbSession,
    _auth: PlatformAuth,
    page: PageQuery,
    keyword: Annotated[str | None, Query(description="名称 / 编码 / 联系人 / 电话")] = None,
    status: Annotated[TenantStatus | None, Query(description="租户状态")] = None,
    industry: Annotated[str | None, Query(description="所属行业")] = None,
) -> dict[str, Any]:
    """租户列表。"""
    records, total = await tenant_service.list_tenants(
        session,
        offset=page.offset,
        limit=page.limit,
        sort_by=page.sort_by,
        order=page.order,
        keyword=keyword,
        status=str(status) if status else None,
        industry=industry,
    )
    return page_response([TenantResponse.model_validate(item) for item in records], total, page)


@router.post(
    "/tenants",
    response_model=TenantResponse,
    status_code=201,
    summary="开通租户",
    description="创建租户（客户开通）。编码全局唯一且统一转为大写。",
    responses={409: {"description": "租户编码已存在"}},
    dependencies=[require_perm(PlatformPerm.TENANT_WRITE)],
)
async def create_tenant(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    payload: TenantCreateRequest = Body(...),
) -> TenantResponse:
    """开通租户。"""
    tenant = await tenant_service.create_tenant(
        session, payload=payload.model_dump(), actor=auth, request=request
    )
    return TenantResponse.model_validate(tenant)


@router.get(
    "/tenants/{tenant_id}",
    response_model=TenantDetailResponse,
    summary="租户详情",
    description=(
        "返回租户资料、关联计数（账号 / 授权 / 客户产品）以及**授权与客户产品清单**。\n\n"
        "这是 P-03 的验收视角：新建客户产品后，在租户详情即可看到它。"
    ),
    dependencies=[require_perm(PlatformPerm.TENANT_READ)],
)
async def get_tenant_detail(
    session: DbSession,
    auth: PlatformAuth,
    tenant_id: str,
) -> TenantDetailResponse:
    """租户详情。"""
    tenant = await tenant_service.get_tenant(session, tenant_id)
    counts = await tenant_service.collect_counts(session, tenant.id)
    accounts = await tenant_service.list_accounts(session, tenant.id)
    authorizations, _ = await catalog_service.list_authorizations(
        session, auth, tenant_id=tenant.id, limit=200
    )
    products, _ = await catalog_service.list_client_products(
        session, auth, tenant_id=tenant.id, limit=200
    )

    return TenantDetailResponse(
        **TenantResponse.model_validate(tenant).model_dump(),
        counts={"authorizations": counts.authorizations,
                "client_products": counts.client_products,
                "users": counts.users},
        accounts=[_account_brief(item) for item in accounts],
        authorizations=authorizations,
        client_products=products,
    )


@router.put(
    "/tenants/{tenant_id}",
    response_model=TenantResponse,
    summary="更新租户资料",
    description="更新租户名称、联系方式、行业等。**编码不可修改**。",
    dependencies=[require_perm(PlatformPerm.TENANT_WRITE)],
)
async def update_tenant(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    tenant_id: str,
    payload: TenantUpdateRequest = Body(...),
) -> TenantResponse:
    """更新租户资料。"""
    tenant = await tenant_service.update_tenant(
        session,
        tenant_id=tenant_id,
        payload=payload.model_dump(exclude_unset=True),
        actor=auth,
        request=request,
    )
    return TenantResponse.model_validate(tenant)


@router.delete(
    "/tenants/{tenant_id}",
    response_model=MessageResult,
    summary="删除租户",
    description=(
        "删除租户。**修复 P-06**：存在登录账号 / 产品授权 / 客户产品任一关联时拒绝删除，"
        "返回 ``CASCADE_CONFLICT`` 并在 ``details`` 中给出各项数量。"
    ),
    responses={409: {"description": "存在关联数据，无法删除"}},
    dependencies=[require_perm(PlatformPerm.TENANT_WRITE)],
)
async def delete_tenant(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    tenant_id: str,
) -> MessageResult:
    """删除租户。"""
    await tenant_service.delete_tenant(
        session, tenant_id=tenant_id, actor=auth, request=request
    )
    return MessageResult(message="租户已删除")


@router.post(
    "/tenants/{tenant_id}/status",
    response_model=TenantResponse,
    summary="启用 / 禁用租户",
    description=(
        "切换租户状态。禁用后该租户下**所有账号立即无法登录**"
        "（登录流程校验租户状态，见 auth_service），但账号本身状态不变，解禁即恢复。"
    ),
    dependencies=[require_perm(PlatformPerm.TENANT_WRITE)],
)
async def set_tenant_status(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    tenant_id: str,
    payload: TenantStatusRequest = Body(...),
) -> TenantResponse:
    """启用 / 禁用租户。"""
    tenant = await tenant_service.set_tenant_status(
        session,
        tenant_id=tenant_id,
        status=str(payload.status),
        reason=payload.reason,
        actor=auth,
        request=request,
    )
    return TenantResponse.model_validate(tenant)


@router.post(
    "/tenants/{tenant_id}/accounts",
    response_model=AccountCreateResult,
    status_code=201,
    summary="为租户创建登录账号",
    description=(
        "为租户开通一个登录账号（商户管理员 / 商户运营）。\n\n"
        "未指定密码时由服务端生成强随机密码，**只在本次响应中返回一次**，"
        "且账号被置为「下次登录必须改密」——既不写死弱口令，也无需人工编造密码。"
    ),
    responses={400: {"description": "账号已存在或角色不合法"}},
    dependencies=[require_perm(PlatformPerm.TENANT_WRITE, PlatformPerm.MEMBER_WRITE)],
)
async def create_tenant_account(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    tenant_id: str,
    payload: AccountCreateRequest = Body(...),
) -> AccountCreateResult:
    """为租户创建登录账号。"""
    issued = await tenant_service.add_tenant_account(
        session,
        tenant_id=tenant_id,
        account=payload.account,
        nickname=payload.nickname,
        role_code=payload.role_code,
        password=payload.password,
        actor=auth,
        request=request,
    )
    return _account_result(issued)


@router.post(
    "/tenants/{tenant_id}/reset-password",
    response_model=PasswordResetResult,
    summary="重置租户账号密码",
    description=(
        "重置指定账号的密码，同时：置为「下次登录必须改密」、清空登录失败计数与锁定、"
        "**吊销该账号全部刷新令牌**（在线旧登录态立即失效）。\n\n"
        "未指定新密码时由服务端生成，且只在本次响应中返回一次。"
    ),
    responses={404: {"description": "账号不存在或不属于该租户"}},
    dependencies=[require_perm(PlatformPerm.TENANT_WRITE)],
)
async def reset_tenant_password(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    tenant_id: str,
    payload: ResetPasswordRequest = Body(...),
) -> PasswordResetResult:
    """重置租户账号密码。"""
    issued = await tenant_service.reset_account_password(
        session,
        tenant_id=tenant_id,
        account=payload.account,
        new_password=payload.new_password,
        actor=auth,
        request=request,
    )
    return PasswordResetResult(
        account=issued.account.account,
        password=issued.password,
        generated=issued.password is not None,
    )


@router.get(
    "/tenant-industries",
    response_model=ListResult[OptionItem],
    summary="租户行业取值",
    description="返回已存在的行业列表，供前端筛选下拉使用。",
    dependencies=[require_perm(PlatformPerm.TENANT_READ)],
)
async def list_tenant_industries(session: DbSession, _auth: PlatformAuth) -> dict[str, Any]:
    """租户行业取值。"""
    industries = await tenant_service.list_industries(session)
    return {
        "records": [{"value": item, "label": item} for item in industries],
        "total": len(industries),
    }


# ===========================================================================
# 二、云服务商
# ===========================================================================


@router.get(
    "/clouds",
    response_model=PageResult[CloudProviderResponse],
    summary="云服务商列表",
    description=(
        "分页查询云服务商。响应只含 ``accessKeyHint`` / ``secretKeyHint``（前 4 位 + ****），"
        "**绝不含明文或密文密钥**。"
    ),
    dependencies=[require_perm(PlatformPerm.CLOUD_READ)],
)
async def list_clouds(
    session: DbSession,
    _auth: PlatformAuth,
    page: PageQuery,
    keyword: Annotated[str | None, Query(description="名称 / 编码")] = None,
    vendor: Annotated[CloudVendor | None, Query(description="厂商")] = None,
    status: Annotated[str | None, Query(description="接入状态")] = None,
) -> dict[str, Any]:
    """云服务商列表。"""
    records, total = await catalog_service.list_clouds(
        session,
        offset=page.offset,
        limit=page.limit,
        sort_by=page.sort_by,
        order=page.order,
        keyword=keyword,
        vendor=str(vendor) if vendor else None,
        status=status,
    )
    return page_response(records, total, page)


@router.post(
    "/clouds",
    response_model=CloudProviderResponse,
    status_code=201,
    summary="新增云服务商",
    description=(
        "新增云服务商。``accessKey`` / ``secretKey`` 为明文入参，服务端**加密后落库**，"
        "响应仅回掩码提示。"
    ),
    dependencies=[require_perm(PlatformPerm.CLOUD_WRITE)],
)
async def create_cloud(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    payload: CloudProviderCreateRequest = Body(...),
) -> CloudProviderResponse:
    """新增云服务商。"""
    cloud = await catalog_service.create_cloud(
        session, payload=payload.model_dump(), actor=auth, request=request
    )
    return catalog_service.cloud_to_response(cloud)


@router.get(
    "/clouds/{cloud_id}",
    response_model=CloudProviderResponse,
    summary="云服务商详情",
    dependencies=[require_perm(PlatformPerm.CLOUD_READ)],
)
async def get_cloud(
    session: DbSession,
    _auth: PlatformAuth,
    cloud_id: str,
) -> CloudProviderResponse:
    """云服务商详情。"""
    cloud = await catalog_service.get_cloud(session, cloud_id)
    return catalog_service.cloud_to_response(cloud)


@router.put(
    "/clouds/{cloud_id}",
    response_model=CloudProviderResponse,
    summary="更新云服务商",
    description=(
        "更新云服务商。密钥字段**留空表示保持原值**；一旦提供新密钥，"
        "接入状态会回到「未连接」并要求重新检测。"
    ),
    dependencies=[require_perm(PlatformPerm.CLOUD_WRITE)],
)
async def update_cloud(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    cloud_id: str,
    payload: CloudProviderUpdateRequest = Body(...),
) -> CloudProviderResponse:
    """更新云服务商。"""
    cloud = await catalog_service.update_cloud(
        session,
        cloud_id=cloud_id,
        payload=payload.model_dump(exclude_unset=True),
        actor=auth,
        request=request,
    )
    return catalog_service.cloud_to_response(cloud)


@router.delete(
    "/clouds/{cloud_id}",
    response_model=MessageResult,
    summary="删除云服务商",
    description="**修复 P-06**：被产品模板或客户产品引用时拒绝删除（``CASCADE_CONFLICT``）。",
    responses={409: {"description": "存在关联数据，无法删除"}},
    dependencies=[require_perm(PlatformPerm.CLOUD_WRITE)],
)
async def delete_cloud(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    cloud_id: str,
) -> MessageResult:
    """删除云服务商。"""
    await catalog_service.delete_cloud(
        session, cloud_id=cloud_id, actor=auth, request=request
    )
    return MessageResult(message="云服务商已删除")


@router.post(
    "/clouds/{cloud_id}/test",
    response_model=CloudTestResponse,
    summary="云服务商连通性检测",
    description=(
        "对厂商接口基址发起一次轻量 HTTP 探测并记录结论。\n\n"
        "**安全语义（ADR-07）**：\n"
        "- 未配置接口地址或密钥 → ``result=NOT_CONFIGURED``、``ok=false``，"
        "不发起任何真实调用，绝不伪造成功\n"
        "- 连通仅代表**网络可达**，不代表密钥有效；密钥有效性将在真正调用"
        "厂商业务接口（P4 生成设备 / P8 激活）时验证\n"
        "- 检测结论会回写到云服务商的 ``lastTestOk`` / ``lastTestMessage`` / ``status``"
    ),
    dependencies=[require_perm(PlatformPerm.CLOUD_WRITE)],
)
async def test_cloud(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    cloud_id: str,
) -> CloudTestResponse:
    """云服务商连通性检测。"""
    return await catalog_service.test_cloud_connectivity(
        session, cloud_id=cloud_id, actor=auth, request=request
    )


# ===========================================================================
# 三、产品模板与授权
# ===========================================================================


@router.get(
    "/templates",
    response_model=PageResult[TemplateResponse],
    summary="产品模板列表",
    description="分页查询产品模板（附被授权租户数与派生出的客户产品数）。",
    dependencies=[require_perm(PlatformPerm.TEMPLATE_READ)],
)
async def list_templates(
    session: DbSession,
    _auth: PlatformAuth,
    page: PageQuery,
    keyword: Annotated[str | None, Query(description="名称 / 编码 / 型号")] = None,
    category: Annotated[str | None, Query(description="品类")] = None,
    network_type: Annotated[NetworkType | None, Query(alias="networkType", description="联网方式")] = None,
    status: Annotated[EnableStatus | None, Query(description="模板状态")] = None,
) -> dict[str, Any]:
    """产品模板列表。"""
    records, total = await catalog_service.list_templates(
        session,
        offset=page.offset,
        limit=page.limit,
        sort_by=page.sort_by,
        order=page.order,
        keyword=keyword,
        category=category,
        network_type=str(network_type) if network_type else None,
        status=str(status) if status else None,
    )
    return page_response(records, total, page)


@router.post(
    "/templates",
    response_model=TemplateResponse,
    status_code=201,
    summary="新增产品模板",
    dependencies=[require_perm(PlatformPerm.TEMPLATE_WRITE)],
)
async def create_template(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    payload: TemplateCreateRequest = Body(...),
) -> TemplateResponse:
    """新增产品模板。"""
    template = await catalog_service.create_template(
        session, payload=payload.model_dump(), actor=auth, request=request
    )
    return await catalog_service.get_template_detail(session, template.id)


@router.get(
    "/templates/{template_id}",
    response_model=TemplateResponse,
    summary="产品模板详情",
    dependencies=[require_perm(PlatformPerm.TEMPLATE_READ)],
)
async def get_template(
    session: DbSession,
    _auth: PlatformAuth,
    template_id: str,
) -> TemplateResponse:
    """产品模板详情。"""
    return await catalog_service.get_template_detail(session, template_id)


@router.put(
    "/templates/{template_id}",
    response_model=TemplateResponse,
    summary="更新产品模板",
    description="更新模板。注意：模板变更**不追溯**已创建的客户产品（后者持有快照字段）。",
    dependencies=[require_perm(PlatformPerm.TEMPLATE_WRITE)],
)
async def update_template(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    template_id: str,
    payload: TemplateUpdateRequest = Body(...),
) -> TemplateResponse:
    """更新产品模板。"""
    await catalog_service.update_template(
        session,
        template_id=template_id,
        payload=payload.model_dump(exclude_unset=True),
        actor=auth,
        request=request,
    )
    return await catalog_service.get_template_detail(session, template_id)


@router.delete(
    "/templates/{template_id}",
    response_model=MessageResult,
    summary="删除产品模板",
    description="**修复 P-06**：存在租户授权或客户产品时拒绝删除（``CASCADE_CONFLICT``）。",
    responses={409: {"description": "存在关联数据，无法删除"}},
    dependencies=[require_perm(PlatformPerm.TEMPLATE_WRITE)],
)
async def delete_template(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    template_id: str,
) -> MessageResult:
    """删除产品模板。"""
    await catalog_service.delete_template(
        session, template_id=template_id, actor=auth, request=request
    )
    return MessageResult(message="产品模板已删除")


@router.post(
    "/templates/{template_id}/authorize",
    response_model=AuthorizeResult,
    summary="授权模板给租户",
    description=(
        "把模板授权给一个或多个租户。**幂等**：已授权的租户计入 ``skipped`` 而非报错，"
        "不存在的租户计入 ``denied``，便于前端给出精确反馈。"
    ),
    dependencies=[require_perm(PlatformPerm.TEMPLATE_WRITE)],
)
async def authorize_template(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    template_id: str,
    payload: AuthorizeRequest = Body(...),
) -> AuthorizeResult:
    """授权模板给租户。"""
    outcome = await catalog_service.authorize_template(
        session,
        template_id=template_id,
        tenant_ids=payload.tenant_ids,
        max_devices=payload.max_devices,
        expires_at=payload.expires_at,
        remark=payload.remark,
        actor=auth,
        request=request,
    )
    return AuthorizeResult(
        template_id=template_id,
        created=outcome.created,
        skipped=outcome.skipped,
        denied=outcome.denied,
        total_authorized=outcome.total_authorized,
    )


@router.get(
    "/templates/{template_id}/authorizations",
    response_model=PageResult[AuthorizationResponse],
    summary="模板的授权租户列表",
    dependencies=[require_perm(PlatformPerm.TEMPLATE_READ)],
)
async def list_template_authorizations(
    session: DbSession,
    auth: PlatformAuth,
    template_id: str,
    page: PageQuery,
) -> dict[str, Any]:
    """模板的授权租户列表。"""
    records, total = await catalog_service.list_authorizations(
        session,
        auth,
        template_id=template_id,
        offset=page.offset,
        limit=page.limit,
    )
    return page_response(records, total, page)


@router.delete(
    "/authorizations/{authorization_id}",
    response_model=MessageResult,
    summary="撤销授权",
    description="**修复 P-06**：该授权下已有客户产品时拒绝撤销（``CASCADE_CONFLICT``）。",
    responses={409: {"description": "存在关联产品，无法撤销"}},
    dependencies=[require_perm(PlatformPerm.TEMPLATE_WRITE)],
)
async def revoke_authorization(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    authorization_id: str,
) -> MessageResult:
    """撤销授权。"""
    await catalog_service.revoke_authorization(
        session, authorization_id=authorization_id, actor=auth, request=request
    )
    return MessageResult(message="授权已撤销")


# ===========================================================================
# 四、客户产品与小程序配置
# ===========================================================================


@router.get(
    "/client-products",
    response_model=PageResult[ClientProductResponse],
    summary="客户产品列表",
    description="分页查询客户产品（商户端「我的产品」的平台视角）。",
    dependencies=[require_perm(PlatformPerm.PRODUCT_READ)],
)
async def list_client_products(
    session: DbSession,
    auth: PlatformAuth,
    page: PageQuery,
    tenant_id: Annotated[str | None, Query(alias="tenantId", description="按租户筛选")] = None,
    template_id: Annotated[str | None, Query(alias="templateId", description="按模板筛选")] = None,
    status: Annotated[EnableStatus | None, Query(description="产品状态")] = None,
    keyword: Annotated[str | None, Query(description="名称 / 编码")] = None,
) -> dict[str, Any]:
    """客户产品列表。"""
    records, total = await catalog_service.list_client_products(
        session,
        auth,
        tenant_id=tenant_id,
        template_id=template_id,
        status=str(status) if status else None,
        keyword=keyword,
        offset=page.offset,
        limit=page.limit,
        sort_by=page.sort_by,
        order=page.order,
    )
    return page_response(records, total, page)


@router.post(
    "/client-products",
    response_model=ClientProductResponse,
    status_code=201,
    summary="创建客户产品",
    description=(
        "为租户创建客户产品（由模板派生）。\n\n"
        "**修复 P-03**：``tenantId`` 与 ``templateId`` 均为必填，且服务端会校验"
        "「该租户已获得该模板的有效授权」，否则返回 ``PRODUCT_NOT_AUTHORIZED``——"
        "因此不会再出现「新建产品未绑定客户」。\n\n"
        "联网方式、云服务商、固件版本会从模板**快照**到产品上，模板后续变更不影响已交付产品。"
    ),
    responses={
        404: {"description": "租户或模板不存在"},
        409: {"description": "产品编码已存在或该租户未获授权"},
    },
    dependencies=[require_perm(PlatformPerm.PRODUCT_WRITE)],
)
async def create_client_product(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    payload: ClientProductCreateRequest = Body(...),
) -> ClientProductResponse:
    """创建客户产品。"""
    product = await catalog_service.create_client_product(
        session, payload=payload.model_dump(), actor=auth, request=request
    )
    return await catalog_service.get_client_product_detail(session, product.id)


@router.get(
    "/client-products/{product_id}",
    response_model=ClientProductResponse,
    summary="客户产品详情",
    description="含租户 / 模板 / 云服务商名称、小程序配置（仅有掩码，无 AppSecret）。",
    dependencies=[require_perm(PlatformPerm.PRODUCT_READ)],
)
async def get_client_product(
    session: DbSession,
    _auth: PlatformAuth,
    product_id: str,
) -> ClientProductResponse:
    """客户产品详情。"""
    return await catalog_service.get_client_product_detail(session, product_id)


@router.put(
    "/client-products/{product_id}",
    response_model=ClientProductResponse,
    summary="更新客户产品",
    description="归属租户与模板不可变更（它们决定产品归属与授权依据）。",
    dependencies=[require_perm(PlatformPerm.PRODUCT_WRITE)],
)
async def update_client_product(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    product_id: str,
    payload: ClientProductUpdateRequest = Body(...),
) -> ClientProductResponse:
    """更新客户产品。"""
    await catalog_service.update_client_product(
        session,
        product_id=product_id,
        payload=payload.model_dump(exclude_unset=True),
        actor=auth,
        request=request,
    )
    return await catalog_service.get_client_product_detail(session, product_id)


@router.delete(
    "/client-products/{product_id}",
    response_model=MessageResult,
    summary="删除客户产品",
    description=(
        "删除客户产品及其小程序配置。\n\n"
        "设备关联校验将在 P4 设备表落地后接入——届时若产品下已有设备，"
        "同样返回 ``CASCADE_CONFLICT``。"
    ),
    dependencies=[require_perm(PlatformPerm.PRODUCT_WRITE)],
)
async def delete_client_product(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    product_id: str,
) -> MessageResult:
    """删除客户产品。"""
    await catalog_service.delete_client_product(
        session, product_id=product_id, actor=auth, request=request
    )
    return MessageResult(message="客户产品已删除")


@router.get(
    "/client-products/{product_id}/miniapp-config",
    response_model=MiniAppConfigResponse | None,
    summary="查询小程序配置",
    description=(
        "返回该客户产品的小程序配置；**尚未配置时返回 ``null``**"
        "（「还没有配置」是正常状态，不是错误）。响应只含 ``appSecretHint`` 掩码。"
    ),
    dependencies=[require_perm(PlatformPerm.PRODUCT_READ)],
)
async def get_miniapp_config(
    session: DbSession,
    _auth: PlatformAuth,
    product_id: str,
) -> MiniAppConfigResponse | None:
    """查询小程序配置。"""
    config = await catalog_service.get_miniapp_config(session, product_id=product_id)
    return catalog_service.miniapp_to_response(config) if config else None


@router.put(
    "/client-products/{product_id}/miniapp-config",
    response_model=MiniAppConfigResponse,
    summary="保存小程序配置",
    description=(
        "创建或更新小程序配置（upsert）。``appSecret`` 留空表示保持原值；"
        "一旦提供则加密落库并刷新掩码。"
    ),
    dependencies=[require_perm(PlatformPerm.PRODUCT_WRITE)],
)
async def save_miniapp_config(
    request: Request,
    session: DbSession,
    auth: PlatformAuth,
    product_id: str,
    payload: MiniAppConfigRequest = Body(...),
) -> MiniAppConfigResponse:
    """保存小程序配置。"""
    config = await catalog_service.upsert_miniapp_config(
        session,
        product_id=product_id,
        payload=payload.model_dump(exclude_unset=True),
        actor=auth,
        request=request,
    )
    return catalog_service.miniapp_to_response(config)


__all__ = ["router"]
