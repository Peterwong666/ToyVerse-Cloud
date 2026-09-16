/* ============================================================
   平台端 · 工厂订单
   ------------------------------------------------------------
   对应后端：
     GET /platform/factories          （工厂下拉；只列 ENABLED）
     GET /platform/factory-orders     （列表：factoryId / status / keyword）
     GET /platform/factory-orders/{id}

   与工厂端 `/factory/orders` 的关系
   --------------------------------
   同一个资源、两种视角：工厂端拿到的是**脱敏后**的序列化结果
   （客户名「中**动」、金额与联系方式不下发），平台端可见完整信息。
   因此本页的客户列优先显示 `customerName`，取不到才退回脱敏字段 ——
   这样后端即使某天收紧了白名单（改为只回 customerNameMasked），
   页面也不会出现空白列。

   详情为什么用弹窗而不是新建详情页
   ------------------------------
   本阶段平台端不需要「工厂工单详情」这条路由：派单动作已经放在
   订单详情页（order_detail.js 的「派单给工厂」），工厂工单在这里
   主要是**核对**用途（烧了多少、抽检结果如何）。就地弹窗既能看全
   burnReports / inspections，又少一条与订单详情重复的路由。
   ============================================================ */

import api from '/shared/core/api.js';
import { esc, formatDate, formatDay, fromHtml, h, html, raw } from '/shared/ui/dom.js';
import { descList, emptyState, loadingState, statGrid, statusTag } from '/shared/ui/components.js';
import { table } from '/shared/ui/table.js';
import { createListPage } from '/shared/app/page.js';
import { modal } from '/shared/ui/modal.js';
import {
  FACTORY_ORDER_STATUS_MAP,
  FACTORY_ORDER_STATUS_OPTIONS,
  INSPECTION_RESULT_MAP,
} from '/pages/factory/orders.js';
import { fetchRecords, notifyError, toOptions } from './common.js';

/** 展示时间 */
function showTime(value) {
  return value ? formatDate(value) : '';
}

/** 卡片外壳（标题 + 内容节点）。标题经 esc 处理，调用方不必自证安全 */
function cardWith(title, children) {
  const wrap = h('div', { class: 'card mt-4' });
  wrap.append(fromHtml(`<div class="card-head"><div class="card-title">${esc(title)}</div></div>`));
  (Array.isArray(children) ? children : [children]).forEach((node) => node && wrap.append(node));
  return wrap;
}

/* ------------------------------------------------------------
   一、详情弹窗
   ------------------------------------------------------------ */

