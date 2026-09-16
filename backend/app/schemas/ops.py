"""运营域请求 / 响应模型（P9）：运营指标 / AI 配置 / 知识库 / 内容库 / OTA。

契约约定
--------
沿用 :class:`app.schemas.catalog.ApiModel` 的 ``alias_generator=to_camel``：
Python 侧 ``snake_case``，对外 JSON ``camelCase``，请求体两种写法都接受。
本文件的字段名是**冻结契约**（前端照此对接），改名即破坏前端。

两条不可动摇的约束
------------------
1. **绝不下发密钥**：``MerchantProviderItem`` 只有 ``configured`` 布尔，
   既没有明文也没有密文，连掩码片段都没有——供应商是平台级资源，
   商户只需要知道「能不能用」。这样即使服务层写错也序列化不出密钥。
2. **温度是放大整数**：``AiConfig.temperature`` 在库里是 ×100 的整数
   （避免浮点漂移），出参在此**还原为小数**、入参由服务层 ×100。
   转换只发生在服务层与本节模型之间，前端永远看到 0.0–2.0 的小数。

为什么指标区域要用 ``date`` 而不是 ``datetime``
-----------------------------------------------
聚合口径按 **UTC 日期**（与快照表 ``metric_date`` 一致，见
:mod:`app.models.ops`）。用 ``date`` 让 Pydantic 直接解析 ``YYYY-MM-DD``，
也让「按日分组」这件事在类型上就成立——传 ``datetime`` 会诱导调用方
带着时区做比较，而快照表的语义是「哪一天」，不是「哪一刻」。
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field

from app.models.enums import ContentItemType, EnableStatus, OtaSupport
from app.schemas.catalog import ApiModel

# ---------------------------------------------------------------------------
# 一、我的产品（商户端 P9 视图）
# ---------------------------------------------------------------------------


class AiConfigSummary(ApiModel):
    """产品详情里的 AI 配置摘要（比 ``AiConfigView`` 少一半字段）。

    详情页只需要回答「这个产品的 AI 配好了吗」，把完整配置视图嵌进产品详情
    会让响应膨胀且与 ``GET /ai-config`` 重复——摘要只保留判断所需的最小集。
    """

    provider_code: str | None = None
    provider_name: str | None = None
    provider_configured: bool = False
    safety_enabled: bool = True
    role_preset_code: str | None = None
    knowledge_base_name: str | None = None
    status: str = str(EnableStatus.DISABLED)


class MerchantProductOpsResponse(ApiModel):
    """商户端「我的产品」详情（P9 版：含设备计数与 AI 摘要）。"""

    id: str
    #: 归属租户。商户端看到的永远是自己的租户，但**必须返回**：
    #: 隔离矩阵用 `tenantId` 做「列表里只有本租户记录」的通用断言，
    #: 少这个字段会让该断言退化成跳过（看起来通过、实际没验）。
    tenant_id: str
    code: str
    name: str
    network_type: str
    network_type_label: str
    template_name: str | None = None
    firmware_version: str | None = None
    status: str
    device_count: int = 0
    activated_device_count: int = 0
    total_interactions: int = 0
    ai_config_summary: AiConfigSummary = Field(default_factory=AiConfigSummary)
    remark: str | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# 二、AI 配置
# ---------------------------------------------------------------------------


class SafetySwitches(ApiModel):
    """内容安全三开关。

    三者映射到 ``AiConfig.safety_enabled`` / ``safety_sensitive_words`` /
    ``safety_llm_review``，P8 的对话链路直接读这三列，因此改完**立即生效**。
    """

    enabled: bool = True
    sensitive_words: bool = True
    llm_review: bool = False


class AiConfigView(ApiModel):
    """某个客户产品的 AI 配置视图。

    ``roleSupported`` 是**业务规则**而不是配置项的镜像：
    联网方式为 ``4G`` 时平台可覆盖对话角色（``true``）；
    为 ``WIFI`` 时角色由厂商智能体配置，平台不覆盖（``false`` 且给出原因）。
    这条规则写在 :mod:`app.services.ai_config_service` 的 docstring 里，
    GET 与 PUT role 两个入口共用同一文案，避免两处各写一句导致口径漂移。

    ``providerHealth`` 是**配置推导**的健康态（已配置密钥 = ``UP``，
    未配置 = ``DOWN``），不是主动网络探测：GET 不应该产生出站调用。
    真实探测结果由 ``/ai/health`` 系列端点（P7）负责。
    """

    product_id: str
    network_type: str
    provider_code: str | None = None
    provider_name: str | None = None
    provider_configured: bool = False
    provider_health: str | None = Field(default=None, description="UP / DOWN / null（无解析结果）")
    role_preset_code: str | None = None
    role_preset_name: str | None = None
    role_supported: bool = True
    role_unsupported_reason: str | None = None
    knowledge_base_id: str | None = None
    knowledge_base_name: str | None = None
    voice_profile_id: str | None = None
    voice_profile_name: str | None = None
    system_prompt: str | None = None
    greeting: str | None = None
    temperature: float | None = Field(default=None, description="0.0–2.0（库内为 ×100 整数）")
    max_tokens: int | None = None
    status: str = str(EnableStatus.DISABLED)
    safety: SafetySwitches = Field(default_factory=SafetySwitches)
    updated_at: datetime | None = None


class AiConfigUpdateRequest(ApiModel):
    """部分更新 AI 配置。

    **未传的字段不动，显式传 ``null`` 表示清空**——服务层用
    Pydantic v2 的 ``model_fields_set`` 区分这两种情况。用「None 即清空」
    会让「只想改问候语」的调用顺手清掉角色预设，那是静默的数据损坏。

    ``temperature`` 入参是 0.0–2.0 的小数，服务层 ×100 取整落库。
    """

    provider_code: str | None = Field(default=None, max_length=64)
    role_preset_code: str | None = Field(default=None, max_length=64)
    knowledge_base_id: str | None = Field(default=None, max_length=36)
    voice_profile_id: str | None = Field(default=None, max_length=36)
    system_prompt: str | None = Field(default=None, max_length=8000)
    greeting: str | None = Field(default=None, max_length=512)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=32768)
    status: EnableStatus | None = None


class AiPromptUpdateRequest(ApiModel):
    """只更新提示词相关字段（系统提示 / 问候语 / 采样参数）。"""

    system_prompt: str | None = Field(default=None, max_length=8000)
    greeting: str | None = Field(default=None, max_length=512)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=32768)


class AiRoleUpdateRequest(ApiModel):
    """只更新角色预设。Wi-Fi 产品会拿到 409 ``INVALID_STATE_TRANSITION``。"""

    role_preset_code: str | None = Field(default=None, max_length=64)


class AiVoiceUpdateRequest(ApiModel):
    """只更新音色档案。"""

    voice_profile_id: str | None = Field(default=None, max_length=36)


class AiSafetyUpdateRequest(ApiModel):
    """只更新内容安全三开关。"""

    enabled: bool | None = None
    sensitive_words: bool | None = None
    llm_review: bool | None = None


class MerchantProviderItem(ApiModel):
    """商户可见的供应商条目。**不含任何密钥或密钥片段。**"""

    code: str
    name: str
    kind: str
    vendor: str | None = None
    is_default: bool = False
    configured: bool = False
    status: str = "ACTIVE"


class RolePresetItem(ApiModel):
    """角色预设（平台内置 ``tenant_id IS NULL`` + 本租户）。"""

    code: str
    name: str
    persona: str | None = None
    greeting: str | None = None
    age_group: str | None = None
    tone: str | None = None
    is_builtin: bool = False


class VoiceProfileItem(ApiModel):
    """音色档案（本租户）。"""

    id: str
    name: str
    provider_code: str | None = None
    language: str | None = None
    status: str = str(EnableStatus.DISABLED)
    train_status: str | None = None


# ---------------------------------------------------------------------------
# 三、知识库
# ---------------------------------------------------------------------------


class KbCreateRequest(ApiModel):
    """创建知识库。"""

    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    client_product_id: str | None = Field(default=None, max_length=36)


class KbUpdateRequest(ApiModel):
    """更新知识库（部分更新；未传不动）。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    status: EnableStatus | None = None
    client_product_id: str | None = Field(default=None, max_length=36)


