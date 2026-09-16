"""领域枚举。

集中定义全部状态取值，作为前后端与文档的共同词汇表。
枚举值统一使用**大写下划线**风格，与数据库中的 ``VARCHAR`` 存储值一致。

状态机权威来源
--------------
设备状态采用**四维模型**（``asset_status`` / ``activation_status`` /
``online_status`` / ``bind_status``），源自参考实现的 Schema 设计。
原型中的单一 9 态枚举降级为**前端派生展示标签**，见 :func:`derive_device_label`。
"""

from __future__ import annotations

from enum import StrEnum

# ---------------------------------------------------------------------------
# 身份与租户
# ---------------------------------------------------------------------------


class TenantStatus(StrEnum):
    """租户状态。"""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class RoleType(StrEnum):
    """角色类型（决定数据可见范围）。"""

    PLATFORM = "PLATFORM"  # 平台方，可见全部租户
    MERCHANT = "MERCHANT"  # 商户（品牌方），仅可见自身租户数据
    FACTORY = "FACTORY"  # 烧录工厂，跨租户但字段脱敏


class UserStatus(StrEnum):
    """账号状态。"""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


#: 内置角色编码 → 显示名
BUILTIN_ROLES: dict[str, tuple[str, RoleType]] = {
    "PLATFORM_ADMIN": ("平台超级管理员", RoleType.PLATFORM),
    "PLATFORM_OPERATOR": ("平台运营", RoleType.PLATFORM),
    "MERCHANT_ADMIN": ("商户管理员", RoleType.MERCHANT),
    "MERCHANT_OPERATOR": ("商户运营", RoleType.MERCHANT),
    "FACTORY_ADMIN": ("工厂管理员", RoleType.FACTORY),
    "FACTORY_OPERATOR": ("工厂操作员", RoleType.FACTORY),
}


# ---------------------------------------------------------------------------
# 网络与云服务商
# ---------------------------------------------------------------------------


class NetworkType(StrEnum):
    """设备联网方式。决定设备生成方、激活路径与二维码格式。"""

    FOUR_G = "4G"  # 集贤方案，ML307N 模组
    WIFI = "WIFI"  # 京东云 JoyInside 方案，ESP32-S3


class CloudProviderStatus(StrEnum):
    """云服务商接入状态。"""

    CONNECTED = "CONNECTED"
    NOT_CONNECTED = "NOT_CONNECTED"


class OtaSupport(StrEnum):
    """OTA 支持情况。"""

    SUPPORTED = "SUPPORTED"  # 集贤：平台可推送
    UNSUPPORTED = "UNSUPPORTED"  # 京东 JoyInside：仅端侧升级


# ---------------------------------------------------------------------------
# 目录域（云服务商 / 产品模板 / 授权 / 客户产品 / 小程序）
# ---------------------------------------------------------------------------


class CloudVendor(StrEnum):
    """云服务商厂商。

    决定设备生成方、激活路径与二维码格式（见 ``qrcode_service``，P4 落地）：

    * ``JIXIAN`` —— 集贤 4G 方案，二维码 ``JX|{SN}|{IMEI}|{ICCID}|{deviceId}``
    * ``JOYINSIDE`` —— 京东云 JoyInside Wi-Fi 方案，二维码 ``JD|{tenant}|{product}|{sn}|{sign}``
    * ``VOLCANO`` —— **火山引擎智能云 · 硬件对话智能体**（端到端实时语音对话，
      兼容乐鑫 ESP32-S3 等主流 IoT 芯片；服务端 OpenAPI 以 ``Aibot*`` 系列为主）
    """

    JIXIAN = "JIXIAN"
    JOYINSIDE = "JOYINSIDE"
    VOLCANO = "VOLCANO"
    OTHER = "OTHER"


#: 云服务商厂商 → 中文展示名
CLOUD_VENDOR_LABELS: dict[CloudVendor, str] = {
    CloudVendor.JIXIAN: "集贤（4G 设备云）",
    CloudVendor.JOYINSIDE: "京东云 JoyInside（Wi-Fi）",
    CloudVendor.VOLCANO: "火山引擎智能云（硬件对话智能体）",
    CloudVendor.OTHER: "其它厂商",
}


class TestResult(StrEnum):
    """连通性检测结果。"""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    NOT_CONFIGURED = "NOT_CONFIGURED"  # 未配置密钥，按 ADR-07 安全失败


