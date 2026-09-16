"""终端用户与充值域模型（P8）：`end_users` / `recharge_plans` / `recharge_orders`。

为什么这三张表由 P8 而不是 P9 落地
----------------------------------
todolist 原计划把它们放在 P9 的 `0012_ops_metrics_ota`。但 P8 的交付物
（小程序登录、4G 流量充值）**直接依赖**它们：

* `end_users` —— 小程序没有它就无法区分「谁绑了这台玩具」；P5 的
  `device_bindings.end_user_id` 一直是「只存不建外键」的松散引用
  （P5 已标注「`end_users` 属 P9」），P8 正是那个要把它接上的阶段。
* `recharge_plans` / `recharge_orders` —— 「4G 充值」是 P8 的验收项，
  没有套餐表就只能在前端硬编码假套餐，那等于伪造一个不存在的能力。

按项目既定的「迁移编号以落库先后顺延」约定，P8 占用 `0014`，
P9 顺延为 `0015_ops_metrics_ota`（只剩指标、OTA、内容库与
`end_users` 之外的表）。

终端用户为什么**不带租户**
--------------------------
`end_users` 刻意没有 `tenant_id`。一个家长可能买过两个品牌的玩具，
把用户账号绑到某一个租户上会立刻产生「同一部手机在不同品牌下是两个账号」
的荒谬结果。终端用户的可见范围由**设备**决定：小程序端的每个端点都先按
SN/设备 ID 取设备，再校验这台的 `device_bindings.end_user_id` 是不是他
（见 :mod:`app.services.miniapp_service`）。作用域收口点是**绑定关系**，
不是租户。

充值为什么按租户配套餐
----------------------
流量是**租户向运营商采购**再卖给终端用户的（不同的品牌有不同资费），
因此 `recharge_plans.tenant_id` 不可为空——平台通用套餐在这里不成立。
商户端的套餐维护归 P9，P8 只负责「读套餐 + 下单」。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UTCDateTime
from app.models.enums import EnableStatus, RechargeOrderStatus, UserStatus


class EndUser(Base, TimestampMixin):
    """终端用户（家长 / 孩子账号）。

    登录方式：手机号 + 短信验证码。**短信通道是可配置的**（``MINIAPP_SMS_PROVIDER``）：

    * ``mock`` —— 开发与验收环境不真实发短信，验证码在响应里回显并标注
      ``mock: true``；生产环境禁止该取值（启动期即拒绝），否则短信校验形同虚设。
    * ``none`` —— 发送接口 503，安全失败（ADR-07 口径）。

    验证码为什么存在**本表**而不是另开一张 ``sms_codes`` 表
    ------------------------------------------------------
    一个手机号同时只应有一个生效的验证码：新码覆盖旧码、验证成功即作废。
    把 ``code_hash`` / ``expires_at`` / ``attempts`` 放在用户行上，
    「同一手机号只有一个待验证码」这条约束就由**主键唯一性**天然保证，
    不需要靠「先删旧码再插新码」这类两步操作去维持（那中间有竞态窗口）。
    """

    __tablename__ = "end_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    #: 手机号即账号，全局唯一
    phone: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    nickname: Mapped[str | None] = mapped_column(String(64))
    avatar: Mapped[str | None] = mapped_column(String(512))

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=UserStatus.ACTIVE, index=True
    )
    #: 未消费的短信验证码：只存摘要，与刷新令牌 / 设备密钥同一口径
    login_code_hash: Mapped[str | None] = mapped_column(String(128))
    login_code_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: 当前验证码已连续输错的次数（重发会归零）
    login_code_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_login_ip: Mapped[str | None] = mapped_column(String(64))
    remark: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<EndUser {self.phone} status={self.status}>"


class RechargePlan(Base, TimestampMixin):
    """4G 流量套餐（租户自有的资费档位）。"""

    __tablename__ = "recharge_plans"
    __table_args__ = (
        # 同一租户内编码唯一：跨租户允许同名编码（不同品牌各有各的「标准包」）
        UniqueConstraint("tenant_id", "code", name="uq_recharge_plans_tenant_code"),
        # 小程序端按「租户 + 启用状态 + 排序」拉套餐列表
        Index("idx_recharge_plans_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(256))

    #: 流量（MB）。用 MB 而不是 GB 是为了避免小数（1.5GB 的存储与比较都更麻烦）
    data_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 有效期（天）
    valid_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    #: 售价（元）。**仅小程序端可见**：工厂端不下发任何金额（P6 的字段白名单）
    price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)

    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_recommended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EnableStatus.ENABLED, index=True
    )
    remark: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<RechargePlan {self.code} {self.name} ¥{self.price}>"


class RechargeOrder(Base, TimestampMixin):
    """流量充值订单。

    支付通道与短信同一口径（``PAYMENT_PROVIDER``）：``mock`` 时支付接口把订单
    直接置为已支付并在响应里标注 ``mock: true``；``none`` 时 503。
    生产环境禁止 ``mock``——否则「充值成功」只是一个字段被人为改掉。
    """

    __tablename__ = "recharge_orders"
    __table_args__ = (
        # 小程序端按「终端用户 + 创建时间」查我的充值记录
        Index("idx_recharge_orders_user_created", "end_user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)

    #: 充值归属：终端用户 + 具体设备（同一用户可能有 4G 与 Wi-Fi 两台机器）
    end_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("end_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("recharge_plans.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: 下单时对套餐的**快照**（金额与流量都要快照：套餐后续改价不应改写历史订单）
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    data_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    plan_name: Mapped[str | None] = mapped_column(String(128), doc="套餐名快照")

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RechargeOrderStatus.PENDING, index=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    failed_reason: Mapped[str | None] = mapped_column(String(512))
    refunded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    remark: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<RechargeOrder {self.order_no} status={self.status} ¥{self.amount}>"
