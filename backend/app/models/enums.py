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
    # 已分配 / 已绑定都要有一条进 FROZEN 的边，否则 FREEZABLE_ASSET_STATUSES
    # 里写着它们「可冻结」却无路可走——P6 实现时正是这么漏过一次：
    # 只补了 FROZEN 的出边（解冻能回去），忘了补入边（冻不了），
    # 由 tests/unit/test_device_state_machine.py 的对称性断言当场抓出。
    AssetStatus.ALLOCATED: frozenset(
        {AssetStatus.BOUND, AssetStatus.RETIRED, AssetStatus.FROZEN}
    ),
    AssetStatus.BOUND: frozenset(
        {AssetStatus.ALLOCATED, AssetStatus.RETIRED, AssetStatus.FROZEN}
    ),
    # ``FROZEN`` 的出边与 FREEZABLE_ASSET_STATUSES **必须严格对称**：
    # 冻结时把原状态记入 previous_asset_status，解冻时原路恢复，
    # 因此「能冻结哪些状态」与「能从 FROZEN 回到哪些状态」是同一件事。
    AssetStatus.FROZEN: frozenset(
        {AssetStatus.IN_STOCK, AssetStatus.ALLOCATED, AssetStatus.BOUND}
    ),
    AssetStatus.RETIRED: frozenset(),
}

#: 允许被冻结的资产状态
#:
#: P4 曾收紧为「只允许 ``IN_STOCK``」，理由是保持与
#: ``ASSET_TRANSITIONS[FROZEN]`` 的对称。但该收紧留下了能力缺口：
#: 绑定只接受 ``ALLOCATED``，与「只能冻结 ``IN_STOCK``」交集为空，
#: 于是**已出货给商户的设备无法被冻结**——欠费停机、内容违规停服
#: 这类真实运营动作全部缺失，绑定流程里的冻结校验也退化成
#: 「语义正确但正常流程不可达」的防御性代码。
#:
#: P6 起放宽为「库存 / 已分配 / 已绑定」三类：它们都是**已存在于现场、
#: 需要被按下暂停键**的状态，也正是冻结真正有意义的场景。
#: ``previous_asset_status`` 保证解冻能原路恢复，对称性不丢。
#:
#: 不含 ``GENERATED``：``ALLOCATABLE_ASSET_STATUSES`` 本就与它无关，
#: 「冻结」能提供的保护是零增量（不入库本身就阻断了后续流转）。
FREEZABLE_ASSET_STATUSES: frozenset[AssetStatus] = frozenset(
    {AssetStatus.IN_STOCK, AssetStatus.ALLOCATED, AssetStatus.BOUND}
)

#: 可被分配（分配单）的资产状态
#:
#: 有两条合法的进入分配链路的路径，对应两种设备来源：
#:
#: * ``IN_STOCK`` —— **平台自有库存**（``tenant_id`` 为空）：批次导入并入库的
#:   设备，或尚未派往工厂的订单设备。P5 阶段唯一的分配来源。
#: * ``SHIPPED`` —— 已经过工厂烧录、抽检并出货的设备。
#:   ``ASSET_TRANSITIONS`` 里 ``SHIPPED → ALLOCATED`` 这条边从 P4 起就存在，
#:   但当时没有工厂环节，P5 把入口限死在 ``IN_STOCK``，两者互相矛盾
#:   （迁移表允许、服务层拒绝）。P6 补齐 ``SHIPPED``，把这条边接通。
#:
#: 不含 ``PRODUCED``：设备必须先「出货登记」，否则等于允许把还在工厂车间里
#: 的设备划给客户。
ALLOCATABLE_ASSET_STATUSES: frozenset[AssetStatus] = frozenset(
    {AssetStatus.IN_STOCK, AssetStatus.SHIPPED}
)