# ---------------------------------------------------------------------------
# 订单
# ---------------------------------------------------------------------------


class OrderStatus(StrEnum):
    """订单生命周期。

    状态机::

        PENDING_AUDIT → APPROVED | REJECTED
        APPROVED      → GENERATING
        GENERATING    → GENERATED
        GENERATED     → IN_STOCK
        IN_STOCK      → PRODUCING
        PRODUCING     → SHIPPED_TO_CLIENT   (烧录数量达标)
        SHIPPED_TO_CLIENT → COMPLETED       (终端完成激活)
    """

    PENDING_AUDIT = "PENDING_AUDIT"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    GENERATING = "GENERATING"
    GENERATED = "GENERATED"
    IN_STOCK = "IN_STOCK"
    PRODUCING = "PRODUCING"
    SHIPPED_TO_CLIENT = "SHIPPED_TO_CLIENT"
    COMPLETED = "COMPLETED"


#: 订单终态（不可再迁移）
ORDER_TERMINAL_STATUSES: frozenset[OrderStatus] = frozenset(
    {OrderStatus.REJECTED, OrderStatus.COMPLETED}
)

#: 订单状态 → 允许迁移到的下一状态
ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING_AUDIT: frozenset({OrderStatus.APPROVED, OrderStatus.REJECTED}),
    OrderStatus.APPROVED: frozenset({OrderStatus.GENERATING}),
    OrderStatus.GENERATING: frozenset({OrderStatus.GENERATED, OrderStatus.APPROVED}),
    OrderStatus.GENERATED: frozenset({OrderStatus.IN_STOCK}),
    OrderStatus.IN_STOCK: frozenset({OrderStatus.PRODUCING}),
    OrderStatus.PRODUCING: frozenset({OrderStatus.SHIPPED_TO_CLIENT}),
    OrderStatus.SHIPPED_TO_CLIENT: frozenset({OrderStatus.COMPLETED}),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.COMPLETED: frozenset(),
}

#: 订单状态 → 中文展示名
ORDER_STATUS_LABELS: dict[OrderStatus, str] = {
    OrderStatus.PENDING_AUDIT: "待审核",
    OrderStatus.APPROVED: "已审核",
    OrderStatus.REJECTED: "已驳回",
    OrderStatus.GENERATING: "生成中",
    OrderStatus.GENERATED: "已生成",
    OrderStatus.IN_STOCK: "已入库",
    OrderStatus.PRODUCING: "生产中",
    OrderStatus.SHIPPED_TO_CLIENT: "已出货",
    OrderStatus.COMPLETED: "已完成",
}


# ---------------------------------------------------------------------------
# 设备：四维状态
# ---------------------------------------------------------------------------


class AssetStatus(StrEnum):
    """设备资产状态（物理流转维度）。

    ``IN_STOCK`` 与 ``FROZEN`` 之间可双向迁移，
    冻结时把原状态记入 ``devices.previous_asset_status``，解冻时恢复。
    """

    PENDING_GEN = "PENDING_GEN"  # 待生成
    GENERATED = "GENERATED"  # 已生成
    IN_STOCK = "IN_STOCK"  # 已入库待生产
    PRODUCING = "PRODUCING"  # 工厂烧录中
    PRODUCED = "PRODUCED"  # 已烧录完成
    SHIPPED = "SHIPPED"  # 已出货
    ALLOCATED = "ALLOCATED"  # 已分配给租户
    BOUND = "BOUND"  # 已被终端用户绑定
    FROZEN = "FROZEN"  # 已冻结
    RETIRED = "RETIRED"  # 已报废


class ActivationStatus(StrEnum):
    """设备激活状态。"""

    NOT_ACTIVATED = "NOT_ACTIVATED"
    ACTIVATING = "ACTIVATING"
    ACTIVATED = "ACTIVATED"
    BIND_FAILED = "BIND_FAILED"  # 激活校验失败（二维码 SN 与设备 SN 不一致）


class OnlineStatus(StrEnum):
    """设备在线状态。"""

    NEVER_ONLINE = "NEVER_ONLINE"
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"


class BindStatus(StrEnum):
    """设备绑定状态（与终端用户的绑定关系）。"""

    UNBOUND = "UNBOUND"
    BOUND = "BOUND"


