/* ============================================================
   工厂端 · 固件版本
   ------------------------------------------------------------
   对应后端 `GET /factory/firmwares`：
     { records: [{ firmwareVersion, orderCount, totalQuantity, burnedCount }] }

   为什么这一页要自己在前端分页
   --------------------------
   该接口**不接受分页参数**：它按 firmware_version 聚合后一次性返回全部
   版本（版本量级是几十，远小于工单/设备）。若直接把它接到 DataTable 上，
   翻到第 2 页时拿到的仍是同一批记录 —— 那是「看起来能翻页、其实数据没变」
   的假分页。因此这里一次取回后按请求页在本地切片，页数与数据严格一致；
   同时把结果缓存起来，翻页不再重复请求（刷新按钮会清缓存）。
   ============================================================ */

import api from '/shared/core/api.js';
import { esc, html, raw } from '/shared/ui/dom.js';
import { progress } from '/shared/ui/components.js';
import { createListPage } from '/shared/app/page.js';

const COLUMNS = [
  {
    key: 'firmwareVersion',
    title: '固件版本',
    sortable: true,
    render: (row) => html`<span class="mono cell-strong">${row.firmwareVersion || '—'}</span>`,
  },
  {
    key: 'orderCount',
    title: '关联工单数',
    align: 'num',
    width: '120px',
    render: (row) => esc(row.orderCount ?? 0),
  },
  {
    key: 'totalQuantity',
    title: '总台数',
    align: 'num',
    width: '110px',
    render: (row) => esc(row.totalQuantity ?? 0),
  },
  {
    key: 'burnedCount',
    title: '已烧录',
    align: 'num',
    width: '110px',
    render: (row) => html`<span class="num">${row.burnedCount ?? 0}</span>
      <span class="text-secondary"> / ${row.totalQuantity ?? 0}</span>`,
  },
  {
    key: 'burn_progress',
    title: '烧录进度',
    width: '180px',
    // 后端未给该版本的百分比字段，用「已烧录 / 总台数」在前端派生；
    // 总台数为 0 时直接给 0，避免除零得到 NaN
    render: (row) => {
      const total = Number(row.totalQuantity ?? 0);
      const burned = Number(row.burnedCount ?? 0);
      const percent = total > 0 ? Math.min(100, (burned / total) * 100) : 0;
      return html`<div class="flex items-center gap-2">
        ${raw(progress(percent, { tone: percent >= 100 ? 'success' : 'brand' }))}
        <span class="text-xs text-secondary num">${percent.toFixed(0)}%</span>
      </div>`;
    },
  },
];

/**
 * 渲染固件版本列表。
 *
 * @param {HTMLElement} container
 * @param {{router: object}} ctx
 */
export async function renderFirmware(container) {
  /** 一次请求的结果缓存；翻页复用，点「刷新」时置空 */
  let cache = null;

  const fetcher = async (params) => {
    if (!cache) {
      const payload = await api.get('/factory/firmwares');
      cache = Array.isArray(payload?.records) ? payload.records : [];
    }
    const start = (params.page - 1) * params.pageSize;
    return {
      records: cache.slice(start, start + params.pageSize),
      total: cache.length,
    };
  };

  return createListPage({
    container,
    title: '固件版本',
    desc: '本工厂已承接工单涉及的固件版本，以及各版本的烧录完成情况。版本由平台派单时指定，工厂端不修改。',
    actions: [{ label: '刷新', icon: 'refresh', action: 'reload-list' }],
    columns: COLUMNS,
    fetcher,
    onAction: (action, target, { table }) => {
      if (action !== 'reload-list') return;
      // 清缓存再加载，保证「刷新」真的重新取数（而不是翻本地那份副本）
      cache = null;
      // 失败时 DataTable 自带错误态与「重新加载」按钮，这里不重复提示
      table.load();
    },
  });
}

export default { renderFirmware };
