/* ============================================================
   商户端 · 运营看板（P9 核心页面）
   ------------------------------------------------------------
   路由：`#/metrics`（支持 `?productId=xxx` 预选产品）

   对应后端（**所有端点都必填 productId**）：
     GET  /merchant/metrics/overview   概览（实时聚合）
     GET  /merchant/metrics/trend      日趋势（来自快照）
     GET  /merchant/metrics/hourly     24 小时分布
     GET  /merchant/metrics/regions    地域分布
     GET  /merchant/metrics/contents   内容 Top
     GET  /merchant/metrics/retention  留存（D1/D3/D7/D30）
     POST /merchant/metrics/rebuild    重建快照（写入日/小时/地域/内容四张表）

   为什么必须把「实时聚合」与「快照」标出来
   --------------------------------------
   概览走实时聚合（每次打开都算，数字总是最新但慢），趋势 / 小时 / 地域 /
   内容排行读的是**快照表**（需要点「刷新快照」才会更新，快但可能滞后）。
   两者口径不同，界面上不写清楚，用户会以为「总交互 100 次但趋势全是 0」
   是数据错了 —— 其实只是快照还没生成。

   为什么要区分两种空态
   -------------------
   「该产品还没有对话数据」是业务事实（没什么可看的）；
   「快照还没生成，请点刷新快照」是可操作的状态（点一下就有了）。
   混成一句「暂无数据」，用户既不知道该不该等，也不知道能不能自救。

   为什么比率为 null 时显示「—」而不是 0%
   --------------------------------------
   D1 留存率 `null` 表示「该 cohort 还没有到期数据」（例如昨天激活的设备
   今天才满 1 天），0% 则表示「到期了一台都没回来」——两者是完全不同的
   结论。把 null 显示成 0% 会让运营得出「留存暴跌」的错误判断。

   为什么图表用 P2 的 chart.js 而不自己写 SVG
   -----------------------------------------
   零构建、零依赖是本项目的硬约束（见 shared/ui/chart.js 头部说明），
   这里只负责把接口数据整理成图表函数的入参形状，不重复造轮子。

   为什么参数下拉/日期改动不写回 URL
   --------------------------------
   写回 URL 会改变 hash → 触发路由重新 dispatch → 整页重渲染并重新请求
   全部端点，体验上等于「点一次日期就刷整页」。预选（从 URL 读 productId）
   已经满足「从产品详情跳进来带着上下文」的需求。
   ============================================================ */

import api from '/shared/core/api.js';
import { PERM } from '/shared/core/auth.js';
import { clear, formatNumber, fromHtml, h, html, raw } from '/shared/ui/dom.js';
import {
  alert,
  card,
  emptyState,
  kvList,
  loadingState,
  statGrid,
  tag,
} from '/shared/ui/components.js';
import { table } from '/shared/ui/table.js';
import { renderPageHead } from '/shared/app/page.js';
import { barChart, bindChartTooltip, heatmap, lineChart } from '/shared/ui/chart.js';
import { confirmDialog } from '/shared/ui/modal.js';
import toast from '/shared/ui/toast.js';
import { fetchRecords, notifyError } from '/pages/platform/common.js';

/** 内容库条目类型（取值同后端 ContentItemType） */
const CONTENT_TYPE_MAP = { SONG: '儿歌', STORY: '故事' };

/** 趋势图的指标切换项（key 与 /metrics/trend 的 point 字段名一致） */
const TREND_METRICS = [
  { key: 'interactions', label: '交互次数' },
  { key: 'activeDevices', label: '活跃设备' },
  { key: 'newActivations', label: '新增激活' },
  { key: 'sessionCount', label: '会话数' },
  { key: 'safetyBlocked', label: '安全拦截' },
  { key: 'avgLatencyMs', label: '平均延迟(ms)' },
];

const DAY_MS = 24 * 60 * 60 * 1000;

/** 相对今天偏移 N 天的 YYYY-MM-DD（本地时区） */
function dayOffset(days) {
  return new Date(Date.now() + days * DAY_MS).toISOString().slice(0, 10);
}

/**
 * 比率展示：null / 空 → '—'（**绝不显示 0%**）。
 * 大于 1 的值视为「后端已经换算成百分数」，否则按小数换算。
 */
