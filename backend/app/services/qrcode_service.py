"""设备二维码生成与解析（P4 关键模块）。

为什么这一层必须是**纯函数**
============================
二维码是**跨端契约**：服务端生成、前端展示、设备端（固件）解析，
三方必须对「字段顺序 / 分隔符 / 签名算法」的理解完全一致。
把它写成不碰数据库、不碰网络的纯函数，收益有三：

1. 可以在单元测试里逐字段钉死格式（``tests/unit/test_qrcode.py``）；
2. 前端与固件同学可以直接读本模块的 docstring 当作协议文档；
3. 以后新增厂商格式（``VOLCANO`` 等）只需新增一对 build/parse 函数，
   不影响既有格式。

两种格式
========

集贤 4G（``JX``）::

    JX|{SN}|{IMEI}|{ICCID}|{deviceId}

* 共 5 段，以 ``|`` 分隔；设备 ID 由集贤云端生成后**回填**，
  因此本地首次生成时第 5 段可能为空（``JX|SN|IMEI|ICCID|``）——
  这是合法的中间态，解析时空串一律归一为 ``None``。

京东云 JoyInside Wi-Fi（``JD``）::

    JD|{tenant_id}|{product_id}|{sn}|{sign}

* 也是 5 段：前缀 + **4 个字段**（租户、产品、SN、签名）。
* ``sign = HMAC-SHA256(签名内容, settings.QR_SIGN_SECRET)`` 的**小写十六进制**。
* **签名内容**（本项目的规范定义，前后端与设备端以此为准）::

      f"{tenant_id}|{product_id}|{sn}"

  即「去掉前缀、去掉签名本身，其余三段按原顺序用 ``|`` 拼接」。
  之所以这样定：设备端只需把收到的串按 ``|`` 切开、丢掉第 1 段与最后 1 段，
  得到的就是签名内容，**不需要记住字段名与顺序**——顺序错了签名自然验不过。

修复原型缺陷（附录 B / todolist P4）
====================================
早期参考实现把 ``clientId``（客户/产品 ID）
误传进了 ``tenant_id`` 的位置，导致二维码里的租户与产品整体错位。
本项目在**签名层面**堵死这类错误：签名内容包含全部三个字段，
顺序一旦写错，校验必然失败。:func:`build_jd_payload` 的参数顺序为
``(tenant_id, product_id, sn)``，并由单元测试钉死「第 2 段 == tenant_id」。
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.core.config import settings
from app.core.errors import qr_invalid
from app.models.enums import NetworkType

#: 字段分隔符
SEPARATOR = "|"

#: 集贤 4G 前缀
JX_PREFIX = "JX"
#: 京东 Wi-Fi 前缀
JD_PREFIX = "JD"

#: 集贤格式段数（含前缀）
JX_SEGMENTS = 5
#: 京东格式段数（含前缀）。前缀之外的 4 个字段：租户、产品、SN、签名
JD_SEGMENTS = 5
#: 京东格式的签名字段个数（不含前缀）
JD_PAYLOAD_FIELDS = 4

#: 签名算法标识（写进接口文档与前端提示）
SIGN_ALGORITHM = "HMAC-SHA256"


class QrFormat(StrEnum):
    """二维码格式。"""

    JX = "JX"
    JD = "JD"


@dataclass(slots=True)
class ParsedQr:
    """解析结果。

    刻意用 ``dataclass`` 而非 Pydantic 模型：解析函数是纯逻辑，
    不应把「HTTP 响应形状」的约束带进协议层。

    Attributes:
        format: 识别出的格式。
        sn: 设备序列号（两种格式都有）。
        text: 原始文本（便于排查与回显）。
        tenant_id: 租户 ID（仅 JD）。
        product_id: 客户产品 ID（仅 JD）。
        imei: IMEI（仅 JX，可能为空）。
        iccid: ICCID（仅 JX，可能为空）。
        vendor_device_id: 厂商侧设备 ID（仅 JX，可能为空）。
    """

    format: QrFormat
    sn: str
    text: str
    tenant_id: str | None = None
    product_id: str | None = None
    imei: str | None = None
    iccid: str | None = None
    vendor_device_id: str | None = None


@dataclass(slots=True)
class QrCodeItem:
    """批量导出用的一条二维码记录。"""

    device_id: str
    sn: str
    network_type: str
    payload: str
    format: QrFormat


def format_of_network(network_type: str | NetworkType | None) -> QrFormat:
    """按联网方式选择二维码格式。

    ``4G`` → 集贤 ``JX``；其余（``WIFI`` / 未知）→ 京东 ``JD``。
    默认取 ``JD`` 而不是抛错：Wi-Fi 是本项目的默认方案，
    未知值落到默认分支比让整批导出失败更有用（真正的问题会在签名校验处暴露）。
    """
    if str(network_type or "") == str(NetworkType.FOUR_G):
        return QrFormat.JX
    return QrFormat.JD


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def _require(value: str | None, field: str) -> str:
    """校验必填字段并去除首尾空白。"""
    cleaned = (value or "").strip()
    if not cleaned:
        raise qr_invalid(f"生成二维码失败：{field} 不能为空")
    return cleaned


def _optional(value: str | None) -> str:
    """可选字段归一为字符串（``None`` → 空串，保持段数稳定）。"""
    return (value or "").strip()


def build_jx_payload(
    sn: str,
    imei: str | None = None,
    iccid: str | None = None,
    device_id: str | None = None,
) -> str:
    """生成集贤 4G 二维码文本。

    Args:
        sn: 设备序列号（必填）。
        imei: IMEI，4G 模组标识；厂商生成后回填，本地生成时可为空。
        iccid: SIM 卡号；同上。
        device_id: 厂商侧设备 ID；同上。

    Returns:
        ``JX|{sn}|{imei}|{iccid}|{device_id}``。

    Raises:
        AppException: ``sn`` 为空（``QR_INVALID``）。

    Note:
        可选字段为空时**保留空段**而非省略，保证段数恒为 5——
        设备端解析器因此可以用固定下标取值。
    """
    return SEPARATOR.join(
        [JX_PREFIX, _require(sn, "SN"), _optional(imei), _optional(iccid), _optional(device_id)]
    )


def jd_sign_content(tenant_id: str, product_id: str, sn: str) -> str:
    """返回京东格式的签名内容（本项目协议规范）。

    规范：``f"{tenant_id}|{product_id}|{sn}"``，即去掉前缀与签名后按原顺序拼接。
    """
    return SEPARATOR.join([tenant_id, product_id, sn])


def sign_jd(tenant_id: str, product_id: str, sn: str) -> str:
    """计算京东格式签名：``HMAC-SHA256(签名内容, QR_SIGN_SECRET)`` 小写十六进制。

    密钥来自 ``settings.QR_SIGN_SECRET``——集中配置在服务端，
    前端与设备端只持有校验所需的同一密钥（演示环境下可下发，生产应放设备侧安全存储）。
    """
    secret = settings.QR_SIGN_SECRET.encode("utf-8")
    content = jd_sign_content(tenant_id, product_id, sn).encode("utf-8")
    return hmac.new(secret, content, hashlib.sha256).hexdigest()


def build_jd_payload(tenant_id: str, product_id: str, sn: str) -> str:
    """生成京东 Wi-Fi 二维码文本。

    ★ 参数顺序是**契约的一部分**：``(tenant_id, product_id, sn)``。

    参考实现 ``data.js:595`` 正是因为把 ``clientId`` 传进了 ``tenant_id``
    的位置，导致整条二维码字段错位。为了让这类错误无法悄悄溜过，
    本函数：

    1. 参数名即位置语义，传参错位在阅读时即可发现；
    2. 签名内容含全部三个字段（见 :func:`jd_sign_content`），
       顺序错 → 签名验不过 → 设备端拒绝，而不是「看起来正常但绑错产品」。

    Args:
        tenant_id: 租户 ID（**第 1 个字段，不是产品 ID**）。
        product_id: 客户产品 ID。
        sn: 设备序列号。

    Returns:
        ``JD|{tenant_id}|{product_id}|{sn}|{sign}``。

    Raises:
        AppException: 任一字段为空（``QR_INVALID``）。
    """
    tenant = _require(tenant_id, "tenant_id")
    product = _require(product_id, "product_id")
    serial = _require(sn, "sn")
    return SEPARATOR.join([JD_PREFIX, tenant, product, serial, sign_jd(tenant, product, serial)])


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def parse_payload(text: str) -> ParsedQr:
    """解析二维码文本。

    支持 :func:`build_jx_payload` 与 :func:`build_jd_payload` 生成的两种格式。
    京东格式**会校验签名**，篡改任一字段（含顺序错位）都会失败。

    Args:
        text: 二维码原始文本（允许含首尾空白与换行）。

    Returns:
        解析结果。

    Raises:
        AppException: 前缀未知、段数不符、必填字段为空或签名不匹配（``QR_INVALID``）。
    """
    raw = (text or "").strip()
    if not raw:
        raise qr_invalid("二维码内容为空")

    segments = raw.split(SEPARATOR)
    prefix = segments[0].strip().upper()

    if prefix == JX_PREFIX:
        return _parse_jx(raw, segments)
    if prefix == JD_PREFIX:
        return _parse_jd(raw, segments)

    raise qr_invalid(f"无法识别的二维码前缀：{segments[0][:16] or '空'}")


def _parse_jx(raw: str, segments: list[str]) -> ParsedQr:
    """解析集贤 4G 格式（不做签名——该格式由厂商云端签发与校验）。"""
    if len(segments) != JX_SEGMENTS:
        raise qr_invalid(f"集贤二维码字段数应为 {JX_SEGMENTS}，实际 {len(segments)}")
    sn = segments[1].strip()
    if not sn:
        raise qr_invalid("集贤二维码缺少 SN")
    return ParsedQr(
        format=QrFormat.JX,
        sn=sn,
        text=raw,
        imei=segments[2].strip() or None,
        iccid=segments[3].strip() or None,
        vendor_device_id=segments[4].strip() or None,
    )


def _parse_jd(raw: str, segments: list[str]) -> ParsedQr:
    """解析京东 Wi-Fi 格式并校验签名。"""
    if len(segments) != JD_SEGMENTS:
        raise qr_invalid(f"京东二维码字段数应为 {JD_SEGMENTS}（含前缀），实际 {len(segments)}")

    tenant_id = segments[1].strip()
    product_id = segments[2].strip()
    sn = segments[3].strip()
    provided = segments[4].strip().lower()

    if not tenant_id:
        raise qr_invalid("京东二维码第 2 段（tenant_id）为空")
    if not product_id:
        raise qr_invalid("京东二维码第 3 段（product_id）为空")
    if not sn:
        raise qr_invalid("京东二维码第 4 段（sn）为空")
    if not provided:
        raise qr_invalid("京东二维码缺少签名")

    expected = sign_jd(tenant_id, product_id, sn)
    # 常量时间比较：签名是密钥派生的，用 == 比较会泄漏前缀匹配长度
    if not hmac.compare_digest(expected, provided):
        raise qr_invalid("京东二维码签名校验失败（内容可能被篡改或字段顺序有误）")

    return ParsedQr(
        format=QrFormat.JD,
        sn=sn,
        text=raw,
        tenant_id=tenant_id,
        product_id=product_id,
    )


# ---------------------------------------------------------------------------
# 批量导出
# ---------------------------------------------------------------------------


def build_qrcodes(devices: Sequence[Any]) -> list[QrCodeItem]:
    """为一批设备生成二维码文本（供 ``GET /platform/orders/{id}/qrcodes``）。

    入参为 ORM ``Device`` 对象序列（只读取属性，不触发懒加载）。
    京东格式需要 ``tenant_id``；平台库存设备（``tenant_id`` 为空）
    无法生成京东二维码，此时抛 ``QR_INVALID`` 并说明原因——
    导出中途静默跳过会让人以为「设备丢了」。

    Args:
        devices: 设备序列。

    Returns:
        与入参等长的二维码记录列表（顺序保持一致，便于前端与 SN 列表对账）。

    Raises:
        AppException: 设备无法生成二维码（缺 SN / 京东格式缺租户）。
    """
    items: list[QrCodeItem] = []
    for device in devices:
        fmt = format_of_network(getattr(device, "network_type", None))
        sn = _require(getattr(device, "sn", None), "SN")
        if fmt is QrFormat.JX:
            payload = build_jx_payload(
                sn,
                imei=getattr(device, "imei", None),
                iccid=getattr(device, "iccid", None),
                device_id=getattr(device, "vendor_device_id", None),
            )
        else:
            tenant_id = getattr(device, "tenant_id", None)
            product_id = getattr(device, "client_product_id", None)
            if not tenant_id:
                raise qr_invalid(f"设备 {sn} 尚未分配给任何租户，无法生成京东二维码")
            if not product_id:
                raise qr_invalid(f"设备 {sn} 未关联客户产品，无法生成京东二维码")
            payload = build_jd_payload(tenant_id, product_id, sn)

        items.append(
            QrCodeItem(
                device_id=str(getattr(device, "id", "")),
                sn=sn,
                network_type=str(getattr(device, "network_type", "")),
                payload=payload,
                format=fmt,
            )
        )
    return items


__all__ = [
    "JD_PAYLOAD_FIELDS",
    "JD_PREFIX",
    "JD_SEGMENTS",
    "JX_PREFIX",
    "JX_SEGMENTS",
    "SEPARATOR",
    "SIGN_ALGORITHM",
    "ParsedQr",
    "QrCodeItem",
    "QrFormat",
    "build_jd_payload",
    "build_jx_payload",
    "build_qrcodes",
    "format_of_network",
    "jd_sign_content",
    "parse_payload",
    "sign_jd",
]
