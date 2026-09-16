/* ============================================================
   工厂端 · 工作台
   ------------------------------------------------------------
   P6 起统计项全部取自真实接口 `GET /factory/stats`，
   接口未就绪时显示「—」而不是编造数字。

   这一页同时承担 P6 的核心说明职责：工厂端能看到什么、看不到什么。
   「字段脱敏规则」卡片的文案必须与后端序列化层的行为**逐条对应**：
     客户名称 → 服务端脱敏为「中**动」形式（首字符 + 星号 + 尾字符）
     订单金额 / 联系方式 / 邮箱 → 服务端根本不下发
   写成「前端隐藏」是不准确的：前端拿不到这些字段，隐藏无从谈起。

   另外补了「近期工单」区块，直接复用 `/factory/orders` 的前 5 条——
   工作台的职责是「一眼看到接下来要做什么」，只给数字不给单据，
   用户还得再点一次才能开工。
   ============================================================ */

import api from '/shared/core/api.js';
import { formatDay, html, raw } from '/shared/ui/dom.js';
import { alert, card, emptyState, kvList, progress, statusTag } from '/shared/ui/components.js';
import { table } from '/shared/ui/table.js';
import { renderDashboard as render } from '/shared/app/dashboard.js';
import { FACTORY_ORDER_STATUS_MAP } from './orders.js';

/** 工厂端视角的五步流程 */
const FACTORY_STEPS = [
  { title: '接收订单' },
  { title: '烧录固件' },
  { title: '贴二维码' },
  { title: '扫码抽检' },
  { title: '出货登记' },
];

/** 近期工单的列定义（工作台只需「做什么、做多少、到哪一步」） */
const RECENT_COLUMNS = [
  {
    key: 'factory_order_no',
    title: '工单号',
    render: (row) => html`<div class="cell-stack">
      <span class="cell-strong mono">${row.factoryOrderNo || row.id}</span>
      <span class="cell-sub mono">${row.orderNo || '—'}</span>
    </div>`,
  },
  {
    key: 'customer_name_masked',
    title: '客户（脱敏）',
    render: (row) => html`<span>${row.customerNameMasked || '—'}</span>`,
  },
  {
    key: 'product_model',
    title: '型号 / 固件',
    render: (row) => html`<div class="cell-stack">
      <span>${row.productModel || '—'}</span>
      <span class="cell-sub mono">${row.firmwareVersion || '—'}</span>
    </div>`,
  },
  {
    key: 'progress',
    title: '进度',
    width: '140px',
    render: (row) => {
      const percent = Number(row.progressPercent ?? 0);
      return html`<div class="flex items-center gap-2">
        ${raw(progress(percent, { tone: percent >= 100 ? 'success' : 'brand' }))}
        <span class="text-xs text-secondary num">${percent.toFixed(0)}%</span>
      </div>`;
    },
  },
  {
    key: 'due_at',
    title: '交期',
    width: '110px',
    render: (row) => (row.dueAt ? formatDay(row.dueAt) : '—'),
  },
  {
    key: 'status',
    title: '状态',
    width: '110px',
    render: (row) => statusTag(row.status, FACTORY_ORDER_STATUS_MAP),
  },
];

/** 字段脱敏说明（文案与后端行为一致，且明确「不是前端隐藏」） */
function maskingBody() {
  const rules = [
    ['客户名称', '首字符 + 星号 + 尾字符（如 <span class="mono">中**动</span>）'],
    ['订单金额', '<span class="text-disabled">服务端不下发</span>'],
    ['联系方式', '<span class="text-disabled">服务端不下发</span>'],
    ['邮箱', '<span class="text-disabled">服务端不下发</span>'],
  ];
  return `
    <div class="mb-3">
      ${alert({
        tone: 'warning',
        text: '以下字段由服务端序列化层处理：工厂端接口**不会返回**真实值，前端不是「隐藏」而是根本拿不到。',
      })}
    </div>
    <div class="kv-list">
      ${rules
        .map(
          ([key, value]) =>
            `<div class="kv-row"><span class="kv-key">${key}</span><span class="kv-value">${value}</span></div>`,
        )
        .join('')}
    </div>
  `;
}

