"""终端用户小程序服务（P8）：登录 / 扫码 / 激活绑定 / 设备 / 设置 / 充值。

作用域收口点是**绑定关系**，不是租户
====================================
管理端（商户 / 工厂 / 平台）的可见范围建立在「角色 + 租户」上，因此收口在
:func:`app.db.scope.scoped` 与 :class:`app.core.deps.AuthContext`。
终端用户**完全不同**：

* ``end_users`` 刻意没有 ``tenant_id``（见 :mod:`app.models.miniapp`）——
  一个家长可能买过两个品牌的玩具，把账号绑到某个租户上会立刻产生
  「同一部手机在不同品牌下是两个账号」的荒谬结果；
* 因此终端用户的一切查询都**不能**走 :func:`app.db.scope.scoped`
  （它要求 ``AuthContext`` 并强制 ``tenant_id`` 条件），
  只能以 :func:`assert_device_owned` 为唯一入口：
  「这台设备的 ``device_bindings.end_user_id`` 是不是我」。

为什么越权返回 404 而不是 403
-----------------------------
与 :func:`app.db.scope.assert_visible` 同一口径：如果能通过错误码区分
「设备不存在」与「设备是别人的」，攻击者就能用错误码差异把设备 ID 枚举出来，
从而知道「这台机器是否真实存在」。小程序端尤其敏感——设备 ID 与家庭绑定，
泄漏存在性等于泄漏「这个 SN 已被激活」。因此**统一返回
``DEVICE_NOT_FOUND``（404）**，不泄漏存在性。

激活为什么可以「先于绑定」通过校验
----------------------------------
``/miniapp/devices/{id}/activate-*`` 是唯一在**绑定之前**就要校验归属的端点。
这个阶段的授权依据不是绑定关系（那时还没有），而是
「设备处于**可被认领**状态」（``ALLOCATED``，未冻结未报废）——
这也正是扫码页的业务语义：用户手里拿着这台机器，扫码 → 立即激活。
因此 :func:`assert_device_owned` 提供 ``allow_activatable`` 开关，
**只有这两个端点**传 ``True``；它绝不会放行「已被他人绑定」的设备。

任务分工
--------
* 本模块承担全部业务编排（校验顺序、状态迁移、审计、事务提交）；
* :mod:`app.services.dialogue_service` 负责对话编排，复用本模块的
  设备归属校验与厂商解析；
* :mod:`app.api.v1.miniapp` 只做「解析参数 → 调服务 → 转响应」。
"""

from __future__ import annotations

