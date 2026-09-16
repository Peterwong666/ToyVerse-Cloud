"""设备四维状态机的规则断言（纯函数，无 IO）。

为什么单独一个文件
------------------
设备状态迁移是本项目**最容易出错也最贵**的地方（一台设备的状态错了，
意味着实体库存、客户资产、计费口径同时对不上）。这里把「冻结语义」这类
**设计决策**钉成断言，避免后续有人顺手放宽枚举却不知道会破坏什么。

本文件只做纯函数断言（不碰数据库）；真正调 `transition_asset` 的库级
用例放在 `tests/integration/test_order_flow.py`。
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from app.models.enums import (
    ALLOCATABLE_ASSET_STATUSES,
    ASSET_TRANSITIONS,
    FREEZABLE_ASSET_STATUSES,
    AssetStatus,
    derive_device_label,
)

pytestmark = pytest.mark.unit


class TestFreezeSemantics:
    """冻结 / 解冻必须是对称的。

    这条规则被改过一次，值得记下原委：P4 曾把 ``FREEZABLE_ASSET_STATUSES``
    收紧为「只允许 ``IN_STOCK``」，理由是保持与 ``ASSET_TRANSITIONS[FROZEN]``
    的对称；但收紧后与「绑定只接受 ``ALLOCATED``」交集为空，导致
    **已出货给商户的设备无法被冻结**——欠费停机、内容违规停服这些真实
    运营动作全部缺失，绑定流程里的冻结校验也退化成不可达的防御性代码。

    P6 按用户决策放宽为 {IN_STOCK, ALLOCATED, BOUND}。注意这里的测试不再
    只断言具体取值，而是断言**对称性本身**：只要
    ``FREEZABLE_ASSET_STATUSES == ASSET_TRANSITIONS[FROZEN]``，
    解冻原路恢复就成立；具体集合变化时两侧必须同改。
    """

    def test_freezable_set_equals_frozen_incoming_edges(self) -> None:
        """能冻结哪些状态，就必须能从 FROZEN 回到哪些状态（对称性）。

        这是「解冻恢复原状」的充要条件：``transition_asset`` 用
        ``previous_asset_status`` 决定解冻目标，而该状态必然来自这个集合。
        """
        assert ASSET_TRANSITIONS[AssetStatus.FROZEN] == FREEZABLE_ASSET_STATUSES

    def test_freezable_statuses_are_pinned(self) -> None:
        """冻结范围被钉死为「库存 / 已分配 / 已绑定」（P6 决策）。"""
        assert frozenset(
            {AssetStatus.IN_STOCK, AssetStatus.ALLOCATED, AssetStatus.BOUND}
        ) == FREEZABLE_ASSET_STATUSES

    def test_freeze_and_thaw_are_symmetric(self) -> None:
        """三类状态都能进 FROZEN，且 FROZEN 能回到它们中的任意一个。"""
        for status in (AssetStatus.IN_STOCK, AssetStatus.ALLOCATED, AssetStatus.BOUND):
            assert AssetStatus.FROZEN in ASSET_TRANSITIONS[status], f"{status} 应可冻结"
            assert status in ASSET_TRANSITIONS[AssetStatus.FROZEN], f"{status} 应可解冻恢复"
        assert AssetStatus.FROZEN in ASSET_TRANSITIONS[AssetStatus.IN_STOCK]

    def test_generated_cannot_be_frozen(self) -> None:
        """``GENERATED`` 不可冻结：不入库本身已阻断后续流转，冻结是零增量。"""
        assert AssetStatus.GENERATED not in FREEZABLE_ASSET_STATUSES

    def test_frozen_is_not_allocatable(self) -> None:
        """冻结的设备不能被分配——这正是「冻结」存在的意义。"""
        assert AssetStatus.FROZEN not in ALLOCATABLE_ASSET_STATUSES

    def test_allocatable_is_in_stock_or_shipped(self) -> None:
        """可分配状态为 ``IN_STOCK`` 与 ``SHIPPED``（P6 补齐了后者）。

        * ``IN_STOCK`` —— 平台自有库存（P5 唯一的分配来源，行为未变）；
        * ``SHIPPED``  —— 经工厂烧录、抽检、出货后的设备。
          ``ASSET_TRANSITIONS`` 里 ``SHIPPED → ALLOCATED`` 从 P4 就存在，
          但 P5 把入口限死在 ``IN_STOCK``，形成「迁移表允许、服务层拒绝」
          的自相矛盾；P6 把这条边接通。
        """
        assert frozenset({AssetStatus.IN_STOCK, AssetStatus.SHIPPED}) == ALLOCATABLE_ASSET_STATUSES

    def test_every_allocatable_status_can_reach_allocated(self) -> None:
        """可分配集合的每个成员都必须在迁移表里真的能走到 ALLOCATED。

        没有这条断言，将来有人往 ``ALLOCATABLE_ASSET_STATUSES`` 里加一个
        迁移表不支持的取值时，服务层会在运行期抛 ``INVALID_STATE_TRANSITION``
        ——一个只在生产流量下才暴露的错误。
        """
        for status in ALLOCATABLE_ASSET_STATUSES:
            assert AssetStatus.ALLOCATED in ASSET_TRANSITIONS[status], (
                f"{status} 不在迁移表里能到达 ALLOCATED"
            )

    def test_produced_requires_shipping_before_allocation(self) -> None:
        """``PRODUCED`` 不可直接分配：设备必须先「出货登记」。"""
        assert AssetStatus.PRODUCED not in ALLOCATABLE_ASSET_STATUSES


class TestAssetTransitionMap:
    """迁移表的自洽性。"""

    def test_every_status_has_an_entry(self) -> None:
        """每个状态都必须在迁移表里有条目（含空集合），否则查表会 KeyError。"""
        for status in AssetStatus:
            assert status in ASSET_TRANSITIONS

    def test_retired_is_terminal(self) -> None:
        """报废是终态，不可再迁移。"""
        assert ASSET_TRANSITIONS[AssetStatus.RETIRED] == frozenset()

    def test_no_self_transition(self) -> None:
        """不允许「迁移到自己」——否则会产生一串无意义的重复事件。"""
        for source, targets in ASSET_TRANSITIONS.items():
            assert source not in targets

    def test_forward_path_is_reachable(self) -> None:
        """主链路 PENDING_GEN → … → BOUND 每一步都可达。"""
        path = [
            AssetStatus.PENDING_GEN,
            AssetStatus.GENERATED,
            AssetStatus.IN_STOCK,
            AssetStatus.PRODUCING,
            AssetStatus.PRODUCED,
            AssetStatus.SHIPPED,
            AssetStatus.ALLOCATED,
            AssetStatus.BOUND,
        ]
        for source, target in pairwise(path):
            assert target in ASSET_TRANSITIONS[source], f"{source} → {target} 不可达"


class TestDerivedLabel:
    """四维状态派生展示标签（ADR-03：9 态只是前端的展示口径）。"""

    def test_frozen_wins_over_other_dimensions(self) -> None:
        """冻结优先展示——运维最需要先看到「这台被冻结了」。"""
        assert (
            derive_device_label(
                AssetStatus.FROZEN, "ACTIVATED", "ONLINE", "BOUND"
            )
            == "已冻结"
        )

    def test_retired_wins_over_frozen(self) -> None:
        """报废优先级高于冻结（报废是终态）。"""
        label = derive_device_label(AssetStatus.RETIRED, "NOT_ACTIVATED", "NEVER_ONLINE", "UNBOUND")
        assert label == "已报废"

    def test_bind_failed_is_visible(self) -> None:
        """绑定失败必须显式可见，不能被「已分配待激活」之类的话术盖住。"""
        assert (
            derive_device_label(AssetStatus.ALLOCATED, "BIND_FAILED", "OFFLINE", "UNBOUND")
            == "激活失败"
        )

    def test_activated_online_and_offline_are_distinguished(self) -> None:
        bound = AssetStatus.BOUND
        assert derive_device_label(bound, "ACTIVATED", "ONLINE", "BOUND") == "已激活在线"
        assert derive_device_label(bound, "ACTIVATED", "OFFLINE", "BOUND") == "已激活离线"