#: 资产状态 → 允许迁移到的下一状态
ASSET_TRANSITIONS: dict[AssetStatus, frozenset[AssetStatus]] = {
    AssetStatus.PENDING_GEN: frozenset({AssetStatus.GENERATED}),
    AssetStatus.GENERATED: frozenset({AssetStatus.IN_STOCK, AssetStatus.FROZEN}),
    AssetStatus.IN_STOCK: frozenset(
        {AssetStatus.PRODUCING, AssetStatus.ALLOCATED, AssetStatus.FROZEN, AssetStatus.RETIRED}
    ),
    AssetStatus.PRODUCING: frozenset({AssetStatus.PRODUCED}),
    AssetStatus.PRODUCED: frozenset({AssetStatus.SHIPPED}),
    AssetStatus.SHIPPED: frozenset({AssetStatus.ALLOCATED}),
    AssetStatus.ALLOCATED: frozenset({AssetStatus.BOUND, AssetStatus.RETIRED}),
    AssetStatus.BOUND: frozenset({AssetStatus.ALLOCATED, AssetStatus.RETIRED}),
    AssetStatus.FROZEN: frozenset({AssetStatus.IN_STOCK}),
    AssetStatus.RETIRED: frozenset(),
}

#: 允许被冻结的资产状态
FREEZABLE_ASSET_STATUSES: frozenset[AssetStatus] = frozenset(
    {AssetStatus.GENERATED, AssetStatus.IN_STOCK}
)

#: 可被分配（下单/分配单）的资产状态
ALLOCATABLE_ASSET_STATUSES: frozenset[AssetStatus] = frozenset({AssetStatus.IN_STOCK})


def derive_device_label(
    asset_status: AssetStatus | str,
    activation_status: ActivationStatus | str,
    online_status: OnlineStatus | str,
    bind_status: BindStatus | str,
) -> str:
    """把四维状态合成为单一展示标签。

    这是原型中 9 态枚举在四维模型下的等价表达，仅用于**前端展示**，
    不参与任何业务判断。

    Args:
        asset_status: 资产状态。
        activation_status: 激活状态。
        online_status: 在线状态。
        bind_status: 绑定状态。

    Returns:
        中文展示标签。
    """
    asset = AssetStatus(asset_status)
    activation = ActivationStatus(activation_status)
    online = OnlineStatus(online_status)
    bind = BindStatus(bind_status)

    if asset is AssetStatus.RETIRED:
        return "已报废"
    if asset is AssetStatus.FROZEN:
        return "已冻结"
    if activation is ActivationStatus.BIND_FAILED:
        return "激活失败"
    if asset is AssetStatus.BOUND or bind is BindStatus.BOUND:
        if activation is ActivationStatus.ACTIVATED and online is OnlineStatus.ONLINE:
            return "已激活在线"
        if activation is ActivationStatus.ACTIVATED:
            return "已激活离线"
        return "已绑定"
    if asset in (AssetStatus.SHIPPED, AssetStatus.PRODUCED):
        return "待激活"
    if asset is AssetStatus.PRODUCING:
        return "生产烧录中"
    if asset is AssetStatus.ALLOCATED:
        return "已分配待激活"
    if asset is AssetStatus.IN_STOCK:
        return "已入库待生产"
    if asset is AssetStatus.GENERATED:
        return "已生成待入库"
    return "待生成"


# ---------------------------------------------------------------------------
# 批次导入
# ---------------------------------------------------------------------------


class BatchStatus(StrEnum):
    """设备批次导入状态。"""

    UPLOADED = "UPLOADED"  # 已上传，待预检
    PRE_CHECKED = "PRE_CHECKED"  # 预检完成
    IMPORTING = "IMPORTING"  # 导入中
    IMPORTED = "IMPORTED"  # 导入完成
    FAILED = "FAILED"  # 导入失败


class BatchLineStatus(StrEnum):
    """批次明细行状态。"""

    VALID = "VALID"
    INVALID = "INVALID"
    IMPORTED = "IMPORTED"
    SKIPPED = "SKIPPED"  # 重复行，已跳过
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# 分配与绑定
# ---------------------------------------------------------------------------


class AllocationStatus(StrEnum):
    """分配单状态。"""

    DRAFT = "DRAFT"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class BindingStatus(StrEnum):
    """绑定记录状态。"""

    BOUND = "BOUND"
    UNBOUND = "UNBOUND"


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


class FactoryOrderStatus(StrEnum):
    """工厂订单状态。"""

    PENDING = "PENDING"  # 待生产
    PRODUCING = "PRODUCING"  # 生产中
    COMPLETED = "COMPLETED"  # 烧录完成
    SHIPPED = "SHIPPED"  # 已出货


