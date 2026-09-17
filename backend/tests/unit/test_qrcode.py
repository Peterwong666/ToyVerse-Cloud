"""单元测试：二维码格式与签名（P4 关键契约）。

这些测试是**跨端协议的可执行文档**：服务端生成、前端展示、设备端解析
三方必须对字段顺序与签名算法理解一致，因此这里逐段断言而不是只断言「能解析」。

特别关注附录 B 的缺陷修复：早期实现把 ``clientId``
误传进 ``tenant_id`` 的位置。下面的
:meth:`TestJdFormat.test_second_segment_is_tenant_id` 把字段位置钉死，
一旦有人改动参数顺序，测试立即失败。
"""

from __future__ import annotations

import hashlib
import hmac
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.core.errors import AppException, ErrorCode
from app.models.enums import NetworkType
from app.services import qrcode_service
from app.services.qrcode_service import (
    QrFormat,
    build_jd_payload,
    build_jx_payload,
    build_qrcodes,
    format_of_network,
    jd_sign_content,
    parse_payload,
    sign_jd,
)

pytestmark = pytest.mark.unit

TENANT_ID = "t-001"
PRODUCT_ID = "prod-t001-cube"
SN = "SN-20260101-DEMO01"


def _fake_device(**overrides: object) -> SimpleNamespace:
    """构造一个只含二维码生成所需属性的伪设备对象（纯函数测试不建库）。"""
    base: dict[str, object] = {
        "id": "d-0001",
        "sn": SN,
        "network_type": str(NetworkType.WIFI),
        "tenant_id": TENANT_ID,
        "client_product_id": PRODUCT_ID,
        "imei": None,
        "iccid": None,
        "vendor_device_id": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# 集贤 4G（JX）
# ---------------------------------------------------------------------------


class TestJxFormat:
    """``JX|{SN}|{IMEI}|{ICCID}|{deviceId}``。"""

    def test_has_exactly_five_segments(self) -> None:
        payload = build_jx_payload(SN, "861234567890123", "89860012345678901234", "vdev-1")
        parts = payload.split("|")
        assert len(parts) == qrcode_service.JX_SEGMENTS == 5
        assert parts[0] == "JX"
        assert parts[1] == SN
        assert parts[2] == "861234567890123"
        assert parts[3] == "89860012345678901234"
        assert parts[4] == "vdev-1"

    def test_optional_fields_keep_segment_count(self) -> None:
        """厂商尚未回填 IMEI/ICCID/设备 ID 时，段数仍为 5（设备端可用固定下标取值）。"""
        payload = build_jx_payload(SN)
        assert payload == f"JX|{SN}|||"
        assert len(payload.split("|")) == 5

    def test_build_and_parse_are_inverse(self) -> None:
        payload = build_jx_payload(SN, "86123", "89860", "vdev-9")
        parsed = parse_payload(payload)
        assert parsed.format is QrFormat.JX
        assert parsed.sn == SN
        assert parsed.imei == "86123"
        assert parsed.iccid == "89860"
        assert parsed.vendor_device_id == "vdev-9"
        assert parsed.tenant_id is None

    def test_empty_optional_fields_parse_as_none(self) -> None:
        parsed = parse_payload(f"JX|{SN}|||")
        assert parsed.imei is None
        assert parsed.iccid is None
        assert parsed.vendor_device_id is None

    def test_wrong_segment_count_is_rejected(self) -> None:
        with pytest.raises(AppException) as excinfo:
            parse_payload(f"JX|{SN}|86123")
        assert excinfo.value.code == ErrorCode.QR_INVALID

    def test_missing_sn_is_rejected(self) -> None:
        with pytest.raises(AppException) as excinfo:
            parse_payload("JX||86123|89860|vdev")
        assert excinfo.value.code == ErrorCode.QR_INVALID

    def test_build_requires_sn(self) -> None:
        with pytest.raises(AppException) as excinfo:
            build_jx_payload("  ")
        assert excinfo.value.code == ErrorCode.QR_INVALID


# ---------------------------------------------------------------------------
# 京东 Wi-Fi（JD）
# ---------------------------------------------------------------------------


class TestJdFormat:
    """``JD|{tenant_id}|{product_id}|{sn}|{sign}``。"""

    def test_second_segment_is_tenant_id(self) -> None:
        """★ 钉死字段顺序：第 2 段必须是 tenant_id，不是 clientId/产品 ID。

        这正是早期实现的缺陷所在——把客户/产品 ID
        传进了租户的位置。参数顺序一旦被改错，本断言立即失败。
        """
        payload = build_jd_payload(TENANT_ID, PRODUCT_ID, SN)
        parts = payload.split("|")

        assert parts[0] == "JD"
        assert parts[1] == TENANT_ID
        assert parts[2] == PRODUCT_ID
        assert parts[3] == SN
        # 前缀之后恰好 4 个字段：租户 / 产品 / SN / 签名
        assert len(parts) - 1 == qrcode_service.JD_PAYLOAD_FIELDS == 4

    def test_argument_position_is_semantic(self) -> None:
        """把两个 ID 传反会得到不同的二维码——说明位置本身就是语义。

        签名无法「猜出」调用方传错参数（它签的就是收到的内容），
        因此参数命名与位置必须由测试守住，而不是靠签名兜底。
        """
        correct = parse_payload(build_jd_payload(TENANT_ID, PRODUCT_ID, SN))
        swapped = parse_payload(build_jd_payload(PRODUCT_ID, TENANT_ID, SN))
        assert correct.tenant_id == TENANT_ID
        assert correct.product_id == PRODUCT_ID
        assert swapped.tenant_id == PRODUCT_ID
        assert correct.text != swapped.text

    def test_signature_matches_documented_algorithm(self) -> None:
        """签名 = HMAC-SHA256(f"{tenant}|{product}|{sn}", QR_SIGN_SECRET) 小写十六进制。"""
        expected = hmac.new(
            settings.QR_SIGN_SECRET.encode("utf-8"),
            f"{TENANT_ID}|{PRODUCT_ID}|{SN}".encode(),
            hashlib.sha256,
        ).hexdigest()

        assert jd_sign_content(TENANT_ID, PRODUCT_ID, SN) == f"{TENANT_ID}|{PRODUCT_ID}|{SN}"
        assert sign_jd(TENANT_ID, PRODUCT_ID, SN) == expected
        assert len(expected) == 64
        assert build_jd_payload(TENANT_ID, PRODUCT_ID, SN).split("|")[4] == expected

    def test_signature_is_deterministic(self) -> None:
        assert sign_jd(TENANT_ID, PRODUCT_ID, SN) == sign_jd(TENANT_ID, PRODUCT_ID, SN)

    def test_different_content_yields_different_signature(self) -> None:
        assert sign_jd(TENANT_ID, PRODUCT_ID, SN) != sign_jd(TENANT_ID, PRODUCT_ID, SN + "X")

    def test_build_and_parse_are_inverse(self) -> None:
        parsed = parse_payload(build_jd_payload(TENANT_ID, PRODUCT_ID, SN))
        assert parsed.format is QrFormat.JD
        assert parsed.tenant_id == TENANT_ID
        assert parsed.product_id == PRODUCT_ID
        assert parsed.sn == SN

    @pytest.mark.parametrize(
        "tampered",
        [
            f"JD|t-999|{PRODUCT_ID}|{SN}|{{sign}}",  # 改租户
            f"JD|{TENANT_ID}|prod-other|{SN}|{{sign}}",  # 改产品
            f"JD|{TENANT_ID}|{PRODUCT_ID}|SN-X|{{sign}}",  # 改 SN
        ],
    )
    def test_tampered_fields_are_rejected(self, tampered: str) -> None:
        """签名覆盖全部三个字段：任一被篡改（含顺序错位）都无法通过校验。"""
        signature = sign_jd(TENANT_ID, PRODUCT_ID, SN)
        with pytest.raises(AppException) as excinfo:
            parse_payload(tampered.format(sign=signature))
        assert excinfo.value.code == ErrorCode.QR_INVALID

    def test_tampered_signature_is_rejected(self) -> None:
        payload = build_jd_payload(TENANT_ID, PRODUCT_ID, SN)
        assert parse_payload(payload)  # 原始串可解析
        tampered = payload[:-1] + ("0" if payload[-1] != "0" else "1")
        with pytest.raises(AppException) as excinfo:
            parse_payload(tampered)
        assert excinfo.value.code == ErrorCode.QR_INVALID

    def test_segment_count_is_enforced(self) -> None:
        signature = sign_jd(TENANT_ID, PRODUCT_ID, SN)
        with pytest.raises(AppException):
            parse_payload(f"JD|{TENANT_ID}|{PRODUCT_ID}|{SN}")  # 缺签名
        with pytest.raises(AppException):
            parse_payload(f"JD|{TENANT_ID}|{PRODUCT_ID}|{SN}|{signature}|extra")

    def test_build_requires_all_three_fields(self) -> None:
        for args in (("", PRODUCT_ID, SN), (TENANT_ID, "", SN), (TENANT_ID, PRODUCT_ID, "")):
            with pytest.raises(AppException) as excinfo:
                build_jd_payload(*args)
            assert excinfo.value.code == ErrorCode.QR_INVALID


# ---------------------------------------------------------------------------
# 通用行为
# ---------------------------------------------------------------------------


class TestParseGuards:
    """解析的边界情况。"""

    @pytest.mark.parametrize("text", ["", "   ", "UNKNOWN|a|b", "junk", "XYZ", "|JD|t|p|s"])
    def test_unknown_prefix_is_rejected(self, text: str) -> None:
        with pytest.raises(AppException) as excinfo:
            parse_payload(text)
        assert excinfo.value.code == ErrorCode.QR_INVALID

    def test_whitespace_is_tolerated(self) -> None:
        """扫码结果常带换行与空格，解析前应归一化。"""
        payload = build_jd_payload(TENANT_ID, PRODUCT_ID, SN)
        assert parse_payload(f"\n  {payload}  \n").tenant_id == TENANT_ID

    def test_lowercase_prefix_is_tolerated(self) -> None:
        assert parse_payload(f"jd|{build_jd_payload(TENANT_ID, PRODUCT_ID, SN).split('|', 1)[1]}")

    @pytest.mark.parametrize(
        ("network", "expected"),
        [
            (NetworkType.FOUR_G, QrFormat.JX),
            ("4G", QrFormat.JX),
            (NetworkType.WIFI, QrFormat.JD),
            ("WIFI", QrFormat.JD),
            (None, QrFormat.JD),  # 未知值落到默认分支（Wi-Fi 是默认方案）
        ],
    )
    def test_format_of_network(self, network: object, expected: QrFormat) -> None:
        assert format_of_network(network) is expected  # type: ignore[arg-type]


class TestBuildQrcodes:
    """批量导出（订单二维码）。"""

    def test_wifi_devices_use_jd_format(self) -> None:
        devices = [
            _fake_device(id="d-1", sn="SN-1"),
            _fake_device(id="d-2", sn="SN-2"),
        ]
        items = build_qrcodes(devices)
        assert [item.sn for item in items] == ["SN-1", "SN-2"]  # 顺序与入参一致
        assert all(item.format is QrFormat.JD for item in items)
        assert all(item.payload.split("|")[1] == TENANT_ID for item in items)

    def test_four_g_devices_use_jx_format(self) -> None:
        items = build_qrcodes(
            [
                _fake_device(
                    network_type=str(NetworkType.FOUR_G),
                    imei="86123",
                    iccid="89860",
                    vendor_device_id="vdev-1",
                )
            ]
        )
        assert items[0].format is QrFormat.JX
        assert items[0].payload == f"JX|{SN}|86123|89860|vdev-1"

    def test_platform_stock_wifi_device_is_rejected(self) -> None:
        """平台库存设备（tenant_id 为空）无法生成京东二维码：应报错而不是静默跳过。"""
        with pytest.raises(AppException) as excinfo:
            build_qrcodes([_fake_device(tenant_id=None)])
        assert excinfo.value.code == ErrorCode.QR_INVALID

    def test_device_without_sn_is_rejected(self) -> None:
        with pytest.raises(AppException):
            build_qrcodes([_fake_device(sn="")])

    def test_empty_batch(self) -> None:
        assert build_qrcodes([]) == []