import hmac
import secrets
from datetime import datetime, timedelta
from typing import Any

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import registry
from app.ai.base import CAP_DEVICE, provider_unavailable_error
from app.ai.registry import ResolvedProvider
from app.core.config import settings
from app.core.deps import EndUserContext
from app.core.errors import (
    AppException,
    ErrorCode,
    account_disabled,
    bind_failed,
    device_already_bound,
    device_not_available,
    device_not_found,
    invalid_state_transition,
    not_found,
    product_not_authorized,
    qr_expired,
    unauthenticated,
    validation_error,
    vendor_unavailable,
)
from app.core.ids import new_id
from app.core.logging import get_logger
from app.core.security import hash_token, issue_end_user_token
from app.db.base import utcnow
from app.models.allocation import DeviceBinding
from app.models.catalog import ClientProduct, CloudProvider, ProductTemplate
from app.models.device import Device
from app.models.enums import (
    CLOUD_VENDOR_LABELS,
    ActivationStatus,
    AiProviderKind,
    AssetStatus,
    AuditAction,
    BindingRecordStatus,
    BindStatus,
    CloudVendor,
    EnableStatus,
    NetworkType,
    RechargeOrderStatus,
    UserStatus,
)
from app.models.identity import Tenant
from app.models.miniapp import EndUser, RechargeOrder, RechargePlan
from app.schemas.miniapp import (
    LOGIN_CODE_LENGTH,
    ActivationResult,
    AuthCodeResponse,
    AuthLoginResponse,
    DeviceSettingsResponse,
    EndUserBrief,
    MiniappDeviceBrief,
    MiniappDeviceDetail,
    MiniappProfileResponse,
    RechargeOrderResponse,
    RechargePayResult,
    RechargePlanListResult,
    RechargePlanResponse,
    ScanDeviceInfo,
    ScanProductInfo,
    ScanResolveResponse,
    UnbindResult,
)
from app.services import (
    allocation_service,
    audit_service,
    device_service,
    qrcode_service,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 设备设置的**键白名单**。只有这三个键会被写入 ``devices.settings``，
#: 其余键一律静默丢弃（不是报错：客户端版本可能比服务端新，
#: 为一个它多传的字段让整个保存失败，对用户体验是净损失）。
#:
#: 为什么白名单在服务层而不是表结构：设置项会随型号与固件迭代
#: （4G 机器要流量提醒、Wi-Fi 机器要配网信息），逐项开列意味着每加一项
#: 就要一次迁移；而客户端可以塞任意键进 JSON 列，所以白名单必须存在，
#: 只是它属于**业务规则**，放服务层才能跟着产品迭代走。
DEVICE_SETTING_KEYS: frozenset[str] = frozenset({"volume", "childMode", "wakeWord"})

#: ``devices.settings`` 里由服务端维护的元数据键（客户端不可写）
SETTINGS_UPDATED_AT_KEY = "updatedAt"

#: 设备设置默认值。``devices.settings`` 为空时返回它——
#: 「没设置过」与「设置为默认值」对用户是同一件事。
DEFAULT_DEVICE_SETTINGS: dict[str, Any] = {
    "volume": 60,
    "childMode": False,
    "wakeWord": "你好小伙伴",
}

#: 联网方式的中文展示名（小程序设备卡片上显示）
NETWORK_LABELS: dict[str, str] = {
    str(NetworkType.FOUR_G): "4G 蜂窝网络",
    str(NetworkType.WIFI): "Wi-Fi 无线网络",
}

#: 终端用户未填解绑原因时的兜底文案。
#: 小程序端不强制填原因（与商户端相反，见 ``schemas.miniapp.UnbindRequest``），
#: 但时间线里必须留下**可读**的原因，不能是空串。
DEFAULT_UNBIND_REASON = "终端用户主动解绑"

#: 充值订单号字符集：剔除 0/O/1/I 等形近字符，便于人工抄录与客服核对
#: （与订单号 / 工单号 / 分配单号同一口径）
RECHARGE_ORDER_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
RECHARGE_ORDER_SUFFIX_LENGTH = 4
#: 订单号冲突时的最大重试次数（4 位随机 + 当天日期，实际几乎不会冲突）
RECHARGE_ORDER_MAX_ATTEMPTS = 5

#: Wi-Fi 设备不需要流量充值的说明文案
WIFI_UNSUPPORTED_REASON = "Wi-Fi 方案通过家庭网络联网，不需要流量充值"

# ---- 扫码不可绑的原因文案（前端直接展示，勿改口径） ----
REASON_FROZEN = "设备已冻结，请联系客服"
REASON_RETIRED = "设备已报废"
REASON_NOT_ALLOCATED = "设备尚未分配，请稍后再试"
REASON_BOUND_BY_OTHER = "设备已被其他账号绑定"
REASON_BOUND_TO_SELF_ACTIVATED = "设备已激活，可直接使用"
REASON_BOUND_TO_SELF = "设备已绑定到你的账号"
#: 产品已停用 / 授权已过期时的用户可见文案。
#: 刻意**不**透出产品名与租户名：那是商户与平台之间的商务信息，
#: 终端用户只需要知道「这台现在绑不了、找客服」，而不该看到合同细节。
REASON_PRODUCT_UNAUTHORIZED = "设备所属产品暂不可用，请联系客服"

# ---- 激活维度的事件类型 ----
#
# 为什么不用 ``device_service.EVENT_*``？那些常量覆盖的是资产 / 绑定 / 在线
# 三个维度；激活维度（``NOT_ACTIVATED → ACTIVATING → ACTIVATED``）在 P8 之前
# 没有服务端入口，因此 ``device_service`` 里没有对应常量。
# 这里补齐时**同时显式传 dimension="activation"**：``DEVICE_EVENT_DIMENSIONS``
# 是一张受维护的映射表，新事件类型不塞进去（它归 P9 或后续阶段维护），
# 而 ``record_event`` 明确支持「显式传入优先」。
EVENT_ACTIVATING = "ACTIVATING"
EVENT_ACTIVATED = "ACTIVATED"
EVENT_BIND_FAILED = "BIND_FAILED"
EVENT_ACTIVATION_FAILED = "ACTIVATION_FAILED"
ACTIVATION_DIMENSION = "activation"

#: 终端用户在审计里的角色标识。
#: 终端用户没有平台角色码（``RoleType`` 只有平台 / 商户 / 工厂），
#: 但审计里必须能一眼区分「这条是家长的操作还是运营的操作」。
END_USER_ACTOR_ROLE = "END_USER"


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _client_ip(request: Request | None) -> str | None:
    """提取客户端 IP（优先取反代透传的真实 IP）。

    与 :func:`app.services.audit_service._client_ip` 同一口径。这里不直接复用
    那个私有函数：``last_login_ip`` 是**用户可见的登录记录**，
    它的取值规则不该被审计模块的重构牵着走。
    """
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else None


def network_label(network_type: str | None) -> str | None:
    """联网方式 → 中文展示名（未知取值原样返回，不抛错）。"""
    if not network_type:
        return None
    return NETWORK_LABELS.get(str(network_type), str(network_type))


def _vendor_label(vendor: str | None) -> str | None:
    """云服务商厂商 → 中文展示名（未知取值原样返回）。

    未知取值不抛错，是因为 ``CloudVendor`` 可能会落后于平台已登记的数据：
    扫码页应该照常显示「OTHER」，而不是因为一个展示名解析失败整个接口 500。
    """
    if not vendor:
        return None
    try:
        return CLOUD_VENDOR_LABELS.get(CloudVendor(vendor)) or vendor
    except ValueError:
        return vendor


def _is_claimable(device: Device) -> bool:
    """设备是否处于「可被扫码认领」状态。

    只有 ``ALLOCATED``（已分配给租户、尚未绑定）才谈得上认领：
    平台库存（``IN_STOCK``）不该被终端用户激活，已绑定（``BOUND``）
    更不该被覆盖——后者由 :func:`assert_device_owned` 的绑定分支处理。
    """
    return str(device.asset_status) == str(AssetStatus.ALLOCATED)


# ---------------------------------------------------------------------------
# 校验与查询
# ---------------------------------------------------------------------------


async def get_end_user(session: AsyncSession, end_user_id: str) -> EndUser:
    """按 ID 取终端用户。

    Raises:
        AppException: 用户不存在（``UNAUTHENTICATED``）——令牌指向一个已被删除
            的账号时，正确的语义是「登录态失效」而不是「404 找不到资源」，
            前者会让小程序跳回登录页，后者只会让它显示一个空白错误页。
    """
    user = (
        await session.execute(select(EndUser).where(EndUser.id == end_user_id))
    ).scalar_one_or_none()
    if user is None:
        raise unauthenticated("登录态已失效，请重新登录")
    return user


async def get_binding(session: AsyncSession, device_id: str) -> DeviceBinding | None:
    """按设备取绑定记录（``device_id`` 唯一，故至多一行）。"""
    return (
        await session.execute(
            select(DeviceBinding).where(DeviceBinding.device_id == device_id)
        )
    ).scalar_one_or_none()


async def assert_device_owned(
    session: AsyncSession,
    ctx: EndUserContext,
    device_id: str,
    *,
    allow_activatable: bool = False,
) -> Device:
    """校验设备属于当前终端用户，返回设备对象。

    **这是小程序端唯一的可见性收口点**（理由见模块 docstring）：
    所有 ``/miniapp/devices/{id}/**`` 端点都必须先过它。

    Args:
        session: 数据库会话。
        ctx: 终端用户上下文。
        device_id: 设备 ID。
        allow_activatable: 是否额外放行「处于可认领状态（``ALLOCATED``）
            的未绑定设备」。**只有两个激活端点传 ``True``**：
            激活在绑定之前发生，那时还没有绑定关系可查，
            授权依据是「这台机器还没被任何人认领」。

    Returns:
        设备对象。

    Raises:
        AppException: 设备不存在、不属于当前用户、或（未开开关时）
            处于可认领状态。**一律 ``DEVICE_NOT_FOUND``（404）**，
            不泄漏设备是否存在。
    """
    device = (
        await session.execute(select(Device).where(Device.id == device_id))
    ).scalar_one_or_none()
    if device is None:
        raise device_not_found("设备不存在")

    binding = await get_binding(session, device.id)
    if (
        binding is not None
        and binding.status == str(BindingRecordStatus.BOUND)
        and binding.end_user_id == ctx.user_id
    ):
        return device

    if allow_activatable and _is_claimable(device):
        return device

    raise device_not_found("设备不存在")


async def list_user_devices(
    session: AsyncSession, ctx: EndUserContext
) -> list[tuple[Device, DeviceBinding]]:
    """列出当前终端用户已绑定的设备（含绑定记录）。

    一次 ``JOIN`` 取回设备与绑定行，而不是先查绑定再逐台回查设备：
    后者在小程序首页就是 N+1 次往返，而设备列表恰恰是最需要快的地方。
    """
    stmt = (
        select(Device, DeviceBinding)
        .join(DeviceBinding, DeviceBinding.device_id == Device.id)
        .where(DeviceBinding.end_user_id == ctx.user_id)
        .where(DeviceBinding.status == str(BindingRecordStatus.BOUND))
        .order_by(Device.bound_at.desc(), Device.created_at.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [(row[0], row[1]) for row in rows]


def to_device_brief(device: Device, binding: DeviceBinding | None = None) -> MiniappDeviceBrief:
    """ORM → 设备摘要。

    ``online`` 用 :func:`app.services.device_service.is_online` 的**派生判据**
    （心跳窗口），而不是落库的 ``online_status`` 投影——后者不会随时间变化，
    设备掉线后会一直停在 ``ONLINE``（见该模块 docstring）。
    """
    return MiniappDeviceBrief(
        id=device.id,
        sn=device.sn,
        # devices 表没有昵称列，P8 只读不写；字段先占位（见 schema 说明）
        nickname=None,
        network_type=device.network_type,
        network_label=network_label(device.network_type),
        asset_status=device.asset_status,
        activation_status=device.activation_status,
        online=device_service.is_online(device),
        online_status=device.online_status,
        last_heartbeat_at=device.last_heartbeat_at,
        firmware_version=device.firmware_version,
        bound_at=binding.bound_at if binding is not None else device.bound_at,
    )


async def build_device_detail(
    session: AsyncSession, device: Device, binding: DeviceBinding | None
) -> MiniappDeviceDetail:
    """组装设备详情（补产品名 / 租户名 / 设置 / 绑定次数）。

    只做**三次**按主键的取名单查询，不做通用 join：详情页是低频路径，
    清晰比省一次查询更重要。
    """
    product_name: str | None = None
    if device.client_product_id:
        name = (
            await session.execute(
                select(ClientProduct.name).where(ClientProduct.id == device.client_product_id)
            )
        ).scalar_one_or_none()
        product_name = str(name) if name else None

    tenant_name: str | None = None
    if device.tenant_id:
        found = (
            await session.execute(select(Tenant.name).where(Tenant.id == device.tenant_id))
        ).scalar_one_or_none()
        tenant_name = str(found) if found else None

    return MiniappDeviceDetail(
        **to_device_brief(device, binding).model_dump(),
        product_name=product_name,
        tenant_name=tenant_name,
        settings=read_device_settings(device),
        bind_count=(binding.bind_count or 0) if binding is not None else 0,
    )


# ---------------------------------------------------------------------------
# 一、认证
# ---------------------------------------------------------------------------


async def send_login_code(
    session: AsyncSession, *, phone: str, request: Request | None = None
) -> AuthCodeResponse:
    """发送登录验证码（首次输入手机号时自动建号）。

    **安全失败优先（ADR-07）**：``MINIAPP_SMS_PROVIDER=none`` 时直接 503，
    不建号、不写库、不返回 ``mockCode``。把通道检查放在最前面是为了
    避免「通道不可用却留下了半条登录状态」这种难以排查的中间态。

    ``mock`` 通道下验证码会**回显**在 ``mockCode`` 里并标注 ``mock=true``：
    这是作品集项目「无真实厂商密钥也能跑通全流程」的取舍（与 P7 的离线
    AI 引擎同一思路）。生产环境禁止该取值，由启动期安全校验拒绝（配置层兜底）。

    验证码只存 SHA-256 摘要，与刷新令牌 / 设备凭证 / 确认令牌同一口径：
    库被读走也拿不到可直接使用的验证码。

    Raises:
        AppException: 短信通道未接入（503）、账号被禁用（403）。
    """
    if settings.MINIAPP_SMS_PROVIDER == "none":
        raise vendor_unavailable(
            "短信通道尚未接入，已按安全策略拒绝发送验证码", vendor="sms"
        )

    user = (
        await session.execute(select(EndUser).where(EndUser.phone == phone))
    ).scalar_one_or_none()
    if user is None:
        # upsert by phone：同一手机号重复请求验证码只会有同一行，
        # 因此「一个手机号同时只有一个待验证码」由主键唯一性天然保证
        user = EndUser(
            id=new_id("end_user"),
            phone=phone,
            status=str(UserStatus.ACTIVE),
            login_code_attempts=0,
        )
        session.add(user)
        await session.flush()

    if user.status != str(UserStatus.ACTIVE):
        raise account_disabled("该账号已被禁用，请联系客服")

    code = f"{secrets.randbelow(10 ** LOGIN_CODE_LENGTH):0{LOGIN_CODE_LENGTH}d}"
    user.login_code_hash = hash_token(code)
    user.login_code_expires_at = utcnow() + _login_code_ttl()
    # 重发即归零：旧码已被覆盖，继续累计旧码的错误次数没有意义
    user.login_code_attempts = 0
    await session.commit()

    mocked = settings.MINIAPP_SMS_PROVIDER == "mock"
    logger.info("终端用户 %s 请求验证码（通道=%s）", phone, settings.MINIAPP_SMS_PROVIDER)
    return AuthCodeResponse(
        sent=True,
        mock=mocked,
        mock_code=code if mocked else None,
        expires_in_seconds=settings.MINIAPP_LOGIN_CODE_TTL_SECONDS,
        message=(
            "当前为模拟短信通道，未真实发送短信，验证码见 mockCode"
            if mocked
            else "验证码已发送"
        ),
    )


async def login(
    session: AsyncSession,
    *,
    phone: str,
    code: str,
    request: Request | None = None,
) -> AuthLoginResponse:
    """验证码登录，返回令牌 + 该用户的设备列表。

    校验顺序与错误码：

    1. 无待验证码 / 已过期 → 409 ``QR_EXPIRED``（文案写明「请重新获取」）；
    2. 验证码不匹配 → 累加 ``login_code_attempts`` 并返回 400 ``VALIDATION_ERROR``，
       ``details.attemptsLeft`` 告诉前端还能试几次；
    3. 达到 ``MINIAPP_LOGIN_MAX_ATTEMPTS`` → **作废该验证码**（清空摘要）后
       再抛 400。作废这一步是必要的：6 位数字只有 100 万种组合，
       若允许在 300 秒有效期内无限次尝试，「短有效期」形同虚设。

    登录成功才写审计 ``END_USER_LOGIN``：失败尝试写的是 400，
    与后台的 ``LOGIN_FAILED`` 不是同一类事件（那边是账号安全，这边是设备归属）。

    Raises:
        AppException: ``QR_EXPIRED``（409）/ ``VALIDATION_ERROR``（400）/
            ``ACCOUNT_DISABLED``（403）。
    """
    user = (
        await session.execute(select(EndUser).where(EndUser.phone == phone))
    ).scalar_one_or_none()
    if user is None:
        # 不区分「手机号没注册」与「验证码过期」：两者对用户的下一步动作
        # 完全相同（重新获取验证码），多一个错误码只会多一个前端分支
        raise qr_expired("验证码已过期，请重新获取")

    if user.status != str(UserStatus.ACTIVE):
        raise account_disabled("该账号已被禁用，请联系客服")

    now = utcnow()
    if (
        user.login_code_hash is None
        or user.login_code_expires_at is None
        or user.login_code_expires_at < now
    ):
        raise qr_expired("验证码已过期，请重新获取")

    # 常量时间比较：摘要是验证码派生的，用 == 比较会泄漏前缀匹配长度
    if not hmac.compare_digest(hash_token(code), user.login_code_hash):
        attempts = (user.login_code_attempts or 0) + 1
        remaining = settings.MINIAPP_LOGIN_MAX_ATTEMPTS - attempts
        if remaining <= 0:
            user.login_code_hash = None
            user.login_code_expires_at = None
            user.login_code_attempts = 0
            await session.commit()
            raise validation_error(
                "验证码连续输错次数过多，已作废该验证码，请重新获取",
                details={"attemptsLeft": 0},
            )
        user.login_code_attempts = attempts
        await session.commit()
        raise validation_error("验证码不正确", details={"attemptsLeft": remaining})

    user.login_code_hash = None
    user.login_code_expires_at = None
    user.login_code_attempts = 0
    user.last_login_at = now
    user.last_login_ip = _client_ip(request)

    token, _expires_at = issue_end_user_token(end_user_id=user.id, phone=user.phone)
    ctx = EndUserContext(user_id=user.id, phone=user.phone)
    devices = await list_user_devices(session, ctx)

    await audit_service.record(
        session,
        action=AuditAction.END_USER_LOGIN,
        actor_account=user.phone,
        actor_role=END_USER_ACTOR_ROLE,
        resource_type="end_user",
        resource_id=user.id,
        summary=f"终端用户 {user.phone} 登录（名下 {len(devices)} 台设备）",
        detail={"phone": user.phone, "deviceCount": len(devices)},
        request=request,
    )
    await session.commit()
    logger.info("终端用户 %s 登录成功", user.phone)

    return AuthLoginResponse(
        access_token=token,
        token_type="Bearer",
        expires_in_seconds=settings.END_USER_TOKEN_EXPIRE_MINUTES * 60,
        user=EndUserBrief(id=user.id, phone=user.phone, nickname=user.nickname),
        devices=[to_device_brief(device, binding) for device, binding in devices],
    )


async def get_profile(session: AsyncSession, ctx: EndUserContext) -> MiniappProfileResponse:
    """终端用户资料（含设备数量）。"""
    user = await get_end_user(session, ctx.user_id)
    devices = await list_user_devices(session, ctx)
    return MiniappProfileResponse(
        id=user.id,
        phone=user.phone,
        nickname=user.nickname,
        avatar=user.avatar,
        device_count=len(devices),
    )


def _login_code_ttl() -> timedelta:
    """验证码有效期。

    单独抽一个函数是为了让「过期时间怎么算」只有一处：将来若改成
    「按小时包」（同一码有效 1 小时），只改这里。
    """
    return timedelta(seconds=settings.MINIAPP_LOGIN_CODE_TTL_SECONDS)


# ---------------------------------------------------------------------------
# 二、扫码解析
# ---------------------------------------------------------------------------


async def resolve_scan(
    session: AsyncSession, ctx: EndUserContext, *, payload: str
) -> ScanResolveResponse:
    """解析扫码结果，返回设备信息与「能不能绑」。

    **本函数不因「不能绑」抛错**：设备已绑他人 / 未分配 / 已冻结都是**正常状态**，
    用 ``bindable=false`` + ``reason`` 表达，页面据此给出解释性文案。
    只有两种根本性错误会抛：

    * ``QR_INVALID``（404）——载荷前缀不对 / 段数不对 / 京东签名不通过；
    * ``DEVICE_NOT_FOUND``（404）——SN 不在设备库（可能是伪造的码）。

    与 P5 的 ``binding_service.precheck`` 的区别：P5 是商户端两步绑定，
    还要签发 5 分钟确认令牌（因为扫码者未必是真正要绑的人）；
    P8 是终端用户自己扫码、同一会话立即激活，**不需要确认令牌**，
    但「单绑约束 / 冻结拦截 / 已分配才可绑 / 产品授权」四项口径与 P5 完全一致
    （产品授权不通过时返回 ``bindable=false`` + 中性文案，不暴露产品名与租户）。

    ``JX`` 载荷由 :func:`app.services.qrcode_service.parse_payload` 解析
    （``JX|SN|IMEI|ICCID|deviceId``），不做签名校验——该格式由集贤云端签发，
    平台侧无法验签；防篡改靠「SN 必须命中设备库」这一步。

    Raises:
        AppException: ``QR_INVALID`` / ``DEVICE_NOT_FOUND``。
    """
    parsed = qrcode_service.parse_payload(payload)

    device = (
        await session.execute(select(Device).where(Device.sn == parsed.sn))
    ).scalar_one_or_none()
    if device is None:
        raise device_not_found("设备不存在，请确认二维码是否为本平台的设备")

    binding = await get_binding(session, device.id)
    bound_to_self = (
        binding is not None
        and binding.status == str(BindingRecordStatus.BOUND)
        and binding.end_user_id == ctx.user_id
    )
    activated = str(device.activation_status) == str(ActivationStatus.ACTIVATED)
    product, product_denied = await _product_authorization(session, device)

    bindable: bool
    reason: str | None
    if str(device.asset_status) == str(AssetStatus.FROZEN):
        # 冻结优先于「已绑自己」：设备被按下暂停键时，用户真正需要知道的是
        # 「它被冻结了、请联系客服」，而不是「它是你的」
        bindable, reason = False, REASON_FROZEN
    elif str(device.asset_status) == str(AssetStatus.RETIRED):
        bindable, reason = False, REASON_RETIRED
    elif bound_to_self:
        # 已经绑到自己的设备不再校验产品授权：授权是「能不能新绑」的闸门，
        # 不该让用户在授权到期后连自己的设备都看不到（那是另一条独立的停服流程）
        bindable = True
        reason = REASON_BOUND_TO_SELF_ACTIVATED if activated else REASON_BOUND_TO_SELF
    elif binding is not None and binding.status == str(BindingRecordStatus.BOUND):
        # 绑定行的 end_user_id 为空（商户端绑定但未指定终端用户）也走这里：
        # 对小程序用户而言它同样是「别人绑的」
        bindable, reason = False, REASON_BOUND_BY_OTHER
    elif not _is_claimable(device):
        bindable, reason = False, REASON_NOT_ALLOCATED
    elif product_denied is not None:
        bindable, reason = False, REASON_PRODUCT_UNAUTHORIZED
    else:
        bindable, reason = True, None

    vendor = await _load_cloud_vendor(session, device, product)
    tenant_name: str | None = None
    if device.tenant_id:
        found = (
            await session.execute(select(Tenant.name).where(Tenant.id == device.tenant_id))
        ).scalar_one_or_none()
        tenant_name = str(found) if found else None

    return ScanResolveResponse(
        format=str(parsed.format),
        network_type=device.network_type,
        bindable=bindable,
        reason=reason,
        device=ScanDeviceInfo(
            id=device.id,
            sn=device.sn,
            network_type=device.network_type,
            asset_status=device.asset_status,
            activation_status=device.activation_status,
            bind_status=device.bind_status,
            device_label=device_service.device_label(device),
            online=device_service.is_online(device),
            imei=device.imei,
            iccid=device.iccid,
            mac=device.mac,
            firmware_version=device.firmware_version,
        ),
        product=(
            ScanProductInfo(id=product.id, code=product.code, name=product.name)
            if product is not None
            else None
        ),
        tenant_name=tenant_name,
        cloud_vendor=vendor,
        cloud_vendor_label=_vendor_label(vendor),
        activation_status=device.activation_status,
        bind_status=device.bind_status,
        asset_status=device.asset_status,
    )


async def _load_product(session: AsyncSession, device: Device) -> ClientProduct | None:
    """取设备挂载的客户产品（扫码页要回显「你扫到的是哪款产品」）。"""
    if not device.client_product_id:
        return None
    return (
        await session.execute(
            select(ClientProduct).where(ClientProduct.id == device.client_product_id)
        )
    ).scalar_one_or_none()


async def _product_authorization(
    session: AsyncSession, device: Device
) -> tuple[ClientProduct | None, str | None]:
    """返回 ``(客户产品, 不可用原因)``；可用时原因为 ``None``。

    判定口径与 P5 的绑定流程**完全一致**（客户产品 ``ENABLED`` + 模板授权行
    ``ENABLED`` 且未过期），并复用同一个判定函数
    :func:`app.services.allocation_service.is_product_authorized`——
    两处各写一遍，迟早出现「分配放行、绑定拦截」这类自相矛盾。

    返回「原因字符串」而不是直接抛异常，是因为两个调用方需要两种呈现：
    扫码页要把不可用翻译成**用户能看懂、且不暴露商务信息**的文案；
    激活接口要抛 409 ``PRODUCT_NOT_AUTHORIZED``（错误码带产品名，给运维排查）。
    """
    product = await _load_product(session, device)
    if product is None:
        return None, "设备未关联有效的客户产品"
    if str(product.status) != str(EnableStatus.ENABLED):
        return product, f"客户产品「{product.name}」已停用"
    authorized = await allocation_service.is_product_authorized(
        session, tenant_id=product.tenant_id, template_id=product.template_id
    )
    if not authorized:
        return product, f"客户产品「{product.name}」所属模板对该租户未授权或授权已过期"
    return product, None


async def _load_cloud_vendor(
    session: AsyncSession, device: Device, product: ClientProduct | None
) -> str | None:
    """取设备背后的云服务商厂商。

    优先用设备自身的 ``cloud_provider_id``（分配时写入的快照），
    为空时才回落到产品的模板——与「快照优先」的既有约定一致：
    模板厂商后续变更不应改写已在现场的设备。
    """
    provider_id = device.cloud_provider_id
    if provider_id is None and product is not None:
        provider_id = (
            await session.execute(
                select(ProductTemplate.cloud_provider_id).where(
                    ProductTemplate.id == product.template_id
                )
            )
        ).scalar_one_or_none()
    if provider_id is None:
        return None
    vendor = (
        await session.execute(select(CloudProvider.vendor).where(CloudProvider.id == provider_id))
    ).scalar_one_or_none()
    return str(vendor) if vendor else None


# ---------------------------------------------------------------------------
# 三、厂商解析与安全失败
# ---------------------------------------------------------------------------


async def resolve_device_provider(session: AsyncSession, device: Device) -> ResolvedProvider:
    """按设备所属客户产品解析供应商（四层顺序见 :mod:`app.ai.registry`）。

    **不校验密钥**：这一步只回答「这台设备该走哪个厂商」，
    调用方再按具体能力域调 :func:`ensure_vendor_capability`。
    拆开两件事的好处是「已激活设备幂等返回」这类不需要厂商能力的路径
    也能用同一个解析结果（例如判断 ``mock`` 字段）。

    Raises:
        AppException: 四层全部未命中已注册供应商（``VENDOR_UNAVAILABLE``，503）。
    """
    registry.ensure_default_registry()
    resolved = await registry.resolve_for_product(
        session, client_product_id=device.client_product_id
    )
    if resolved is None:
        raise AppException(
            ErrorCode.VENDOR_UNAVAILABLE,
            "该设备未解析到可用的云服务商（请检查客户产品与模板的云厂商配置）",
            details={
                "ok": False,
                "layer": registry.LAYER_NONE,
                "deviceId": device.id,
                "clientProductId": device.client_product_id,
            },
        )
    return resolved


async def ensure_vendor_capability(
    session: AsyncSession,
    resolved: ResolvedProvider,
    *,
    capability: str,
    ctx: EndUserContext,
    request: Request | None = None,
) -> None:
    """校验「能力域支持」与「已配置密钥」，任一不过即安全失败并写审计。

    与 P7 的 ``/ai/chat`` 同一条红线（ADR-07）：**绝不伪造成功**。
    区别只是执行者从后台账号变成了终端用户，因此审计里用
    ``actor_account=手机号`` + ``actor_role=END_USER`` 记录，
    而不是硬塞一个 ``AuthContext``。

    审计**单独提交**（与 ``app.api.v1.ai._record_vendor_failure`` 同一取舍）：
    这条审计是「安全拒绝」的证据，若与业务事务绑在一起，
    业务回滚会把证据一起丢掉，运维就无法证明「当时确实拒绝了」。

    Raises:
        AppException: 供应商不支持该能力，或未配置密钥（``VENDOR_UNAVAILABLE``，503）。
    """
    provider = resolved.provider
    try:
        if not provider.supports(capability):
            raise provider_unavailable_error(provider, capability)
        provider.ensure_configured()
    except AppException as exc:
        await audit_service.record(
            session,
            action=AuditAction.VENDOR_CALL_FAILED,
            actor_account=ctx.phone,
            actor_role=END_USER_ACTOR_ROLE,
            resource_type="ai_provider",
            resource_id=provider.code,
            summary=f"终端用户侧厂商调用被安全拒绝：{exc.message}",
            detail={
                "code": str(exc.code),
                "providerCode": provider.code,
                "layer": resolved.layer,
                "capability": capability,
                "ok": False,
            },
            success=False,
            request=request,
        )
        await session.commit()
        raise


# ---------------------------------------------------------------------------
# 四、激活与绑定
# ---------------------------------------------------------------------------


async def _bind_to_end_user(
    session: AsyncSession,
    ctx: EndUserContext,
    device: Device,
    *,
    request: Request | None = None,
) -> DeviceBinding:
    """把设备绑定到当前终端用户（复用 ``DeviceBinding`` 同一行）。

    ``device_bindings.device_id`` 有唯一约束，因此**一台设备恒一行**：
    已存在（例如商户端解绑后留下的）那行会被复用，把 ``end_user_id``
    指向当前用户、``status`` 置回 ``BOUND``、``bind_count += 1``。
    复用而不是新增，既满足数据库约束，也让「反复解绑重绑」在
    ``bind_count`` 上留下痕迹（异常设备可据此识别）。

    资产状态走 :func:`app.services.device_service.transition_asset`
    （``ALLOCATED → BOUND``，事件维度 ``bind``），不手写字段赋值——
    「迁移合法 + 事件留痕」两件事由该函数保证（与 P5 绑定同一口径）。

    ``confirm_token_hash`` 一并置空：P8 的扫码者与激活者是同一会话，
    不需要 P5 的确认令牌；若该行此前被商户端 ``precheck`` 过，
    这里顺手销毁掉那张悬空的令牌，避免它日后被误用。
    """
    if not device.tenant_id:
        # 没有租户的设备不可能处于 ALLOCATED（分配是写入 tenant_id 的同一步），
        # 真出现说明数据被绕过服务层改过，此时宁可报「不存在」也不能绑上去
        raise device_not_found("设备不存在")

    now = utcnow()
    binding = await get_binding(session, device.id)
    if binding is None:
        binding = DeviceBinding(
            id=new_id("device_binding"),
            device_id=device.id,
            tenant_id=device.tenant_id,
            status=str(BindingRecordStatus.BOUND),
            bind_count=0,
        )
        session.add(binding)
    elif binding.status == str(BindingRecordStatus.BOUND) and binding.end_user_id == ctx.user_id:
        # 已经是「本用户 + 已绑定」→ 空操作。
        #
        # 这条早返回不是优化，而是**语义必需**：``_activate`` 的幂等分支也会
        # 调到这里（已激活设备重复激活要保证绑定关系），若继续往下走会
        # ``bind_count`` 每次 +1 并把设备再迁一次 ``ALLOCATED → BOUND``
        # （``BOUND → BOUND`` 本就不在迁移表里）。``bind_count`` 的用途是
        # 「识别反复解绑重绑的异常设备」，被重复激活灌水就失去意义了。
        return binding
    elif (
        binding.status == str(BindingRecordStatus.BOUND)
        and binding.end_user_id
        and binding.end_user_id != ctx.user_id
    ):
        # 单绑约束的**服务层**兜底：一台设备不能同时属于两个终端账号。
        # 正常路径上这一步不可达（scan/resolve 的 bindable 已拦过），
        # 但「激活」「幂等回放」都能按设备 ID 直接调用，多一层显式判断
        # 比依赖调用顺序可靠——单绑是 P5 就下沉到数据库约束的不变式
        # （UNIQUE(device_id)），这里补的是「两个 end_user 争同一行」的情形。
        raise device_already_bound("设备已被其他账号绑定，如需转移请先在原账号解绑")

    binding.tenant_id = device.tenant_id
    binding.client_product_id = device.client_product_id
    binding.end_user_id = ctx.user_id
    binding.status = str(BindingRecordStatus.BOUND)
    binding.bound_at = now
    # 绑定者是终端用户而不是商户账号，用手机号留痕（审计里能对上是谁）
    binding.bound_by = ctx.phone
    binding.bind_count = (binding.bind_count or 0) + 1
    binding.confirm_token_hash = None
    binding.confirm_expires_at = None
    binding.unbound_at = None
    binding.unbind_reason = None

    device.bind_status = str(BindStatus.BOUND)
    device.bound_at = now
    await device_service.transition_asset(
        session,
        device,
        AssetStatus.BOUND,
        actor=None,
        request=request,
        event_type=device_service.EVENT_BOUND,
        dimension="bind",
    )
    return binding


def _idempotent_result(device: Device, resolved: ResolvedProvider) -> ActivationResult | None:
    """已激活设备的幂等结果；未激活返回 ``None``。

    ``mock`` 取**当前解析到的**供应商类型：激活当时用的供应商没有落库
    （``DialogueSession`` 那种「记录来源」的字段设备上不存在），
    因此这里诚实标注「按当前配置看它是不是模拟的」。
    """
    if str(device.activation_status) != str(ActivationStatus.ACTIVATED):
        return None
    return ActivationResult(
        device_id=device.id,
        sn=device.sn,
        network_type=device.network_type,
        activation_status=device.activation_status,
        bind_status=device.bind_status,
        activated_at=device.activated_at,
        vendor_message="设备已激活，无需重复操作",
        mock=resolved.provider.kind is AiProviderKind.LOCAL,
    )


async def _activate(
    session: AsyncSession,
    ctx: EndUserContext,
    device: Device,
    *,
    resolved: ResolvedProvider,
    extra: dict[str, Any],
    request: Request | None = None,
) -> ActivationResult:
    """执行厂商激活并绑定（4G / Wi-Fi 共用的后半段）。

    状态路径严格按 ``NOT_ACTIVATED → ACTIVATING → ACTIVATED``：

    * 先落 ``ACTIVATING`` 并**提交**，再调厂商。这样厂商调用卡住 / 进程被杀时，
      时间线上能看到「它停在 ACTIVATING」，而不是一片空白；
    * 厂商未配置密钥或返回失败 → **退回 ``NOT_ACTIVATED``** 再抛 503。
      退回是必要的：否则设备会永久停在 ``ACTIVATING``，用户连重试的机会都没有
      （ADR-07「绝不伪造成功」的落地细节）；
    * 成功才 ``ACTIVATED`` + 绑定。**绑定只发生在激活成功之后**，
      避免出现「绑了但没激活」的半成品。

    Note:
        事件写入的 ``actor`` 传 ``None``，终端用户身份放在 ``detail`` 里：
        :func:`app.services.device_service.record_event` 的 ``actor`` 参数
        类型是管理端的 ``AuthContext``，而终端用户没有角色 / 租户，
        硬造一个 ``AuthContext`` 会让「设备时间线的操作者是谁」这个语义失真。

    Raises:
        AppException: 厂商不可用（503，已退回 ``NOT_ACTIVATED``）。
    """
    provider = resolved.provider

    idempotent = _idempotent_result(device, resolved)
    if idempotent is not None:
        # ★ 「已激活」不等于「已绑定当前账号」。
        #
        # 走这条路径的典型场景：设备在别处激活过、或上一位用户解绑了它
        # （解绑**刻意不改变** ``activation_status``——设备确实激活过）。
        # 此时如果只回放激活结果，用户会看到「激活成功」，但库里没有绑定关系，
        # 接下来进对话页会被 WS 以 4403 拒绝——**P8 浏览器验收实测到这个缺陷**。
        #
        # 因此幂等分支必须**同时保证绑定关系**：已绑给当前用户则原地返回，
        # 未绑定则补绑，绑给了别人则抛 409（单绑约束）。
        await _bind_to_end_user(session, ctx, device, request=request)
        await session.commit()
        # 绑定之后重新构造结果，让 ``bindStatus`` 反映真实状态（而不是回放前的旧值）
        refreshed = _idempotent_result(device, resolved)
        assert refreshed is not None  # 刚判过，类型收窄
        return refreshed

    # 产品授权校验放在**幂等分支之后**：已经激活的设备不该因为授权到期而
    # 变成「不可激活」，那会让本来能用的机器在重试时突然报错。
    # 同时也放在 Wi-Fi 的 SN 校验之后（调用方顺序决定），与 P5 的
    # 「授权校验最后判」保持同一优先级口径。
    _product, denied = await _product_authorization(session, device)
    if denied is not None:
        raise product_not_authorized(denied)

    previous = str(device.activation_status)
    device.activation_status = str(ActivationStatus.ACTIVATING)
    await session.flush()
    await device_service.record_event(
        session,
        device,
        event_type=EVENT_ACTIVATING,
        dimension=ACTIVATION_DIMENSION,
        from_status=previous,
        to_status=str(ActivationStatus.ACTIVATING),
        summary=f"终端用户 {ctx.phone} 发起设备激活",
        detail={"endUserId": ctx.user_id, "phone": ctx.phone, "networkType": device.network_type},
        request=request,
    )
    await session.commit()

    try:
        await ensure_vendor_capability(
            session, resolved, capability=CAP_DEVICE, ctx=ctx, request=request
        )
        result = await provider.activate_device(device_id=device.id, sn=device.sn, extra=extra)
        if not result.success:
            raise vendor_unavailable(
                f"厂商激活失败：{result.message}",
                vendor=provider.vendor or provider.code,
            )
    except AppException as exc:
        device.activation_status = str(ActivationStatus.NOT_ACTIVATED)
        await device_service.record_event(
            session,
            device,
            event_type=EVENT_ACTIVATION_FAILED,
            dimension=ACTIVATION_DIMENSION,
            from_status=str(ActivationStatus.ACTIVATING),
            to_status=str(ActivationStatus.NOT_ACTIVATED),
            summary=f"设备激活失败：{exc.message}",
            detail={
                "endUserId": ctx.user_id,
                "phone": ctx.phone,
                "code": str(exc.code),
                "providerCode": provider.code,
            },
            request=request,
        )
        await audit_service.record(
            session,
            action=AuditAction.ACTIVATE_DEVICE,
            actor_account=ctx.phone,
            actor_role=END_USER_ACTOR_ROLE,
            tenant_id=device.tenant_id,
            resource_type="device",
            resource_id=device.id,
            summary=f"激活设备 {device.sn} 失败：{exc.message}",
            detail={
                "code": str(exc.code),
                "networkType": device.network_type,
                "providerCode": provider.code,
                "ok": False,
            },
            success=False,
            request=request,
        )
        await session.commit()
        raise

    now = utcnow()
    device.activation_status = str(ActivationStatus.ACTIVATED)
    device.activated_at = now
    await device_service.record_event(
        session,
        device,
        event_type=EVENT_ACTIVATED,
        dimension=ACTIVATION_DIMENSION,
        from_status=str(ActivationStatus.ACTIVATING),
        to_status=str(ActivationStatus.ACTIVATED),
        summary=f"设备 {device.sn} 激活成功（{device.network_type}）",
        detail={
            "endUserId": ctx.user_id,
            "phone": ctx.phone,
            "providerCode": provider.code,
            "mock": result.simulated,
            "vendorMessage": result.message,
        },
        request=request,
    )
    binding = await _bind_to_end_user(session, ctx, device, request=request)
    await audit_service.record(
        session,
        action=AuditAction.ACTIVATE_DEVICE,
        actor_account=ctx.phone,
        actor_role=END_USER_ACTOR_ROLE,
        tenant_id=device.tenant_id,
        resource_type="device",
        resource_id=device.id,
        summary=f"终端用户激活并绑定设备 {device.sn}（第 {binding.bind_count} 次绑定）",
        detail={
            "endUserId": ctx.user_id,
            "networkType": device.network_type,
            "providerCode": provider.code,
            "mock": result.simulated,
            "bindCount": binding.bind_count,
        },
        request=request,
    )
    await session.commit()
    logger.info("设备 %s 已由终端用户 %s 激活并绑定", device.sn, ctx.phone)

    return ActivationResult(
        device_id=device.id,
        sn=device.sn,
        network_type=device.network_type,
        activation_status=device.activation_status,
        bind_status=device.bind_status,
        activated_at=device.activated_at,
        vendor_message=result.message,
        mock=result.simulated,
    )


async def activate_4g(
    session: AsyncSession,
    ctx: EndUserContext,
    device_id: str,
    *,
    request: Request | None = None,
) -> ActivationResult:
    """4G（集贤）激活：``NOT_ACTIVATED → ACTIVATING → 调厂商 → ACTIVATED`` + 绑定。

    厂商侧要 IMEI / ICCID / 厂商设备 ID 才能定位模组，因此把它们放进 ``extra``
    原样转交；这些字段来自设备记录（二维码回填），不信任客户端上报。

    已激活的设备**幂等返回**，不重复调厂商、不重复绑定。

    Raises:
        AppException: 设备不可见（404 ``DEVICE_NOT_FOUND``）/ 不是 4G（400）/
            厂商未接入（503）。
    """
    device = await assert_device_owned(session, ctx, device_id, allow_activatable=True)
    if str(device.network_type) != str(NetworkType.FOUR_G):
        raise validation_error(
            "该设备不是 4G 设备，请走 Wi-Fi 配网激活流程",
            details={"networkType": device.network_type},
        )
    resolved = await resolve_device_provider(session, device)
    extra: dict[str, Any] = {
        "networkType": device.network_type,
        "imei": device.imei,
        "iccid": device.iccid,
        "vendorDeviceId": device.vendor_device_id,
    }
    return await _activate(session, ctx, device, resolved=resolved, extra=extra, request=request)


async def activate_wifi(
    session: AsyncSession,
    ctx: EndUserContext,
    device_id: str,
    *,
    reported_sn: str,
    reported_mac: str,
    request: Request | None = None,
) -> ActivationResult:
    """Wi-Fi（京东 JoyInside）激活：**先校验上报 SN/MAC 与设备记录一致**，再调厂商。

    这是 todolist 明确点名的行为：配网后设备端上报自己读到的 SN，
    服务端必须与二维码解析出的设备记录比对——不一致说明上报的根本不是这台，
    此时把 ``activation_status`` 置为 ``BIND_FAILED``、写事件与审计，
    并以 409 ``BIND_FAILED`` 结束（``details`` 带 ``qrSn`` / ``reportedSn``）。
    静默放行会造出「A 的二维码激活了 B 的设备」这类无法追责的脏数据。

    ``mac`` 与记录不符时按同一口径处理（同样 ``BIND_FAILED``）；
    记录里 MAC 为空则以本次上报为准写入——设备 MAC 常在生产阶段才回填，
    若不允许补写，Wi-Fi 激活在早期批次上永远走不通。

    已激活设备在 SN/MMAC 校验**之前**幂等返回：设备既然已激活，
    说明此前校验已通过；这时拿一次新的上报去比对，
    只会把「正常的重试」判成 ``BIND_FAILED``，反而把用户能用的设备打坏。

    Raises:
        AppException: 设备不可见（404）/ 不是 Wi-Fi（400）/
            SN 或 MAC 不一致（409 ``BIND_FAILED``）/ 厂商未接入（503）。
    """
    device = await assert_device_owned(session, ctx, device_id, allow_activatable=True)
    if str(device.network_type) != str(NetworkType.WIFI):
        raise validation_error(
            "该设备不是 Wi-Fi 设备，请走 4G 激活流程",
            details={"networkType": device.network_type},
        )

    resolved = await resolve_device_provider(session, device)
    idempotent = _idempotent_result(device, resolved)
    if idempotent is not None:
        return idempotent

    previous = str(device.activation_status)
    if reported_sn != device.sn:
        await _mark_bind_failed(
            session,
            ctx,
            device,
            previous=previous,
            summary=f"Wi-Fi 激活失败：上报 SN 与设备记录不一致（{reported_sn} ≠ {device.sn}）",
            detail={"qrSn": device.sn, "reportedSn": reported_sn},
            request=request,
        )
        raise bind_failed(
            "设备上报的序列号与二维码不一致，激活已终止",
            details={"qrSn": device.sn, "reportedSn": reported_sn},
        )

    if device.mac and reported_mac.upper() != device.mac.upper():
        # MAC 比较忽略大小写：设备端常以小写上报，而工厂导入多为大写
        await _mark_bind_failed(
            session,
            ctx,
            device,
            previous=previous,
            summary=f"Wi-Fi 激活失败：上报 MAC 与设备记录不一致（{reported_mac} ≠ {device.mac}）",
            detail={"expectedMac": device.mac, "reportedMac": reported_mac},
            request=request,
        )
        raise bind_failed(
            "设备上报的 MAC 与平台记录不一致，激活已终止",
            details={"expectedMac": device.mac, "reportedMac": reported_mac},
        )

    if not device.mac:
        # 记录为空则以本次上报为准（见 docstring）
        device.mac = reported_mac
        await session.flush()

    extra: dict[str, Any] = {
        "networkType": device.network_type,
        "mac": device.mac,
        "vendorDeviceId": device.vendor_device_id,
        "clientProductId": device.client_product_id,
    }
    return await _activate(session, ctx, device, resolved=resolved, extra=extra, request=request)


async def _mark_bind_failed(
    session: AsyncSession,
    ctx: EndUserContext,
    device: Device,
    *,
    previous: str,
    summary: str,
    detail: dict[str, Any],
    request: Request | None,
) -> None:
    """把激活状态置为 ``BIND_FAILED`` 并留下事件与审计，然后提交。

    提交发生在抛异常之前：这是一个**业务事实**（这台设备校验失败过），
    不能被调用方后续的异常处理回滚掉，否则「SN 不一致」在系统里就不存在了。
    """
    device.activation_status = str(ActivationStatus.BIND_FAILED)
    await device_service.record_event(
        session,
        device,
        event_type=EVENT_BIND_FAILED,
        dimension=ACTIVATION_DIMENSION,
        from_status=previous,
        to_status=str(ActivationStatus.BIND_FAILED),
        summary=summary,
        detail={"endUserId": ctx.user_id, "phone": ctx.phone, **detail},
        request=request,
    )
    await audit_service.record(
        session,
        action=AuditAction.ACTIVATE_DEVICE,
        actor_account=ctx.phone,
        actor_role=END_USER_ACTOR_ROLE,
        tenant_id=device.tenant_id,
        resource_type="device",
        resource_id=device.id,
        summary=summary,
        detail={**detail, "networkType": device.network_type, "ok": False},
        success=False,
        request=request,
    )
    await session.commit()


async def unbind_device(
    session: AsyncSession,
    ctx: EndUserContext,
    device_id: str,
    *,
    reason: str | None = None,
    request: Request | None = None,
) -> UnbindResult:
    """终端用户解绑设备。

    与 P5 的 ``POST /merchant/devices/{id}/unbind`` **同口径**：

    * ``device_bindings.status → UNBOUND`` + ``unbound_at`` / ``unbind_reason``；
    * 设备 ``bind_status → UNBOUND``，资产状态 ``BOUND → ALLOCATED``
      （回到「已分配给租户、等待下一个用户」），**不退回平台库存**；
    * 写设备事件（维度 ``bind``）与审计。

    ``activation_status`` **保持不变**：设备已经激活过，解绑只是解除归属关系。
    把它一起退回 ``NOT_ACTIVATED`` 会让同一台机器在下一位用户手里重复走一次
    厂商激活（集贤 / 京东都会拒绝重复激活），从而产生一个「永远激活不了」
    的设备——这正是「解绑 ≠ 恢复出厂」的语义所在。

    Raises:
        AppException: 设备不可见（404）或当前未绑定（409 ``DEVICE_NOT_AVAILABLE``）。
    """
    device = await assert_device_owned(session, ctx, device_id)
    binding = await get_binding(session, device.id)
    if binding is None or binding.status != str(BindingRecordStatus.BOUND):
        raise device_not_available("该设备当前未绑定，无法解绑")

    now = utcnow()
    resolved_reason = reason or DEFAULT_UNBIND_REASON
    binding.status = str(BindingRecordStatus.UNBOUND)
    binding.unbound_at = now
    binding.unbind_reason = resolved_reason
    binding.unbound_by = ctx.phone

    device.bind_status = str(BindStatus.UNBOUND)
    await device_service.transition_asset(
        session,
        device,
        AssetStatus.ALLOCATED,
        reason=resolved_reason,
        actor=None,
        request=request,
        event_type=device_service.EVENT_UNBOUND,
        dimension="bind",
    )

    await audit_service.record(
        session,
        action=AuditAction.UNBIND_DEVICE,
        actor_account=ctx.phone,
        actor_role=END_USER_ACTOR_ROLE,
        tenant_id=device.tenant_id,
        resource_type="device_binding",
        resource_id=binding.id,
        summary=f"终端用户解绑设备 {device.sn}：{resolved_reason}",
        detail={
            "endUserId": ctx.user_id,
            "deviceId": device.id,
            "sn": device.sn,
            "reason": resolved_reason,
            "activationStatus": device.activation_status,
        },
        request=request,
    )
    await session.commit()
    logger.info("设备 %s 已由终端用户 %s 解绑", device.sn, ctx.phone)

    return UnbindResult(
        device_id=device.id,
        sn=device.sn,
        bind_status=device.bind_status,
        asset_status=device.asset_status,
        activation_status=device.activation_status,
        unbound_at=now,
    )


# ---------------------------------------------------------------------------
# 五、设备设置
# ---------------------------------------------------------------------------


def read_device_settings(device: Device) -> dict[str, Any]:
    """读取设备设置（补默认值、剔除非白名单键、含服务端维护的 ``updatedAt``）。

    返回的是**合并后的副本**，不是 ``device.settings`` 本身：
    直接返回原字典会让调用方在无意中改到 ORM 对象上的 JSON 值
    （SQLAlchemy 不会检测到原地修改，于是改动既不落库也不报错）。
    """
    stored = device.settings or {}
    merged = dict(DEFAULT_DEVICE_SETTINGS)
    merged.update({key: value for key, value in stored.items() if key in DEVICE_SETTING_KEYS})
    updated_at = stored.get(SETTINGS_UPDATED_AT_KEY)
    if updated_at is not None:
        merged[SETTINGS_UPDATED_AT_KEY] = updated_at
    return merged


def to_settings_response(device: Device) -> DeviceSettingsResponse:
    """ORM → 设置响应（类型收敛 + ``updatedAt`` 解析）。"""
    merged = read_device_settings(device)
    raw_updated = merged.get(SETTINGS_UPDATED_AT_KEY)
    updated_at: datetime | None = None
    if isinstance(raw_updated, str):
        try:
            updated_at = datetime.fromisoformat(raw_updated)
        except ValueError:
            # 脏数据不该让设置页整页报错：读不出来就当「未知写入时间」
            logger.warning("设备 %s 的 settings.updatedAt 无法解析：%r", device.id, raw_updated)
    return DeviceSettingsResponse(
        volume=int(merged["volume"]),
        child_mode=bool(merged["childMode"]),
        wake_word=str(merged["wakeWord"]),
        updated_at=updated_at,
    )


async def update_device_settings(
    session: AsyncSession,
    ctx: EndUserContext,
    device_id: str,
    *,
    updates: dict[str, Any],
) -> DeviceSettingsResponse:
    """更新设备设置（局部更新，未知键静默忽略）。

    Args:
        updates: **camelCase 键**的待更新字段（由路由从请求体按别名导出），
            未出现的键保持原值。

    **白名单校验在这里**（而不是表结构 / schema）：
    :data:`DEVICE_SETTING_KEYS` 是业务规则，会随型号与固件迭代；
    客户端可能比服务端新，多传一个服务端还不认识的键时，
    忽略比抛 400 更合适（保存其余字段仍然是用户想要的结果）。

    ``volume`` 的超范围在服务层再判一次：schema 层拦的是 HTTP 请求，
    服务层拦的是**所有**调用方（含将来可能的内部调用），
    这类「数值边界」一旦落库就会变成前端无法解释的脏数据。

    Raises:
        AppException: 设备不可见（404）或 ``volume`` 越界（400）。
    """
    device = await assert_device_owned(session, ctx, device_id)

    filtered = {key: value for key, value in updates.items() if key in DEVICE_SETTING_KEYS}
    volume = filtered.get("volume")
    if volume is not None and not (0 <= int(volume) <= 100):
        raise validation_error("音量必须在 0–100 之间", details={"field": "volume", "min": 0, "max": 100})

    if not filtered:
        # 全是未知键：不改任何东西，也不刷新 updatedAt——
        # 「什么也没改」不该在设置页显示出一个新的修改时间
        return to_settings_response(device)

    stored = dict(device.settings or {})
    stored.update(filtered)
    stored[SETTINGS_UPDATED_AT_KEY] = utcnow().isoformat()
    # 必须整体赋新字典：JSON 列的原地修改不会被 SQLAlchemy 检测到
    device.settings = stored
    await session.flush()
    await session.commit()
    return to_settings_response(device)


# ---------------------------------------------------------------------------
# 六、流量充值
# ---------------------------------------------------------------------------


def _ensure_four_g(device: Device) -> None:
    """校验设备是 4G（充值只对 4G 有意义）。

    Raises:
        AppException: 非 4G 设备（400）。
    """
    if str(device.network_type) != str(NetworkType.FOUR_G):
        raise validation_error(
            "只有 4G 设备需要流量充值",
            details={"networkType": device.network_type},
        )


def to_recharge_plan_response(plan: RechargePlan) -> RechargePlanResponse:
    """ORM → 套餐响应。"""
    return RechargePlanResponse(
        id=plan.id,
        code=plan.code,
        name=plan.name,
        description=plan.description,
        data_mb=plan.data_mb,
        valid_days=plan.valid_days,
        price=float(plan.price),
        is_recommended=bool(plan.is_recommended),
    )


def to_recharge_order_response(order: RechargeOrder) -> RechargeOrderResponse:
    """ORM → 充值订单响应（金额 / 流量取**订单快照**，不查套餐）。"""
    return RechargeOrderResponse(
        id=order.id,
        order_no=order.order_no,
        device_id=order.device_id,
        plan_id=order.plan_id,
        plan_name=order.plan_name,
        amount=float(order.amount),
        data_mb=order.data_mb,
        valid_days=order.valid_days,
        status=order.status,
        paid_at=order.paid_at,
        failed_reason=order.failed_reason,
        refunded_at=order.refunded_at,
        created_at=order.created_at,
    )


def generate_recharge_order_no(now: datetime | None = None) -> str:
    """生成充值订单号 ``RC-YYYYMMDD-XXXX``。

    与订单号 / 工单号 / 分配单号同一口径：日期 + 随机后缀（字符集剔除形近字符），
    而不是自增序号——自增需要取号表或序列，在 SQLite 与 PostgreSQL 上语义不同。
    """
    stamp = (now or utcnow()).strftime("%Y%m%d")
    suffix = "".join(secrets.choice(RECHARGE_ORDER_ALPHABET) for _ in range(RECHARGE_ORDER_SUFFIX_LENGTH))
    return f"RC-{stamp}-{suffix}"


async def _next_recharge_order_no(session: AsyncSession) -> str:
    """取一个未被占用的充值订单号（冲突重试 :data:`RECHARGE_ORDER_MAX_ATTEMPTS` 次）。"""
    for _ in range(RECHARGE_ORDER_MAX_ATTEMPTS):
        candidate = generate_recharge_order_no()
        exists = (
            await session.execute(
                select(RechargeOrder.id).where(RechargeOrder.order_no == candidate)
            )
        ).scalar_one_or_none()
        if exists is None:
            return candidate
    raise AppException(ErrorCode.INTERNAL_ERROR, "充值订单号生成失败，请重试")


async def list_recharge_plans(
    session: AsyncSession, ctx: EndUserContext, *, device_id: str
) -> RechargePlanListResult:
    """列出设备所属租户的可售套餐（**仅 4G**）。

    Wi-Fi 设备返回 ``supported=false`` + 说明文案 + 空数组，**不是错误**：
    「这台机器不需要流量充值」是一个正常结论，用 4xx 表达会逼前端
    把它写成异常分支，反而让真实错误淹没在异常处理里。

    ``records`` 只含 ``status=ENABLED`` 且 ``tenant_id`` 与设备一致的套餐，
    按 ``sort_order`` 升序——展示顺序是运营配置的结果，不该由前端排。

    Raises:
        AppException: 设备不可见（404）。
    """
    device = await assert_device_owned(session, ctx, device_id)
    if str(device.network_type) != str(NetworkType.FOUR_G):
        return RechargePlanListResult(supported=False, reason=WIFI_UNSUPPORTED_REASON, records=[])

    stmt = (
        select(RechargePlan)
        .where(RechargePlan.tenant_id == device.tenant_id)
        .where(RechargePlan.status == str(EnableStatus.ENABLED))
        .order_by(RechargePlan.sort_order.asc(), RechargePlan.created_at.asc())
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return RechargePlanListResult(
        supported=True,
        reason=None,
        records=[to_recharge_plan_response(plan) for plan in rows],
    )


async def create_recharge_order(
    session: AsyncSession,
    ctx: EndUserContext,
    *,
    device_id: str,
    plan_id: str,
    request: Request | None = None,
) -> RechargeOrderResponse:
    """充值下单（状态 ``PENDING``，金额与流量快照到订单）。

    下单时把 ``amount`` / ``data_mb`` / ``valid_days`` / ``plan_name``
    **快照**进订单：套餐后续改价不得改写历史订单，否则对账时「实付金额」
    会跟着套餐一起变，用户投诉时无法还原当时的成交条件。

    **同一设备存在 ``PENDING`` 订单时不拒绝**：这是刻意的取舍。
    用户「先下一单没付、再下一单」在真实场景里很常见（换了套餐、
    支付失败想重来），系统拒绝只会逼用户去翻未支付订单；
    而多张 ``PENDING`` 订单本身不产生任何资费影响——
    流量只在 ``PAID`` 后才生效（P9 的生效逻辑按 ``PAID`` 订单计算）。
    真正需要防的重复是「同一单被支付两次」，那由状态机挡住（见 :func:`pay_recharge_order`）。

    Raises:
        AppException: 设备不可见（404）/ 非 4G（400）/ 套餐不存在或已下架（404 / 400）。
    """
    device = await assert_device_owned(session, ctx, device_id)
    _ensure_four_g(device)

    plan = (
        await session.execute(select(RechargePlan).where(RechargePlan.id == plan_id))
    ).scalar_one_or_none()
    if plan is None or plan.tenant_id != device.tenant_id:
        # 「套餐不存在」与「套餐属于别的租户」统一按不存在处理：
        # 后者不该让小程序用户知道「另一个品牌的资费档位长什么样」
        raise not_found("套餐不存在")
    if str(plan.status) != str(EnableStatus.ENABLED):
        raise validation_error("该套餐已下架，请选择其它套餐", details={"planId": plan.id})

    order = RechargeOrder(
        id=new_id("recharge_order"),
        order_no=await _next_recharge_order_no(session),
        end_user_id=ctx.user_id,
        device_id=device.id,
        plan_id=plan.id,
        tenant_id=device.tenant_id,
        # 快照（见 docstring）
        amount=plan.price,
        data_mb=plan.data_mb,
        valid_days=plan.valid_days,
        plan_name=plan.name,
        status=str(RechargeOrderStatus.PENDING),
    )
    session.add(order)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.RECHARGE_ORDER,
        actor_account=ctx.phone,
        actor_role=END_USER_ACTOR_ROLE,
        tenant_id=device.tenant_id,
        resource_type="recharge_order",
        resource_id=order.id,
        summary=f"终端用户为设备 {device.sn} 下单充值「{plan.name}」（¥{float(plan.price):.2f}）",
        detail={
            "orderNo": order.order_no,
            "endUserId": ctx.user_id,
            "deviceId": device.id,
            "sn": device.sn,
            "planId": plan.id,
            "planCode": plan.code,
            "amount": float(order.amount),
            "dataMb": order.data_mb,
        },
        request=request,
    )
    await session.commit()
    logger.info("充值订单 %s 已创建（设备 %s，套餐 %s）", order.order_no, device.sn, plan.code)
    return to_recharge_order_response(order)


async def pay_recharge_order(
    session: AsyncSession,
    ctx: EndUserContext,
    order_id: str,
    *,
    request: Request | None = None,
) -> RechargePayResult:
    """支付充值订单（``mock`` 通道下直接置为已支付并标注 ``mock=true``）。

    只允许 ``PENDING → PAID``：重复支付（已 ``PAID``）与对 ``FAILED`` /
    ``REFUNDED`` 订单支付一律 409 ``INVALID_STATE_TRANSITION``。
    这条判断不能省——它是「同一单被扣两次钱」的最后一道闸，
    而支付是最不能靠前端自觉的地方。

    ``PAYMENT_PROVIDER=none`` 时 503 安全失败（ADR-07），不把订单改成任何状态。

    Raises:
        AppException: 订单不存在或不属于本人（404）/ 支付通道未接入（503）/
            状态不允许支付（409）。
    """
    order = (
        await session.execute(
            select(RechargeOrder)
            .where(RechargeOrder.id == order_id)
            .where(RechargeOrder.end_user_id == ctx.user_id)
        )
    ).scalar_one_or_none()
    if order is None:
        # 收口在每个端点自己的资源上：订单的归属看 ``end_user_id``，
        # 与设备端点看绑定关系同理（都是「是不是我的」而不是「属于哪个租户」）
        raise not_found("充值订单不存在")

    if settings.PAYMENT_PROVIDER == "none":
        raise vendor_unavailable("支付通道尚未接入，暂无法支付", vendor="payment")

    if str(order.status) != str(RechargeOrderStatus.PENDING):
        raise invalid_state_transition(
            f"订单当前状态为 {order.status}，不能支付",
            current=order.status,
            target=str(RechargeOrderStatus.PAID),
        )

    now = utcnow()
    order.status = str(RechargeOrderStatus.PAID)
    order.paid_at = now

    await audit_service.record(
        session,
        action=AuditAction.RECHARGE_ORDER,
        actor_account=ctx.phone,
        actor_role=END_USER_ACTOR_ROLE,
        tenant_id=order.tenant_id,
        resource_type="recharge_order",
        resource_id=order.id,
        summary=f"充值订单 {order.order_no} 支付成功（¥{float(order.amount):.2f}，模拟通道）",
        detail={
            "orderNo": order.order_no,
            "endUserId": ctx.user_id,
            "deviceId": order.device_id,
            "amount": float(order.amount),
            "paidAt": now.isoformat(),
        },
        request=request,
    )
    await session.commit()
    logger.info("充值订单 %s 已支付", order.order_no)
    # 当前只有 mock 一条可用通道（none 已在上面 503），因此这里恒为 True；
    # 接入真实支付后此处按通道取值，字段已预留（前端不判定支付真实性）
    return RechargePayResult(order=to_recharge_order_response(order), mock=True)


async def list_recharge_orders(
    session: AsyncSession,
    ctx: EndUserContext,
    *,
    device_id: str,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[RechargeOrderResponse], int]:
    """分页查询某台设备的充值记录（按创建时间倒序）。

    先过 ``assert_device_owned``：充值记录里含金额与套餐名，
    它属于「谁绑了这台设备」的延伸信息，同样只能给设备主人看。

    Raises:
        AppException: 设备不可见（404）。
    """
    device = await assert_device_owned(session, ctx, device_id)
    conditions = [
        RechargeOrder.end_user_id == ctx.user_id,
        RechargeOrder.device_id == device.id,
    ]
    total = int(
        (
            await session.execute(
                select(func.count()).select_from(RechargeOrder).where(*conditions)
            )
        ).scalar_one()
    )
    stmt = (
        select(RechargeOrder)
        .where(*conditions)
        .order_by(RechargeOrder.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return [to_recharge_order_response(order) for order in rows], total


__all__ = [
    "ACTIVATION_DIMENSION",
    "DEFAULT_DEVICE_SETTINGS",
    "DEFAULT_UNBIND_REASON",
    "DEVICE_SETTING_KEYS",
    "END_USER_ACTOR_ROLE",
    "EVENT_ACTIVATED",
    "EVENT_ACTIVATING",
    "EVENT_ACTIVATION_FAILED",
    "EVENT_BIND_FAILED",
    "NETWORK_LABELS",
    "REASON_BOUND_BY_OTHER",
    "REASON_BOUND_TO_SELF",
    "REASON_BOUND_TO_SELF_ACTIVATED",
    "REASON_FROZEN",
    "REASON_NOT_ALLOCATED",
    "REASON_PRODUCT_UNAUTHORIZED",
    "REASON_RETIRED",
    "RECHARGE_ORDER_ALPHABET",
    "SETTINGS_UPDATED_AT_KEY",
    "WIFI_UNSUPPORTED_REASON",
    "activate_4g",
    "activate_wifi",
    "assert_device_owned",
    "build_device_detail",
    "create_recharge_order",
    "ensure_vendor_capability",
    "generate_recharge_order_no",
    "get_binding",
    "get_end_user",
    "get_profile",
    "list_recharge_orders",
    "list_recharge_plans",
    "list_user_devices",
    "login",
    "network_label",
    "pay_recharge_order",
    "read_device_settings",
    "resolve_device_provider",
    "resolve_scan",
    "send_login_code",
    "to_device_brief",
    "to_recharge_order_response",
    "to_recharge_plan_response",
    "to_settings_response",
    "unbind_device",
    "update_device_settings",
]