class InspectionResult(StrEnum):
    """抽检结果。"""

    PASS = "PASS"
    FAIL = "FAIL"


# ---------------------------------------------------------------------------
# AI 与对话
# ---------------------------------------------------------------------------


class AiProviderKind(StrEnum):
    """AI 供应商类型。"""

    DEVICE_CLOUD = "DEVICE_CLOUD"  # 设备云（集贤 / JoyInside），负责设备生成与激活
    MODEL = "MODEL"  # 大模型服务（火山 / 百度），负责对话能力
    LOCAL = "LOCAL"  # 本地离线模拟引擎


class AiProviderStatus(StrEnum):
    """AI 供应商状态。"""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class MessageRole(StrEnum):
    """对话消息角色。"""

    USER = "USER"
    ASSISTANT = "ASSISTANT"
    SYSTEM = "SYSTEM"


class MessageContentType(StrEnum):
    """消息内容类型。"""

    TEXT = "TEXT"
    AUDIO = "AUDIO"


class DialogueSessionStatus(StrEnum):
    """对话会话状态。"""

    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class ContentItemType(StrEnum):
    """内容库条目类型。"""

    SONG = "SONG"
    STORY = "STORY"


# ---- P7 追加：供应商健康与知识库文件状态 ----
#
# 为什么单独定义 `ProviderHealth` 而不复用 `AiProviderStatus`？
# 两者语义正交：`AiProviderStatus` 是**平台侧的启用/停用开关**（人为决策），
# `ProviderHealth` 是**探测出来的运行时可用性**（客观事实）。把「管理员停用」
# 和「密钥没配 / 网络不通」混成一个字段，会让运维无法区分「谁关的」。
class ProviderHealth(StrEnum):
    """供应商运行时健康状态（探测结果，非人为开关）。"""

    UP = "UP"  # 可达且已配置
    DEGRADED = "DEGRADED"  # 可达但能力受限（如仅网络通、密钥未验证）
    DOWN = "DOWN"  # 未配置密钥或不可达，调用将安全失败（ADR-07）


class VoiceTrainStatus(StrEnum):
    """自定义音色训练任务状态（对应火山 `TrainTTSVoiceType` / `BatchListVoiceTrainStatus`）。"""

    PENDING = "PENDING"
    TRAINING = "TRAINING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class KbFileStatus(StrEnum):
    """知识库文件解析状态。

    刻意把「已入库」与「已解析」分开：文件上传成功不代表能被检索命中，
    运营看板需要区分「传了但没解析成功」这类沉默故障。
    """

    PENDING = "PENDING"  # 已上传待解析
    PARSING = "PARSING"
    PARSED = "PARSED"  # 已切块入检索索引
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# 充值
# ---------------------------------------------------------------------------


class RechargeOrderStatus(StrEnum):
    """充值订单状态。"""

    PENDING = "PENDING"
    PAID = "PAID"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


# ---------------------------------------------------------------------------
# OTA
# ---------------------------------------------------------------------------


class OtaPushStatus(StrEnum):
    """OTA 推送状态。"""

    PENDING = "PENDING"
    PUSHING = "PUSHING"
    SUCCESS = "SUCCESS"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


class AuditAction(StrEnum):
    """审计动作类型。"""

    LOGIN = "LOGIN"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGOUT = "LOGOUT"
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    AUTHORIZE = "AUTHORIZE"
    AUDIT_ORDER = "AUDIT_ORDER"
    GENERATE_DEVICES = "GENERATE_DEVICES"
    FREEZE_DEVICE = "FREEZE_DEVICE"
    THAW_DEVICE = "THAW_DEVICE"
    RETIRE_DEVICE = "RETIRE_DEVICE"
    ALLOCATE_DEVICE = "ALLOCATE_DEVICE"
    BIND_DEVICE = "BIND_DEVICE"
    UNBIND_DEVICE = "UNBIND_DEVICE"
    ACTIVATE_DEVICE = "ACTIVATE_DEVICE"
    SIMULATE_HEARTBEAT = "SIMULATE_HEARTBEAT"
    BURN_REPORT = "BURN_REPORT"
    INSPECT = "INSPECT"
    OTA_PUSH = "OTA_PUSH"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    VENDOR_CALL_FAILED = "VENDOR_CALL_FAILED"


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------


class EnableStatus(StrEnum):
    """通用的启用/停用状态。"""

    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
