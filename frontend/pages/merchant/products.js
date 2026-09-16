/* ============================================================
   商户端 · 我的产品
   ------------------------------------------------------------
   路由：`#/products`（列表）

   数据来源与「不要臆造字段」
   --------------------------
   列表复用 P4 为下单选择而做的 `GET /merchant/products`
   （响应复用平台端的 ClientProductResponse，租户隔离在服务层收口）。
   P9 契约给这个接口追加了统计与 AI 摘要字段
   （`deviceCount` / `activatedDeviceCount` / `totalInteractions` /
   `aiConfigSummary`），但**列表端点可能尚未返回它们**。

   因此本页的取值策略是「有则显示，无则显示 -」：
   不伪造 0（0 会被误读成「确实没有设备」），也不在列表里逐行回查
   详情接口 —— 那是一次点击 20 个请求的 N+1，列表页不能这么干。
   缺字段时页头会挂一条说明，引导用户去详情页看完整数字；
   详情页走 `GET /merchant/products/{id}`，那里是契约承诺的完整形状。

   AI 配置状态列
   --------------
   优先用 `aiConfigSummary`（status 取值与后端 EnableStatus 一致：
   ENABLED / DISABLED）；列表未返回摘要时退化为平台字段 `aiEnabled`。
   供应商未配置密钥时额外挂一个警示标签 —— 「配置了但密钥没配」
   与「没配置」是两种不同的故障，混成一个标签会让人查错方向。
   ============================================================ */

import api from '/shared/core/api.js';
import { formatNumber, fromHtml, html, raw } from '/shared/ui/dom.js';
import { alert, statusTag, tag } from '/shared/ui/components.js';
import { createListPage } from '/shared/app/page.js';
import { ENABLE_OPTIONS, NETWORK_MAP } from '/pages/platform/common.js';

/** AI 配置启用状态（取值同后端 EnableStatus，见 models/ai.py 的 AiConfig.status） */
const AI_ENABLE_MAP = {
  ENABLED: { text: '已启用', tone: 'success' },
  DISABLED: { text: '未启用', tone: 'default' },
};

/** 统计列：数字用 formatNumber（空值统一回落 '-'，不伪造 0） */
function numCell(value) {
  return html`<span class="num">${formatNumber(value)}</span>`;
}

/** 表格列定义 */
const COLUMNS = [
  {
    key: 'name',
    title: '产品',
    sortable: true,
    render: (row) => html`<div class="cell-stack">
      <span class="cell-strong">${row.name || '-'}</span>
      <span class="cell-sub mono">${row.code || row.id}</span>
    </div>`,
  },
  {
    key: 'network_type',
    title: '联网方式',
    width: '110px',
    render: (row) => statusTag(row.networkType, NETWORK_MAP),
  },
  {
    key: 'template_name',
    title: '来源模板',
    render: (row) => html`<div class="cell-stack">
      <span>${row.templateName || '-'}</span>
      <span class="cell-sub mono">${row.templateCode || row.templateId || '-'}</span>
    </div>`,
  },
  {
    key: 'firmware_version',
    title: '固件版本',
    width: '110px',
    render: (row) => html`<span class="mono">${row.firmwareVersion || '-'}</span>`,
  },
  { key: 'device_count', title: '设备数', align: 'num', width: '90px', render: (row) => numCell(row.deviceCount) },
  {
    key: 'activated_device_count',
    title: '已激活',
    align: 'num',
    width: '90px',
    render: (row) => numCell(row.activatedDeviceCount),
  },
  {
    key: 'total_interactions',
    title: '累计交互',
    align: 'num',
    width: '110px',
    render: (row) => numCell(row.totalInteractions),
  },
  {
    key: 'ai_config',
    title: 'AI 配置',
    width: '150px',
    render: (row) => {
      const summary = row.aiConfigSummary;
      if (!summary) {
        // 列表未返回摘要 → 退化到平台字段，不编造状态
        return row.aiEnabled ? tag('已配置', 'success') : tag('未配置', 'warning');
      }
      return html`<div class="cell-stack">
        ${raw(statusTag(summary.status, AI_ENABLE_MAP))}
        ${raw(
          summary.providerConfigured === false
            ? html`<span class="cell-sub">${tag('供应商未配置密钥', 'danger')}</span>`
            : html`<span class="cell-sub">${summary.providerName || summary.providerCode || ''}</span>`,
        )}
      </div>`;
    },
  },
  {
    key: 'actions',
    title: '操作',
    align: 'right',
    width: '90px',
    nowrap: true,
    render: (row) =>
      html`<div class="btn-group">
        <button type="button" class="btn btn-sm" data-action="open-product" data-id="${row.id}">详情</button>
      </div>`,
  },
];

/**
 * 渲染「我的产品」页。
 *
 * @param {HTMLElement} container
 * @param {{router: object}} ctx
 */
export function renderProducts(container, ctx) {
  const { router } = ctx;

  return createListPage({
    container,
    title: '我的产品',
    desc: '本租户名下的客户产品（服务端强制按租户隔离）。点「详情」可查看设备与 AI 配置摘要，并跳转到 AI 配置 / 运营看板。',
    actions: [{ label: '刷新', icon: 'refresh', action: 'reload-list' }],
    columns: COLUMNS,
    rowAction: 'open-product',
    fetcher: (params) =>
      api.get('/merchant/products', {
        params: {
          page: params.page,
          pageSize: params.pageSize,
          sortBy: params.sortBy,
          order: params.order,
          keyword: params.keyword,
          status: params.status,
        },
      }),
    filters: [
      { key: 'keyword', placeholder: '名称 / 编码', width: '220px' },
      { key: 'status', type: 'select', options: [{ value: '', label: '全部状态' }, ...ENABLE_OPTIONS] },
    ],
    onReady: (host, instance) => {
      // 列表缺统计字段时给一句解释，避免用户面对一整列 '-' 以为是数据坏了
      if (instance.state.loading || !instance.rows.length) return;
      const lacksStats = instance.rows.some((row) => row.deviceCount === undefined);
      if (!lacksStats) return;
      host.prepend(
        fromHtml(
          alert({
            tone: 'neutral',
            title: '列表未包含统计字段',
            text:
              '当前产品列表接口没有返回设备数 / 激活数 / 累计交互等统计字段，因此这些列显示为 -。' +
              '请点「详情」查看完整数字（详情接口按契约返回统计与 AI 配置摘要）。',
          }),
        ),
      );
    },
    onAction: async (action, target, { table: tbl }) => {
      if (action === 'reload-list') {
        tbl.load();
        return;
      }

      if (action === 'open-product') {
        // 行点击（data-key）与按钮点击（data-id）两种来源都要兼容
        const id = target.dataset.id || target.dataset.key;
        if (!id) return;
        router.goByName('merchantProductDetail', { id });
      }
    },
  });
}

export default { renderProducts };
