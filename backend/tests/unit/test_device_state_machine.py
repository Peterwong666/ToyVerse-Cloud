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
    """冻结 / 解冻必须是对称的（这是 P4 阶段定下的决策）。"""

    def test_only_in_stock_can_be_frozen(self) -> None:
        """只允许冻结 IN_STOCK。

        若有人把 GENERATED 加回来，解冻时就无法原路恢复
        （``ASSET_TRANSITIONS[FROZEN]`` 只含 IN_STOCK），语义会立刻变得含糊。
        """
        assert frozenset({AssetStatus.IN_STOCK}) == FREEZABLE_ASSET_STATUSES

    def test_freeze_and_thaw_are_symmetric(self) -> None:
        """IN_STOCK ⇄ FROZEN 双向可达，且 FROZEN 只能回到 IN_STOCK。"""
        assert AssetStatus.FROZEN in ASSET_TRANSITIONS[AssetStatus.IN_STOCK]
        assert ASSET_TRANSITIONS[AssetStatus.FROZEN] == frozenset({AssetStatus.IN_STOCK})

    def test_frozen_is_not_allocatable(self) -> None:
        """冻结的设备不能被分配——这正是「冻结」存在的意义。"""
        assert AssetStatus.FROZEN not in ALLOCATABLE_ASSET_STATUSES

    def test_allocatable_is_in_stock_only(self) -> None:
        """可分配状态只有 IN_STOCK（P5 的分配单依赖此规则）。"""
        assert frozenset({AssetStatus.IN_STOCK}) == ALLOCATABLE_ASSET_STATUSES


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