async function openFactoryOrderDetail(row) {
  const body = h('div');
  body.append(fromHtml(loadingState('正在读取工单详情…')));
  const dialog = modal({
    title: `工厂工单 ${row.factoryOrderNo || row.id}`,
    subtitle: '平台端可见完整信息（工厂端同一资源为脱敏视图）',
    size: 'xl',
    body,
  });

  try {
    const data = await api.get(`/platform/factory-orders/${row.id}`);
    const total = Number(data.quantity ?? 0);
    const burned = Number(data.burnedCount ?? 0);
    const remaining = Number(data.remaining ?? Math.max(0, total - burned));
    const percent = Number(data.progressPercent ?? (total ? (burned / total) * 100 : 0));

    body.replaceChildren(
      fromHtml(
        statGrid([
          { label: '数量', value: total, unit: '台', icon: 'package', tone: 'brand', foot: `平台订单 ${data.orderNo || '—'}` },
          { label: '已烧录', value: burned, unit: '台', icon: 'fire', tone: 'accent', foot: `上报 ${(data.burnReports || []).length} 次` },
          { label: '剩余', value: remaining, unit: '台', icon: 'clipboard', tone: remaining ? 'warning' : 'success', foot: '烧录达标后转「烧录完成」' },
          {
            label: '进度',
            value: `${percent.toFixed(0)}%`,
            icon: 'chartBar',
            tone: percent >= 100 ? 'success' : 'teal',
            foot: FACTORY_ORDER_STATUS_MAP[data.status]?.text || data.statusLabel || data.status,
          },
        ]),
      ),
    );

    body.append(
      cardWith(
        '工单信息',
        fromHtml(
          descList([
            ['工单号', data.factoryOrderNo],
            ['平台订单号', data.orderNo],
            ['承接工厂', data.factoryName || data.factoryId],
            ['客户', data.customerName || data.customerNameMasked],
            ['型号', data.productModel],
            ['固件版本', data.firmwareVersion],
            ['工单状态', FACTORY_ORDER_STATUS_MAP[data.status]?.text || data.statusLabel || data.status],
            ['平台订单状态', data.orderStatusLabel || data.orderStatus],
            ['数量', data.quantity],
            ['已烧录', data.burnedCount],
            ['剩余', data.remaining],
            ['派单时间', showTime(data.assignedAt)],
            ['交期', showTime(data.dueAt)],
            ['出货时间', showTime(data.shippedAt)],
            ['生产要求', data.productionNote],
          ]),
        ),
      ),
    );

    /* ---- 烧录记录 ---- */
    const burns = Array.isArray(data.burnReports) ? data.burnReports : [];
    body.append(
      cardWith(
        '烧录记录',
        fromHtml(
          burns.length
            ? table({
                columns: [
                  { key: 'burned_count', title: '本次上报', align: 'num', width: '100px', render: (r) => html`<span class="num">${r.burnedCount ?? 0}</span>` },
                  {
                    key: 'sn_range',
                    title: 'SN 区间',
                    render: (r) =>
                      r.snFrom || r.snTo
                        ? html`<span class="mono text-sm">${r.snFrom || '—'} ~ ${r.snTo || '—'}</span>`
                        : html`<span class="text-secondary">未填写</span>`,
                  },
                  { key: 'operator', title: '操作人', width: '120px', render: (r) => esc(r.operator || '—') },
                  { key: 'note', title: '备注', render: (r) => esc(r.note || '—') },
                  { key: 'reported_at', title: '上报时间', width: '160px', render: (r) => esc(formatDate(r.reportedAt || r.createdAt)) },
                ],
                rows: burns,
                emptyText: '暂无烧录记录',
              })
            : emptyState({ icon: 'fire', title: '还没有烧录记录', desc: '工厂上报后这里会按批次列出。' }),
        ),
      ),
    );

    /* ---- 抽检记录 ---- */
    const inspections = Array.isArray(data.inspections) ? data.inspections : [];
    const summary = data.inspectionSummary || {};
    const inspectNodes = [
      fromHtml(
        descList(
          [
            ['抽检总数', summary.total ?? inspections.length],
            ['合格', summary.passCount ?? 0],
            ['不合格', summary.failCount ?? 0],
          ],
          { cols: 2 },
        ),
      ),
      fromHtml(
        inspections.length
          ? table({
              columns: [
                { key: 'sn', title: 'SN', render: (r) => html`<span class="mono text-sm">${r.sn || '—'}</span>` },
                { key: 'result', title: '结果', width: '110px', render: (r) => statusTag(r.result, INSPECTION_RESULT_MAP) },
                {
                  key: 'defect_code',
                  title: '不良代码',
                  width: '140px',
                  render: (r) => (r.defectCode ? html`<span class="mono text-sm">${r.defectCode}</span>` : html`<span class="text-secondary">—</span>`),
                },
                { key: 'inspector', title: '抽检人', width: '120px', render: (r) => esc(r.inspector || '—') },
                { key: 'note', title: '备注', render: (r) => esc(r.note || '—') },
                { key: 'inspected_at', title: '抽检时间', width: '160px', render: (r) => esc(formatDate(r.inspectedAt || r.createdAt)) },
              ],
              rows: inspections,
              emptyText: '暂无抽检记录',
            })
          : emptyState({ icon: 'checkCircle', title: '还没有抽检记录', desc: '工厂登记抽检结果后会出现在这里。' }),
      ),
    ];
    body.append(cardWith('抽检记录', inspectNodes));
  } catch (error) {
    body.replaceChildren(
      fromHtml(
        emptyState({
          icon: 'alertTriangle',
          title: '工单详情读取失败',
          desc: error?.message || '请稍后重试',
        }),
      ),
    );
  }

  return dialog.result;
}

/* ------------------------------------------------------------
   二、列表
   ------------------------------------------------------------ */