class KbFileResponse(ApiModel):
    """知识库文件。``status`` 与 ``statusLabel`` 成对给出，前端无需自建映射表。"""

    id: str
    filename: str
    content_type: str | None = None
    size_bytes: int | None = None
    status: str
    status_label: str
    chunk_count: int = 0
    error_message: str | None = None
    created_at: datetime
    parsed_at: datetime | None = None


class KbResponse(ApiModel):
    """知识库列表项 / 基本信息。"""

    id: str
    name: str
    description: str | None = None
    status: str
    doc_count: int = 0
    chunk_count: int = 0
    client_product_id: str | None = None
    file_count: int = 0
    parsed_file_count: int = 0
    updated_at: datetime


class KbDetailResponse(KbResponse):
    """知识库详情：附文件清单。"""

    files: list[KbFileResponse] = Field(default_factory=list)


class KbFileDeleteResult(ApiModel):
    """删除知识库文件的结果。

    删除是**幂等**动作：存储对象已经不存在时同样返回 ``deleted=true``——
    调用方想要的终态已经达成，让「重复删除」报错只会逼客户端写无意义的
    try/except，反而掩盖真实错误（与 :mod:`app.core.storage` 同一口径）。
    """

    deleted: bool = True


# ---------------------------------------------------------------------------
# 四、内容库（只读）
# ---------------------------------------------------------------------------