function pct(value) {
  if (value === null || value === undefined || value === '') return '—';
  const num = Number(value);
  if (!Number.isFinite(num)) return '—';
  return num > 1 ? `${num.toFixed(1)}%` : `${(num * 100).toFixed(1)}%`;
}

/** 静默取数：看板上某个端点失败不该让整页变成报错页，也不该连弹 6 个 toast */
async function safeGet(path, params) {
  try {
    return await api.get(path, { params, silent: true });
  } catch {
    return null;
  }
}

/** 把卡片包一层普通 div：避免 `.card + .card` 的相邻兄弟外边距打乱栅格行 */
function gridCell(cardEl) {
  const wrap = h('div');
  wrap.append(cardEl);
  return wrap;
}

/**
 * 渲染运营看板。
 *
 * @param {HTMLElement} container
 * @param {{query: object}} ctx
 */
export async function renderMetrics(container, ctx) {
  const { query } = ctx;

  clear(container);
  const root = h('div', { class: 'page-body' });
  container.append(root);

  renderPageHead(root, {
    title: '运营看板',
    desc:
      '设备与对话的运营指标。概览为**实时聚合**；趋势 / 24 小时 / 地域 / 内容排行读快照，' +
      '区间内没有数据时请点右上角「刷新快照」重建。',
    actions: [
      {
        label: '刷新快照',
        icon: 'database',
        variant: 'primary',
        action: 'rebuild-snapshot',
        perm: PERM.merchant.metricsRead,
      },
      { label: '刷新', icon: 'refresh', variant: 'ghost', action: 'reload-metrics' },
    ],
  });

  /* ---- 产品下拉（没有产品就没有指标可看） ---- */
  const products = await fetchRecords('/merchant/products');
  if (!products.length) {
    root.append(
      fromHtml(
        emptyState({
          icon: 'package',
          title: '暂无可分析的产品',
          desc: '运营指标按「客户产品」维度统计，本租户还没有客户产品，因此没有可展示的数据。',
        }),
      ),
    );
    return;
  }

  const requested = query?.productId;
  let productId = products.some((item) => String(item.id) === String(requested))
    ? String(requested)
    : String(products[0].id);

  let from = dayOffset(-6);
  let to = dayOffset(0);

  const productSelect = h('select', { class: 'select', style: { maxWidth: '300px' } });
  products.forEach((item) => {
    productSelect.append(h('option', { value: item.id, text: `${item.name}（${item.code}）` }));
  });
  productSelect.value = productId;

  const fromInput = h('input', { class: 'input', type: 'date', value: from, style: { width: '150px' } });
  const toInput = h('input', { class: 'input', type: 'date', value: to, style: { width: '150px' } });

  const toolbar = h('div', { class: 'card mb-4' });
  toolbar.append(
    h(
      'div',
      { class: 'flex items-center gap-3 flex-wrap' },
      h('span', { class: 'text-secondary text-sm', text: '客户产品' }),
      productSelect,
      h('span', { class: 'text-secondary text-sm', text: '统计区间' }),
      fromInput,
      h('span', { class: 'text-secondary', text: '~' }),
      toInput,
      fromHtml('<button type="button" class="btn btn-sm" data-action="apply-range">查询</button>'),
    ),
    h('div', {
      class: 'field-hint mt-2',
      text: '默认展示最近 7 天。24 小时 / 地域 / 内容排行按区间结束日期统计；切换产品立即重新聚合。',
    }),
  );
  root.append(toolbar);

  /* ---- 区块宿主：一次创建、反复 replaceChildren，避免监听器随刷新叠加 ---- */
  const noteHost = h('div', { class: 'mb-4' });
  const overviewHost = h('div');

  const chartsHost = h('div');
  chartsHost.style.cssText =
    'display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px;margin-bottom:16px';

  const trendCard = fromHtml(card({ title: '日趋势', subtitle: '来自快照表，按天聚合' }));
  const hourCard = fromHtml(card({ title: '24 小时分布', subtitle: '交互次数按小时的分布' }));
  const regionCard = fromHtml(card({ title: '地域分布', subtitle: '按地区聚合的交互次数' }));
  const contentCard = fromHtml(card({ title: '内容 Top', subtitle: '故事 / 儿歌被点播的排行' }));

  const trendBody = h('div');
  const trendNote = h('div');
  const hourBody = h('div');
  const hourNote = h('div');
  const regionBody = h('div');
  const regionNote = h('div');
  const contentBody = h('div');

  const metricSelect = h('select', { class: 'select', style: { width: '150px' } });
  TREND_METRICS.forEach((item) => {
    metricSelect.append(h('option', { value: item.key, text: item.label }));
  });
  trendCard.querySelector('.card-head')?.append(metricSelect);
  trendCard.append(trendBody, trendNote);
  hourCard.append(hourBody, hourNote);
  regionCard.append(regionBody, regionNote);
  contentCard.append(contentBody);
  chartsHost.append(
    gridCell(trendCard),
    gridCell(hourCard),
    gridCell(regionCard),
    gridCell(contentCard),
  );

  const retentionCard = fromHtml(
    card({
      title: '留存',
      subtitle: '按激活日期分组的次留 / 3 留 / 7 留 / 30 留；比率为「—」表示该 cohort 尚无数据。',
    }),
  );
  const retentionBody = h('div');
  retentionCard.append(retentionBody);

  root.append(noteHost, overviewHost, chartsHost, retentionCard);

  /* ---- 趋势数据缓存：切换指标只重画图，不重新请求 ---- */
  let trendPoints = [];

  function paintTrendChart() {
    const metric = metricSelect.value;
    const data = trendPoints.map((point) => ({
      label: String(point.date || '').slice(5),
      value: Number(point[metric]) || 0,
    }));
    trendBody.replaceChildren(fromHtml(lineChart({ data, height: 240 })));

    const chartHost = trendBody.querySelector('.chart');
    if (chartHost) {
      bindChartTooltip(chartHost, (index) => {
        const point = trendPoints[index] || {};
        const item = TREND_METRICS.find((entry) => entry.key === metric);
        return html`<div class="chart-tooltip-title">${point.date || ''}</div>
          <div class="chart-tooltip-value">${item?.label || metric}：${formatNumber(point[metric])}</div>`;
      });
    }
  }

  metricSelect.addEventListener('change', paintTrendChart);

  /* ---- 概览（实时聚合） ---- */
  function paintOverview(overview) {
    if (!overview) {
      overviewHost.replaceChildren();
      return;
    }

    const sourceText = overview.source === 'live' ? '实时聚合' : overview.source || '来源未知';
    const rangeText = `${overview.range?.from || from} ~ ${overview.range?.to || to}`;

    overviewHost.replaceChildren(
      fromHtml(
        html`<div class="flex items-center gap-2 flex-wrap mb-3">
          <span class="text-secondary text-sm">统计区间</span>
          <span class="mono">${rangeText}</span>
          <span class="text-secondary text-sm" style="margin-left:8px">数据来源</span>
          ${raw(tag(sourceText, overview.source === 'live' ? 'teal' : 'default', { dot: true }))}
          ${raw(overview.hasSnapshot === false ? tag('快照未生成', 'warning') : tag('快照已生成', 'success'))}
        </div>`,
      ),
    );

    overviewHost.append(
      fromHtml(
        statGrid([
          {
            label: '设备总数',
            value: formatNumber(overview.totalDevices),
            unit: '台',
            icon: 'device',
            tone: 'brand',
            foot: '本产品名下的全部设备',
          },
          {
            label: '已激活',
            value: formatNumber(overview.activatedDevices),
            unit: '台',
            icon: 'checkCircle',
            tone: 'info',
            foot: '终端用户已完成激活',
          },
          {
            label: '活跃设备',
            value: formatNumber(overview.activeDevices),
            unit: '台',
            icon: 'activity',
            tone: 'teal',
            foot: '区间内产生过交互的设备',
          },
          {
            label: '总交互',
            value: formatNumber(overview.totalInteractions),
            unit: '次',
            icon: 'chat',
            tone: 'accent',
            foot: `助手消息 ${formatNumber(overview.assistantMessages)} 条`,
          },
          {
            label: '会话数',
            value: formatNumber(overview.sessionCount),
            unit: '次',
            icon: 'clock',
            tone: 'info',
            foot: `独立终端用户 ${formatNumber(overview.uniqueEndUsers)}`,
          },
          {
            label: '平均端到端延迟',
            value: formatNumber(overview.avgLatencyMs),
            unit: 'ms',
            icon: 'signal',
            tone: 'coral',
            foot: '从收到问题到播报完成',
          },
          {
            label: '安全拦截',
            value: formatNumber(overview.safetyBlocked),
            unit: '次',
            icon: 'shield',
            tone: 'warning',
            foot: '命中内容安全的回复',
          },
          {
            label: '内容命中',
            value: formatNumber(overview.contentHits),
            unit: '次',
            icon: 'book',
            tone: 'brand',
            foot: '故事 / 儿歌被点播',
          },
        ]),
      ),
    );

    const activated = Number(overview.activatedDevices);
    const active = Number(overview.activeDevices);
    overviewHost.append(
      fromHtml(
        kvList([
          ['人均交互（活跃设备）', formatNumber(overview.avgInteractionsPerActiveDevice)],
          ['人均消息（会话）', formatNumber(overview.avgMessagesPerSession)],
          ['活跃设备占比', pct(Number.isFinite(activated) && activated > 0 ? active / activated : null)],
        ]),
      ),
    );
  }

  /* ---- 空态提示：区分「没有数据」与「快照没生成」 ---- */
  function paintNotes(overview, trend) {
    const points = Array.isArray(trend?.points) ? trend.points : [];

    if (!overview) {
      noteHost.replaceChildren(
        fromHtml(
          alert({
            tone: 'danger',
            title: '运营指标读取失败',
            text:
              '概览接口没有返回数据。可能是当前账号缺少指标读取权限、该产品不属于本租户，' +
              '或后端指标端点尚未上线；下方图表将以空态展示。',
          }),
        ),
      );
      return;
    }

    if (points.length > 0) {
      noteHost.replaceChildren();
      return;
    }

    const total = Number(overview.totalInteractions);

    if (overview.hasSnapshot === false) {
      noteHost.replaceChildren(
        fromHtml(
          alert({
            tone: 'info',
            title: '快照还没生成，请点右上角「刷新快照」',
            text:
              '日趋势 / 24 小时 / 地域 / 内容排行读的是快照表。当前所选区间还没有快照数据，' +
              '点「刷新快照」后会按所选日期范围重新聚合写入。',
          }),
        ),
      );
      return;
    }

    if (!Number.isFinite(total) || total === 0) {
      noteHost.replaceChildren(
        fromHtml(
          alert({
            tone: 'neutral',
            title: '该产品还没有对话数据',
            text:
              '该产品在所选区间内没有产生交互（设备未激活、未绑定，或终端用户还没开始对话）。' +
              '可换一个区间，或先确认设备已上线。',
          }),
        ),
      );
      return;
    }

    noteHost.replaceChildren(
      fromHtml(
        alert({
          tone: 'warning',
          title: '快照未覆盖所选区间',
          text:
            `实时聚合显示区间内有 ${formatNumber(total)} 次交互，但快照没有对应的日数据点。` +
            '通常是快照还没跑到这个日期，点「刷新快照」重建后再看趋势。',
        }),
      ),
    );
  }

  /* ---- 24 小时热力 ---- */
  function paintHourly(hourly) {
    const hours = Array.isArray(hourly?.hours) ? hourly.hours : [];

    if (!hours.length) {
      hourBody.replaceChildren(
        fromHtml(html`<div class="p-4 text-center text-sm text-secondary">暂无数据</div>`),
      );
      hourNote.replaceChildren();
      return;
    }

    const values = hours.map((item) => Number(item.interactions) || 0);
    const peak = hours.reduce(
      (best, item) => (Number(item.interactions) > Number(best.interactions) ? item : best),
      hours[0],
    );

    hourBody.replaceChildren(fromHtml(heatmap({ values, label: '交互次数' })));
    hourNote.replaceChildren(
      fromHtml(
        html`<div class="field-hint mt-2">
          峰值时段：${peak.hour}:00（${formatNumber(peak.interactions)} 次交互，
          ${formatNumber(peak.activeDevices)} 台活跃设备）
        </div>`,
      ),
    );

    const chartHost = hourBody.querySelector('.chart');
    if (chartHost) {
      bindChartTooltip(chartHost, (index) => {
        const item = hours[index] || {};
        return html`<div class="chart-tooltip-title">${item.hour ?? index}:00</div>
          <div class="chart-tooltip-value">交互 ${formatNumber(item.interactions)} 次 · 活跃设备 ${formatNumber(item.activeDevices)} 台</div>`;
      });
    }
  }

  /* ---- 地域分布 ---- */
  function paintRegions(regions) {
    const list = Array.isArray(regions?.regions) ? regions.regions : [];
    const data = list.map((item) => ({
      label: item.region || '未知',
      value: Number(item.interactions) || 0,
    }));

    regionBody.replaceChildren(fromHtml(barChart({ data, height: 240, showValues: true })));
    regionNote.replaceChildren();
    if (!list.length) {
      regionNote.append(
        fromHtml(
          html`<div class="field-hint">该区间没有地域数据（设备未上报地区，或快照尚未生成）。</div>`,
        ),
      );
    }

    const chartHost = regionBody.querySelector('.chart');
    if (chartHost) {
      bindChartTooltip(chartHost, (index) => {
        const item = list[index] || {};
        return html`<div class="chart-tooltip-title">${item.region || '未知'}</div>
          <div class="chart-tooltip-value">交互 ${formatNumber(item.interactions)} 次 · 设备 ${formatNumber(item.deviceCount)} 台</div>`;
      });
    }
  }

  /* ---- 内容 Top ---- */
  function paintContents(contents) {
    const records = Array.isArray(contents?.records) ? contents.records : [];
    contentBody.replaceChildren(
      fromHtml(
        table({
          columns: [
            { key: 'rank', title: '#', width: '48px', align: 'num', render: (row) => String(row.rank ?? '') },
            {
              key: 'title',
              title: '内容',
              render: (row) => html`<div class="cell-stack">
                <span class="cell-strong">${row.title || '-'}</span>
                <span class="cell-sub">${CONTENT_TYPE_MAP[row.type] || row.type || ''}</span>
              </div>`,
            },
            {
              key: 'hits',
              title: '点播次数',
              align: 'num',
              width: '110px',
              render: (row) => html`<span class="num">${formatNumber(row.hits)}</span>`,
            },
          ],
          rows: records,
          emptyText: '该区间没有内容点播记录',
        }),
      ),
    );
  }

  /* ---- 留存 ---- */
  function paintRetention(retention) {
    if (!retention) {
      retentionBody.replaceChildren(
        fromHtml(emptyState({ icon: 'alertTriangle', title: '留存数据读取失败', desc: '请稍后重试。' })),
      );
      return;
    }

    const summary = retention.summary || {};
    const cards = [
      { key: 'd1Rate', label: 'D1 留存' },
      { key: 'd3Rate', label: 'D3 留存' },
      { key: 'd7Rate', label: 'D7 留存' },
      { key: 'd30Rate', label: 'D30 留存' },
    ].map((item) => ({
      label: item.label,
      value: pct(summary[item.key]),
      icon: 'users',
      tone: 'brand',
      // null → 明确写「尚无数据」，避免被读成 0%
      foot:
        summary[item.key] === null || summary[item.key] === undefined
          ? '该 cohort 尚无数据'
          : `观察窗口 ${retention.days ?? 30} 天`,
    }));

    const cohorts = Array.isArray(retention.cohorts) ? retention.cohorts : [];
    const tableWrap = h('div', { class: 'mt-4' });
    tableWrap.append(
      fromHtml(
        table({
          columns: [
            { key: 'cohortDate', title: '激活日期', width: '110px', render: (row) => String(row.cohortDate || '') },
            { key: 'activated', title: '激活设备', align: 'num', width: '90px', render: (row) => String(row.activated ?? '') },
            { key: 'd1', title: 'D1', align: 'num', width: '80px', render: (row) => pct(row.d1) },
            { key: 'd3', title: 'D3', align: 'num', width: '80px', render: (row) => pct(row.d3) },
            { key: 'd7', title: 'D7', align: 'num', width: '80px', render: (row) => pct(row.d7) },
            { key: 'd30', title: 'D30', align: 'num', width: '80px', render: (row) => pct(row.d30) },
          ],
          rows: cohorts,
          emptyText: '所选窗口内还没有激活的设备',
        }),
      ),
    );

    retentionBody.replaceChildren(
      fromHtml(statGrid(cards)),
      tableWrap,
      fromHtml(
        kvList([
          ['流失设备数', formatNumber(retention.churnedDevices)],
          ['回访率', pct(retention.returnRate)],
          [
            '平均回访间隔',
            retention.avgIntervalDays === null || retention.avgIntervalDays === undefined
              ? '—'
              : `${formatNumber(retention.avgIntervalDays)} 天`,
          ],
        ]),
      ),
    );
  }

  /* ---- 一次性取全部指标 ---- */
  async function loadAll() {
    noteHost.replaceChildren(fromHtml(loadingState('正在聚合运营指标…')));
    overviewHost.replaceChildren();
    trendBody.replaceChildren(fromHtml(loadingState('加载中…')));
    trendNote.replaceChildren();
    hourBody.replaceChildren(fromHtml(loadingState('加载中…')));
    hourNote.replaceChildren();
    regionBody.replaceChildren(fromHtml(loadingState('加载中…')));
    regionNote.replaceChildren();
    contentBody.replaceChildren(fromHtml(loadingState('加载中…')));
    retentionBody.replaceChildren(fromHtml(loadingState('正在计算留存…')));

    const params = { productId, from, to };
    const [overview, trend, hourly, regions, contents, retention] = await Promise.all([
      safeGet('/merchant/metrics/overview', params),
      safeGet('/merchant/metrics/trend', { ...params, granularity: 'day' }),
      safeGet('/merchant/metrics/hourly', { productId, date: to }),
      safeGet('/merchant/metrics/regions', { productId, date: to }),
      safeGet('/merchant/metrics/contents', { productId, date: to, limit: 10 }),
      safeGet('/merchant/metrics/retention', { productId, days: 30 }),
    ]);

    paintNotes(overview, trend);
    paintOverview(overview);

    trendPoints = Array.isArray(trend?.points) ? trend.points : [];
    paintTrendChart();
    trendNote.replaceChildren();
    if (!trendPoints.length) {
      trendNote.append(
        fromHtml(
          html`<div class="field-hint">
            ${overview?.hasSnapshot === false ? '快照尚未生成，请点「刷新快照」。' : '所选区间没有日数据点。'}
          </div>`,
        ),
      );
    }

    paintHourly(hourly);
    paintRegions(regions);
    paintContents(contents);
    paintRetention(retention);
  }

  /* ---- 重建快照 ---- */
  async function rebuildSnapshot() {
    const { confirmed } = await confirmDialog({
      title: '刷新运营快照',
      description: `将按 ${from} ~ ${to} 重新聚合该产品的日 / 小时 / 地域 / 内容排行数据并写入快照表。`,
      detail: '重建只写入快照表，不会修改原始对话数据；区间越大耗时越长。',
      tone: 'warning',
      confirmText: '开始重建',
    });
    if (!confirmed) return;

    const handle = toast.loading('正在重建快照…');
    try {
      const result = await api.post(
        '/merchant/metrics/rebuild',
        { productId, dateFrom: from, dateTo: to },
        { idempotencyKey: api.newIdempotencyKey() },
      );
      handle.close();
      // 行数必须回显：用户凭它判断「重建是否真的写了东西」，而不是只看一句成功
      toast.success(
        `快照已重建：日汇总 ${result?.dailyRows ?? 0} 行 / 小时 ${result?.hourlyRows ?? 0} 行 / ` +
          `地域 ${result?.regionRows ?? 0} 行 / 内容 ${result?.contentRows ?? 0} 行` +
          `${Array.isArray(result?.dates) ? `（覆盖 ${result.dates.length} 天）` : ''}`,
        { duration: 8000 },
      );
      await loadAll();
    } catch (error) {
      handle.close();
      notifyError(error, '快照重建失败');
    }
  }

  /* ---- 交互接线（绑在本次渲染新建的 root 上，随页面销毁） ---- */
  productSelect.addEventListener('change', () => {
    productId = productSelect.value;
    loadAll();
  });
  fromInput.addEventListener('change', () => {
    from = fromInput.value || from;
  });
  toInput.addEventListener('change', () => {
    to = toInput.value || to;
  });

  root.addEventListener('click', (event) => {
    const target = event.target.closest('[data-action]');
    if (!target) return;
    const action = target.dataset.action;

    if (action === 'apply-range') {
      if (from > to) {
        toast.warning('开始日期不能晚于结束日期');
        return;
      }
      loadAll();
      return;
    }
    if (action === 'reload-metrics') {
      loadAll();
      return;
    }
    if (action === 'rebuild-snapshot') {
      rebuildSnapshot();
    }
  });

  await loadAll();
}

export default { renderMetrics };
