"""绑定服务：扫码预检 → 确认绑定 → 解绑 → 绑定记录查询。

两步绑定流程（``precheck`` → ``bind``）
======================================
为什么不是一步到位？因为扫码结果**不可信**：用户完全可能扫了隔壁那台的码。
因此先预检并回显「你扫到的是 SN-xxx / 星辰小方块」，让人确认；
同时签发一张**短时**（``QR_CONFIRM_TOKEN_TTL_SECONDS``，默认 300 秒）
**用一次即销毁**的确认令牌——「扫了码但没点确认」不会占住设备的绑定名额。

令牌的落库方式
--------------
``device_bindings.confirm_token_hash`` 只存 SHA-256 摘要，明文只在
``precheck`` 响应里出现一次；``bind`` 成功后把摘要**置空**（销毁），
因此同一张令牌不可能被用第二次。与刷新令牌、设备凭证同一处理方式。

单绑约束
--------
``device_bindings.device_id`` 上有唯一索引——「一台设备只能绑一个终端用户」
由**数据库**硬保证，不依赖服务层的「先查后写」（那种写法在并发下必然漏）。
服务层的检查只负责给出**可读的错误**（带上原绑定的 ID / 时间 / 终端用户）。

幂等与令牌销毁的顺序陷阱
------------------------
``bind`` 支持 ``Idempotency-Key``，且回放必须发生在**令牌校验之前**：
令牌在首次绑定成功时就已销毁，若先校验令牌再查幂等快照，
同一个键的第二次请求会拿到 ``QR_EXPIRED``——那就不是「重放返回同一条」了。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from fastapi import Request
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import AuthContext
from app.core.errors import (
    AppException,
    ErrorCode,
    device_frozen,
    device_not_available,
    device_not_found,
    device_not_in_tenant,
    product_not_authorized,
    qr_expired,
    qr_invalid,
)
from app.core.ids import new_id
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.scope import scoped
from app.models.allocation import DeviceBinding
from app.models.catalog import ClientProduct
from app.models.device import Device
from app.models.enums import (
    AssetStatus,
    AuditAction,
    BindingRecordStatus,
    BindStatus,
    EnableStatus,
)
from app.models.identity import Tenant
from app.schemas.binding import BindingPrecheckResponse, BindingResponse
from app.services import allocation_service, audit_service, device_service, qrcode_service

logger = get_logger(__name__)

#: 绑定记录状态 → 中文展示名。
#:
#: 为什么不在 ``app.models.enums`` 里定义？枚举模块已冻结（P5 期间由主会话
#: 维护），而这条映射只服务于绑定响应，放在使用它的模块里更贴近职责。
BINDING_STATUS_LABELS: dict[BindingRecordStatus, str] = {
    BindingRecordStatus.PENDING: "待确认",
    BindingRecordStatus.BOUND: "已绑定",
    BindingRecordStatus.UNBOUND: "已解绑",
}


# ---------------------------------------------------------------------------
# 摘要
# ---------------------------------------------------------------------------


def digest(value: str) -> str:
    """计算明文（令牌 / 密钥）的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def payload_hash(text: str) -> str:
    """计算二维码载荷摘要。

    比较前先 ``strip``：二维码扫描器给出的文本常带换行，
    而生成时不可能带——不归一化就会出现「内容看着一样却校验不过」。
    与 ``qrcode_service.parse_payload`` 的归一化口径保持一致。

    同时把**前缀段**归一为大写：``parse_payload`` 用 ``.upper()`` 识别前缀
    （见 ``QrFormat`` 的格式判别，P4 的 ``tests/unit/test_qrcode.py`` 已把这一
    宽容行为钉死），若这里按字节严格比较，``jd|…`` 就会先被判为「格式合法」、
    再被判为「伪造」，同一份输入得到两个互相矛盾的结论，报错文案也会误导
    排查。前缀只是一个**格式标签**、不承载任何安全语义（租户 / 产品 / SN /
    签名都在其后各段），因此只归一前缀、其余字节仍严格比对。
    """
    raw = (text or "").strip()
    segments = raw.split(qrcode_service.SEPARATOR)
    if len(segments) > 1:
        head = segments[0].strip().upper()
        if head in {qrcode_service.JX_PREFIX, qrcode_service.JD_PREFIX}:
            segments[0] = head
            raw = qrcode_service.SEPARATOR.join(segments)
    return digest(raw)


# ---------------------------------------------------------------------------
# 转换与查询
# ---------------------------------------------------------------------------


