/* ============================================================
   平台端 · 订单管理
   ------------------------------------------------------------
   对应后端 `/platform/orders`：
     GET  列表（筛选 status / tenantId / clientProductId / keyword）
     POST /{id}/audit   审核（通过 / 驳回）

   本页只承担「浏览 + 审核」两件事，**生成设备 / 二维码** 放在详情页
   （order_detail.js）。原因与租户页一致：生成设备是有副作用的低频动作，
   放在需要「先点进某一单」的位置能显著降低误触概率。

   状态机（与后端 OrderStatus 一致）：
     PENDING_AUDIT → APPROVED | REJECTED → GENERATING → GENERATED → …
   因此列表里只有 PENDING_AUDIT 的行会出现「审核」按钮 ——
   按钮不出现比「点了才报 409」体验更好。
   ============================================================ */

import api from '/shared/core/api.js';
import auth, { PERM } from '/shared/core/auth.js';
import { esc, formatDate, html, raw } from '/shared/ui/dom.js';
import { statusTag } from '/shared/ui/components.js';
import { createListPage } from '/shared/app/page.js';
import { formModal } from '/shared/ui/form.js';
import toast from '/shared/ui/toast.js';
import {
  NETWORK_MAP,
  ORDER_STATUS_MAP,
  ORDER_STATUS_OPTIONS,
  fetchRecords,
  toOptions,
} from './common.js';

/** 表格列定义 */
const COLUMNS = [
  {
    key: 'orderNo',
    title: '订单号',
    sortable: true,
    render: (row) => html`<div class="cell-stack">
      <button type="button" class="btn-link text-left" data-action="open-order" data-id="${esc(row.id)}">
        ${row.orderNo || row.id}
      </button>
      <span class="cell-sub">${row.applicantName || '—'}</span>
    </div>`,
  },
  {
    key: 'tenant_name',
    title: '归属租户',
    render: (row) => html`<div class="cell-stack">
      <span>${row.tenantName || '-'}</span>
      <span class="cell-sub mono">${row.tenantCode || row.tenantId || '-'}</span>
    </div>`,
  },
  {
    key: 'client_product_name',
    title: '客户产品',
    render: (row) => html`<div class="cell-stack">
      <span>${row.clientProductName || '-'}</span>
      <span class="cell-sub mono">${row.clientProductCode || row.clientProductId || '-'}</span>
    </div>`,
  },
  { key: 'quantity', title: '数量', align: 'num', width: '80px', render: (row) => esc(row.quantity ?? '-') },
  { key: 'status', title: '状态', width: '110px', render: (row) => statusTag(row.status, ORDER_STATUS_MAP) },
  {
    key: 'network_type',
    title: '联网方式',
    width: '110px',
    render: (row) => statusTag(row.networkType, NETWORK_MAP),
  },
  {
    key: 'device_count',
    title: '已生成 / 需求',
    align: 'num',
    width: '120px',
    // 两个数字都属于「本单」，合并显示让「生成了多少 / 还差多少」一眼可见
    render: (row) => html`<span class="num">${row.generatedCount ?? 0}</span>
      <span class="text-secondary"> / ${row.deviceCount ?? row.quantity ?? '-'}</span>`,
  },
  {
    key: 'created_at',
    title: '下单时间',
    sortable: true,
    width: '150px',
    render: (row) => esc(formatDate(row.createdAt)),
  },
  {
    key: 'actions',
    title: '操作',
    align: 'right',
    width: '150px',
    nowrap: true,
    render: (row) => {
      const canAudit = row.status === 'PENDING_AUDIT' && auth.hasPerm(PERM.platform.orderWrite);
      return html`<div class="btn-group">
        ${raw(
          `<button type="button" class="btn btn-sm btn-ghost" data-action="open-order" data-id="${esc(row.id)}">详情</button>`,
        )}
        ${raw(
          canAudit
            ? `<button type="button" class="btn btn-sm btn-primary" data-action="audit-order" data-id="${esc(row.id)}">审核</button>`
            : '',
        )}
      </div>`;
    },
  },
];