class ContentItemResponse(ApiModel):
    """内容库条目。

    ``scope`` 区分「平台公共」与「本租户自建」——两者在同一列表里混排，
    没有这个字段前端就无法解释「为什么有些条目我改不了」。
    """

    id: str
    type: str = str(ContentItemType.STORY)
    type_label: str = "故事"
    title: str
    description: str | None = None
    age_group: str | None = None
    duration_seconds: int | None = None
    status: str = str(EnableStatus.ENABLED)
    hit_count: int = 0
    scope: str = Field(default="PLATFORM", description="PLATFORM（平台公共）/ TENANT（本租户）")


# ---------------------------------------------------------------------------
# 五、运营指标
# ---------------------------------------------------------------------------


class MetricsRange(ApiModel):
    """查询区间（含首尾）。

    ``from`` 是 Python 关键字，因此字段名写成 ``from_`` 并显式声明别名，
    对外 JSON 仍是契约要求的 ``from``。
    """

    from_: date = Field(alias="from")
    to: date


class MetricsOverviewResponse(ApiModel):
    """实时聚合概览（``source: "live"``）。

    ★ 每个数字都来自对原始表（``devices`` / ``dialogue_sessions`` /
    ``dialogue_messages``）的 ``GROUP BY`` / ``COUNT``，没有任何系数还原
    （遗留缺陷 P-07 的成因就是「拿一个数乘系数」）。
    """

    source: str = "live"
    product_id: str
    product_name: str
    range: MetricsRange
    total_devices: int = 0
    activated_devices: int = 0
    active_devices: int = 0
    total_interactions: int = 0
    assistant_messages: int = 0
    session_count: int = 0
    unique_end_users: int = 0
    safety_blocked: int = 0
    content_hits: int = 0
    avg_latency_ms: float = 0.0
    avg_interactions_per_active_device: float = 0.0
    avg_messages_per_session: float = 0.0
    has_snapshot: bool = False


class TrendPoint(ApiModel):
    """日趋势的一个点（缺失日期补 0 点，图表因此不会断线）。"""

    date: date
    interactions: int = 0
    active_devices: int = 0
    new_activations: int = 0
    session_count: int = 0
    safety_blocked: int = 0
    avg_latency_ms: int = 0


class MetricsTrendResponse(ApiModel):
    """日趋势（``source: "snapshot"``，读 ``metrics_daily``）。"""

    source: str = "snapshot"
    granularity: str = "day"
    points: list[TrendPoint] = Field(default_factory=list)


class HourPoint(ApiModel):
    """小时分布的一个点。"""

    hour: int
    interactions: int = 0
    active_devices: int = 0


class MetricsHourlyResponse(ApiModel):
    """24 小时热力（``source: "snapshot"``，恒 24 项，缺失补 0）。"""

    source: str = "snapshot"
    date: date
    hours: list[HourPoint] = Field(default_factory=list)


class RegionPoint(ApiModel):
    """地域分布的一项。``region`` 为空时写「未知」——不猜。"""

    region: str
    device_count: int = 0
    interactions: int = 0


class MetricsRegionResponse(ApiModel):
    """地域分布（``source: "snapshot"``，按 deviceCount 降序）。"""

    source: str = "snapshot"
    date: date
    regions: list[RegionPoint] = Field(default_factory=list)


class ContentRankItem(ApiModel):
    """内容热度排行的一项（标题为快照值，内容改名/下架不影响历史复盘）。"""

    rank: int
    content_id: str | None = None
    title: str
    type: str
    hits: int = 0


class MetricsContentResponse(ApiModel):
    """内容热度排行（``source: "snapshot"``）。"""

    source: str = "snapshot"
    date: date
    records: list[ContentRankItem] = Field(default_factory=list)


class RetentionCohort(ApiModel):
    """一个激活日 cohort 的留存明细。``dN`` 为「激活日 + N 天当天有过对话」的台数。"""

    cohort_date: date
    activated: int = 0
    d1: int = 0
    d3: int = 0
    d7: int = 0
    d30: int = 0


