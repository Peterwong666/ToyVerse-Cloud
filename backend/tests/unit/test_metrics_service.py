"""metrics_service 单元测试。

覆盖口径：

* 纯函数：``resolve_range`` / ``_iter_dates`` / ``_as_int`` / ``_rate`` / ``_ratio``
* 实时聚合：``build_overview``（设备 / 会话 / 消息三组指标与派生均值）
* 快照读取：``build_trend`` / ``build_hourly`` / ``build_regions`` / ``build_contents``
* 留存：``build_retention``（cohort / 流失 / 回头率 / 活跃间隔）
* 快照重建：``rebuild_metrics``（幂等 upsert、地域「未知」归并、内容删除回落）

全部用例直接调用服务层函数，数据库走 conftest 的 ``db`` 夹具（函数级空库）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext
from app.core.errors import AppException, ErrorCode
from app.core.ids import new_id, new_uuid
from app.db.base import utcnow
from app.models.ai import DialogueMessage, DialogueSession
from app.models.catalog import ClientProduct, ProductTemplate
from app.models.device import Device
from app.models.enums import (
    ActivationStatus,
    AssetStatus,
    ContentItemType,
    MessageRole,
    NetworkType,
    RoleType,
)
from app.models.identity import Tenant
from app.models.ops import (
    ContentHotRanking,
    ContentItem,
    MetricsDaily,
    MetricsHourly,
    MetricsRegion,
)
from app.services import metrics_service
from app.services.metrics_service import (
    MAX_REBUILD_DAYS,
    UNKNOWN_REGION,
    _as_int,
    _iter_dates,
    _rate,
    _ratio,
    resolve_range,
)

pytestmark = pytest.mark.unit

TODAY: date = utcnow().date()
YESTERDAY: date = TODAY - timedelta(days=1)
DAY_BEFORE: date = TODAY - timedelta(days=2)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    """构造某天某时刻的 naive UTC 时间（与 ``func.date`` 口径一致）。"""
    return datetime(day.year, day.month, day.day, hour, minute)


def _platform_auth() -> AuthContext:
    return AuthContext(
        user_id="u-platform",
        account="admin",
        role_code="PLATFORM_ADMIN",
        role_type=RoleType.PLATFORM,
    )


def _merchant_auth(tenant: Tenant) -> AuthContext:
    return AuthContext(
        user_id="u-merchant",
        account="13812345679",
        role_code="MERCHANT_ADMIN",
        role_type=RoleType.MERCHANT,
        tenant_id=tenant.id,
        tenant_code=tenant.code,
    )


async def _make_tenant(db: AsyncSession, *, code: str = "T001") -> Tenant:
    tenant = Tenant(id=new_id("tenant"), code=code, name=f"租户{code}", status="ACTIVE")
    db.add(tenant)
    await db.flush()
    return tenant


async def _make_product(
    db: AsyncSession,
    tenant: Tenant,
    *,
    code: str = "prod-1",
    name: str = "故事机",
    network_type: str = str(NetworkType.WIFI),
) -> ClientProduct:
    template = ProductTemplate(
        id=new_id("product_template"),
        code=f"tpl-{code}",
        name=f"{name}模板",
        network_type=network_type,
    )
    db.add(template)
    await db.flush()
    product = ClientProduct(
        id=new_id("client_product"),
        tenant_id=tenant.id,
        template_id=template.id,
        code=code,
        name=name,
        network_type=network_type,
    )
    db.add(product)
    await db.flush()
    return product


async def _make_device(
    db: AsyncSession,
    tenant: Tenant,
    product: ClientProduct,
    *,
    sn: str,
    asset_status: str = str(AssetStatus.BOUND),
    activation_status: str = str(ActivationStatus.ACTIVATED),
    activated_at: datetime | None = None,
    region: str | None = None,
) -> Device:
    device = Device(
        id=new_id("device"),
        tenant_id=tenant.id,
        client_product_id=product.id,
        sn=sn,
        network_type=product.network_type,
        asset_status=asset_status,
        activation_status=activation_status,
        activated_at=activated_at,
        region=region,
    )
    db.add(device)
    await db.flush()
    return device


async def _make_session(
    db: AsyncSession,
    tenant: Tenant,
    product: ClientProduct,
    device: Device | None,
    *,
    created_at: datetime,
    end_user_id: str | None = None,
) -> DialogueSession:
    session = DialogueSession(
        id=new_id("dialogue_session"),
        tenant_id=tenant.id,
        client_product_id=product.id,
        device_id=device.id if device else None,
        end_user_id=end_user_id,
        created_at=created_at,
    )
    db.add(session)
    await db.flush()
    return session


async def _make_message(
    db: AsyncSession,
    tenant: Tenant,
    session: DialogueSession,
    *,
    seq: int,
    role: str,
    created_at: datetime,
    content: str = "内容",
    latency_ms: int | None = None,
    safety_flag: str | None = None,
    content_item_id: str | None = None,
) -> DialogueMessage:
    message = DialogueMessage(
        id=new_id("dialogue_message"),
        session_id=session.id,
        tenant_id=tenant.id,
        seq=seq,
        role=role,
        content=content,
        latency_ms=latency_ms,
        safety_flag=safety_flag,
        content_item_id=content_item_id,
        created_at=created_at,
    )
    db.add(message)
    await db.flush()
    return message


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_resolve_range_defaults_to_last_7_days() -> None:
    """默认区间为「最近 7 天（含今天）」。"""
    start, end = resolve_range(None, None)
    assert end == utcnow().date()
    assert (end - start).days == 6


def test_resolve_range_explicit_bounds() -> None:
    start, end = resolve_range(DAY_BEFORE, YESTERDAY)
    assert (start, end) == (DAY_BEFORE, YESTERDAY)


def test_resolve_range_rejects_reversed_bounds() -> None:
    with pytest.raises(AppException) as excinfo:
        resolve_range(YESTERDAY, DAY_BEFORE)
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
    assert excinfo.value.status_code == 400
    assert excinfo.value.details == {"from": YESTERDAY.isoformat(), "to": DAY_BEFORE.isoformat()}


def test_iter_dates_is_closed_ascending() -> None:
    days = _iter_dates(DAY_BEFORE, TODAY)
    assert days == [DAY_BEFORE, YESTERDAY, TODAY]


def test_as_int_normalizes_aggregation_values() -> None:
    assert _as_int(None) == 0
    assert _as_int(3) == 3


def test_rate_returns_none_on_zero_denominator() -> None:
    assert _rate(1, 0) is None
    assert _rate(1, 4) == 25.0


def test_ratio_returns_zero_on_zero_denominator() -> None:
    assert _ratio(1, 0) == 0.0
    assert _ratio(3, 2) == 1.5


# ---------------------------------------------------------------------------
# build_overview（实时聚合）
# ---------------------------------------------------------------------------


async def test_overview_empty_product_returns_zeros(db: AsyncSession) -> None:
    """没有任何设备与对话时，所有计数为 0、均值为 0.0、无快照。"""
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)

    view = await metrics_service.build_overview(db, _platform_auth(), product.id)

    assert view.source == "live"
    assert view.product_id == product.id
    assert view.product_name == product.name
    assert view.total_devices == 0
    assert view.activated_devices == 0
    assert view.active_devices == 0
    assert view.total_interactions == 0
    assert view.assistant_messages == 0
    assert view.session_count == 0
    assert view.unique_end_users == 0
    assert view.safety_blocked == 0
    assert view.content_hits == 0
    assert view.avg_latency_ms == 0.0
    assert view.avg_interactions_per_active_device == 0.0
    assert view.avg_messages_per_session == 0.0
    assert view.has_snapshot is False


async def test_overview_aggregates_devices_sessions_and_messages(db: AsyncSession) -> None:
    """三组指标全部来自原始表聚合；RETIRED 设备不计入设备数。"""
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    d1 = await _make_device(db, tenant, product, sn="SN-1")
    await _make_device(
        db, tenant, product, sn="SN-2", activation_status=str(ActivationStatus.NOT_ACTIVATED)
    )
    await _make_device(db, tenant, product, sn="SN-3", asset_status=str(AssetStatus.RETIRED))
    content = ContentItem(
        id=new_id("content_item"), tenant_id=None, type=str(ContentItemType.STORY), title="小兔子"
    )
    db.add(content)
    await db.flush()

    s1 = await _make_session(db, tenant, product, d1, created_at=_at(YESTERDAY, 9), end_user_id="eu-1")
    s2 = await _make_session(db, tenant, product, d1, created_at=_at(YESTERDAY, 14), end_user_id="eu-2")
    await _make_message(db, tenant, s1, seq=1, role=str(MessageRole.USER), created_at=_at(YESTERDAY, 9, 1))
    await _make_message(
        db, tenant, s1, seq=2, role=str(MessageRole.ASSISTANT),
        created_at=_at(YESTERDAY, 9, 1), latency_ms=100,
    )
    await _make_message(db, tenant, s2, seq=1, role=str(MessageRole.USER), created_at=_at(YESTERDAY, 14, 1))
    await _make_message(
        db, tenant, s2, seq=2, role=str(MessageRole.ASSISTANT),
        created_at=_at(YESTERDAY, 14, 1), latency_ms=300,
        safety_flag="BLOCKED", content_item_id=content.id,
    )

    view = await metrics_service.build_overview(db, _platform_auth(), product.id)

    assert view.total_devices == 2  # RETIRED 不计入
    assert view.activated_devices == 1
    assert view.session_count == 2
    assert view.active_devices == 1
    assert view.unique_end_users == 2
    assert view.total_interactions == 2  # USER 消息条数
    assert view.assistant_messages == 2
    assert view.safety_blocked == 1
    assert view.content_hits == 1
    assert view.avg_latency_ms == 200.0
    assert view.avg_interactions_per_active_device == 2.0
    assert view.avg_messages_per_session == 2.0
    assert view.has_snapshot is False


async def test_overview_respects_date_range(db: AsyncSession) -> None:
    """区间外的会话与消息不计入。"""
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    d1 = await _make_device(db, tenant, product, sn="SN-1")
    session = await _make_session(db, tenant, product, d1, created_at=_at(DAY_BEFORE, 9))
    await _make_message(db, tenant, session, seq=1, role=str(MessageRole.USER), created_at=_at(DAY_BEFORE, 9, 1))

    view = await metrics_service.build_overview(
        db, _platform_auth(), product.id, date_from=YESTERDAY, date_to=TODAY
    )

    assert view.session_count == 0
    assert view.total_interactions == 0
    assert view.range.from_ == YESTERDAY
    assert view.range.to == TODAY


async def test_overview_has_snapshot_flag(db: AsyncSession) -> None:
    """区间内存在 metrics_daily 行时 ``has_snapshot`` 为 True。"""
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    db.add(
        MetricsDaily(
            id=new_uuid(),
            tenant_id=tenant.id,
            client_product_id=product.id,
            metric_date=YESTERDAY,
            total_interactions=7,
        )
    )
    await db.flush()

    view = await metrics_service.build_overview(
        db, _platform_auth(), product.id, date_from=YESTERDAY, date_to=YESTERDAY
    )

    assert view.has_snapshot is True


async def test_overview_unknown_product_raises_404(db: AsyncSession) -> None:
    with pytest.raises(AppException) as excinfo:
        await metrics_service.build_overview(db, _platform_auth(), "cprod-missing")
    assert excinfo.value.code == ErrorCode.RESOURCE_NOT_FOUND
    assert excinfo.value.status_code == 404


async def test_overview_cross_tenant_raises_404(db: AsyncSession) -> None:
    """商户访问其他租户的产品得到 404（不暴露资源存在性）。"""
    tenant = await _make_tenant(db, code="T001")
    other = await _make_tenant(db, code="T002")
    product = await _make_product(db, tenant)

    with pytest.raises(AppException) as excinfo:
        await metrics_service.build_overview(db, _merchant_auth(other), product.id)
    assert excinfo.value.code == ErrorCode.RESOURCE_NOT_FOUND


async def test_overview_merchant_sees_own_product(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    view = await metrics_service.build_overview(db, _merchant_auth(tenant), product.id)
    assert view.product_id == product.id


# ---------------------------------------------------------------------------
# 快照读取：trend / hourly / regions / contents
# ---------------------------------------------------------------------------


def _add_daily(
    db: AsyncSession,
    tenant: Tenant,
    product: ClientProduct,
    day: date,
    **values: Any,
) -> None:
    db.add(
        MetricsDaily(
            id=new_uuid(),
            tenant_id=tenant.id,
            client_product_id=product.id,
            metric_date=day,
            **values,
        )
    )


async def test_trend_fills_missing_dates_with_zero_points(db: AsyncSession) -> None:
    """缺失日期补 0 点，响应恒为 snapshot + day。"""
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    _add_daily(
        db, tenant, product, YESTERDAY,
        total_interactions=9, active_devices=3, new_activations=2,
        session_count=4, safety_blocked=1, avg_latency_ms=120,
    )
    await db.flush()

    trend = await metrics_service.build_trend(
        db, _platform_auth(), product.id, date_from=DAY_BEFORE, date_to=TODAY
    )

    assert trend.source == "snapshot"
    assert trend.granularity == "day"
    assert [point.date for point in trend.points] == [DAY_BEFORE, YESTERDAY, TODAY]
    assert trend.points[0].interactions == 0  # 缺失日补 0
    assert trend.points[1].interactions == 9
    assert trend.points[1].active_devices == 3
    assert trend.points[1].new_activations == 2
    assert trend.points[1].session_count == 4
    assert trend.points[1].safety_blocked == 1
    assert trend.points[1].avg_latency_ms == 120
    assert trend.points[2].interactions == 0


async def test_trend_rejects_non_day_granularity(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    with pytest.raises(AppException) as excinfo:
        await metrics_service.build_trend(
            db, _platform_auth(), product.id, granularity="hour"
        )
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
    assert excinfo.value.details == {"granularity": "hour"}


async def test_hourly_always_returns_24_points(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    db.add(
        MetricsHourly(
            id=new_uuid(),
            tenant_id=tenant.id,
            client_product_id=product.id,
            metric_date=YESTERDAY,
            hour=9,
            interactions=5,
            active_devices=2,
        )
    )
    await db.flush()

    hourly = await metrics_service.build_hourly(
        db, _platform_auth(), product.id, metric_date=YESTERDAY
    )

    assert hourly.source == "snapshot"
    assert hourly.date == YESTERDAY
    assert len(hourly.hours) == 24
    assert hourly.hours[9].interactions == 5
    assert hourly.hours[9].active_devices == 2
    assert hourly.hours[0].interactions == 0


async def test_regions_sorted_by_device_count_desc(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    for region, count, interactions in (("华东", 3, 7), ("华北", 5, 1), ("华南", 5, 0)):
        db.add(
            MetricsRegion(
                id=new_uuid(),
                tenant_id=tenant.id,
                client_product_id=product.id,
                metric_date=YESTERDAY,
                region=region,
                device_count=count,
                interactions=interactions,
            )
        )
    await db.flush()

    result = await metrics_service.build_regions(
        db, _platform_auth(), product.id, metric_date=YESTERDAY
    )

    # device_count 降序，并列时 region 升序
    assert [(r.region, r.device_count, r.interactions) for r in result.regions] == [
        ("华北", 5, 1),
        ("华南", 5, 0),
        ("华东", 3, 7),
    ]


async def test_contents_ordered_by_rank_and_limited(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    for rank, title, hits in ((2, "老二", 3), (1, "老大", 9)):
        db.add(
            ContentHotRanking(
                id=new_uuid(),
                tenant_id=tenant.id,
                client_product_id=product.id,
                metric_date=YESTERDAY,
                content_id=f"ci-{rank}",
                content_title=title,
                content_type=str(ContentItemType.STORY),
                hits=hits,
                rank=rank,
            )
        )
    await db.flush()

    result = await metrics_service.build_contents(
        db, _platform_auth(), product.id, metric_date=YESTERDAY
    )
    assert [record.rank for record in result.records] == [1, 2]
    assert result.records[0].title == "老大"
    assert result.records[0].hits == 9

    limited = await metrics_service.build_contents(
        db, _platform_auth(), product.id, metric_date=YESTERDAY, limit=1
    )
    assert len(limited.records) == 1
    assert limited.records[0].rank == 1


# ---------------------------------------------------------------------------
# build_retention（实时计算）
# ---------------------------------------------------------------------------


async def test_retention_empty_window_returns_null_rates(db: AsyncSession) -> None:
    """无激活设备时：cohort 为空、比率全为 null（不是 0）。"""
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)

    result = await metrics_service.build_retention(db, _platform_auth(), product.id)

    assert result.cohorts == []
    assert result.summary.d1_rate is None
    assert result.summary.d3_rate is None
    assert result.summary.d7_rate is None
    assert result.summary.d30_rate is None
    assert result.churned_devices == 0
    assert result.return_rate is None
    assert result.avg_interval_days is None


async def test_retention_computes_cohort_churn_return_and_interval(db: AsyncSession) -> None:
    """两台昨天激活的设备：一台今天回访，一台 7 天零会话流失。"""
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    retained = await _make_device(
        db, tenant, product, sn="SN-KEEP", activated_at=_at(YESTERDAY, 10)
    )
    await _make_device(db, tenant, product, sn="SN-LOST", activated_at=_at(YESTERDAY, 11))

    await _make_session(db, tenant, product, retained, created_at=_at(YESTERDAY, 12))
    await _make_session(db, tenant, product, retained, created_at=_at(TODAY, 8))

    result = await metrics_service.build_retention(db, _platform_auth(), product.id, days=30)

    assert len(result.cohorts) == 1
    cohort = result.cohorts[0]
    assert cohort.cohort_date == YESTERDAY
    assert cohort.activated == 2
    assert cohort.d1 == 1  # 只有 retained 在「激活日 +1」有会话
    assert cohort.d3 == 0
    assert cohort.d7 == 0
    assert cohort.d30 == 0

    assert result.summary.d1_rate == 50.0
    assert result.summary.d3_rate == 0.0
    assert result.churned_devices == 1  # SN-LOST 激活后 7 天零会话
    assert result.return_rate == 100.0  # 活跃过的设备中 1/1 有 ≥2 个活跃日
    assert result.avg_interval_days == 1.0


async def test_retention_ignores_devices_activated_outside_window(db: AsyncSession) -> None:
    """窗口外的 cohort 不出现，但分母为 0 时比率仍为 null。"""
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    await _make_device(
        db, tenant, product, sn="SN-OLD", activated_at=_at(TODAY - timedelta(days=40), 10)
    )

    result = await metrics_service.build_retention(db, _platform_auth(), product.id, days=30)

    assert result.cohorts == []
    assert result.summary.d1_rate is None


# ---------------------------------------------------------------------------
# rebuild_metrics（幂等快照重建）
# ---------------------------------------------------------------------------


async def test_rebuild_rejects_span_over_92_days(db: AsyncSession) -> None:
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    with pytest.raises(AppException) as excinfo:
        await metrics_service.rebuild_metrics(
            db,
            _platform_auth(),
            product_id=product.id,
            date_from=TODAY - timedelta(days=MAX_REBUILD_DAYS),
            date_to=TODAY,
        )
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
    assert excinfo.value.details == {"days": MAX_REBUILD_DAYS + 1, "maxDays": MAX_REBUILD_DAYS}


async def test_rebuild_writes_daily_hourly_region_and_content(db: AsyncSession) -> None:
    """一天全量重建：日汇总 / 24 小时 / 地域（含「未知」）/ 内容排行。"""
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    d1 = await _make_device(
        db, tenant, product, sn="SN-1", activated_at=_at(YESTERDAY, 8), region="华东"
    )
    d2 = await _make_device(
        db, tenant, product, sn="SN-2", activated_at=_at(YESTERDAY, 9), region=None
    )
    # RETIRED 设备不计入地域设备数
    await _make_device(db, tenant, product, sn="SN-3", asset_status=str(AssetStatus.RETIRED))

    content = ContentItem(
        id=new_id("content_item"), tenant_id=tenant.id, type=str(ContentItemType.SONG), title="两只老虎"
    )
    db.add(content)
    await db.flush()
    deleted_content_id = new_id("content_item")  # 不存在的内容 → 标题回落

    s1 = await _make_session(db, tenant, product, d1, created_at=_at(YESTERDAY, 9, 15), end_user_id="eu-1")
    s2 = await _make_session(db, tenant, product, d2, created_at=_at(YESTERDAY, 14, 5), end_user_id="eu-2")
    await _make_message(db, tenant, s1, seq=1, role=str(MessageRole.USER), created_at=_at(YESTERDAY, 9, 15))
    await _make_message(
        db, tenant, s1, seq=2, role=str(MessageRole.ASSISTANT),
        created_at=_at(YESTERDAY, 9, 15), latency_ms=100, content_item_id=content.id,
    )
    await _make_message(db, tenant, s1, seq=3, role=str(MessageRole.USER), created_at=_at(YESTERDAY, 9, 20))
    await _make_message(
        db, tenant, s1, seq=4, role=str(MessageRole.ASSISTANT),
        created_at=_at(YESTERDAY, 9, 20), latency_ms=200,
        safety_flag="BLOCKED", content_item_id=content.id,
    )
    await _make_message(db, tenant, s2, seq=1, role=str(MessageRole.USER), created_at=_at(YESTERDAY, 14, 5))
    await _make_message(
        db, tenant, s2, seq=2, role=str(MessageRole.ASSISTANT),
        created_at=_at(YESTERDAY, 14, 5), content_item_id=deleted_content_id,
    )

    result = await metrics_service.rebuild_metrics(
        db,
        _platform_auth(),
        product_id=product.id,
        date_from=YESTERDAY,
        date_to=YESTERDAY,
    )

    assert result.daily_rows == 1
    assert result.hourly_rows == 24
    assert result.region_rows == 2  # 华东 + 未知
    assert result.content_rows == 2
    assert result.dates == [YESTERDAY]
    assert result.rebuilt_at is not None

    daily = (
        await db.execute(
            select(MetricsDaily).where(
                MetricsDaily.client_product_id == product.id,
                MetricsDaily.metric_date == YESTERDAY,
            )
        )
    ).scalar_one()
    assert daily.new_activations == 2
    assert daily.active_devices == 2
    assert daily.total_interactions == 3
    assert daily.assistant_messages == 3
    assert daily.session_count == 2
    assert daily.unique_end_users == 2
    assert daily.safety_blocked == 1
    assert daily.total_latency_ms == 300
    assert daily.avg_latency_ms == 150

    hourly_rows = (
        (
            await db.execute(
                select(MetricsHourly).where(
                    MetricsHourly.client_product_id == product.id,
                    MetricsHourly.metric_date == YESTERDAY,
                )
            )
        )
        .scalars()
        .all()
    )
    by_hour = {row.hour: row for row in hourly_rows}
    assert len(hourly_rows) == 24
    assert by_hour[9].interactions == 2
    assert by_hour[9].active_devices == 1
    assert by_hour[14].interactions == 1
    assert by_hour[14].active_devices == 1
    assert by_hour[3].interactions == 0

    region_rows = (
        (
            await db.execute(
                select(MetricsRegion).where(
                    MetricsRegion.client_product_id == product.id,
                    MetricsRegion.metric_date == YESTERDAY,
                )
            )
        )
        .scalars()
        .all()
    )
    # 中文排序依赖数据库 collation（SQLite 按字节序），这里按集合断言
    assert {(row.region, row.device_count, row.interactions) for row in region_rows} == {
        (UNKNOWN_REGION, 1, 1),
        ("华东", 1, 2),
    }

    ranking_rows = (
        (
            await db.execute(
                select(ContentHotRanking)
                .where(
                    ContentHotRanking.client_product_id == product.id,
                    ContentHotRanking.metric_date == YESTERDAY,
                )
                .order_by(ContentHotRanking.rank)
            )
        )
        .scalars()
        .all()
    )
    assert [(row.rank, row.content_title, row.content_type, row.hits) for row in ranking_rows] == [
        (1, "两只老虎", str(ContentItemType.SONG), 2),
        (2, "（内容已删除）", str(ContentItemType.STORY), 1),
    ]


async def test_rebuild_is_idempotent_and_cleans_stale_rankings(db: AsyncSession) -> None:
    """重建两次行数不翻倍；内容消失后旧排行行被清理。"""
    tenant = await _make_tenant(db)
    product = await _make_product(db, tenant)
    d1 = await _make_device(db, tenant, product, sn="SN-1", region="华东")
    session = await _make_session(db, tenant, product, d1, created_at=_at(YESTERDAY, 10))
    await _make_message(
        db, tenant, session, seq=1, role=str(MessageRole.USER), created_at=_at(YESTERDAY, 10, 1)
    )
    await _make_message(
        db, tenant, session, seq=2, role=str(MessageRole.ASSISTANT),
        created_at=_at(YESTERDAY, 10, 1), content_item_id=new_id("content_item"),
    )

    auth = _platform_auth()
    first = await metrics_service.rebuild_metrics(
        db, auth, product_id=product.id, date_from=YESTERDAY, date_to=YESTERDAY
    )
    assert first.content_rows == 1

    second = await metrics_service.rebuild_metrics(
        db, auth, product_id=product.id, date_from=YESTERDAY, date_to=YESTERDAY
    )
    assert second.daily_rows == 1
    daily_count = (
        await db.execute(
            select(func.count())
            .select_from(MetricsDaily)
            .where(MetricsDaily.client_product_id == product.id)
        )
    ).scalar_one()
    assert daily_count == 1  # upsert 而不是重复插入

    # 删除全部消息后重建：旧的内容热度行必须被清掉
    for message in (await db.execute(select(DialogueMessage))).scalars().all():
        await db.delete(message)
    await db.flush()

    third = await metrics_service.rebuild_metrics(
        db, auth, product_id=product.id, date_from=YESTERDAY, date_to=YESTERDAY
    )
    assert third.content_rows == 0
    ranking_count = (
        await db.execute(
            select(func.count())
            .select_from(ContentHotRanking)
            .where(ContentHotRanking.client_product_id == product.id)
        )
    ).scalar_one()
    assert ranking_count == 0
