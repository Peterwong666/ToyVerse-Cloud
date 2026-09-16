/* ============================================================
   商户端 · 我的订单
   ------------------------------------------------------------
   对应后端 `/merchant/orders`：
     GET   列表（服务端强制只返回本租户的订单）
     POST  下单

   两点刻意的取舍：
     1. 下单请求带 `idempotencyKey` —— 网络抖动或用户连点两次时，
        后端据此识别「同一笔下单」，避免生成两张订单。
     2. 「客户产品」下拉来自 `/merchant/products`（服务端已用租户作用域收口）。
        该接口不可用、或本租户还没有已启用的产品时返回空数组，
        此时**降级为手填产品 ID** 并说明原因 —— 宁可让用户手填，
        也不能偷偷改用平台端接口冒充（商户账号没有平台权限，
        那样只会得到一个 403，且用户不知道发生了什么）。
   ============================================================ */

import api from '/shared/core/api.js';
import { PERM } from '/shared/core/auth.js';
import { esc, formatDate, formatMoney, fromHtml, h, html } from '/shared/ui/dom.js';
import { alert, statusTag } from '/shared/ui/components.js';
import { createListPage } from '/shared/app/page.js';
import { formModal } from '/shared/ui/form.js';
import toast from '/shared/ui/toast.js';
import {
  NETWORK_MAP,
  ORDER_STATUS_MAP,
  ORDER_STATUS_OPTIONS,
  fetchRecords,
  toOptions,
} from '/pages/platform/common.js';

/** 表格列定义（客户产品名称需靠下拉数据映射，故用工厂函数生成） */
function buildColumns({ productLabels }) {
  return [
    {
      key: 'orderNo',
      title: '订单号',
      sortable: true,
      render: (row) => html`<div class="cell-stack">
      <span class="cell-strong mono">${row.orderNo || row.id}</span>
      <span class="cell-sub">${row.applicantName || '—'}</span>
    </div>`,
    },
    {
      key: 'client_product_name',
      title: '客户产品',
      // 订单响应本身带 clientProductName；产品下拉能映射到时作为兜底，
      // 两者都没有才显示产品 ID，避免出现「-」
      render: (row) => {
        const known = productLabels.get(String(row.clientProductId));
        return html`<div class="cell-stack">
          <span>${row.clientProductName || known?.name || '-'}</span>
          <span class="cell-sub mono">${row.clientProductCode || known?.code || row.clientProductId || '-'}</span>
        </div>`;
      },
    },
    { key: 'quantity', title: '数量', align: 'num', width: '80px', render: (row) => esc(row.quantity ?? '-') },
    { key: 'status', title: '状态', width: '110px', render: (row) => statusTag(row.status, ORDER_STATUS_MAP) },
    {
      key: 'network_type',
      title: '联网方式',
      width: '105px',
      render: (row) => statusTag(row.networkType, NETWORK_MAP),
    },
    {
      key: 'total_amount',
      title: '金额',
      align: 'num',
      width: '110px',
      render: (row) => esc(formatMoney(row.totalAmount)),
    },
    {
      key: 'created_at',
      title: '下单时间',
      sortable: true,
      width: '150px',
      render: (row) => esc(formatDate(row.createdAt)),
    },
  ];
}

/**
 * 打开「下单」弹窗。
 *
 * @param {object[]} productOptions 客户产品下拉项（为空则降级为手填）
 * @param {boolean} useDropdown true 用下拉，false 用手填 ID
 */
async function openCreateOrder({ productOptions, useDropdown }) {
  const productField = useDropdown
    ? {
        key: 'clientProductId',
        label: '客户产品',
        type: 'select',
        required: true,
        span: 2,
        options: [{ value: '', label: '请选择产品' }, ...productOptions],
      }
    : {
        key: 'clientProductId',
        label: '客户产品 ID',
        required: true,
        maxLength: 64,
        span: 2,
        placeholder: '填写客户产品 ID',
        // 说明为什么是手填而不是下拉，避免用户以为「产品没配置」是系统故障
        hint: '暂无可选产品，请先在平台侧为该租户创建客户产品；也可直接填写客户产品 ID。',
      };

  const created = await formModal({
    title: '提交订单',
    subtitle: '提交后由平台审核，审核通过才会生成设备',
    size: 'md',
    submitText: '提交订单',
    fields: [
      productField,
      {
        key: 'quantity',
        label: '数量',
        type: 'number',
        required: true,
        min: 1,
        max: 9999,
        value: 1,
        hint: '一次下单的设备台数',
      },
      { key: 'applicantName', label: '申请人', maxLength: 64, placeholder: '默认取当前账号' },
      { key: 'applicantPhone', label: '联系电话', maxLength: 32 },
      { key: 'remark', label: '备注', type: 'textarea', maxLength: 1000, span: 2 },
    ],
    onSubmit: (values) =>
      api.post('/merchant/orders', values, { idempotencyKey: api.newIdempotencyKey() }),
  });

  return created;
}

/**
 * 渲染「我的订单」页。
 *
 * @param {HTMLElement} container
 */
export async function renderMerchantOrders(container) {
  // 只取「已启用」的产品：停用产品下单会被后端拒绝，不如一开始就不出现在选项里
  const products = await fetchRecords('/merchant/products', { status: 'ENABLED' });
  const productOptions = toOptions(products, (item) => `${item.name}（${item.code}）`);
  const productLabels = new Map(
    products.map((item) => [String(item.id), { name: item.name, code: item.code }]),
  );

  const page = createListPage({
    container,
    title: '我的订单',
    desc: '本租户的订单，只能看到自己的数据（服务端强制按租户隔离）。',
    actions: [
      { label: '刷新', icon: 'refresh', action: 'reload-list' },
      {
        label: '下单',
        icon: 'plus',
        variant: 'primary',
        action: 'new-order',
        perm: PERM.merchant.orderWrite,
      },
    ],
    columns: buildColumns({ productLabels }),
    fetcher: (params) =>
      api.get('/merchant/orders', {
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
      { key: 'keyword', placeholder: '订单号', width: '200px' },
      { key: 'status', type: 'select', options: ORDER_STATUS_OPTIONS },
    ],
    onAction: async (action, target, { table: tbl, reload }) => {
      if (action === 'reload-list') {
        tbl.load();
        return;
      }

      if (action === 'new-order') {
        const created = await openCreateOrder({ productOptions, useDropdown: productOptions.length > 0 });
        if (created) {
          toast.success('订单已提交，等待平台审核');
          reload();
        }
      }
    },
  });

  /* ---- 降级说明：让用户知道为什么这里没有产品下拉 ---- */
  if (!productOptions.length) {
    const note = h('div', { class: 'mb-4' });
    note.append(
      fromHtml(
        alert({
          tone: 'neutral',
          title: '暂无可选产品',
          text: '没有取到本租户已启用的客户产品，下单时需直接填写客户产品 ID。若确实还没有产品，请先在平台侧创建并授权。',
        }),
      ),
    );
    page.root.insertBefore(note, page.root.children[1] || null);
  }

  return page;
}

export default { renderMerchantOrders };
