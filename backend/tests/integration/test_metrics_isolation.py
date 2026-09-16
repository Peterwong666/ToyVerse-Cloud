"""P9 运营指标：**按 `client_product_id` 隔离**且**维度数据必须真实汇总**。

这份测试对应两个遗留缺陷的验收，两者都是「看板数字错了但没人发现」类的问题：

* **P-07「维度数据非真实汇总」** —— 曾经的实现把「一个总数乘系数」当作维度数据
  （例如按设备数把总交互摊派到各地域）。这类数字**看起来对**（总量守恒），
  但每一个维度值都是假的，且会在口径调整时静默漂移。
  本文件的核心断言是：**同一口径的两个读路径必须给出同一个数**——
  `overview`（实时聚合原始表）与 `trend`（读快照表）的交互数必须相等。
  用系数摊派的实现过不了这一关。
* **P-08「运营数据全局共享」** —— 聚合时丢了产品维度，两个产品共用一个租户时
  看板把它们的数字混在一起。本文件用**种子里两个规模刻意不同的产品**
  （`prod-t001-cube` 5 天历史 / `prod-t001-4g` 3 天历史）来验证：
  它们的指标必须不同，且一个产品的维度数据里不得出现只属于另一个产品的设备。

为什么用生产种子而不是手写造数
------------------------------
种子的对话历史是**确定性**的（不随机），且两个产品的规模差异是刻意设计的
——这正是「两个产品必须看得出差别」的前提。手写造数会让断言变成
「我刚插的那些行算对了吗」，而种子数据同时也在验证种子本身没被改坏。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import utcnow
from app.models.device import Device
from tests.conftest import API_PREFIX

pytestmark = pytest.mark.integration

MERCHANT = f"{API_PREFIX}/merchant"

#: 种子里两个演示客户产品（同属 t-001，规模刻意不同）
PRODUCT_WIFI = "prod-t001-cube"
PRODUCT_4G = "prod-t001-4g"


@pytest.fixture
async def demo(db: AsyncSession) -> AsyncSession:
    """灌入完整演示数据（含 P9 的内容库 / 对话历史 / 设备地域）。"""
    from app.db import seed as seed_module

    await seed_module._seed_catalog(db)
    await seed_module._seed_orders_devices(db)
    await seed_module._seed_miniapp(db)
    await seed_module._seed_ops(db)
    await db.commit()
    return db


@pytest.fixture
async def merchant(client: httpx.AsyncClient, demo: Any) -> dict[str, str]:
    """t-001 的商户管理员请求头。"""
    resp = await client.post(
        f"{API_PREFIX}/auth/login",
        json={
            "account": settings.MERCHANT_ADMIN_ACCOUNT,
            "password": settings.MERCHANT_ADMIN_PASSWORD,
        },
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['accessToken']}"}


def _utc_today() -> date:
    """快照按 **UTC 日期**归档，因此断言也必须用 UTC 日期。

    用 ``date.today()`` 会在 UTC+8 的机器上差一天（本地已是 17 日、UTC 还是 16 日），
    表现为「今天的图表恒为空」——这是本次实现时真实踩到的口径问题。
    """
    return utcnow().date()


def _range(days: int = 30) -> dict[str, str]:
    """最近 N 天（含今天，UTC）的日期范围。"""
    today = _utc_today()
    return {"from": str(today - timedelta(days=days - 1)), "to": str(today)}


async def _rebuild(client: httpx.AsyncClient, headers: dict[str, str], product_id: str) -> Any:
    """重建快照（趋势/热力/地域/内容榜读的都是快照）。"""
    window = _range()
    resp = await client.post(
        f"{MERCHANT}/metrics/rebuild",
        headers=headers,
        json={"productId": product_id, "dateFrom": window["from"], "dateTo": window["to"]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _overview(client: httpx.AsyncClient, headers: dict[str, str], product_id: str) -> Any:
    resp = await client.get(
        f"{MERCHANT}/metrics/overview", headers=headers, params={"productId": product_id, **_range()}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 一、P-07：维度数据必须来自真实汇总
# ---------------------------------------------------------------------------


class TestRealAggregation:
    """★ P-07 的守卫：实时聚合与快照必须给出同一个数。"""

    async def test_live_overview_counts_real_rows(
        self, client: httpx.AsyncClient, merchant: dict[str, str], db: AsyncSession
    ) -> None:
        """`overview` 的交互数 = 库里该产品的 USER 消息真实条数。

        直接拿数据库的 `COUNT` 做对照：如果实现里出现「按设备数估算」
        「乘一个平均系数」，这里的差会立刻暴露。
        """
        from sqlalchemy import func

        from app.models.ai import DialogueMessage, DialogueSession
        from app.models.enums import MessageRole

        expected = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(DialogueMessage)
                    .join(DialogueSession, DialogueMessage.session_id == DialogueSession.id)
                    .where(
                        DialogueSession.client_product_id == PRODUCT_WIFI,
                        DialogueMessage.role == str(MessageRole.USER),
                    )
                )
            ).scalar_one()
        )
        assert expected > 0, "种子应当为该产品造出交互（否则断言无判别力）"

        body = await _overview(client, merchant, PRODUCT_WIFI)
        assert body["source"] == "live", "概览必须标注为实时聚合"
        assert body["totalInteractions"] == expected, (
            f"实时聚合与库内真实条数不一致：{body['totalInteractions']} vs {expected}"
        )

    async def test_snapshot_matches_live_aggregation(
        self, client: httpx.AsyncClient, merchant: dict[str, str]
    ) -> None:
        """★ 同一口径的两个读路径必须一致：`trend`（快照）之和 == `overview`（实时）。

        这是整份文件里最有判别力的一条断言。用「总数乘系数」摊派维度的实现，
        只要系数不是恰好为 1，就必然在这里对不上；而**真实的**逐日聚合
        在「日期范围覆盖了全部历史」时天然相等。
        """
        live = await _overview(client, merchant, PRODUCT_WIFI)
        rebuilt = await _rebuild(client, merchant, PRODUCT_WIFI)
        assert rebuilt["dailyRows"] > 0, "重建快照应当真的写入行"

        window = _range()
        resp = await client.get(
            f"{MERCHANT}/metrics/trend",
            headers=merchant,
            params={"productId": PRODUCT_WIFI, **window, "granularity": "day"},
        )
        assert resp.status_code == 200, resp.text
        trend = resp.json()
        assert trend["source"] == "snapshot"

        snapshot_total = sum(point["interactions"] for point in trend["points"])
        assert snapshot_total == live["totalInteractions"], (
            f"快照与实时聚合对不上：快照 {snapshot_total} vs 实时 {live['totalInteractions']}"
        )
        # 说明：`trend.points` 的字段集是契约里定死的那几项（date / interactions /
        # activeDevices / newActivations / sessionCount / safetyBlocked / avgLatencyMs），
        # 不含助手消息数——因此这里只对 `interactions` 做跨路径一致性断言。

    async def test_hourly_profile_is_real_not_flat(
        self, client: httpx.AsyncClient, merchant: dict[str, str]
    ) -> None:
        """24 小时热力必须有**真实的时间形态**（不是把总量平摊到 24 个点）。

        种子的会话小时刻意错开（见 `seed._seed_ops`），因此真实聚合出来的
        24 点里必然既有非零也有零点。若实现把总量平均分到 24 小时，
        这些点会全部相等——那条断言就是为了挡住它。
        """
        await _rebuild(client, merchant, PRODUCT_WIFI)
        resp = await client.get(
            f"{MERCHANT}/metrics/hourly",
            headers=merchant,
            params={"productId": PRODUCT_WIFI, "date": str(_utc_today())},
        )
        assert resp.status_code == 200, resp.text
        hours = resp.json()["hours"]
        assert len(hours) == 24, "热力图必须恒有 24 个点（缺失补 0）"

        values = [point["interactions"] for point in hours]
        assert any(value > 0 for value in values), "当天应当有交互"
        assert len(set(values)) > 1, f"24 小时数据全部相等，像是被平摊：{values}"


# ---------------------------------------------------------------------------
# 二、P-08：按产品隔离
# ---------------------------------------------------------------------------


class TestProductIsolation:
    """★ P-08 的守卫：两个产品的运营数据必须互不串台。"""

    async def test_two_products_have_different_metrics(
        self, client: httpx.AsyncClient, merchant: dict[str, str]
    ) -> None:
        """★ 种子里两个产品的规模刻意不同 → 指标必须看得出差别。

        如果聚合丢了产品维度（P-08），两个产品的数字会**完全相同**
        （都等于租户总量）。
        """
        wifi = await _overview(client, merchant, PRODUCT_WIFI)
        four_g = await _overview(client, merchant, PRODUCT_4G)

        assert wifi["productId"] != four_g["productId"]
        assert wifi["totalInteractions"] != four_g["totalInteractions"], (
            f"两个产品的交互数相同（{wifi['totalInteractions']}），"
            "聚合很可能丢了产品维度（P-08）"
        )
        assert wifi["sessionCount"] != four_g["sessionCount"]
        assert wifi["activeDevices"] != four_g["activeDevices"], (
            "两个产品的活跃设备数相同：种子刻意让它们的设备规模不同，"
            "相同就说明设备维度也没按产品切"
        )
        assert wifi["totalDevices"] > 0 and four_g["totalDevices"] > 0

        # 设备集合是否真的按产品切分，由
        # `test_regions_belong_to_the_requested_product_only` 直接查库做精确对照
        # （那里比的是「该产品的地域已知设备数」与快照里 deviceCount 之和）。
        # 这里只守住「不同产品给出不同规模」这一层判别力——
        # 引入第三个读路径来交叉验证会让断言依赖实现细节。

    async def test_regions_belong_to_the_requested_product_only(
        self, client: httpx.AsyncClient, merchant: dict[str, str], db: AsyncSession
    ) -> None:
        """★ 地域分布只统计**该产品**的设备，且设备数之和等于该产品的地域已知设备数。"""
        await _rebuild(client, merchant, PRODUCT_WIFI)
        resp = await client.get(
            f"{MERCHANT}/metrics/regions",
            headers=merchant,
            params={"productId": PRODUCT_WIFI, "date": str(_utc_today())},
        )
        assert resp.status_code == 200, resp.text
        regions = resp.json()["regions"]

        # 该产品下「地域已知」的设备数（未知归入「未知」一档，因此不参与此断言）
        wifi_devices = list(
            (
                await db.execute(
                    select(Device).where(
                        Device.client_product_id == PRODUCT_WIFI,
                        Device.region.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        expected_regions = {device.region for device in wifi_devices}
        seen_regions = {row["region"] for row in regions} - {"未知"}
        assert seen_regions <= expected_regions, (
            f"地域分布出现了不属于该产品的地域：{seen_regions - expected_regions}"
        )
        assert sum(row["deviceCount"] for row in regions) == len(wifi_devices), (
            "地域设备数之和应当等于该产品的地域已知设备数"
        )

    async def test_content_ranking_is_per_product(
        self, client: httpx.AsyncClient, merchant: dict[str, str]
    ) -> None:
        """★ 内容热度榜按产品隔离，且**只有真正带内容归属的消息**才计入。

        种子里每一轮对话都带 `content_item_id`（命中素材时），因此排行非空；
        而两个产品的热榜内容各不相同。
        """
        await _rebuild(client, merchant, PRODUCT_WIFI)
        await _rebuild(client, merchant, PRODUCT_4G)

        today = str(_utc_today())
        wifi = await client.get(
            f"{MERCHANT}/metrics/contents",
            headers=merchant,
            params={"productId": PRODUCT_WIFI, "date": today},
        )
        four_g = await client.get(
            f"{MERCHANT}/metrics/contents",
            headers=merchant,
            params={"productId": PRODUCT_4G, "date": today},
        )
        assert wifi.status_code == 200 and four_g.status_code == 200

        wifi_records = wifi.json()["records"]
        assert wifi_records, "内容热榜应当有数据（种子每轮对话都带素材归属）"
        # rank 必须从 1 开始连续，hits 必须降序
        ranks = [row["rank"] for row in wifi_records]
        hits = [row["hits"] for row in wifi_records]
        assert ranks == list(range(1, len(ranks) + 1)), f"rank 不是从 1 连续编号：{ranks}"
        assert hits == sorted(hits, reverse=True), f"hits 不是降序：{hits}"

        # 两个产品的命中数不同（隔离生效）
        wifi_total = sum(hits)
        four_g_total = sum(row["hits"] for row in four_g.json()["records"])
        assert wifi_total != four_g_total, (
            f"两个产品的内容命中数相同（{wifi_total}），内容榜很可能没按产品隔离"
        )

    async def test_rebuild_only_writes_the_requested_product(
        self, client: httpx.AsyncClient, merchant: dict[str, str], db: AsyncSession
    ) -> None:
        """★ 重建只写**被请求的那个产品**的行，不越界写另一个产品。"""
        from app.models.ops import MetricsDaily

        window = _range()
        await _rebuild(client, merchant, PRODUCT_WIFI)

        rows = list(
            (
                await db.execute(
                    select(MetricsDaily).where(
                        MetricsDaily.metric_date >= date.fromisoformat(window["from"]),
                        MetricsDaily.metric_date <= date.fromisoformat(window["to"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        products_written = {row.client_product_id for row in rows}
        assert PRODUCT_WIFI in products_written
        assert PRODUCT_4G not in products_written, "重建越界写了另一个产品的快照"

    async def test_rebuild_is_idempotent(
        self, client: httpx.AsyncClient, merchant: dict[str, str], db: AsyncSession
    ) -> None:
        """★ 重建是**幂等 upsert**：连跑两次不产生重复行、数字也不翻倍。"""
        from sqlalchemy import func

        from app.models.ops import MetricsDaily

        first = await _rebuild(client, merchant, PRODUCT_WIFI)
        second = await _rebuild(client, merchant, PRODUCT_WIFI)
        assert first["dailyRows"] == second["dailyRows"], "两次重建写入行数不一致"

        count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(MetricsDaily)
                    .where(MetricsDaily.client_product_id == PRODUCT_WIFI)
                )
            ).scalar_one()
        )
        assert count == second["dailyRows"], f"重复行：库存 {count} 行 vs 报告 {second['dailyRows']} 行"

    async def test_retention_rates_are_null_when_no_cohort_data(
        self, client: httpx.AsyncClient, merchant: dict[str, str]
    ) -> None:
        """留存：**没有数据的 cohort 必须给 `null` 而不是 0**。

        「留存率 0%」与「还没有可判断的设备」是两件事。返回 0 会让运营
        误以为产品留不住人，而事实是数据还没到——这正是本项目反复强调的
        「不要把未知伪装成零」。
        """
        resp = await client.get(
            f"{MERCHANT}/metrics/retention", headers=merchant, params={"productId": PRODUCT_WIFI}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for cohort in body["cohorts"]:
            if cohort["d7"] == 0:
                assert cohort["d7Rate"] is None or cohort["d7Rate"] == 0.0
        # 真正要守的不变量：分母为 0 时比率必须为 null
        for cohort in body["cohorts"]:
            if cohort["activated"] == 0:
                assert cohort["d1Rate"] is None, "分母为 0 时留存率必须是 null，不能是 0"