/** 列定义（工厂名称靠下拉数据映射） */
function buildColumns(factoryLabels) {
  return [
    {
      key: 'factory_order_no',
      title: '工单号',
      sortable: true,
      render: (row) => html`<div class="cell-stack">
        <button type="button" class="btn-link text-left" data-action="open-factory-order" data-id="${esc(row.id)}">
          ${row.factoryOrderNo || row.id}
        </button>
        <span class="cell-sub mono">${row.orderNo || '—'}</span>
      </div>`,
    },
    {
      key: 'factory_name',
      title: '承接工厂',
      render: (row) => {
        const known = factoryLabels.get(String(row.factoryId));
        return html`<div class="cell-stack">
          <span>${row.factoryName || known?.name || '—'}</span>
          <span class="cell-sub mono">${known?.code || row.factoryId || '—'}</span>
        </div>`;
      },
    },
    {
      key: 'customer_name',
      title: '客户',
      // 平台端可见完整客户名；后端若收紧白名单只回脱敏值，也不会空白
      render: (row) => html`<div class="cell-stack">
        <span>${row.customerName || row.customerNameMasked || '—'}</span>
        ${raw(
          row.customerName && row.customerNameMasked
            ? `<span class="cell-sub mono">${esc(row.customerNameMasked)}</span>`
            : '',
        )}
      </div>`,
    },
    {
      key: 'product_model',
      title: '型号 / 固件',
      render: (row) => html`<div class="cell-stack">
        <span>${row.productModel || '—'}</span>
        <span class="cell-sub mono">${row.firmwareVersion || '—'}</span>
      </div>`,
    },
    { key: 'quantity', title: '数量', align: 'num', width: '80px', render: (row) => esc(row.quantity ?? '—') },
    {
      key: 'burned_count',
      title: '已烧录',
      align: 'num',
      width: '110px',
      render: (row) => html`<span class="num">${row.burnedCount ?? 0}</span>
        <span class="text-secondary"> / 剩 ${row.remaining ?? 0}</span>`,
    },
    {
      key: 'progress',
      title: '进度',
      width: '110px',
      align: 'num',
      render: (row) => esc(`${Number(row.progressPercent ?? 0).toFixed(0)}%`),
    },
    {
      key: 'status',
      title: '状态',
      width: '110px',
      render: (row) => statusTag(row.status, FACTORY_ORDER_STATUS_MAP),
    },
    {
      key: 'due_at',
      title: '交期',
      sortable: true,
      width: '120px',
      render: (row) => esc(row.dueAt ? formatDay(row.dueAt) : '—'),
    },
    {
      key: 'actions',
      title: '操作',
      align: 'right',
      width: '110px',
      nowrap: true,
      render: (row) =>
        html`<button type="button" class="btn btn-sm btn-ghost" data-action="open-factory-order" data-id="${esc(row.id)}">详情</button>`,
    },
  ];
}

/**
 * 渲染平台端工厂订单列表。
 *
 * @param {HTMLElement} container
 * @param {{router: object}} ctx
 */
export async function renderFactoryOrders(container) {
  // 工厂下拉静默容错：取不到只影响名称展示（会退回显示 factoryId），不影响浏览
  const factories = await fetchRecords('/platform/factories');
  const enabled = factories.filter((item) => item.status === 'ENABLED');
  const factoryOptions = toOptions(enabled, (item) => `${item.name}（${item.code}）`);
  const factoryLabels = new Map(
    factories.map((item) => [String(item.id), { name: item.name, code: item.code }]),
  );

  return createListPage({
    container,
    title: '工厂订单',
    desc: '平台派发给工厂的生产工单。工厂端看到的是脱敏视图（客户名「中**动」，金额与联系方式不下发），平台端可见完整信息。派单入口在订单详情页。',
    actions: [{ label: '刷新', icon: 'refresh', action: 'reload-list' }],
    columns: buildColumns(factoryLabels),
    fetcher: (params) =>
      api.get('/platform/factory-orders', {
        params: {
          page: params.page,
          pageSize: params.pageSize,
          sortBy: params.sortBy,
          order: params.order,
          keyword: params.keyword,
          factoryId: params.factoryId,
          status: params.status,
        },
      }),
    filters: [
      { key: 'keyword', placeholder: '工单号 / 平台订单号', width: '220px' },
      {
        key: 'factoryId',
        type: 'select',
        options: [{ value: '', label: '全部工厂' }, ...factoryOptions],
      },
      { key: 'status', type: 'select', options: FACTORY_ORDER_STATUS_OPTIONS },
    ],
    onAction: async (action, target, { table }) => {
      if (action === 'reload-list') {
        table.load();
        return;
      }
      if (action === 'open-factory-order') {
        const row = target.dataset.id ? table.findRowById(target.dataset.id) : null;
        if (!row) return;
        try {
          await openFactoryOrderDetail(row);
        } catch (error) {
          notifyError(error, '工单详情读取失败');
        }
      }
    },
  });
}

export default { renderFactoryOrders };