async def binding_to_response(
    session: AsyncSession, binding: DeviceBinding, device: Device | None = None
) -> BindingResponse:
    """ORM → 绑定记录响应（附设备侧快照）。

    设备侧快照（``sn`` / ``deviceLabel`` / ``assetStatus`` / ``bindStatus`` /
    ``networkType``）随绑定记录一起返回，列表页因此可以自解释，
    不必让前端再按 ``deviceId`` 逐台回查（那会变成 N+1 个请求）。

    Args:
        session: 数据库会话。
        binding: 绑定记录。
        device: 已加载的设备对象；列表场景由调用方批量取好传入，避免逐行查询。
    """
    response = BindingResponse.model_validate(binding)
    response.status_label = BINDING_STATUS_LABELS.get(
        BindingRecordStatus(binding.status), binding.status
    )

    if device is None:
        device = (
            await session.execute(select(Device).where(Device.id == binding.device_id))
        ).scalar_one_or_none()
    if device is not None:
        response.sn = device.sn
        response.device_label = device_service.device_label(device)
        response.asset_status = device.asset_status
        response.bind_status = device.bind_status
        response.network_type = device.network_type
    return response


async def get_binding_by_device(session: AsyncSession, device_id: str) -> DeviceBinding | None:
    """按设备取绑定记录（``device_id`` 唯一，故至多一行）。"""
    return (
        await session.execute(
            select(DeviceBinding).where(DeviceBinding.device_id == device_id)
        )
    ).scalar_one_or_none()


