"""单元测试：订单与设备状态机、租户作用域、错误码契约。

这些都是纯逻辑验证，不依赖数据库与 HTTP。
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from app.core.errors import (
    AppException,
    ErrorCode,
    cascade_conflict,
    device_frozen,
    invalid_state_transition,
    not_found,
    vendor_unavailable,
)
from app.models.enums import (
    ORDER_STATUS_LABELS,
    ORDER_TERMINAL_STATUSES,
    ORDER_TRANSITIONS,
    ActivationStatus,
    AssetStatus,
    BindStatus,
    OnlineStatus,
    OrderStatus,
    derive_device_label,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 订单状态机
# ---------------------------------------------------------------------------


class TestOrderStateMachine:
    """订单生命周期迁移规则。"""

    def test_happy_path_is_fully_connected(self) -> None:
        """主干路径必须每一步都可达。"""
        path = [
            OrderStatus.PENDING_AUDIT,
            OrderStatus.APPROVED,
            OrderStatus.GENERATING,
            OrderStatus.GENERATED,
            OrderStatus.IN_STOCK,
            OrderStatus.PRODUCING,
            OrderStatus.SHIPPED_TO_CLIENT,
            OrderStatus.COMPLETED,
        ]
        for current, nxt in pairwise(path):
            assert nxt in ORDER_TRANSITIONS[current], f"{current} 无法迁移到 {nxt}"

    def test_audit_can_approve_or_reject(self) -> None:
        allowed = ORDER_TRANSITIONS[OrderStatus.PENDING_AUDIT]
        assert allowed == frozenset({OrderStatus.APPROVED, OrderStatus.REJECTED})

    def test_rejected_is_terminal(self) -> None:
        assert OrderStatus.REJECTED in ORDER_TERMINAL_STATUSES
        assert ORDER_TRANSITIONS[OrderStatus.REJECTED] == frozenset()

    def test_completed_is_terminal(self) -> None:
        assert OrderStatus.COMPLETED in ORDER_TERMINAL_STATUSES
        assert ORDER_TRANSITIONS[OrderStatus.COMPLETED] == frozenset()

    def test_cannot_skip_audit(self) -> None:
        """未审核的订单不得直接进入生产。"""
        assert OrderStatus.PRODUCING not in ORDER_TRANSITIONS[OrderStatus.PENDING_AUDIT]
        assert OrderStatus.GENERATING not in ORDER_TRANSITIONS[OrderStatus.PENDING_AUDIT]

    def test_cannot_ship_without_production(self) -> None:
        assert OrderStatus.SHIPPED_TO_CLIENT not in ORDER_TRANSITIONS[OrderStatus.IN_STOCK]

    def test_generating_can_rollback_to_approved(self) -> None:
        """厂商调用失败时允许回退重试，对应 PRD「集贤 API 失败则订单回滚」。"""
        assert OrderStatus.APPROVED in ORDER_TRANSITIONS[OrderStatus.GENERATING]

    def test_every_status_has_a_label(self) -> None:
        for status in OrderStatus:
            assert status in ORDER_STATUS_LABELS, f"{status} 缺少中文展示名"
            assert ORDER_STATUS_LABELS[status]


# ---------------------------------------------------------------------------
# 设备四维状态
# ---------------------------------------------------------------------------


class TestDeviceLabelDerivation:
    """四维状态 → 单一展示标签（原型 9 态在四维模型下的等价表达）。"""

    @pytest.mark.parametrize(
        ("asset", "activation", "online", "bind", "expected"),
        [
            # 正常在库
            ("IN_STOCK", "NOT_ACTIVATED", "NEVER_ONLINE", "UNBOUND", "已入库待生产"),
            ("GENERATED", "NOT_ACTIVATED", "NEVER_ONLINE", "UNBOUND", "已生成待入库"),
            ("PENDING_GEN", "NOT_ACTIVATED", "NEVER_ONLINE", "UNBOUND", "待生成"),
            # 生产与出货
            ("PRODUCING", "NOT_ACTIVATED", "NEVER_ONLINE", "UNBOUND", "生产烧录中"),
            ("PRODUCED", "NOT_ACTIVATED", "NEVER_ONLINE", "UNBOUND", "待激活"),
            ("SHIPPED", "NOT_ACTIVATED", "NEVER_ONLINE", "UNBOUND", "待激活"),
            # 分配与激活
            ("ALLOCATED", "NOT_ACTIVATED", "NEVER_ONLINE", "UNBOUND", "已分配待激活"),
            ("BOUND", "ACTIVATED", "ONLINE", "BOUND", "已激活在线"),
            ("BOUND", "ACTIVATED", "OFFLINE", "BOUND", "已激活离线"),
            ("BOUND", "ACTIVATING", "NEVER_ONLINE", "UNBOUND", "已绑定"),
            # 异常与终态
            ("ALLOCATED", "BIND_FAILED", "NEVER_ONLINE", "UNBOUND", "激活失败"),
            ("FROZEN", "NOT_ACTIVATED", "OFFLINE", "UNBOUND", "已冻结"),
            ("RETIRED", "ACTIVATED", "OFFLINE", "BOUND", "已报废"),
        ],
    )
    def test_derives_expected_label(
        self,
        asset: str,
        activation: str,
        online: str,
        bind: str,
        expected: str,
    ) -> None:
        assert derive_device_label(asset, activation, online, bind) == expected

    def test_retired_takes_priority_over_everything(self) -> None:
        """已报废是终态，优先于激活与绑定状态展示。"""
        label = derive_device_label("RETIRED", "ACTIVATED", "ONLINE", "BOUND")
        assert label == "已报废"

    def test_frozen_takes_priority_over_activation(self) -> None:
        label = derive_device_label("FROZEN", "BIND_FAILED", "OFFLINE", "UNBOUND")
        assert label == "已冻结"

    def test_bind_failed_is_visible(self) -> None:
        """激活失败必须能展示出来，否则运维无法发现异常设备。"""
        label = derive_device_label("ALLOCATED", "BIND_FAILED", "NEVER_ONLINE", "UNBOUND")
        assert "失败" in label

    def test_accepts_enum_instances_too(self) -> None:
        """枚举成员与字符串值都应可用（数据库取出的是字符串）。"""
        assert (
            derive_device_label(
                AssetStatus.IN_STOCK,
                ActivationStatus.NOT_ACTIVATED,
                OnlineStatus.NEVER_ONLINE,
                BindStatus.UNBOUND,
            )
            == "已入库待生产"
        )

    def test_rejects_unknown_status(self) -> None:
        with pytest.raises(ValueError):
            derive_device_label("NOT_A_STATUS", "NOT_ACTIVATED", "NEVER_ONLINE", "UNBOUND")


# ---------------------------------------------------------------------------
# 错误码契约
# ---------------------------------------------------------------------------


class TestErrorContract:
    """错误码与 HTTP 状态码的映射是前后端契约，必须有测试守护。"""

    @pytest.mark.parametrize(
        ("code", "expected_status"),
        [
            (ErrorCode.UNAUTHENTICATED, 401),
            (ErrorCode.INVALID_CREDENTIALS, 401),
            (ErrorCode.ACCOUNT_LOCKED, 403),
            (ErrorCode.ACCOUNT_DISABLED, 403),
            (ErrorCode.TENANT_DISABLED, 403),
            (ErrorCode.PERMISSION_DENIED, 403),
            (ErrorCode.DEVICE_NOT_IN_TENANT, 403),
            (ErrorCode.VALIDATION_ERROR, 400),
            (ErrorCode.RESOURCE_NOT_FOUND, 404),
            (ErrorCode.DEVICE_NOT_FOUND, 404),
            (ErrorCode.QR_INVALID, 404),
            (ErrorCode.CASCADE_CONFLICT, 409),
            (ErrorCode.IDEMPOTENCY_CONFLICT, 409),
            (ErrorCode.DEVICE_ALREADY_BOUND, 409),
            (ErrorCode.DEVICE_FROZEN, 409),
            (ErrorCode.INVALID_STATE_TRANSITION, 409),
            (ErrorCode.VENDOR_UNAVAILABLE, 503),
            (ErrorCode.INTERNAL_ERROR, 500),
        ],
    )
    def test_status_mapping(self, code: ErrorCode, expected_status: int) -> None:
        assert AppException(code).status_code == expected_status

    def test_every_code_has_chinese_default_message(self) -> None:
        for code in ErrorCode:
            exc = AppException(code)
            assert exc.message, f"{code} 缺少默认提示"
            assert exc.message != str(code), f"{code} 未配置中文提示"

    def test_payload_shape_is_stable(self) -> None:
        exc = not_found("设备不存在")
        payload = exc.to_payload(trace_id="abc123")
        assert payload == {
            "code": "RESOURCE_NOT_FOUND",
            "message": "设备不存在",
            "traceId": "abc123",
        }

    def test_payload_includes_details_when_present(self) -> None:
        exc = invalid_state_transition(current="IN_STOCK", target="BOUND")
        payload = exc.to_payload(trace_id="t1")
        assert payload["details"] == {"current": "IN_STOCK", "target": "BOUND"}

    def test_details_omitted_when_absent(self) -> None:
        assert "details" not in device_frozen().to_payload(trace_id="t1")

    def test_cascade_conflict_carries_associations(self) -> None:
        exc = cascade_conflict(details={"devices": 12, "orders": 3})
        assert exc.status_code == 409
        assert exc.details == {"devices": 12, "orders": 3}

    def test_vendor_unavailable_names_the_vendor(self) -> None:
        """厂商不可用必须指明是哪一家，便于排查。"""
        exc = vendor_unavailable(vendor="jixian")
        assert exc.status_code == 503
        assert exc.details == {"vendor": "jixian"}
        assert "供应商" in exc.message

    def test_error_code_is_string_enum(self) -> None:
        """错误码需可直接 JSON 序列化。"""
        assert str(ErrorCode.VENDOR_UNAVAILABLE) == "VENDOR_UNAVAILABLE"
        assert f"{ErrorCode.QR_EXPIRED}" == "QR_EXPIRED"


# ---------------------------------------------------------------------------
# 分页契约
# ---------------------------------------------------------------------------


class TestPaginationContract:
    """确保分页响应在两条产出路径上形状一致。

    ``page_response()``（dict）与 ``PageResult``（Pydantic 模型）是同一契约的
    两种产出方式，必须保持字段名一致，否则前端会时好时坏。
    """

    def test_dict_and_model_agree_on_field_names(self) -> None:
        from app.core.pagination import PageParams, page_response
        from app.schemas.common import PageResult

        dict_keys = set(page_response([], 0, PageParams()).keys())
        model_keys = set(PageResult[dict].model_json_schema()["properties"].keys())

        assert dict_keys == model_keys, (
            f"分页字段不一致：dict 独有 {dict_keys - model_keys}，"
            f"model 独有 {model_keys - dict_keys}"
        )

    def test_page_params_computes_offset_and_limit(self) -> None:
        from app.core.pagination import PageParams

        params = PageParams(page=3, page_size=20)
        assert params.offset == 40
        assert params.limit == 20

    def test_total_pages_rounds_up(self) -> None:
        from app.core.pagination import PageParams, page_response

        assert page_response([], 41, PageParams(page=1, page_size=20))["totalPages"] == 3
        assert page_response([], 40, PageParams(page=1, page_size=20))["totalPages"] == 2
        assert page_response([], 0, PageParams(page=1, page_size=20))["totalPages"] == 0