#: 设备事件类型 → 事件所属维度（``device_events.event_type`` → ``dimension``）
#:
#: 与状态迁移表同样的理由：时间线的每条事件都要标明「哪一维变了」，
#: 散落在各调用点自行赋值，必然出现同一事件归属不同维度的情况。
DEVICE_EVENT_DIMENSIONS: dict[str, str] = {
    "GENERATED": "asset",
    "IMPORTED": "asset",
    "IN_STOCK": "asset",
    "PRODUCING": "asset",
    "PRODUCED": "asset",
    "SHIPPED": "asset",
    "ALLOCATED": "asset",
    "FROZEN": "asset",
    "THAWED": "asset",
    "RETIRED": "asset",
    "BOUND": "bind",
    "UNBOUND": "bind",
    "ACTIVATING": "activation",
    "ACTIVATED": "activation",
    "BIND_FAILED": "activation",
    "HEARTBEAT": "online",
    "ONLINE": "online",
    "OFFLINE": "online",
}


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
    """分配单状态。

    状态机::

        DRAFT     → EXECUTING
        EXECUTING → COMPLETED | FAILED
        FAILED    → EXECUTING   (允许修复后重跑，只补未成功的明细)

    ``COMPLETED`` 再次调用执行接口**不做状态迁移**，而是直接回放既有结果
    （幂等语义，与 P4 的「已入库订单再调生成接口」保持一致）。
    """

    DRAFT = "DRAFT"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AllocationItemStatus(StrEnum):
    """分配单明细行状态。

    刻意与 :class:`AllocationStatus` 分开：一张分配单里「哪几台成功、
    哪几台失败」是逐行的信息，把整体状态当成明细状态会丢失失败原因。
    """

    PENDING = "PENDING"  # 待执行
    ALLOCATED = "ALLOCATED"  # 已分配给租户
    FAILED = "FAILED"  # 分配失败（原因写在 error_message）
    SKIPPED = "SKIPPED"  # 已处于目标状态，重跑时跳过


#: 分配单状态 → 允许迁移到的下一状态
ALLOCATION_TRANSITIONS: dict[AllocationStatus, frozenset[AllocationStatus]] = {
    AllocationStatus.DRAFT: frozenset({AllocationStatus.EXECUTING}),
    AllocationStatus.EXECUTING: frozenset(
        {AllocationStatus.COMPLETED, AllocationStatus.FAILED}
    ),
    AllocationStatus.FAILED: frozenset({AllocationStatus.EXECUTING}),
    AllocationStatus.COMPLETED: frozenset(),
}

#: 分配单状态 → 中文展示名
ALLOCATION_STATUS_LABELS: dict[AllocationStatus, str] = {
    AllocationStatus.DRAFT: "草稿",
    AllocationStatus.EXECUTING: "执行中",
    AllocationStatus.COMPLETED: "已完成",
    AllocationStatus.FAILED: "执行失败",
}


class BindingStatus(StrEnum):
    """**设备维度**的绑定状态（``devices.bind_status``，ADR-03 的第四维）。

    只有「已绑定 / 未绑定」两个取值——设备自身的状态不该表达
    「正在等待某张确认令牌」这类**流程中间态**。
    """

    UNBOUND = "UNBOUND"
    BOUND = "BOUND"


class BindingRecordStatus(StrEnum):
    """**绑定记录**的生命周期状态（``device_bindings.status``）。

    为什么单独开一个枚举，而不是给 :class:`BindingStatus` 加 ``PENDING``？
    两者描述的对象不同：

    * ``BindingStatus`` 说的是**设备**（第四维状态机，只有两个取值）；
    * ``BindingRecordStatus`` 说的是**记录**，而记录确实存在
      「已扫码预检、等待确认令牌」这个合法的中间态。

    合成一个枚举会造出「设备已绑定、但记录还是 PENDING」这种
    类型上无法自洽的状态——用两个枚举把这种组合从语法上排除掉。
    """

    PENDING = "PENDING"  # 已通过 precheck 并签发确认令牌，等待 bind 确认
    BOUND = "BOUND"
    UNBOUND = "UNBOUND"


class CredentialType(StrEnum):
    """设备凭证类型（``device_credentials.credential_type``）。

    只存 **SHA-256 摘要**，明文仅在签发响应里出现一次。
    """

    DEVICE_SECRET = "DEVICE_SECRET"  # 设备侧心跳 / 上报用的密钥
    PRODUCT_SECRET = "PRODUCT_SECRET"  # 产品级密钥
    VENDOR_TOKEN = "VENDOR_TOKEN"  # 厂商侧令牌


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


