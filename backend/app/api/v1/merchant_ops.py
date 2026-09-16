"""商户端 P9 API：我的产品详情 / AI 配置 / 知识库 / 内容库 / 运营指标。

为什么单独一个模块而不并入 ``app/api/v1/merchant.py``
-----------------------------------------------------
``merchant.py`` 承载 P4/P5/P8 的端点（订单 / 设备 / 绑定），**已被测试钉住**；
P9 与它们是并行开发的两条线，追加到同一文件必然冲突。因此 P9 的商户端端点
全部落在本模块，两边只在 ``router.py`` 的一行 include 上相遇。

★ 关于 ``GET /merchant/products/{product_id}`` 与 P4 的路径重合
--------------------------------------------------------------
``merchant.py`` 里已有同名端点（P4 为下单选择而做的详情），但 P9 的契约与
前端 ``pages/merchant/product_detail.js`` 都要求该路径返回**统计 + AI 摘要**
的完整形状（``deviceCount`` / ``activatedDeviceCount`` / ``totalInteractions`` /
``aiConfigSummary``）。约束是「不许改 merchant.py」，因此本模块按 include
顺序**先注册**，使 P9 的形状在该路径上生效（P4 的那条成为被遮蔽的死路由）。
既有测试在该路径上只断言「跨租户 → 404」，两种形状都满足。

路由层职责边界
--------------
只做「解析参数 → 调服务 → 转响应」：不写状态机、不写租户过滤。
租户过滤统一在服务层经 :func:`app.db.scope.scoped` 收口（ADR-08），
越权一律 404。

权限码复用（**不新增**）
------------------------
内容库读复用 ``merchant:ai-config:read``：内容库是产品/内容素材的延伸，
而 P9 不应改动既有角色权限矩阵（``app/core/permissions.py`` 有测试钉住
内置角色的权限集合）。新增权限码会让 MERCHANT_OPERATOR 之类的既有角色
突然「少了权限」或需要重新分配，收益不抵风险。
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, File, Query, Request, UploadFile

from app.core.deps import DbSession, MerchantAuth, require_perm
from app.core.pagination import PageParams, page_params, page_response
from app.core.permissions import MerchantPerm
from app.db.base import utcnow
from app.models.enums import ContentItemType, KbFileStatus
from app.schemas.common import ListResult, MessageResult, PageResult
from app.schemas.ops import (
    AiConfigUpdateRequest,
    AiConfigView,
    AiPromptUpdateRequest,
    AiRoleUpdateRequest,
    AiSafetyUpdateRequest,
    AiVoiceUpdateRequest,
    ContentItemResponse,
    KbCreateRequest,
    KbDetailResponse,
    KbFileDeleteResult,
    KbFileResponse,
    KbResponse,
    KbUpdateRequest,
    MerchantProductOpsResponse,
    MerchantProviderItem,
    MetricsContentResponse,
    MetricsHourlyResponse,
    MetricsOverviewResponse,
    MetricsRebuildRequest,
    MetricsRebuildResponse,
    MetricsRegionResponse,
    MetricsRetentionResponse,
    MetricsTrendResponse,
    RolePresetItem,
    VoiceProfileItem,
)
from app.services import ai_config_service, metrics_service

router = APIRouter(prefix="/merchant", tags=["商户端 · AI 配置与运营"])

PageQuery = Annotated[PageParams, Depends(page_params)]


# ===========================================================================
# 一、我的产品
# ===========================================================================


@router.get(
    "/products",
    response_model=PageResult[MerchantProductOpsResponse],
    summary="我的产品列表（P9：每行含设备计数与 AI 摘要）",
    description=(
        "分页查询**本租户**的客户产品；每行都带设备数 / 已激活数 / 累计交互与 AI 配置摘要。\n\n"
        "为什么列表也带计数：这是商户最常打开的一页，只给基础字段会让前端逐行回查详情"
        "（20 行 = 21 个请求）。计数在本端点内以批量 `GROUP BY` 完成。\n\n"
        "同一路径在 P4 曾由 ``merchant.py`` 提供一版**仅基础字段**的列表（供下单下拉用）；"
        "P9 起收敛到本端点——**同一路径只能有一个处理函数**，否则行为取决于路由注册顺序，"
        "是一类很难排查的隐性缺陷。"
    ),
    dependencies=[require_perm(MerchantPerm.PRODUCT_READ)],
)
async def list_my_products_ops(
    session: DbSession,
    auth: MerchantAuth,
    page: PageQuery,
    keyword: Annotated[str | None, Query(description="名称 / 编码关键字")] = None,
) -> dict[str, Any]:
    """我的产品列表（P9）。"""
    records, total = await ai_config_service.list_products_ops(
        session,
        auth,
        offset=page.offset,
        limit=page.limit,
        keyword=keyword,
    )
    return page_response(records, total, page)


@router.get(
    "/products/{product_id}",
    response_model=MerchantProductOpsResponse,
    summary="我的产品详情（P9：含设备计数与 AI 摘要）",
    description=(
        "本租户客户产品详情：设备数 / 已激活数 / 累计交互 + AI 配置摘要。\n\n"
        "计数一律走真实 `COUNT`（P-07 口径），不做系数还原。"
        "访问其他租户的产品返回 **404**（不返回 403）——"
        "避免通过错误码探测他人资源是否存在。"
    ),
    responses={404: {"description": "产品不存在或不属于本租户"}},
    dependencies=[require_perm(MerchantPerm.PRODUCT_READ)],
)
async def get_my_product_ops(
    session: DbSession,
    auth: MerchantAuth,
    product_id: str,
) -> MerchantProductOpsResponse:
    """我的产品详情（P9）。"""
    return await ai_config_service.build_product_ops_detail(session, auth, product_id)


# ===========================================================================
# 二、AI 配置
# ===========================================================================


@router.get(
    "/products/{product_id}/ai-config",
    response_model=AiConfigView,
    summary="读取 AI 配置",
    description=(
        "返回该客户产品的 AI 配置视图。**产品还没配过时返回默认视图**"
        "（`status=DISABLED`、安全开关取列默认值），而不是 404——"
        "否则配置页打不开，商户找不到入口去配。\n\n"
        "`roleSupported` 是业务规则：`4G` 为 `true`；`WIFI` 为 `false` 并给出"
        "`roleUnsupportedReason`（角色由厂商智能体配置，平台不覆盖）。\n\n"
        "`providerHealth` 是**配置推导**的健康态（已配置密钥 = `UP`，未配置 = `DOWN`），"
        "不是主动网络探测——GET 不应产生出站调用。\n\n"
        "`temperature` 出参为小数（0.0–2.0）；库内存的是 ×100 的整数。"
        "响应**不含任何密钥或密钥片段**。"
    ),
    dependencies=[require_perm(MerchantPerm.AI_CONFIG_READ)],
)
async def get_ai_config(
    session: DbSession,
    auth: MerchantAuth,
    product_id: str,
) -> AiConfigView:
    """读取 AI 配置。"""
    return await ai_config_service.get_ai_config_view(session, auth, product_id)


@router.put(
    "/products/{product_id}/ai-config",
    response_model=AiConfigView,
    summary="更新 AI 配置（部分更新）",
    description=(
        "**部分更新**：未传的字段不动，显式传 `null` 表示清空。\n\n"
        "校验：\n"
        "- `providerCode` 必须是已注册且**已配置密钥**的供应商，否则 409 "
        "`PRODUCT_NOT_AUTHORIZED`（文案说明「选定后对话会安全失败」）\n"
        "- `knowledgeBaseId` / `voiceProfileId` 必须存在且属于本租户\n"
        "- `status=ENABLED` 时若解析不到可用供应商 → 409 并说明原因\n"
        "- `rolePresetCode` 在 Wi-Fi 产品上 → 409 `INVALID_STATE_TRANSITION`"
        "（与专用 role 端点同一判据、同一文案）\n\n"
        "`temperature` 入参为小数 0.0–2.0，服务层 ×100 取整落库。"
    ),
    responses={
        404: {"description": "产品 / 知识库 / 音色不存在或不属于本租户"},
        409: {"description": "供应商未配置密钥、Wi-Fi 产品写角色、启用时无可用供应商"},
    },
    dependencies=[require_perm(MerchantPerm.AI_CONFIG_WRITE)],
)
async def update_ai_config(
    request: Request,
    session: DbSession,
    auth: MerchantAuth,
    product_id: str,
    payload: AiConfigUpdateRequest = Body(...),
) -> AiConfigView:
    """更新 AI 配置。"""
    return await ai_config_service.update_ai_config(
        session, auth, product_id, payload, request=request
    )


@router.put(
    "/products/{product_id}/ai-config/prompt",
    response_model=AiConfigView,
    summary="更新提示词与采样参数",
    description="只更新 `systemPrompt` / `greeting` / `temperature` / `maxTokens`（部分更新）。",
    dependencies=[require_perm(MerchantPerm.AI_CONFIG_WRITE)],
)
async def update_ai_prompt(
    request: Request,
    session: DbSession,
    auth: MerchantAuth,
    product_id: str,
    payload: AiPromptUpdateRequest = Body(...),
) -> AiConfigView:
    """更新提示词与采样参数。"""
    return await ai_config_service.update_prompt(
        session, auth, product_id, payload, request=request
    )


@router.put(
    "/products/{product_id}/ai-config/role",
    response_model=AiConfigView,
    summary="更新对话角色",
    description=(
        "只更新 `rolePresetCode`。**Wi-Fi 产品 → 409 `INVALID_STATE_TRANSITION`**，"
        "`details` 带 `{networkType: \"WIFI\", reason: \"...\"}`（与 GET 的 "
        "`roleUnsupportedReason` 同一文案）。显式传 `null` 清空在 Wi-Fi 上允许。"
    ),
    responses={409: {"description": "Wi-Fi 方案的对话角色由厂商智能体配置"}},
    dependencies=[require_perm(MerchantPerm.AI_CONFIG_WRITE)],
)
async def update_ai_role(
    request: Request,
    session: DbSession,
    auth: MerchantAuth,
    product_id: str,
    payload: AiRoleUpdateRequest = Body(...),
) -> AiConfigView:
    """更新对话角色。"""
    return await ai_config_service.update_role(session, auth, product_id, payload, request=request)


@router.put(
    "/products/{product_id}/ai-config/voice",
    response_model=AiConfigView,
    summary="更新音色",
    description="只更新 `voiceProfileId`（部分更新；传 `null` 清空）。",
    dependencies=[require_perm(MerchantPerm.AI_CONFIG_WRITE)],
)
async def update_ai_voice(
    request: Request,
    session: DbSession,
    auth: MerchantAuth,
    product_id: str,
    payload: AiVoiceUpdateRequest = Body(...),
) -> AiConfigView:
    """更新音色。"""
    return await ai_config_service.update_voice(session, auth, product_id, payload, request=request)


@router.put(
    "/products/{product_id}/ai-config/safety",
    response_model=AiConfigView,
    summary="更新内容安全开关",
    description=(
        "只更新 `enabled` / `sensitiveWords` / `llmReview` 三个开关，"
        "映射到 `AiConfig` 的三列。P8 的对话链路在 `assistant.delta` 之前读它们，"
        "因此**下一次对话即生效**。"
    ),
    dependencies=[require_perm(MerchantPerm.AI_CONFIG_WRITE)],
)
async def update_ai_safety(
    request: Request,
    session: DbSession,
    auth: MerchantAuth,
    product_id: str,
    payload: AiSafetyUpdateRequest = Body(...),
) -> AiConfigView:
    """更新内容安全开关。"""
    return await ai_config_service.update_safety(session, auth, product_id, payload, request=request)


@router.get(
    "/ai/providers",
    response_model=ListResult[MerchantProviderItem],
    summary="可用供应商清单",
    description=(
        "已注册供应商清单。**绝不含 apiKey / secretKey 或其片段**——"
        "只给「是否已配置」的布尔 `configured`。密钥是平台级凭据，"
        "商户既不需要也不应拿到。"
    ),
    dependencies=[require_perm(MerchantPerm.AI_CONFIG_READ)],
)
async def list_ai_providers(session: DbSession, auth: MerchantAuth) -> dict[str, Any]:
    """可用供应商清单。"""
    records = await ai_config_service.list_providers(session)
    return {"records": records, "total": len(records)}


@router.get(
    "/role-presets",
    response_model=ListResult[RolePresetItem],
    summary="角色预设清单（平台内置 + 本租户）",
    description="平台内置角色（`tenantId` 为空）与本租户自建角色，只列启用中的。",
    dependencies=[require_perm(MerchantPerm.AI_CONFIG_READ)],
)
async def list_role_presets(session: DbSession, auth: MerchantAuth) -> dict[str, Any]:
    """角色预设清单。"""
    records = await ai_config_service.list_role_presets(session, auth)
    return {"records": records, "total": len(records)}


@router.get(
    "/voice-profiles",
    response_model=ListResult[VoiceProfileItem],
    summary="音色清单（本租户）",
    description="本租户的音色档案，含训练状态 `trainStatus`。",
    dependencies=[require_perm(MerchantPerm.AI_CONFIG_READ)],
)
async def list_voice_profiles(session: DbSession, auth: MerchantAuth) -> dict[str, Any]:
    """音色清单。"""
    records = await ai_config_service.list_voice_profiles(session, auth)
    return {"records": records, "total": len(records)}


# ===========================================================================
# 三、知识库
# ===========================================================================


@router.get(
    "/knowledge-bases",
    response_model=PageResult[KbResponse],
    summary="知识库列表",
    description="分页查询本租户的知识库（租户过滤在服务层经 `scoped()` 收口）。",
    dependencies=[require_perm(MerchantPerm.KB_READ)],
)
async def list_knowledge_bases(
    session: DbSession,
    auth: MerchantAuth,
    page: PageQuery,
    keyword: Annotated[str | None, Query(description="名称关键字")] = None,
) -> dict[str, Any]:
    """知识库列表。"""
    records, total = await ai_config_service.list_knowledge_bases(
        session, auth, keyword=keyword, offset=page.offset, limit=page.limit
    )
    return page_response(records, total, page)


@router.post(
    "/knowledge-bases",
    response_model=KbResponse,
    status_code=201,
    summary="创建知识库",
    description="创建本租户的知识库，可选关联一个本租户的客户产品（关联越权 → 404）。",
    dependencies=[require_perm(MerchantPerm.KB_WRITE)],
)
async def create_knowledge_base(
    request: Request,
    session: DbSession,
    auth: MerchantAuth,
    payload: KbCreateRequest = Body(...),
) -> KbResponse:
    """创建知识库。"""
    return await ai_config_service.create_knowledge_base(
        session,
        auth,
        name=payload.name,
        description=payload.description,
        client_product_id=payload.client_product_id,
        request=request,
    )


@router.get(
    "/knowledge-bases/{kb_id}",
    response_model=KbDetailResponse,
    summary="知识库详情",
    description="含文件清单（按上传时间倒序）。访问其他租户的知识库返回 404。",
    responses={404: {"description": "知识库不存在或不属于本租户"}},
    dependencies=[require_perm(MerchantPerm.KB_READ)],
)
async def get_knowledge_base(
    session: DbSession,
    auth: MerchantAuth,
    kb_id: str,
) -> KbDetailResponse:
    """知识库详情。"""
    return await ai_config_service.get_knowledge_base_detail(session, auth, kb_id)


@router.put(
    "/knowledge-bases/{kb_id}",
    response_model=KbResponse,
    summary="更新知识库",
    description="部分更新；`clientProductId` 显式传 `null` 表示解除产品关联。",
    dependencies=[require_perm(MerchantPerm.KB_WRITE)],
)
async def update_knowledge_base(
    request: Request,
    session: DbSession,
    auth: MerchantAuth,
    kb_id: str,
    payload: KbUpdateRequest = Body(...),
) -> KbResponse:
    """更新知识库。"""
    return await ai_config_service.update_knowledge_base(
        session,
        auth,
        kb_id,
        name=payload.name,
        description=payload.description,
        status=str(payload.status) if payload.status is not None else None,
        client_product_id=payload.client_product_id,
        fields_set=set(payload.model_fields_set),
        request=request,
    )


@router.delete(
    "/knowledge-bases/{kb_id}",
    response_model=MessageResult,
    summary="删除知识库",
    description=(
        "**被任何 `aiConfigs` 引用时 → 409 `CASCADE_CONFLICT`**，"
        "`details` 带 `{products: [{id, name}]}`；否则级联删除文件与存储对象。"
    ),
    responses={409: {"description": "仍被 AI 配置引用"}},
    dependencies=[require_perm(MerchantPerm.KB_WRITE)],
)
async def delete_knowledge_base(
    request: Request,
    session: DbSession,
    auth: MerchantAuth,
    kb_id: str,
) -> MessageResult:
    """删除知识库。"""
    await ai_config_service.delete_knowledge_base(session, auth, kb_id, request=request)
    return MessageResult(message="知识库已删除")


@router.get(
    "/knowledge-bases/{kb_id}/files",
    response_model=PageResult[KbFileResponse],
    summary="知识库文件列表",
    description="分页查询文件，可按解析状态 `status` 筛选。",
    dependencies=[require_perm(MerchantPerm.KB_READ)],
)
async def list_kb_files(
    session: DbSession,
    auth: MerchantAuth,
    kb_id: str,
    page: PageQuery,
    status: Annotated[KbFileStatus | None, Query(description="解析状态")] = None,
) -> dict[str, Any]:
    """知识库文件列表。"""
    records, total = await ai_config_service.list_kb_files(
        session,
        auth,
        kb_id,
        status=str(status) if status else None,
        offset=page.offset,
        limit=page.limit,
    )
    return page_response(records, total, page)


@router.post(
    "/knowledge-bases/{kb_id}/files",
    response_model=KbFileResponse,
    status_code=201,
    summary="上传知识库文件",
    description=(
        "`multipart/form-data`，字段名 `file`。扩展名白名单由 "
        "`KB_ALLOWED_EXTENSIONS` 决定，大小上限 `KB_FILE_MAX_BYTES`。\n\n"
        "**同一知识库内内容完全相同的文件 → 409 `VALIDATION_ERROR`**，"
        "`details.existingFileId` 指向既有文件（不静默复用：响应契约里没有"
        "「这是既有文件」的字段，静默复用会让前端分不清「刚传成功」与「早就有」）。\n\n"
        "落盘 key 由服务端生成（`kb/{租户}/{文件ID}{扩展名}`）；"
        "原始文件名只入库展示，不进路径。上传后 `status=PENDING`，需再调解析接口。"
    ),
    responses={400: {"description": "扩展名不允许或超过大小上限"}, 409: {"description": "文件已存在"}},
    dependencies=[require_perm(MerchantPerm.KB_WRITE)],
)
async def upload_kb_file(
    request: Request,
    session: DbSession,
    auth: MerchantAuth,
    kb_id: str,
    file: Annotated[UploadFile, File(description="知识库文件")],
) -> KbFileResponse:
    """上传知识库文件。"""
    content = await file.read()
    return await ai_config_service.upload_kb_file(
        session,
        auth,
        kb_id,
        filename=file.filename or "upload.txt",
        content_type=file.content_type,
        content=content,
        request=request,
    )


@router.delete(
    "/knowledge-bases/{kb_id}/files/{file_id}",
    response_model=KbFileDeleteResult,
    summary="删除知识库文件",
    description="删除库记录与存储对象；对象不存在也返回成功（**幂等**）。",
    dependencies=[require_perm(MerchantPerm.KB_WRITE)],
)
async def delete_kb_file(
    request: Request,
    session: DbSession,
    auth: MerchantAuth,
    kb_id: str,
    file_id: str,
) -> KbFileDeleteResult:
    """删除知识库文件。"""
    await ai_config_service.delete_kb_file(
        session, auth, kb_id, file_id, request=request
    )
    return KbFileDeleteResult(deleted=True)


@router.post(
    "/knowledge-bases/{kb_id}/files/{file_id}/parse",
    response_model=KbFileResponse,
    summary="解析知识库文件",
    description=(
        "**诚实实现**：`txt/md/csv/json` 从存储读文本、按空行/段落切块、"
        "写回 `chunkCount`、`status=PARSED`；`pdf/docx` 本阶段无解析器 → "
        "`status=FAILED` + 说明「需要 PDF/DOCX 解析器」，**绝不假装解析成功**。"
    ),
    dependencies=[require_perm(MerchantPerm.KB_WRITE)],
)
async def parse_kb_file(
    request: Request,
    session: DbSession,
    auth: MerchantAuth,
    kb_id: str,
    file_id: str,
) -> KbFileResponse:
    """解析知识库文件。"""
    return await ai_config_service.parse_kb_file(
        session, auth, kb_id, file_id, request=request
    )


# ===========================================================================
# 四、内容库（只读）
# ===========================================================================


@router.get(
    "/content-items",
    response_model=PageResult[ContentItemResponse],
    summary="内容库列表（平台公共 + 本租户，只读）",
    description=(
        "范围 = **平台公共内容**（`tenantId` 为空）+ **本租户自建内容**；"
        "`scope` 字段标明来源。\n\n"
        "**为什么只读**：内容库的运营维护涉及内容审核流程，不在 P9 范围；"
        "P9 的验收项是「内容热度榜」（读）。只读端点避免了先造一个能绕过"
        "审核的写入口。\n\n"
        "权限复用 `merchant:ai-config:read`：内容库是产品/内容素材的延伸，"
        "新增权限码会改动既有角色权限矩阵（有测试钉住）。"
    ),
    dependencies=[require_perm(MerchantPerm.AI_CONFIG_READ)],
)
async def list_my_content_items(
    session: DbSession,
    auth: MerchantAuth,
    page: PageQuery,
    item_type: Annotated[ContentItemType | None, Query(alias="type", description="内容类型")] = None,
    keyword: Annotated[str | None, Query(description="标题 / 描述关键字")] = None,
) -> dict[str, Any]:
    """内容库列表。"""
    records, total = await ai_config_service.list_content_items(
        session,
        auth,
        item_type=str(item_type) if item_type else None,
        keyword=keyword,
        offset=page.offset,
        limit=page.limit,
    )
    return page_response(records, total, page)


# ===========================================================================
# 五、运营指标
# ===========================================================================


def _resolve_metric_date(value: date | None) -> date:
    """未指定日期时取今天（UTC）——与快照口径一致。"""
    return value or utcnow().date()


@router.get(
    "/metrics/overview",
    response_model=MetricsOverviewResponse,
    summary="运营概览（实时聚合）",
    description=(
        "`source: \"live\"`。**每个数字都来自对原始表的 `GROUP BY` / `COUNT`**"
        "（`devices` / `dialogue_sessions` / `dialogue_messages`），"
        "禁止「拿一个数乘系数」这类写法（遗留缺陷 P-07 的成因）。\n\n"
        "`from` / `to` 默认最近 7 天（含今天）；聚合按 **UTC 日期**。\n"
        "`totalDevices` 不含已报废（`RETIRED`）设备；`totalInteractions` "
        "以 **USER 消息**计数（一问一答算一次；助手消息可能被安全整段替换）。\n"
        "`hasSnapshot` 表示该区间是否已有快照，供前端决定是否提示「去刷新快照」。"
    ),
    dependencies=[require_perm(MerchantPerm.METRICS_READ)],
)
async def metrics_overview(
    session: DbSession,
    auth: MerchantAuth,
    product_id: Annotated[str, Query(alias="productId", description="客户产品 ID")],
    date_from: Annotated[date | None, Query(alias="from", description="起始日期")] = None,
    date_to: Annotated[date | None, Query(alias="to", description="结束日期")] = None,
) -> MetricsOverviewResponse:
    """运营概览（实时聚合）。"""
    return await metrics_service.build_overview(
        session, auth, product_id, date_from=date_from, date_to=date_to
    )


@router.get(
    "/metrics/trend",
    response_model=MetricsTrendResponse,
    summary="日趋势（读快照）",
    description=(
        "`source: \"snapshot\"`，读 `metrics_daily`。按日期升序，**缺日补 0 点**"
        "（图表因此不会断线）。`granularity` 本阶段仅支持 `day`。"
    ),
    dependencies=[require_perm(MerchantPerm.METRICS_READ)],
)
async def metrics_trend(
    session: DbSession,
    auth: MerchantAuth,
    product_id: Annotated[str, Query(alias="productId")],
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    granularity: Annotated[str, Query(description="粒度，仅支持 day")] = "day",
) -> MetricsTrendResponse:
    """日趋势。"""
    return await metrics_service.build_trend(
        session,
        auth,
        product_id,
        date_from=date_from,
        date_to=date_to,
        granularity=granularity,
    )


@router.get(
    "/metrics/hourly",
    response_model=MetricsHourlyResponse,
    summary="24 小时热力（读快照）",
    description=(
        "`source: \"snapshot\"`，读 `metrics_hourly`，**恒 24 项**（缺失小时补 0）。"
        "`date` 省略时取今天（UTC）。"
    ),
    dependencies=[require_perm(MerchantPerm.METRICS_READ)],
)
async def metrics_hourly(
    session: DbSession,
    auth: MerchantAuth,
    product_id: Annotated[str, Query(alias="productId")],
    metric_date: Annotated[date | None, Query(alias="date", description="快照日期")] = None,
) -> MetricsHourlyResponse:
    """24 小时热力。"""
    return await metrics_service.build_hourly(
        session, auth, product_id, metric_date=_resolve_metric_date(metric_date)
    )


@router.get(
    "/metrics/regions",
    response_model=MetricsRegionResponse,
    summary="地域分布（读快照）",
    description=(
        "`source: \"snapshot\"`，读 `metrics_region`，按 `deviceCount` 降序。"
        "地域来自 `devices.region`，为空时归入「未知」（不猜）。"
    ),
    dependencies=[require_perm(MerchantPerm.METRICS_READ)],
)
async def metrics_regions(
    session: DbSession,
    auth: MerchantAuth,
    product_id: Annotated[str, Query(alias="productId")],
    metric_date: Annotated[date | None, Query(alias="date")] = None,
) -> MetricsRegionResponse:
    """地域分布。"""
    return await metrics_service.build_regions(
        session, auth, product_id, metric_date=_resolve_metric_date(metric_date)
    )


@router.get(
    "/metrics/contents",
    response_model=MetricsContentResponse,
    summary="内容热度榜（读快照）",
    description=(
        "`source: \"snapshot\"`，读 `content_hot_ranking`。标题为**快照值**："
        "内容改名或下架后，历史排行仍显示当时的名字。"
    ),
    dependencies=[require_perm(MerchantPerm.METRICS_READ)],
)
async def metrics_contents(
    session: DbSession,
    auth: MerchantAuth,
    product_id: Annotated[str, Query(alias="productId")],
    metric_date: Annotated[date | None, Query(alias="date")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="返回条数")] = 10,
) -> MetricsContentResponse:
    """内容热度榜。"""
    return await metrics_service.build_contents(
        session,
        auth,
        product_id,
        metric_date=_resolve_metric_date(metric_date),
        limit=limit,
    )


@router.get(
    "/metrics/retention",
    response_model=MetricsRetentionResponse,
    summary="留存（实时计算，按设备）",
    description=(
        "留存必须按**设备**算，快照表没有设备级明细，因此这里实时计算。\n\n"
        "口径：按 `devices.activatedAt` 的日期分 cohort；`dN` = 该 cohort 中的设备在"
        "「激活日 + N 天」当天有过对话的台数；`Rate = dN / activated × 100`，"
        "**分母为 0 时给 `null` 而不是 0**（否则「没数据」会被读成「留存为 0」）。\n\n"
        "`churnedDevices` = 激活后 7 天内零会话的设备数；`returnRate` = 有过 ≥2 个"
        "不同活跃日的设备 / 有过 ≥1 个活跃日的设备；`avgIntervalDays` = 相邻活跃日"
        "间隔的均值（不足 2 个活跃日不计入）。"
    ),
    dependencies=[require_perm(MerchantPerm.METRICS_READ)],
)
async def metrics_retention(
    session: DbSession,
    auth: MerchantAuth,
    product_id: Annotated[str, Query(alias="productId")],
    days: Annotated[int, Query(ge=1, le=365, description="cohort 观察天数")] = 30,
) -> MetricsRetentionResponse:
    """留存（实时计算）。"""
    return await metrics_service.build_retention(session, auth, product_id, days=days)


@router.post(
    "/metrics/rebuild",
    response_model=MetricsRebuildResponse,
    summary="重建运营快照",
    description=(
        "**幂等 upsert**（按各表唯一键：有则更新、无则插入），跨度上限 92 天。\n\n"
        "`contentHotRanking` 从 `dialogue_messages.contentItemId` 真实 `GROUP BY`"
        "统计（只统计非空归属），并写入标题快照；`rank` 按 hits 降序编号。\n\n"
        "写入 `AuditAction.REBUILD_METRICS`（`resourceType=metrics`）。\n"
        "`dateTo` **允许**是今天：商户端的刷新按钮常常就是刷「含今天」，强行禁止"
        "会让界面出现「刷新到今天却报错」；但今天的快照会随时间变化，"
        "前端应提示「今日数据仍在累积」。"
    ),
    responses={400: {"description": "日期区间非法或跨度超过 92 天"}},
    dependencies=[require_perm(MerchantPerm.METRICS_READ)],
)
async def rebuild_metrics(
    request: Request,
    session: DbSession,
    auth: MerchantAuth,
    payload: MetricsRebuildRequest = Body(...),
) -> MetricsRebuildResponse:
    """重建运营快照。"""
    return await metrics_service.rebuild_metrics(
        session,
        auth,
        product_id=payload.product_id,
        date_from=payload.date_from,
        date_to=payload.date_to,
        request=request,
    )


__all__ = ["router"]