/**
 * 渲染工厂端工作台。
 *
 * @param {HTMLElement} container
 */
export async function renderDashboard(container) {
  /* ---- 真实统计 + 近期工单（各自静默容错，任一失败不影响另一块） ---- */
  const [stats, recentPayload] = await Promise.all([
    api.get('/factory/stats', { silent: true }).catch(() => null),
    api
      .get('/factory/orders', {
        params: { pageSize: 5, sortBy: 'createdAt', order: 'desc' },
        silent: true,
      })
      .catch(() => null),
  ]);

  const value = (key) => stats?.[key] ?? '—';
  /** 百分比单独处理：接口未就绪时显示「—」而不是「—%」 */
  const percentText = (key) =>
    stats?.[key] === undefined || stats?.[key] === null ? '—' : `${stats[key]}%`;
  const recent = Array.isArray(recentPayload?.records) ? recentPayload.records : [];

  /** 四张统计卡：待生产 / 生产中 / 已完成 / 固件版本数（口径见各卡 foot） */
  const statCards = [
    {
      label: '待生产订单',
      unit: '单',
      icon: 'clipboard',
      tone: 'accent',
      foot: `工单总数 ${value('totalOrders')}`,
      load: () => value('pendingOrders'),
    },
    {
      label: '生产中订单',
      unit: '单',
      icon: 'fire',
      tone: 'brand',
      foot: `累计烧录 ${value('burnedQuantity')} / ${value('totalQuantity')} 台`,
      load: () => value('producingOrders'),
    },
    {
      label: '已完成订单',
      unit: '单',
      icon: 'checkCircle',
      tone: 'teal',
      foot: `已出货 ${value('shippedOrders')} 单`,
      load: () => value('completedOrders'),
    },
    {
      label: '固件版本',
      unit: '个',
      icon: 'disk',
      tone: 'coral',
      foot: '本工厂工单涉及的固件版本',
      load: () => value('firmwareCount'),
    },
  ];

  /* ---- 生产概览：把「台数 / 进度 / 抽检」这些不占卡片位的数字放到一张卡里 ---- */
  const overviewCard = card({
    title: '生产概览',
    subtitle: '来自 /factory/stats',
    body: kvList([
      ['工单总数', `${value('totalOrders')} 单`],
      ['需求总台数', `${value('totalQuantity')} 台`],
      ['累计烧录', `${value('burnedQuantity')} 台`],
      ['整体烧录进度', percentText('burnProgressPercent')],
      ['抽检总次数', `${value('inspectionTotal')} 次`],
      ['抽检合格', `${value('inspectionPass')} 次`],
      ['抽检不合格', `${value('inspectionFail')} 次`],
    ]),
  });

  const maskCard = card({ title: '字段脱敏规则', body: maskingBody() });

  const recentCard = card({
    title: '近期工单',
    subtitle: '最近 5 条',
    actions: '<a class="btn btn-sm btn-ghost" href="#/orders">查看全部</a>',
    body: recent.length
      ? table({ columns: RECENT_COLUMNS, rows: recent, emptyText: '暂无生产工单' })
      : emptyState({
          icon: 'clipboard',
          title: '暂无生产工单',
          desc: '平台把订单派给工厂后，工单会出现在这里，并带上型号、固件版本与交期。',
        }),
  });

  return render(container, {
    desc: '工厂端 · 跨租户承接生产任务，客户信息已脱敏',
    scopeLabel: '跨租户（字段脱敏）',

    stats: statCards,

    flowSteps: FACTORY_STEPS,
    flowTitle: '生产流程',
    flowSubtitle: '从接单到出货',
    flowNote:
      '工厂端只看到生产必需的信息：产品型号、固件版本、数量与二维码清单。客户名称在服务端脱敏为「中**动」形式，金额、联系方式与邮箱则**根本不下发**。',

    extraCards: [overviewCard, maskCard, recentCard],
  });
}

export default { renderDashboard };