class FactoryOrderStatus(StrEnum):
    """工厂订单（生产工单）状态。

    状态机::

        PENDING   → PRODUCING          (工厂首次烧录上报)
        PRODUCING → COMPLETED          (累计烧录数达标，需 ``burned_count == quantity``)
        COMPLETED → SHIPPED            (工厂出货登记)

    工厂端的动作只有两个会改状态：**烧录上报**（推进到 ``PRODUCING`` /
    ``COMPLETED``）与**出货登记**（推进到 ``SHIPPED``）。
    抽检不改工单状态——抽检是质量动作，抽检不合格应当由人决定返工或降级，
    让抽检自动把工单打回 ``PENDING`` 会掩盖真实问题。
    """

    PENDING = "PENDING"  # 待生产
    PRODUCING = "PRODUCING"  # 生产中
    COMPLETED = "COMPLETED"  # 烧录完成
    SHIPPED = "SHIPPED"  # 已出货


#: 工厂工单状态 → 允许迁移到的下一状态
FACTORY_ORDER_TRANSITIONS: dict[FactoryOrderStatus, frozenset[FactoryOrderStatus]] = {
    FactoryOrderStatus.PENDING: frozenset({FactoryOrderStatus.PRODUCING}),
    FactoryOrderStatus.PRODUCING: frozenset({FactoryOrderStatus.COMPLETED}),
    FactoryOrderStatus.COMPLETED: frozenset({FactoryOrderStatus.SHIPPED}),
    FactoryOrderStatus.SHIPPED: frozenset(),
}

#: 工厂工单终态（不可再迁移）
FACTORY_ORDER_TERMINAL_STATUSES: frozenset[FactoryOrderStatus] = frozenset(
    {FactoryOrderStatus.SHIPPED}
)

#: 工厂工单状态 → 中文展示名
FACTORY_ORDER_STATUS_LABELS: dict[FactoryOrderStatus, str] = {
    FactoryOrderStatus.PENDING: "待生产",
    FactoryOrderStatus.PRODUCING: "生产中",
    FactoryOrderStatus.COMPLETED: "烧录完成",
    FactoryOrderStatus.SHIPPED: "已出货",
}


class InspectionResult(StrEnum):
    """抽检结果。

    刻意只有「合格 / 不合格」两个取值：抽检是**判定动作**，
    更多的细分（待复检、降级使用）属于处置流程，不该塞进结果枚举里——
    否则「抽检合格率」会因为口径不一变成不可比的数字。
    """

    PASS = "PASS"
    FAIL = "FAIL"


#: 抽检结果 → 中文展示名
INSPECTION_RESULT_LABELS: dict[InspectionResult, str] = {
    InspectionResult.PASS: "合格",
    InspectionResult.FAIL: "不合格",
}


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
    DISPATCH_FACTORY_ORDER = "DISPATCH_FACTORY_ORDER"
    BURN_REPORT = "BURN_REPORT"
    INSPECT = "INSPECT"
    SHIP_FACTORY_ORDER = "SHIP_FACTORY_ORDER"
    STOCK_IN_DEVICE = "STOCK_IN_DEVICE"
    #: 终端用户登录（P8）。与后台的 ``LOGIN`` 分开：两者的审计对象与
    #: 排查路径完全不同（一个查账号权限，一个查设备归属纠纷）。
    END_USER_LOGIN = "END_USER_LOGIN"
    #: 终端用户充值下单 / 支付（P8）
    RECHARGE_ORDER = "RECHARGE_ORDER"
    #: 内容安全拦截（P8）：命中即写审计，便于运营回溯「被拦了什么」
    CONTENT_BLOCKED = "CONTENT_BLOCKED"
    OTA_PUSH = "OTA_PUSH"
    #: 重建运营快照（P9）。单独一个动作而不是复用 UPDATE：快照重建会**批量写**
    #: metrics_* 四张表，运营复盘时需要能把「数字什么时候被固化过」单独筛出来。
    REBUILD_METRICS = "REBUILD_METRICS"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    VENDOR_CALL_FAILED = "VENDOR_CALL_FAILED"


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------


class EnableStatus(StrEnum):
    """通用的启用/停用状态。"""

    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