async def list_bindings(
    session: AsyncSession,
    auth: AuthContext,
    *,
    offset: int = 0,
    limit: int = 20,
    status: str | None = None,
    device_id: str | None = None,
    keyword: str | None = None,
) -> tuple[list[BindingResponse], int]:
    """分页查询绑定记录（租户过滤**必须**经 :func:`scoped`，ADR-08）。

    ``keyword`` 匹配设备 SN：``device_bindings`` 上没有 SN 列，
    用一条 ``IN (SELECT id FROM devices WHERE sn LIKE ...)`` 子查询完成，
    而不是 join 后再 ``DISTINCT``（join 会把行数与总数都弄乱）。
    """
    conditions: list[ColumnElement[bool]] = []
    if status:
        conditions.append(DeviceBinding.status == status)
    if device_id:
        conditions.append(DeviceBinding.device_id == device_id)
    if keyword:
        pattern = f"%{keyword.strip()}%"
        conditions.append(
            DeviceBinding.device_id.in_(select(Device.id).where(Device.sn.like(pattern)))
        )

    total = int(
        (
            await session.execute(
                scoped(
                    select(func.count()).select_from(DeviceBinding), DeviceBinding, auth
                ).where(*conditions)
            )
        ).scalar_one()
    )
    stmt = (
        scoped(select(DeviceBinding), DeviceBinding, auth)
        .where(*conditions)
        .order_by(DeviceBinding.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())

    devices: dict[str, Device] = {}
    device_ids = [row.device_id for row in rows]
    if device_ids:
        found = (
            await session.execute(select(Device).where(Device.id.in_(device_ids)))
        ).scalars()
        devices = {item.id: item for item in found}
    return [
        await binding_to_response(session, row, device=devices.get(row.device_id))
        for row in rows
    ], total


# ---------------------------------------------------------------------------
# 校验辅助
# ---------------------------------------------------------------------------


def _already_bound_error(device: Device, binding: DeviceBinding | None) -> AppException:
    """构造「设备已被绑定」异常。

    带上原绑定的 ID / 时间 / 终端用户，是因为这句提示最常见的用途是
    **客服核对**（「这台是不是我上个月绑过的那台」）。只回一句
    「设备已被绑定」会让人只能靠猜。

    ``device_already_bound()`` 便捷构造器不接受 ``details``，
    这里用基类显式传（错误码仍是既有的 ``DEVICE_ALREADY_BOUND``，未新增码）。
    """
    details = None
    if binding is not None:
        details = {
            "bindingId": binding.id,
            "boundAt": binding.bound_at.isoformat() if binding.bound_at else None,
            "endUserId": binding.end_user_id,
        }
    return AppException(
        ErrorCode.DEVICE_ALREADY_BOUND,
        f"设备 {device.sn} 已被绑定，请先解绑后再重新绑定",
        details=details,
    )


async def _ensure_product_authorized(session: AsyncSession, device: Device) -> ClientProduct | None:
    """校验设备所挂的客户产品仍对该租户有效。

    与分配单的口径完全一致（授权行 ``ENABLED`` 且未过期），
    因此复用 :func:`app.services.allocation_service.is_product_authorized`——
    两处各写一遍判断，迟早出现「分配放行、绑定拦截」这类自相矛盾。

    Raises:
        AppException: 设备未关联产品、产品已停用或未授权（``PRODUCT_NOT_AUTHORIZED``）。
    """
    if not device.client_product_id:
        raise product_not_authorized("设备未关联客户产品，无法绑定")
    product = (
        await session.execute(
            select(ClientProduct).where(ClientProduct.id == device.client_product_id)
        )
    ).scalar_one_or_none()
    if product is None:
        raise product_not_authorized("设备关联的客户产品不存在")
    if str(product.status) != str(EnableStatus.ENABLED):
        raise product_not_authorized(f"客户产品「{product.name}」已停用，无法绑定设备")
    if not await allocation_service.is_product_authorized(
        session, tenant_id=product.tenant_id, template_id=product.template_id
    ):
        raise product_not_authorized(
            f"客户产品「{product.name}」所属模板对该租户未授权或授权已过期"
        )
    return product


async def _tenant_name(session: AsyncSession, tenant_id: str) -> str | None:
    """取租户名（供预检回显「扫到的设备属于谁」）。"""
    name = (
        await session.execute(select(Tenant.name).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    return str(name) if name else None


# ---------------------------------------------------------------------------
# 预检
# ---------------------------------------------------------------------------


async def precheck(
    session: AsyncSession,
    auth: AuthContext,
    *,
    qr_payload: str,
) -> BindingPrecheckResponse:
    """扫码预检：判定能不能绑、绑给谁，并签发一次性确认令牌。

    判定顺序就是返回码的优先级（顺序不能随意调换）：

    1. 二维码格式 / 签名（``QR_INVALID``）——连格式都不对，后面都无从谈起；
    2. 设备存在（``DEVICE_NOT_FOUND``）——「码对了但设备不在库里」多半是伪造；
    3. **SHA-256 命中**——用设备记录重算规范载荷并比对摘要，挡住「合法格式
       但字段被改过」的码（JX 格式没有签名，这一步是它唯一的防篡改手段）；
    4. 租户匹配（``DEVICE_NOT_IN_TENANT``）——京东码自带租户，且设备必须有归属；
    5. 冻结拦截（``DEVICE_FROZEN``）；
    6. 单绑约束（``DEVICE_ALREADY_BOUND``，带原绑定信息）；
    7. 可绑状态（只有 ``ALLOCATED`` 能绑，否则 ``DEVICE_NOT_AVAILABLE``）；
    8. 产品授权（``PRODUCT_NOT_AUTHORIZED``）。

    为什么 7 之前先判 6？设备已绑定时，用户真正需要知道的是「它已经绑过了、
    绑给谁了」，而不是「它当前状态不是 ALLOCATED」——后者是前者的结果。

    预检成功会把绑定行置为 ``PENDING`` 并写入令牌摘要。**不写审计**：
    扫码只是「准备动作」，尚未产生任何业务事实；绑定 / 解绑才有审计
    （``BIND_DEVICE`` / ``UNBIND_DEVICE``）。绑定行本身就是预检的留痕。

    Raises:
        AppException: 见上表。
    """
    parsed = qrcode_service.parse_payload(qr_payload)

    device = (
        await session.execute(select(Device).where(Device.sn == parsed.sn))
    ).scalar_one_or_none()
    if device is None:
        raise device_not_found("设备不存在")

    # ---- SHA-256 命中：用设备记录重算规范载荷，与提交载荷比对 ----
    if parsed.format is qrcode_service.QrFormat.JX:
        canonical = qrcode_service.build_jx_payload(
            device.sn, device.imei, device.iccid, device.vendor_device_id
        )
    else:
        if not device.tenant_id or not device.client_product_id:
            raise qr_invalid("设备尚未关联租户或客户产品，无法生成京东格式二维码")
        canonical = qrcode_service.build_jd_payload(
            device.tenant_id, device.client_product_id, device.sn
        )
    if payload_hash(canonical) != payload_hash(parsed.text):
        raise qr_invalid("二维码内容与设备记录不一致，可能是伪造或已过期")

    tenant_id = auth.require_tenant_id()

    # ---- 租户匹配 ----
    if parsed.format is qrcode_service.QrFormat.JD and parsed.tenant_id != tenant_id:
        raise device_not_in_tenant("二维码不属于当前租户")
    if device.tenant_id is None:
        raise device_not_in_tenant("该设备尚未分配给任何租户，请先由平台分配")
    if device.tenant_id != tenant_id:
        raise device_not_in_tenant()

    # ---- 冻结拦截 ----
    if str(device.asset_status) == str(AssetStatus.FROZEN):
        raise device_frozen(f"设备 {device.sn} 已冻结，无法绑定")

    binding = await get_binding_by_device(session, device.id)

    # ---- 单绑约束 ----
    if str(device.bind_status) == str(BindStatus.BOUND) or (
        binding is not None and binding.status == str(BindingRecordStatus.BOUND)
    ):
        raise _already_bound_error(device, binding)

    # ---- 可绑状态：只有「已分配给租户」的设备才谈得上绑定 ----
    if str(device.asset_status) != str(AssetStatus.ALLOCATED):
        raise device_not_available(
            f"设备当前状态为 {device.asset_status}，只有已分配给租户的设备才能绑定"
        )

    # ---- 产品授权 ----
    product = await _ensure_product_authorized(session, device)

    token = secrets.token_urlsafe(32)
    now = utcnow()
    expires_at = now + timedelta(seconds=settings.QR_CONFIRM_TOKEN_TTL_SECONDS)

    if binding is None:
        binding = DeviceBinding(
            id=new_id("device_binding"),
            device_id=device.id,
            tenant_id=tenant_id,
            status=str(BindingRecordStatus.PENDING),
            bind_count=0,
        )
        session.add(binding)

    # 复用同一行（一台设备恒一行）：重新扫码即重置为 PENDING 并签发新令牌，
    # 旧令牌因摘要被覆盖而立即失效。
    binding.tenant_id = tenant_id
    binding.client_product_id = device.client_product_id
    binding.status = str(BindingRecordStatus.PENDING)
    binding.qr_format = str(parsed.format)
    # 只存载荷摘要，不存明文载荷：库里存着完整二维码文本，等于把
    # 「可复制的绑定凭据」复制了一份（京东码含 HMAC 签名）。
    binding.qr_payload_hash = payload_hash(parsed.text)
    binding.confirm_token_hash = digest(token)
    binding.confirm_expires_at = expires_at
    binding.confirmed_at = None
    await session.flush()
    await session.commit()

    logger.info("设备 %s 预检通过，已签发确认令牌（格式 %s）", device.sn, parsed.format)
    return BindingPrecheckResponse(
        binding_id=binding.id,
        device_id=device.id,
        sn=device.sn,
        network_type=device.network_type,
        device_label=device_service.device_label(device),
        asset_status=device.asset_status,
        tenant_id=tenant_id,
        tenant_name=await _tenant_name(session, tenant_id),
        client_product_id=device.client_product_id,
        client_product_name=product.name if product is not None else None,
        qr_format=str(parsed.format),
        confirm_token=token,
        expires_at=expires_at,
        expires_in_seconds=settings.QR_CONFIRM_TOKEN_TTL_SECONDS,
    )


# ---------------------------------------------------------------------------
# 绑定 / 解绑
# ---------------------------------------------------------------------------


async def bind(
    session: AsyncSession,
    auth: AuthContext,
    *,
    device_id: str,
    confirm_token: str,
    end_user_id: str | None = None,
    remark: str | None = None,
    request: Request | None = None,
) -> BindingResponse:
    """确认绑定（校验顺序见模块 docstring 与 ``precheck`` 的对应说明）。

    校验顺序：设备存在且属于本租户（越权一律「不存在」）→ 冻结 →
    单绑约束 → 绑定行必须处于 ``PENDING``（否则 ``QR_EXPIRED``）→
    令牌未过期 → 令牌摘要匹配（``QR_INVALID``）→ 产品授权。

    Raises:
        AppException: ``DEVICE_FROZEN`` / ``DEVICE_ALREADY_BOUND`` / ``QR_EXPIRED`` /
            ``QR_INVALID`` / ``PRODUCT_NOT_AUTHORIZED``。
    """
    device = await device_service.get_device(session, auth, device_id)
    tenant_id = auth.require_tenant_id()

    if str(device.asset_status) == str(AssetStatus.FROZEN):
        raise device_frozen(f"设备 {device.sn} 已冻结，无法绑定")

    binding = await get_binding_by_device(session, device.id)
    if str(device.bind_status) == str(BindStatus.BOUND) or (
        binding is not None and binding.status == str(BindingRecordStatus.BOUND)
    ):
        raise _already_bound_error(device, binding)

    if binding is None or binding.status != str(BindingRecordStatus.PENDING):
        raise qr_expired("确认令牌已过期或已被使用，请重新扫码")
    if binding.confirm_expires_at is None or binding.confirm_expires_at < utcnow():
        raise qr_expired()

    stored_hash = binding.confirm_token_hash or ""
    # 常量时间比较：摘要是令牌派生的，用 == 比较会泄漏前缀匹配长度
    if not hmac.compare_digest(digest(confirm_token), stored_hash):
        raise qr_invalid("确认令牌无效")

    await _ensure_product_authorized(session, device)

    now = utcnow()
    # 令牌销毁：摘要置空后，同一张令牌再也匹配不上任何提交
    binding.confirm_token_hash = None
    binding.confirm_expires_at = None
    binding.status = str(BindingRecordStatus.BOUND)
    binding.tenant_id = tenant_id
    binding.confirmed_at = now
    binding.bound_at = now
    binding.bound_by = auth.account
    binding.bind_count = (binding.bind_count or 0) + 1
    binding.end_user_id = end_user_id
    binding.remark = remark

    device.bind_status = str(BindStatus.BOUND)
    device.bound_at = now
    # 资产状态 ALLOCATED → BOUND（迁移表允许）。事件类型与维度都显式声明：
    # 事件说的是「绑定」这件事（``BOUND`` / ``bind``），而不是抽象的资产变化——
    # 时间线按维度分组时，绑定记录才会落在绑定维度里。
    await device_service.transition_asset(
        session,
        device,
        AssetStatus.BOUND,
        actor=auth,
        request=request,
        event_type=device_service.EVENT_BOUND,
        dimension="bind",
    )
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.BIND_DEVICE,
        tenant_id=tenant_id,
        actor=auth,
        resource_type="device_binding",
        resource_id=binding.id,
        summary=f"绑定设备 {device.sn}（第 {binding.bind_count} 次绑定）",
        detail={
            "deviceId": device.id,
            "sn": device.sn,
            "tenantId": tenant_id,
            "clientProductId": binding.client_product_id,
            "endUserId": binding.end_user_id,
            "bindCount": binding.bind_count,
            "qrFormat": binding.qr_format,
        },
        request=request,
    )
    await session.commit()
    logger.info("设备 %s 绑定成功（第 %d 次）", device.sn, binding.bind_count)
    return await binding_to_response(session, binding, device=device)


async def unbind(
    session: AsyncSession,
    auth: AuthContext,
    *,
    device_id: str,
    reason: str,
    request: Request | None = None,
) -> BindingResponse:
    """解绑设备（原因必填，与冻结 / 报废同一口径）。

    解绑把设备退回 ``ALLOCATED``——**不是**退回 ``IN_STOCK``：
    设备仍然属于该租户，只是不再绑定任何终端用户。退回到平台库存
    会让人以为「这台又能分配给别的租户了」，那是两回事。

    Raises:
        AppException: 设备不可见（``RESOURCE_NOT_FOUND``）或当前未绑定
            （``DEVICE_NOT_AVAILABLE``）。
    """
    device = await device_service.get_device(session, auth, device_id)
    tenant_id = auth.require_tenant_id()

    binding = await get_binding_by_device(session, device.id)
    if binding is None or binding.status != str(BindingRecordStatus.BOUND):
        raise device_not_available("该设备当前未绑定，无法解绑")

    now = utcnow()
    binding.status = str(BindingRecordStatus.UNBOUND)
    binding.unbound_at = now
    binding.unbind_reason = reason
    binding.unbound_by = auth.account

    device.bind_status = str(BindStatus.UNBOUND)
    # 资产状态 BOUND → ALLOCATED（迁移表允许）。事件类型声明为 ``UNBOUND``、
    # 维度为 ``bind``：若用默认值，时间线里会多出一条让人费解的
    # 「ALLOCATED」（解绑怎么会是「已分配」？）。
    await device_service.transition_asset(
        session,
        device,
        AssetStatus.ALLOCATED,
        reason=reason,
        actor=auth,
        request=request,
        event_type=device_service.EVENT_UNBOUND,
        dimension="bind",
    )
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.UNBIND_DEVICE,
        tenant_id=tenant_id,
        actor=auth,
        resource_type="device_binding",
        resource_id=binding.id,
        summary=f"解绑设备 {device.sn}：{reason}",
        detail={
            "deviceId": device.id,
            "sn": device.sn,
            "tenantId": tenant_id,
            "endUserId": binding.end_user_id,
            "reason": reason,
        },
        request=request,
    )
    await session.commit()
    logger.info("设备 %s 已解绑：%s", device.sn, reason)
    return await binding_to_response(session, binding, device=device)


__all__ = [
    "BINDING_STATUS_LABELS",
    "bind",
    "binding_to_response",
    "digest",
    "get_binding_by_device",
    "list_bindings",
    "payload_hash",
    "precheck",
    "unbind",
]