/**
 * 审核弹窗。
 *
 * 条件必填的处理：`rejectReason` 只在选择「驳回」时必填，
 * 表单的静态校验（required / pattern）无法表达这种依赖关系，
 * 因此在 onSubmit 里拦截并抛出**带字段信息的错误**——
 * formModal 会把它回填为字段级红字（弹窗保持打开），
 * 用户不必关掉重开就能修正。
 *
 * @param {object} row 订单行
 */
async function openAuditModal(row) {
  const audited = await formModal({
    title: `审核订单 ${row.orderNo || row.id}`,
    subtitle: '审核通过后即可在本页「生成设备」；驳回必须说明原因（会展示给商户端）',
    size: 'md',
    submitText: '提交审核',
    fields: [
      {
        key: 'decision',
        label: '审核结论',
        type: 'radio',
        required: true,
        value: 'APPROVED',
        span: 2,
        options: [
          { value: 'APPROVED', label: '通过' },
          { value: 'REJECTED', label: '驳回' },
        ],
      },
      {
        key: 'rejectReason',
        label: '驳回原因',
        type: 'textarea',
        maxLength: 500,
        span: 2,
        hint: '选择「驳回」时必填；商户端会看到这句话，请写清楚需要改什么',
      },
      {
        key: 'remark',
        label: '审核备注',
        type: 'textarea',
        maxLength: 1000,
        span: 2,
        hint: '可选，仅平台端内部可见',
      },
    ],
    onSubmit: (values) => {
      if (values.decision === 'REJECTED' && !values.rejectReason) {
        const error = new Error('选择「驳回」时必须填写驳回原因');
        error.details = [{ field: 'rejectReason', message: '请填写驳回原因' }];
        throw error;
      }
      return api.post(
        `/platform/orders/${row.id}/audit`,
        {
          decision: values.decision,
          remark: values.remark || null,
          // 通过时不下发驳回原因：避免把上一次的输入误存进历史
          rejectReason: values.decision === 'REJECTED' ? values.rejectReason : null,
        },
        { idempotencyKey: api.newIdempotencyKey() },
      );
    },
  });

  return audited;
}

/**
 * 渲染订单管理页。
 *
 * @param {HTMLElement} container
 * @param {{router: object}} ctx
 */
export async function renderOrders(container, ctx) {
  const { router } = ctx;

  // 下拉数据静默容错：取不到租户时退化为「全部租户」，不影响列表浏览
  const tenants = await fetchRecords('/platform/tenants');
  const tenantOptions = toOptions(tenants, (item) => `${item.name}（${item.code}）`);

  return createListPage({
    container,
    title: '订单管理',
    desc: '商户提交的订单在此审核；审核通过后才可生成设备。状态流转：待审核 → 已审核 → 生成中 → 已生成 → …',
    actions: [{ label: '刷新', icon: 'refresh', action: 'reload-list' }],
    columns: COLUMNS,
    fetcher: (params) =>
      api.get('/platform/orders', {
        params: {
          page: params.page,
          pageSize: params.pageSize,
          sortBy: params.sortBy,
          order: params.order,
          keyword: params.keyword,
          status: params.status,
          tenantId: params.tenantId,
          clientProductId: params.clientProductId,
        },
      }),
    filters: [
      { key: 'keyword', placeholder: '订单号', width: '200px' },
      { key: 'tenantId', type: 'select', options: [{ value: '', label: '全部租户' }, ...tenantOptions] },
      { key: 'status', type: 'select', options: ORDER_STATUS_OPTIONS },
    ],
    onAction: async (action, target, { table, reload }) => {
      const row = target.dataset.id ? table.findRowById(target.dataset.id) : null;

      if (action === 'reload-list') {
        table.load();
        return;
      }

      if (action === 'open-order') {
        if (row) router.goByName('orderDetail', { id: row.id });
        return;
      }

      if (action === 'audit-order') {
        if (!row) return;
        if (row.status !== 'PENDING_AUDIT') {
          // 列表可能已过期（他人刚审过），给出明确原因并刷新
          toast.warning('该订单已被处理，请刷新后查看最新状态');
          reload();
          return;
        }
        const audited = await openAuditModal(row);
        if (audited) {
          toast.success(audited.status === 'REJECTED' ? '订单已驳回' : '订单已通过审核');
          reload();
        }
      }
    },
  });
}

export default { renderOrders };
