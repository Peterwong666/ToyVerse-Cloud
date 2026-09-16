"""单元测试：订单与设备的状态机（服务层强制，而非只测枚举表）。

为什么还要单独测服务层？
------------------------
``tests/unit/test_domain_rules.py`` 验证的是 ``ORDER_TRANSITIONS`` /
``ASSET_TRANSITIONS`` **这张表**本身是否自洽；本文件验证的是
**服务层确实拿这张表来拦截**——否则表写得再对，只要有一处代码绕过它
（例如直接 ``order.status = ...``），线上照样会出现非法状态。

这两类测试的分工：
* 枚举表错 → 规则错；
* 服务层绕过表 → 规则形同虚设。
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from app.core.errors import AppException, ErrorCode
from app.models.device import Device, DeviceEvent
from app.models.enums import (
    ASSET_TRANSITIONS,
    ORDER_TERMINAL_STATUSES,
    ORDER_TRANSITIONS,
    ActivationStatus,
    AssetStatus,
    BindStatus,
    OnlineStatus,
    OrderStatus,
)
from app.models.order import Order
from app.services import device_service, order_service

pytestmark = pytest.mark.unit

#: 订单号格式：ORD-YYYYMMDD-XXXX
ORDER_NO_PATTERN = re.compile(r"^ORD-\d{8}-[2-9A-HJ-NP-Z]{4}$")
#: 本地 SN 格式：SN-YYYYMMDD-XXXXXX（字符集剔除 0/O/1/I）
LOCAL_SN_PATTERN = re.compile(r"^SN-\d{8}-[2-9A-HJ-NP-Z]{6}$")


class FakeSession:
    """只实现 ``transition_asset`` 所需的最小会话接口。

    状态机是纯逻辑，不需要真数据库；用假会话把「校验 + 事件落痕」
    两件事一起测到，比搭一个库快几个数量级。
    """

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        return None

    @property
    def events(self) -> list[DeviceEvent]:
        return [item for item in self.added if isinstance(item, DeviceEvent)]


def make_order(status: OrderStatus) -> Order:
    """构造一个未落库的订单对象。"""
    return Order(
        id="ord-1",
        order_no="ORD-20260101-AAAA",
        tenant_id="t-001",
        client_product_id="prod-1",
        quantity=2,
        status=str(status),
    )


def make_device(status: AssetStatus = AssetStatus.IN_STOCK, **overrides: Any) -> Device:
    """构造一个未落库的设备对象。"""
    base: dict[str, Any] = {
        "id": "d-1",
        "sn": "SN-20260101-AAAAAA",
        "tenant_id": "t-001",
        "network_type": "WIFI",
        "asset_status": str(status),
        # 四维状态里另外三维也必须有值：展示标签由四维共同派生
        "activation_status": str(ActivationStatus.NOT_ACTIVATED),
        "online_status": str(OnlineStatus.NEVER_ONLINE),
        "bind_status": str(BindStatus.UNBOUND),
    }
    base.update(overrides)
    return Device(**base)


# ---------------------------------------------------------------------------
# 订单状态机
# ---------------------------------------------------------------------------


class TestOrderTransitions:
    """订单迁移：合法全通、非法被拦截、终态封死。"""

    def test_happy_path_is_allowed(self) -> None:
        """主干路径逐步迁移全部合法。"""
        order = make_order(OrderStatus.PENDING_AUDIT)
        path = [
            OrderStatus.APPROVED,
            OrderStatus.GENERATING,
            OrderStatus.GENERATED,
            OrderStatus.IN_STOCK,
            OrderStatus.PRODUCING,
            OrderStatus.SHIPPED_TO_CLIENT,
            OrderStatus.COMPLETED,
        ]
        for target in path:
            order_service.transition_order(order, target)
            assert order.status == str(target)

    def test_reject_is_allowed_from_pending_audit(self) -> None:
        order = make_order(OrderStatus.PENDING_AUDIT)
        order_service.transition_order(order, OrderStatus.REJECTED)
        assert order.status == str(OrderStatus.REJECTED)

    def test_generating_can_roll_back_to_approved(self) -> None:
        """生成失败（厂商未配置）时要把订单退回 APPROVED，这条边必须存在。"""
        order = make_order(OrderStatus.GENERATING)
        order_service.transition_order(order, OrderStatus.APPROVED)
        assert order.status == str(OrderStatus.APPROVED)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (OrderStatus.PENDING_AUDIT, OrderStatus.GENERATING),
            (OrderStatus.PENDING_AUDIT, OrderStatus.COMPLETED),
            (OrderStatus.APPROVED, OrderStatus.IN_STOCK),
            (OrderStatus.GENERATED, OrderStatus.GENERATING),
            (OrderStatus.IN_STOCK, OrderStatus.COMPLETED),
            (OrderStatus.SHIPPED_TO_CLIENT, OrderStatus.IN_STOCK),
        ],
    )
    def test_illegal_transitions_are_rejected(self, current: OrderStatus, target: OrderStatus) -> None:
        """非法迁移抛 INVALID_STATE_TRANSITION，且 details 带 current / target。"""
        assert target not in ORDER_TRANSITIONS[current], "该组合应是非法迁移，请修正用例"
        order = make_order(current)

        with pytest.raises(AppException) as excinfo:
            order_service.transition_order(order, target)

        assert excinfo.value.code == ErrorCode.INVALID_STATE_TRANSITION
        assert excinfo.value.details == {"current": str(current), "target": str(target)}
        assert order.status == str(current), "校验失败不得改动状态"

    @pytest.mark.parametrize("terminal", sorted(ORDER_TERMINAL_STATUSES))
    def test_terminal_statuses_are_frozen(self, terminal: OrderStatus) -> None:
        """终态（REJECTED / COMPLETED）不可再迁移到任何状态。"""
        assert ORDER_TRANSITIONS[terminal] == frozenset()
        for target in OrderStatus:
            if target is terminal:
                continue
            with pytest.raises(AppException) as excinfo:
                order_service.transition_order(make_order(terminal), target)
            assert excinfo.value.code == ErrorCode.INVALID_STATE_TRANSITION


class TestOrderNumber:
    """订单号格式 ``ORD-YYYYMMDD-XXXX``。"""

    def test_format(self) -> None:
        assert ORDER_NO_PATTERN.match(order_service.generate_order_no())

    def test_contains_no_confusable_characters(self) -> None:
        """随机部分不含 0/O/1/I——单号会被人工抄录到工单上。"""
        for _ in range(50):
            suffix = order_service.generate_order_no().rsplit("-", 1)[1]
            assert not set(suffix) & set("01OI")

    def test_is_random(self) -> None:
        numbers = {order_service.generate_order_no() for _ in range(50)}
        assert len(numbers) > 1


class TestLocalSn:
    """本地 SN 规则 ``SN-YYYYMMDD-XXXXXX``（Wi-Fi 方案）。"""

    def test_format(self) -> None:
        assert LOCAL_SN_PATTERN.match(order_service.build_local_sn())

    def test_is_random(self) -> None:
        assert len({order_service.build_local_sn() for _ in range(50)}) > 1


# ---------------------------------------------------------------------------
# 设备资产状态机
# ---------------------------------------------------------------------------


class TestAssetTransitions:
    """设备资产维度：合法全通、非法被拦截、每次变更写事件。"""

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (AssetStatus.PENDING_GEN, AssetStatus.GENERATED),
            (AssetStatus.GENERATED, AssetStatus.IN_STOCK),
            (AssetStatus.IN_STOCK, AssetStatus.PRODUCING),
            (AssetStatus.PRODUCING, AssetStatus.PRODUCED),
            (AssetStatus.PRODUCED, AssetStatus.SHIPPED),
            (AssetStatus.SHIPPED, AssetStatus.ALLOCATED),
            (AssetStatus.ALLOCATED, AssetStatus.BOUND),
            (AssetStatus.BOUND, AssetStatus.ALLOCATED),
        ],
    )
    async def test_legal_transitions_writes_event(
        self, current: AssetStatus, target: AssetStatus
    ) -> None:
        session = FakeSession()
        device = make_device(current)

        await device_service.transition_asset(session, device, target)

        assert device.asset_status == str(target)
        assert len(session.events) == 1
        event = session.events[0]
        assert event.dimension == "asset"
        assert event.from_status == str(current)
        assert event.to_status == str(target)
        assert event.device_id == device.id

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (AssetStatus.PENDING_GEN, AssetStatus.IN_STOCK),
            (AssetStatus.GENERATED, AssetStatus.BOUND),
            (AssetStatus.IN_STOCK, AssetStatus.BOUND),
            (AssetStatus.RETIRED, AssetStatus.IN_STOCK),
            (AssetStatus.IN_STOCK, AssetStatus.IN_STOCK),
        ],
    )
    async def test_illegal_transitions_are_rejected(
        self, current: AssetStatus, target: AssetStatus
    ) -> None:
        assert target not in ASSET_TRANSITIONS[current], "该组合应是非法迁移，请修正用例"
        session = FakeSession()
        device = make_device(current)

        with pytest.raises(AppException) as excinfo:
            await device_service.transition_asset(session, device, target)

        assert excinfo.value.code == ErrorCode.INVALID_STATE_TRANSITION
        assert excinfo.value.details == {"current": str(current), "target": str(target)}
        assert device.asset_status == str(current)
        assert session.events == []

    async def test_retired_is_terminal(self) -> None:
        assert ASSET_TRANSITIONS[AssetStatus.RETIRED] == frozenset()
        for target in AssetStatus:
            if target is AssetStatus.RETIRED:
                continue
            with pytest.raises(AppException) as excinfo:
                await device_service.transition_asset(FakeSession(), make_device(AssetStatus.RETIRED), target)
            assert excinfo.value.code == ErrorCode.INVALID_STATE_TRANSITION

    async def test_freeze_records_previous_status(self) -> None:
        session = FakeSession()
        device = make_device(AssetStatus.IN_STOCK)

        await device_service.transition_asset(
            session, device, AssetStatus.FROZEN, reason="涉嫌异常流量"
        )

        assert device.asset_status == str(AssetStatus.FROZEN)
        assert device.previous_asset_status == str(AssetStatus.IN_STOCK)
        assert device.freeze_reason == "涉嫌异常流量"
        assert device.frozen_at is not None
        assert session.events[0].event_type == device_service.EVENT_FROZEN
        assert session.events[0].detail == {"reason": "涉嫌异常流量"}

    async def test_thaw_restores_previous_status(self) -> None:
        session = FakeSession()
        device = make_device(
            AssetStatus.FROZEN, previous_asset_status=str(AssetStatus.IN_STOCK)
        )

        await device_service.transition_asset(session, device, AssetStatus.IN_STOCK)

        assert device.asset_status == str(AssetStatus.IN_STOCK)
        assert device.previous_asset_status is None
        assert device.freeze_reason is None
        assert device.frozen_at is None
        assert session.events[0].event_type == device_service.EVENT_THAWED

    async def test_thaw_from_generated_falls_back_to_in_stock(self) -> None:
        """``FROZEN → GENERATED`` 不在迁移表内，解冻应回落到 IN_STOCK 并说明原因。

        这是枚举表里的一处**已知不对称**（可冻结 GENERATED，但只能恢复到
        IN_STOCK），因此必须有明确的回落行为，而不是硬写一个非法状态。
        """
        session = FakeSession()
        device = make_device(
            AssetStatus.FROZEN, previous_asset_status=str(AssetStatus.GENERATED)
        )

        await device_service.transition_asset(session, device, AssetStatus.IN_STOCK)

        assert device.asset_status == str(AssetStatus.IN_STOCK)
        event = session.events[0]
        assert "adjustment" in (event.detail or {})
        assert event.detail["restoredFrom"] == str(AssetStatus.GENERATED)

    async def test_freeze_only_from_freezable_statuses(self) -> None:
        """已报废设备不允许冻结——迁移表管不到这一层，服务层单独判断。"""
        session = FakeSession()
        device = make_device(AssetStatus.RETIRED)

        with pytest.raises(AppException) as excinfo:
            await device_service.transition_asset(session, device, AssetStatus.FROZEN)

        assert excinfo.value.code == ErrorCode.INVALID_STATE_TRANSITION
        assert session.events == []

    async def test_retire_records_reason(self) -> None:
        session = FakeSession()
        device = make_device(AssetStatus.ALLOCATED)

        await device_service.transition_asset(
            session, device, AssetStatus.RETIRED, reason="硬件损坏"
        )

        assert device.asset_status == str(AssetStatus.RETIRED)
        assert device.retire_reason == "硬件损坏"
        assert device.retired_at is not None
        assert session.events[0].event_type == device_service.EVENT_RETIRED

    async def test_actor_is_recorded_on_event(self) -> None:
        """事件必须记下操作者，否则时间线无法回答「谁干的」。"""

        class FakeAuth:
            user_id = "u-1"
            account = "admin"

        session = FakeSession()
        await device_service.transition_asset(
            session,
            make_device(AssetStatus.IN_STOCK),
            AssetStatus.FROZEN,
            reason="演示",
            actor=FakeAuth(),  # type: ignore[arg-type]
        )

        event = session.events[0]
        assert event.actor_id == "u-1"
        assert event.actor_account == "admin"


class TestDeviceLabel:
    """展示标签派生（仅展示，不参与业务判断）。"""

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (AssetStatus.RETIRED, "已报废"),
            (AssetStatus.FROZEN, "已冻结"),
            (AssetStatus.IN_STOCK, "已入库待生产"),
            (AssetStatus.GENERATED, "已生成待入库"),
            (AssetStatus.ALLOCATED, "已分配待激活"),
        ],
    )
    def test_labels(self, status: AssetStatus, expected: str) -> None:
        assert device_service.device_label(make_device(status)) == expected