class RetentionSummary(ApiModel):
    """留存汇总比率。

    ★ 分母为 0 时给 ``null`` 而不是 0：把「没有数据」读成「留存为 0」
    会让人做出完全相反的产品判断（这是运营看板最典型的误读）。
    """

    d1_rate: float | None = None
    d3_rate: float | None = None
    d7_rate: float | None = None
    d30_rate: float | None = None


class MetricsRetentionResponse(ApiModel):
    """留存（实时计算，按**设备**算；快照表没有设备级明细）。"""

    product_id: str
    days: int
    cohorts: list[RetentionCohort] = Field(default_factory=list)
    summary: RetentionSummary = Field(default_factory=RetentionSummary)
    churned_devices: int = 0
    return_rate: float | None = None
    avg_interval_days: float | None = None


class MetricsRebuildRequest(ApiModel):
    """重建运营快照（幂等 upsert）。"""

    product_id: str = Field(min_length=1)
    date_from: date
    date_to: date


class MetricsRebuildResponse(ApiModel):
    """重建结果（各表写入行数与覆盖日期）。"""

    daily_rows: int = 0
    hourly_rows: int = 0
    region_rows: int = 0
    content_rows: int = 0
    dates: list[date] = Field(default_factory=list)
    rebuilt_at: datetime


# ---------------------------------------------------------------------------
# 六、OTA（平台端）
# ---------------------------------------------------------------------------


class OtaPushStats(ApiModel):
    """某个固件包的推送统计（按设备逐条汇总）。"""

    total: int = 0
    success: int = 0
    failed: int = 0
    pending: int = 0


class OtaPackageResponse(ApiModel):
    """固件包列表项。

    ``has_file`` 为 ``false`` 表示这是一个**元数据登记**（种子里的演示包就是
    如此）。无文件的包不能真正推送——推送时逐台写 ``FAILED`` 并说明原因，
    绝不因为「界面看起来能推」就伪造成功。
    """

    id: str
    template_id: str
    template_name: str | None = None
    version: str
    release_notes: str | None = None
    file_name: str | None = None
    file_size: int | None = None
    checksum: str | None = None
    min_version: str | None = None
    is_forced: bool = False
    status: str
    status_label: str
    published_at: datetime | None = None
    has_file: bool = False
    push_stats: OtaPushStats = Field(default_factory=OtaPushStats)
    created_at: datetime


class OtaRecordResponse(ApiModel):
    """单台设备的推送记录。"""

    id: str
    package_id: str
    device_id: str
    sn: str | None = None
    status: str
    from_version: str | None = None
    to_version: str
    error_message: str | None = None
    vendor_message: str | None = None
    pushed_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


class OtaPackageDetailResponse(OtaPackageResponse):
    """固件包详情：附最近 20 条推送记录。"""

    records: list[OtaRecordResponse] = Field(default_factory=list)


class OtaPackageUpdateRequest(ApiModel):
    """更新固件包（部分更新；文件本身不可替换——换了字节就该发新版本号）。"""

    release_notes: str | None = Field(default=None, max_length=4000)
    min_version: str | None = Field(default=None, max_length=64)
    is_forced: bool | None = None
    status: EnableStatus | None = None


class OtaPushRequest(ApiModel):
    """推送固件包给某个客户产品。

    ``device_ids`` 省略时推送该产品下**全部非 RETIRED 设备**；
    给定 ID 时逐台校验归属，不属于该产品的计入 ``failed``（**不整批拒绝**）——
    整批拒绝会让「99 台正确的设备因 1 个错 ID 而全部推不了」。
    """

    client_product_id: str = Field(min_length=1)
    device_ids: list[str] | None = None


class OtaPushRecordItem(ApiModel):
    """单台设备的推送结果。

    ``status`` 取值含 ``SKIPPED``（设备已在目标版本）：该值**只出现在响应里**，
    不写入 ``ota_records``——那张表记的是「推送」，而被跳过的一台从未被推送。
    """

    device_id: str
    sn: str | None = None
    status: str
    from_version: str | None = None
    to_version: str
    error_message: str | None = None
    vendor_message: str | None = None


class OtaPushResult(ApiModel):
    """一次推送的汇总结果。"""

    package_id: str
    client_product_id: str
    ota_support: str = str(OtaSupport.SUPPORTED)
    requested: int = 0
    pushed: int = 0
    failed: int = 0
    skipped: int = 0
    records: list[OtaPushRecordItem] = Field(default_factory=list)
