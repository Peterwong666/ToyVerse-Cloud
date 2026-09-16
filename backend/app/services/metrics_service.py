"""运营指标服务（P9）：实时聚合 + 快照读写。

口径的权威来源
--------------
每个指标**怎么算**写在 :class:`app.models.ops.MetricsDaily` 的 docstring 里，
本模块是它的实现。任何人问「这个数字是什么」都该先读那里，再读这里——
两处口径不一致是运营看板最典型的事故。

实时 vs 快照（两条互不替代的路径）
----------------------------------
* **实时聚合**（``overview`` / ``retention``）：直接对 ``devices`` /
  ``dialogue_sessions`` / ``dialogue_messages`` 做 ``GROUP BY`` / ``COUNT``。
  回答「现在是多少」。
* **快照读取**（``trend`` / ``hourly`` / ``regions`` / ``contents``）：
  读 ``metrics_*`` / ``content_hot_ranking``。回答「过去几天怎么走的」。

★ 红线（遗留缺陷 P-07「维度数据非真实汇总」的架构性修复）
--------------------------------------------------------
本模块**禁止**任何「拿一个数乘系数」「按设备数摊派」的写法。
每个数字都必须能追溯到一条 ``COUNT`` / ``SUM`` / ``AVG`` 语句。
代价是实时接口略慢，收益是口径变化时不会静默失真——
而「静默失真」正是 P-07 最难排查的原因：数字看起来永远合理。

产品维度不是筛选条件，是表结构的一部分
--------------------------------------
遗留缺陷 **P-08「运营数据全局共享」** 的根因是两个产品共用一个租户时把
交互混在一起算。因此本模块所有查询都同时带 ``client_product_id``，
且先经 :func:`app.services.ai_config_service.get_product` 校验产品归属——
不属于本租户的产品返回 404（不返回 403，避免探测他人资源是否存在）。

时间口径统一为 **UTC 日期**
---------------------------
``func.date(col)`` 取的是库里存的 UTC 日期（``UTCDateTime`` 存的就是 UTC），
与快照表 ``metric_date`` 一致。展示时区换算由前端按租户时区完成——
在服务端做时区换算会让「同一份快照在不同时区读出不同的日汇总」。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import Request
from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext
from app.core.errors import validation_error
from app.core.ids import new_uuid
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.scope import scoped
from app.models.ai import DialogueMessage, DialogueSession
from app.models.device import Device
from app.models.enums import (
    ActivationStatus,
    AssetStatus,
    AuditAction,
    MessageRole,
)
from app.models.ops import ContentHotRanking, ContentItem, MetricsDaily, MetricsHourly, MetricsRegion
from app.schemas.ops import (
    ContentRankItem,
    HourPoint,
    MetricsContentResponse,
    MetricsHourlyResponse,
    MetricsOverviewResponse,
    MetricsRange,
    MetricsRebuildResponse,
    MetricsRegionResponse,
    MetricsRetentionResponse,
    MetricsTrendResponse,
    RegionPoint,
    RetentionCohort,
    RetentionSummary,
    TrendPoint,
)
from app.services import ai_config_service, audit_service

logger = get_logger(__name__)

#: 实时概览的默认窗口（含今天）
DEFAULT_OVERVIEW_DAYS = 7

#: 一次快照重建允许的最大日期跨度（防一次请求写爆表）
MAX_REBUILD_DAYS = 92

#: 24 小时热力的点数（恒 24，缺失补 0）
HOURS_PER_DAY = 24

#: 地域未知时的归类名（未知即未知，不猜——见 ``Device.region`` 的 docstring）
UNKNOWN_REGION = "未知"

#: 判定「激活后流失」的观察窗口（激活后 N 天内零会话）
CHURN_WINDOW_DAYS = 7


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------


def _date_between(column: Any, date_from: date, date_to: date) -> ColumnElement[bool]:
    """构造「UTC 日期落在 [from, to]」的条件。

    用 ``func.date()`` 而不是 ``created_at >= from AND created_at < to+1day``：
    后者需要把 date 还原成带时区的 datetime，容易在跨时区时错一天；
    ``func.date()`` 与快照表的 ``metric_date`` 是同一口径（库里存的就是 UTC）。
    """
    return func.date(column).between(date_from.isoformat(), date_to.isoformat())


def resolve_range(date_from: date | None, date_to: date | None) -> tuple[date, date]:
    """解析查询区间：默认最近 7 天（含今天）。

    Raises:
        AppException: 起始日期晚于结束日期（``VALIDATION_ERROR``）。
    """
    today = utcnow().date()
    resolved_to = date_to or today
    resolved_from = date_from or (resolved_to - timedelta(days=DEFAULT_OVERVIEW_DAYS - 1))
    if resolved_from > resolved_to:
        raise validation_error(
            "起始日期不能晚于结束日期",
            details={"from": resolved_from.isoformat(), "to": resolved_to.isoformat()},
        )
    return resolved_from, resolved_to


def _iter_dates(date_from: date, date_to: date) -> list[date]:
    """闭区间内的每一天（升序）。"""
    span = (date_to - date_from).days
    return [date_from + timedelta(days=offset) for offset in range(span + 1)]


def _as_int(value: Any) -> int:
    """把聚合结果（可能是 ``None`` / ``Decimal``）统一成 ``int``。"""
    if value is None:
        return 0
    return int(value)


def _rate(numerator: int, denominator: int) -> float | None:
    """百分比比率；分母为 0 时返回 ``None``。

    ★ 返回 ``None`` 而不是 ``0``：把「没有数据」读成「留存为 0」会让运营
    做出完全相反的产品判断（例如「上线首日留存 0，产品失败了」）。
    """
    if denominator <= 0:
        return None
    return round(numerator / denominator * 100, 2)


def _ratio(numerator: int, denominator: int) -> float:
    """普通比值；分母为 0 时返回 0.0（均值类指标用）。"""
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 2)


def _sum_where(condition: ColumnElement[bool]) -> Any:
    """``SUM(CASE WHEN condition THEN 1 ELSE 0 END)``。

    用 ``CASE`` 而不是聚合的 ``FILTER (WHERE ...)``：SQLite 的 FILTER 支持
    依赖编译时的 SQLite 版本，而 ``CASE`` 在 SQLite 与 PostgreSQL 上都稳定。
    """
    return func.sum(case((condition, 1), else_=0))


def _message_base(auth: AuthContext) -> Any:
    """对话消息 × 会话的**聚合**基座（带租户收口）。

    ⚠️ **只用于聚合**：它以 ``select(func.count())`` 起手，调用方只能再叠加
    ``SUM/CASE`` 这类聚合列。若叠加的是行级列（如 ``created_at``），生成的
    ``SELECT count(*), created_at`` 没有 ``GROUP BY``，SQLite 会返回**一行**
    任意行——按行统计的场景必须自己写 ``select(...).select_from(...)``。
    """
    return scoped(
        select(func.count())
        .select_from(DialogueMessage)
        .join(DialogueSession, DialogueMessage.session_id == DialogueSession.id),
        DialogueMessage,
        auth,
    )


# ---------------------------------------------------------------------------
# 一、实时聚合：概览
# ---------------------------------------------------------------------------


async def build_overview(
    session: AsyncSession,
    auth: AuthContext,
    product_id: str,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> MetricsOverviewResponse:
    """实时聚合概览（``source: "live"``）。

    每个数字都来自对原始表的聚合（P-07 红线），不做系数还原。

    Raises:
        AppException: 产品不存在 / 不属于本租户 / 日期区间非法。
    """
    product = await ai_config_service.get_product(session, auth, product_id)
    start, end = resolve_range(date_from, date_to)

    device_base = scoped(select(func.count()).select_from(Device), Device, auth).where(
        Device.client_product_id == product.id,
        Device.asset_status != str(AssetStatus.RETIRED),
    )
    total_devices = _as_int((await session.execute(device_base)).scalar_one())
    activated_devices = _as_int(
        (
            await session.execute(
                device_base.where(Device.activation_status == str(ActivationStatus.ACTIVATED))
            )
        ).scalar_one()
    )

    # 会话指标：活跃设备（去重设备）/ 会话数 / 活跃终端用户
    session_row = (
        await session.execute(
            scoped(
                select(
                    func.count(),
                    func.count(func.distinct(DialogueSession.device_id)),
                    func.count(func.distinct(DialogueSession.end_user_id)),
                )
                .select_from(DialogueSession),
                DialogueSession,
                auth,
            ).where(
                DialogueSession.client_product_id == product.id,
                _date_between(DialogueSession.created_at, start, end),
            )
        )
    ).one()
    session_count = _as_int(session_row[0])
    active_devices = _as_int(session_row[1])
    unique_end_users = _as_int(session_row[2])

    # 消息指标：一问一答算一次交互（USER 计数），安全拦截按助手消息计数
    message_row = (
        await session.execute(
            _message_base(auth)
            .add_columns(
                _sum_where(DialogueMessage.role == str(MessageRole.USER)),
                _sum_where(DialogueMessage.role == str(MessageRole.ASSISTANT)),
                _sum_where(
                    (DialogueMessage.role == str(MessageRole.ASSISTANT))
                    & DialogueMessage.safety_flag.isnot(None)
                ),
                _sum_where(
                    (DialogueMessage.role == str(MessageRole.ASSISTANT))
                    & DialogueMessage.content_item_id.isnot(None)
                ),
                func.coalesce(func.sum(DialogueMessage.latency_ms), 0),
                _sum_where(
                    (DialogueMessage.role == str(MessageRole.ASSISTANT))
                    & DialogueMessage.latency_ms.isnot(None)
                ),
            )
            .where(
                DialogueSession.client_product_id == product.id,
                _date_between(DialogueMessage.created_at, start, end),
            )
        )
    ).one()
    total_interactions = _as_int(message_row[1])
    assistant_messages = _as_int(message_row[2])
    safety_blocked = _as_int(message_row[3])
    content_hits = _as_int(message_row[4])
    total_latency_ms = _as_int(message_row[5])
    latency_samples = _as_int(message_row[6])

    has_snapshot = (
        _as_int(
            (
                await session.execute(
                    scoped(
                        select(func.count()).select_from(MetricsDaily), MetricsDaily, auth
                    ).where(
                        MetricsDaily.client_product_id == product.id,
                        MetricsDaily.metric_date.between(start, end),
                    )
                )
            ).scalar_one()
        )
        > 0
    )

    return MetricsOverviewResponse(
        source="live",
        product_id=product.id,
        product_name=product.name,
        range=MetricsRange(from_=start, to=end),
        total_devices=total_devices,
        activated_devices=activated_devices,
        active_devices=active_devices,
        total_interactions=total_interactions,
        assistant_messages=assistant_messages,
        session_count=session_count,
        unique_end_users=unique_end_users,
        safety_blocked=safety_blocked,
        content_hits=content_hits,
        avg_latency_ms=round(total_latency_ms / latency_samples, 1) if latency_samples else 0.0,
        avg_interactions_per_active_device=_ratio(total_interactions, active_devices),
        avg_messages_per_session=_ratio(total_interactions + assistant_messages, session_count),
        has_snapshot=has_snapshot,
    )


# ---------------------------------------------------------------------------
# 二、快照读取
# ---------------------------------------------------------------------------


async def build_trend(
    session: AsyncSession,
    auth: AuthContext,
    product_id: str,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    granularity: str = "day",
) -> MetricsTrendResponse:
    """日趋势（读 ``metrics_daily``，缺失日期补 0 点使图表不断线）。

    Raises:
        AppException: 产品不可见 / 日期非法 / 不支持的粒度。
    """
    if granularity != "day":
        raise validation_error(
            f"不支持的粒度「{granularity}」，本阶段仅支持 day",
            details={"granularity": granularity},
        )
    product = await ai_config_service.get_product(session, auth, product_id)
    start, end = resolve_range(date_from, date_to)

    rows = list(
        (
            await session.execute(
                scoped(select(MetricsDaily), MetricsDaily, auth)
                .where(
                    MetricsDaily.client_product_id == product.id,
                    MetricsDaily.metric_date.between(start, end),
                )
                .order_by(MetricsDaily.metric_date.asc())
            )
        )
        .scalars()
        .all()
    )
    by_date = {row.metric_date: row for row in rows}

    points: list[TrendPoint] = []
    for day in _iter_dates(start, end):
        row = by_date.get(day)
        points.append(
            TrendPoint(
                date=day,
                interactions=row.total_interactions if row else 0,
                active_devices=row.active_devices if row else 0,
                new_activations=row.new_activations if row else 0,
                session_count=row.session_count if row else 0,
                safety_blocked=row.safety_blocked if row else 0,
                avg_latency_ms=row.avg_latency_ms if row else 0,
            )
        )
    return MetricsTrendResponse(source="snapshot", granularity=granularity, points=points)


async def build_hourly(
    session: AsyncSession,
    auth: AuthContext,
    product_id: str,
    *,
    metric_date: date,
) -> MetricsHourlyResponse:
    """24 小时热力（读 ``metrics_hourly``，**恒 24 项**，缺失补 0）。"""
    product = await ai_config_service.get_product(session, auth, product_id)
    rows = list(
        (
            await session.execute(
                scoped(select(MetricsHourly), MetricsHourly, auth).where(
                    MetricsHourly.client_product_id == product.id,
                    MetricsHourly.metric_date == metric_date,
                )
            )
        )
        .scalars()
        .all()
    )
    by_hour = {row.hour: row for row in rows}
    hours = [
        HourPoint(
            hour=hour,
            interactions=by_hour[hour].interactions if hour in by_hour else 0,
            active_devices=by_hour[hour].active_devices if hour in by_hour else 0,
        )
        for hour in range(HOURS_PER_DAY)
    ]
    return MetricsHourlyResponse(source="snapshot", date=metric_date, hours=hours)


async def build_regions(
    session: AsyncSession,
    auth: AuthContext,
    product_id: str,
    *,
    metric_date: date,
) -> MetricsRegionResponse:
    """地域分布（读 ``metrics_region``，按设备数降序）。"""
    product = await ai_config_service.get_product(session, auth, product_id)
    rows = list(
        (
            await session.execute(
                scoped(select(MetricsRegion), MetricsRegion, auth)
                .where(
                    MetricsRegion.client_product_id == product.id,
                    MetricsRegion.metric_date == metric_date,
                )
                .order_by(
                    MetricsRegion.device_count.desc(),
                    MetricsRegion.region.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return MetricsRegionResponse(
        source="snapshot",
        date=metric_date,
        regions=[
            RegionPoint(
                region=row.region,
                device_count=row.device_count,
                interactions=row.interactions,
            )
            for row in rows
        ],
    )


async def build_contents(
    session: AsyncSession,
    auth: AuthContext,
    product_id: str,
    *,
    metric_date: date,
    limit: int = 10,
) -> MetricsContentResponse:
    """内容热度排行（读 ``content_hot_ranking``）。"""
    product = await ai_config_service.get_product(session, auth, product_id)
    rows = list(
        (
            await session.execute(
                scoped(select(ContentHotRanking), ContentHotRanking, auth)
                .where(
                    ContentHotRanking.client_product_id == product.id,
                    ContentHotRanking.metric_date == metric_date,
                )
                .order_by(ContentHotRanking.rank.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return MetricsContentResponse(
        source="snapshot",
        date=metric_date,
        records=[
            ContentRankItem(
                rank=row.rank,
                content_id=row.content_id,
                title=row.content_title,
                type=row.content_type,
                hits=row.hits,
            )
            for row in rows
        ],
    )


# ---------------------------------------------------------------------------
# 三、留存（实时计算）
# ---------------------------------------------------------------------------


async def build_retention(
    session: AsyncSession,
    auth: AuthContext,
    product_id: str,
    *,
    days: int = 30,
) -> MetricsRetentionResponse:
    """留存（**按设备**实时计算，因为快照表没有设备级明细）。

    口径（写死在这里，避免每次实现各算一套）：

    * **cohort** —— 按 ``devices.activated_at`` 的 UTC 日期分组，只取最近
      ``days`` 天内的 cohort
    * ``dN`` —— 该 cohort 中的设备在「激活日 + N 天」**当天**有过对话
      （``dialogue_sessions`` 有该设备的会话且 ``created_at`` 落在那一天）的台数
    * ``Rate`` —— ``dN / activated × 100``；分母 0 时 ``null``（不是 0）
    * ``churnedDevices`` —— 激活后 7 天内零会话的设备数
    * ``returnRate`` —— 有过 ≥2 个**不同活跃日**的设备 / 有过 ≥1 个活跃日的设备
      （分母取「曾经活跃过的设备」而不是全部设备：否则这个比率会被大量
      「从未激活过」的设备稀释，读起来不再是「回头率」）
    * ``avgIntervalDays`` —— 相邻活跃日间隔的均值（不足 2 个活跃日不计入）
    """
    product = await ai_config_service.get_product(session, auth, product_id)
    today = utcnow().date()
    window_start = today - timedelta(days=max(days, 1) - 1)

    device_rows = list(
        (
            await session.execute(
                scoped(select(Device.id, Device.activated_at), Device, auth).where(
                    Device.client_product_id == product.id,
                    Device.asset_status != str(AssetStatus.RETIRED),
                    Device.activated_at.isnot(None),
                )
            )
        ).all()
    )
    activated_map: dict[str, date] = {}
    for row in device_rows:
        activated_at = row[1]
        if activated_at is None:
            continue
        activated_map[str(row[0])] = activated_at.date()

    device_ids = list(activated_map)
    active_days: dict[str, set[date]] = {device_id: set() for device_id in device_ids}
    if device_ids:
        # 每条会话只取「设备 + 日期」，在 Python 侧归并成活跃日集合。
        #
        # 为什么不按小时/日期分组下推 SQL：``hour`` 的提取在 SQLite
        # （strftime）与 PostgreSQL（extract）上函数名不同，而 ``date`` 提取
        # 只解决了日粒度；这里一次性取出「设备 + 日期」两个维度，既跨方言
        # 又同时满足留存（按日）与间隔（按日排序）两个用途。
        rows = list(
            (
                await session.execute(
                    scoped(
                        select(
                            DialogueSession.device_id,
                            func.date(DialogueSession.created_at),
                        ).select_from(DialogueSession),
                        DialogueSession,
                        auth,
                    ).where(
                        DialogueSession.client_product_id == product.id,
                        DialogueSession.device_id.in_(device_ids),
                    )
                )
            ).all()
        )
        for row in rows:
            device_id = str(row[0]) if row[0] is not None else ""
            if device_id not in active_days or row[1] is None:
                continue
            active_days[device_id].add(date.fromisoformat(str(row[1])))

    # ---- cohort 明细 ----
    cohorts: list[RetentionCohort] = []
    offsets = (1, 3, 7, 30)
    totals = dict.fromkeys(offsets, 0)
    activated_total = 0
    cohort_dates = sorted({day for day in activated_map.values() if day >= window_start})
    for cohort_date in cohort_dates:
        members = [device_id for device_id, day in activated_map.items() if day == cohort_date]
        counts: dict[int, int] = {}
        for offset in offsets:
            target = cohort_date + timedelta(days=offset)
            counts[offset] = sum(1 for device_id in members if target in active_days[device_id])
            totals[offset] += counts[offset]
        activated_total += len(members)
        cohorts.append(
            RetentionCohort(
                cohort_date=cohort_date,
                activated=len(members),
                d1=counts[1],
                d3=counts[3],
                d7=counts[7],
                d30=counts[30],
            )
        )

    # ---- 流失 / 回头 / 间隔 ----
    churned = 0
    ever_active = 0
    returned = 0
    intervals: list[int] = []
    for device_id, activated_date in activated_map.items():
        days_set = active_days[device_id]
        if not any(
            activated_date + timedelta(days=offset) in days_set
            for offset in range(CHURN_WINDOW_DAYS)
        ):
            churned += 1
        if days_set:
            ever_active += 1
            ordered = sorted(days_set)
            if len(ordered) >= 2:
                returned += 1
                intervals.extend(
                    (ordered[index + 1] - ordered[index]).days
                    for index in range(len(ordered) - 1)
                )

    return MetricsRetentionResponse(
        product_id=product.id,
        days=days,
        cohorts=cohorts,
        summary=RetentionSummary(
            d1_rate=_rate(totals[1], activated_total),
            d3_rate=_rate(totals[3], activated_total),
            d7_rate=_rate(totals[7], activated_total),
            d30_rate=_rate(totals[30], activated_total),
        ),
        churned_devices=churned,
        return_rate=_rate(returned, ever_active),
        avg_interval_days=(
            round(sum(intervals) / len(intervals), 2) if intervals else None
        ),
    )


# ---------------------------------------------------------------------------
# 四、快照重建（幂等 upsert）
# ---------------------------------------------------------------------------


async def _daily_values(
    session: AsyncSession, auth: AuthContext, product_id: str, metric_date: date
) -> dict[str, int]:
    """某一天的日汇总（全部来自原始表的聚合）。"""
    new_activations = _as_int(
        (
            await session.execute(
                scoped(select(func.count()).select_from(Device), Device, auth).where(
                    Device.client_product_id == product_id,
                    _date_between(Device.activated_at, metric_date, metric_date),
                )
            )
        ).scalar_one()
    )

    session_row = (
        await session.execute(
            scoped(
                select(
                    func.count(),
                    func.count(func.distinct(DialogueSession.device_id)),
                    func.count(func.distinct(DialogueSession.end_user_id)),
                ).select_from(DialogueSession),
                DialogueSession,
                auth,
            ).where(
                DialogueSession.client_product_id == product_id,
                _date_between(DialogueSession.created_at, metric_date, metric_date),
            )
        )
    ).one()

    message_row = (
        await session.execute(
            _message_base(auth)
            .add_columns(
                _sum_where(DialogueMessage.role == str(MessageRole.USER)),
                _sum_where(DialogueMessage.role == str(MessageRole.ASSISTANT)),
                _sum_where(
                    (DialogueMessage.role == str(MessageRole.ASSISTANT))
                    & DialogueMessage.safety_flag.isnot(None)
                ),
                func.coalesce(func.sum(DialogueMessage.latency_ms), 0),
                _sum_where(
                    (DialogueMessage.role == str(MessageRole.ASSISTANT))
                    & DialogueMessage.latency_ms.isnot(None)
                ),
            )
            .where(
                DialogueSession.client_product_id == product_id,
                _date_between(DialogueMessage.created_at, metric_date, metric_date),
            )
        )
    ).one()

    total_latency = _as_int(message_row[4])
    latency_samples = _as_int(message_row[5])
    return {
        "new_activations": new_activations,
        "active_devices": _as_int(session_row[1]),
        "total_interactions": _as_int(message_row[1]),
        "assistant_messages": _as_int(message_row[2]),
        "session_count": _as_int(session_row[0]),
        "unique_end_users": _as_int(session_row[2]),
        "safety_blocked": _as_int(message_row[3]),
        "total_latency_ms": total_latency,
        "avg_latency_ms": (
            int(round(total_latency / latency_samples)) if latency_samples else 0
        ),
    }


async def _upsert_daily(
    session: AsyncSession,
    *,
    tenant_id: str,
    product_id: str,
    metric_date: date,
    values: dict[str, int],
) -> None:
    """按 ``(tenant, product, date)`` upsert 一行日汇总。"""
    row = (
        await session.execute(
            select(MetricsDaily).where(
                MetricsDaily.tenant_id == tenant_id,
                MetricsDaily.client_product_id == product_id,
                MetricsDaily.metric_date == metric_date,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = MetricsDaily(
            # 指标表在 ``app.core.ids.PREFIX`` 里没有登记前缀，而 ids.py
            # 不在本阶段的可改文件清单内 → 用通用 UUID 助手生成主键。
            # 指标行是内部派生数据，不需要「一眼识别类型」的可读前缀。
            id=new_uuid(),
            tenant_id=tenant_id,
            client_product_id=product_id,
            metric_date=metric_date,
        )
        session.add(row)
    for key, value in values.items():
        setattr(row, key, value)
    await session.flush()


async def _upsert_hourly(
    session: AsyncSession,
    *,
    tenant_id: str,
    product_id: str,
    metric_date: date,
    hour: int,
    interactions: int,
    active_devices: int,
) -> None:
    """按 ``(tenant, product, date, hour)`` upsert 一行小时分布。"""
    row = (
        await session.execute(
            select(MetricsHourly).where(
                MetricsHourly.tenant_id == tenant_id,
                MetricsHourly.client_product_id == product_id,
                MetricsHourly.metric_date == metric_date,
                MetricsHourly.hour == hour,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = MetricsHourly(
            id=new_uuid(),
            tenant_id=tenant_id,
            client_product_id=product_id,
            metric_date=metric_date,
            hour=hour,
        )
        session.add(row)
    row.interactions = interactions
    row.active_devices = active_devices
    await session.flush()


async def _upsert_region(
    session: AsyncSession,
    *,
    tenant_id: str,
    product_id: str,
    metric_date: date,
    region: str,
    device_count: int,
    interactions: int,
) -> None:
    """按 ``(tenant, product, date, region)`` upsert 一行地域分布。"""
    row = (
        await session.execute(
            select(MetricsRegion).where(
                MetricsRegion.tenant_id == tenant_id,
                MetricsRegion.client_product_id == product_id,
                MetricsRegion.metric_date == metric_date,
                MetricsRegion.region == region,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = MetricsRegion(
            id=new_uuid(),
            tenant_id=tenant_id,
            client_product_id=product_id,
            metric_date=metric_date,
            region=region,
        )
        session.add(row)
    row.device_count = device_count
    row.interactions = interactions
    await session.flush()


async def _upsert_content_ranking(
    session: AsyncSession,
    *,
    tenant_id: str,
    product_id: str,
    metric_date: date,
    entries: list[tuple[str, str, str, int]],
) -> int:
    """按 ``(tenant, product, date, content_id)`` upsert 内容热度排行。

    ``entries`` 为 ``(content_id, title, type, hits)``，已按 hits 降序。
    除 upsert 外还会**清理当天已不在榜的旧行**：内容下架或归属变化后，
    上一次重建留下的行若不清掉，排行会显示一个「本次根本没统计到」的标题。
    """
    existing = {
        row.content_id: row
        for row in (
            await session.execute(
                select(ContentHotRanking).where(
                    ContentHotRanking.tenant_id == tenant_id,
                    ContentHotRanking.client_product_id == product_id,
                    ContentHotRanking.metric_date == metric_date,
                )
            )
        )
        .scalars()
        .all()
    }
    kept: set[str] = set()
    for index, (content_id, title, content_type, hits) in enumerate(entries, start=1):
        kept.add(content_id)
        row = existing.get(content_id)
        if row is None:
            row = ContentHotRanking(
                id=new_uuid(),
                tenant_id=tenant_id,
                client_product_id=product_id,
                metric_date=metric_date,
                content_id=content_id,
            )
            session.add(row)
        row.content_title = title
        row.content_type = content_type
        row.hits = hits
        row.rank = index
        await session.flush()

    for stale_id, stale_row in existing.items():
        if stale_id not in kept:
            await session.delete(stale_row)
    return len(entries)


async def rebuild_metrics(
    session: AsyncSession,
    auth: AuthContext,
    *,
    product_id: str,
    date_from: date,
    date_to: date,
    request: Request | None = None,
) -> MetricsRebuildResponse:
    """重建运营快照（幂等 upsert）。

    幂等性来自「按各表唯一键 upsert」而不是「先清后写」：同样是重建两次，
    第二次得到完全相同的行（除 ``updated_at``），且不会因为一次失败而
    把上一次的快照清空——历史数据宁可暂时陈旧，也不要因为半途失败而消失。

    ``dateTo`` 允许是今天（不禁止）：商户端的刷新按钮常常就是刷「含今天」，
    强行禁止会让界面出现「刷新到今天却报错」。但今天的快照会随时间变化，
    因此响应里不改任何文案，由前端提示「今日数据仍在累积」。

    Raises:
        AppException: 产品不可见 / 日期区间非法 / 跨度超过 92 天。
    """
    product = await ai_config_service.get_product(session, auth, product_id)
    start, end = resolve_range(date_from, date_to)
    span = (end - start).days + 1
    if span > MAX_REBUILD_DAYS:
        raise validation_error(
            f"重建跨度不得超过 {MAX_REBUILD_DAYS} 天（当前 {span} 天），"
            "请分多次重建",
            details={"days": span, "maxDays": MAX_REBUILD_DAYS},
        )

    dates = _iter_dates(start, end)
    daily_rows = 0
    hourly_rows = 0
    region_rows = 0
    content_rows = 0

    for day in dates:
        values = await _daily_values(session, auth, product.id, day)
        await _upsert_daily(
            session,
            tenant_id=product.tenant_id,
            product_id=product.id,
            metric_date=day,
            values=values,
        )
        daily_rows += 1

        # 小时分布：一次取「会话 + 用户消息」的 created_at 在 Python 侧按小时归并。
        #
        # 为什么不按小时下推 GROUP BY：小时提取在 SQLite（strftime）与
        # PostgreSQL（extract）上函数名不同；按日聚合用 ``func.date`` 已经
        # 足够，小时维度只在重建时用到，Python 归并换来了跨方言的一致性。
        hour_interactions = dict.fromkeys(range(HOURS_PER_DAY), 0)
        hour_devices: dict[int, set[str]] = {hour: set() for hour in range(HOURS_PER_DAY)}
        # ★ 这里**不能**复用 `_message_base`：它是为「聚合」设计的基座
        # （`select(func.count())` 起手），再 `.add_columns(created_at)` 会生成
        # `SELECT count(*), created_at FROM ...` —— 没有 GROUP BY，SQLite 只返回
        # **一行**（count=6、created_at 取自任意一条），于是小时分布被算成
        # 「某个小时 1 次」而不是「9 点 3 次、14 点 3 次」。
        # 这个错误不会报错、总量也对不上但不显眼，属于典型「看板数字悄悄错」。
        # 需要**逐行**数据时必须显式从 DialogueMessage 取行。
        user_rows = list(
            (
                await session.execute(
                    scoped(
                        select(DialogueMessage.created_at)
                        .select_from(DialogueMessage)
                        .join(DialogueSession, DialogueMessage.session_id == DialogueSession.id),
                        DialogueMessage,
                        auth,
                    ).where(
                        DialogueSession.client_product_id == product.id,
                        DialogueMessage.role == str(MessageRole.USER),
                        _date_between(DialogueMessage.created_at, day, day),
                    )
                )
            ).all()
        )
        for row in user_rows:
            created_at = row[-1]
            if created_at is None:
                continue
            hour_interactions[created_at.hour] += 1
        session_rows = list(
            (
                await session.execute(
                    scoped(
                        select(DialogueSession.created_at, DialogueSession.device_id).select_from(
                            DialogueSession
                        ),
                        DialogueSession,
                        auth,
                    ).where(
                        DialogueSession.client_product_id == product.id,
                        _date_between(DialogueSession.created_at, day, day),
                    )
                )
            ).all()
        )
        for row in session_rows:
            created_at = row[0]
            if created_at is None or row[1] is None:
                continue
            hour_devices[created_at.hour].add(str(row[1]))

        for hour in range(HOURS_PER_DAY):
            await _upsert_hourly(
                session,
                tenant_id=product.tenant_id,
                product_id=product.id,
                metric_date=day,
                hour=hour,
                interactions=hour_interactions[hour],
                active_devices=len(hour_devices[hour]),
            )
            hourly_rows += 1

        # 地域分布
        device_rows = list(
            (
                await session.execute(
                    scoped(select(Device.id, Device.region), Device, auth).where(
                        Device.client_product_id == product.id,
                        Device.asset_status != str(AssetStatus.RETIRED),
                    )
                )
            ).all()
        )
        region_of_device: dict[str, str] = {
            str(row[0]): (str(row[1]) if row[1] else UNKNOWN_REGION) for row in device_rows
        }
        device_count_by_region: dict[str, int] = {}
        for region in region_of_device.values():
            device_count_by_region[region] = device_count_by_region.get(region, 0) + 1

        interactions_by_region: dict[str, int] = {}
        session_to_device = {
            str(row[0]): str(row[1])
            for row in (
                await session.execute(
                    scoped(
                        select(DialogueSession.id, DialogueSession.device_id).select_from(
                            DialogueSession
                        ),
                        DialogueSession,
                        auth,
                    ).where(
                        DialogueSession.client_product_id == product.id,
                        _date_between(DialogueSession.created_at, day, day),
                    )
                )
            ).all()
        }
        if session_to_device:
            per_session = (
                await session.execute(
                    scoped(
                        select(DialogueMessage.session_id, func.count())
                        .select_from(DialogueMessage),
                        DialogueMessage,
                        auth,
                    )
                    .where(
                        DialogueMessage.session_id.in_(list(session_to_device)),
                        DialogueMessage.role == str(MessageRole.USER),
                        _date_between(DialogueMessage.created_at, day, day),
                    )
                    .group_by(DialogueMessage.session_id)
                )
            ).all()
            for row in per_session:
                device_id = session_to_device.get(str(row[0]))
                region = region_of_device.get(device_id or "", UNKNOWN_REGION)
                interactions_by_region[region] = interactions_by_region.get(region, 0) + int(row[1])

        region_names = sorted(set(device_count_by_region) | set(interactions_by_region))
        for region in region_names:
            await _upsert_region(
                session,
                tenant_id=product.tenant_id,
                product_id=product.id,
                metric_date=day,
                region=region,
                device_count=device_count_by_region.get(region, 0),
                interactions=interactions_by_region.get(region, 0),
            )
            region_rows += 1

        # 内容热度：只统计**非空归属**，并写入标题快照
        ranking_rows = (
            await session.execute(
                scoped(
                    select(DialogueMessage.content_item_id, func.count())
                    .select_from(DialogueMessage)
                    .join(DialogueSession, DialogueMessage.session_id == DialogueSession.id),
                    DialogueMessage,
                    auth,
                )
                .where(
                    DialogueSession.client_product_id == product.id,
                    DialogueMessage.content_item_id.isnot(None),
                    _date_between(DialogueMessage.created_at, day, day),
                )
                .group_by(DialogueMessage.content_item_id)
                .order_by(func.count().desc())
            )
        ).all()
        content_ids = [str(row[0]) for row in ranking_rows if row[0] is not None]
        titles: dict[str, tuple[str, str]] = {}
        if content_ids:
            for row in (
                await session.execute(
                    select(ContentItem.id, ContentItem.title, ContentItem.type).where(
                        ContentItem.id.in_(content_ids)
                    )
                )
            ).all():
                titles[str(row[0])] = (str(row[1]), str(row[2]))
        entries = [
            (
                str(row[0]),
                titles.get(str(row[0]), ("（内容已删除）", "STORY"))[0],
                titles.get(str(row[0]), ("（内容已删除）", "STORY"))[1],
                int(row[1]),
            )
            for row in ranking_rows
            if row[0] is not None
        ]
        content_rows += await _upsert_content_ranking(
            session,
            tenant_id=product.tenant_id,
            product_id=product.id,
            metric_date=day,
            entries=entries,
        )

    await audit_service.record(
        session,
        action=AuditAction.REBUILD_METRICS,
        actor=auth,
        tenant_id=product.tenant_id,
        resource_type="metrics",
        resource_id=product.id,
        summary=(
            f"重建客户产品「{product.name}」运营快照"
            f"（{start.isoformat()} ~ {end.isoformat()}）"
        ),
        detail={
            "dates": len(dates),
            "dailyRows": daily_rows,
            "hourlyRows": hourly_rows,
            "regionRows": region_rows,
            "contentRows": content_rows,
        },
        request=request,
    )
    await session.commit()
    logger.info(
        "运营快照已重建：产品=%s 日期=%s~%s 日=%d 小时=%d 地域=%d 内容=%d",
        product.code,
        start,
        end,
        daily_rows,
        hourly_rows,
        region_rows,
        content_rows,
    )
    return MetricsRebuildResponse(
        daily_rows=daily_rows,
        hourly_rows=hourly_rows,
        region_rows=region_rows,
        content_rows=content_rows,
        dates=dates,
        rebuilt_at=utcnow(),
    )


__all__ = [
    "DEFAULT_OVERVIEW_DAYS",
    "MAX_REBUILD_DAYS",
    "build_contents",
    "build_hourly",
    "build_overview",
    "build_regions",
    "build_retention",
    "build_trend",
    "rebuild_metrics",
    "resolve_range",
]
