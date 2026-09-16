/* ============================================================
   工厂端 · 生产订单
   ------------------------------------------------------------
   对应后端 `/factory/orders`：
     GET 列表（status / keyword / 分页 / 排序）

   ★ 字段脱敏是 P6 的核心卖点，本页只出现**服务端已脱敏**的数据：
     客户名形如「中**动」（customerNameMasked）；金额、联系方式、
     邮箱等敏感字段后端**根本不下发**，前端即使想展示也无从取到。
     因此这里不存在「漏转义/误展示明文」的隐患，脱敏由序列化层兜底。

   ★ 本文件同时充当工厂端的「共用件出口」。
     烧录上报（burn.js）、抽检确认（inspect.js）、工单详情
     （order_detail.js）都要用同一套状态映射、可生产状态集合与错误提示。
     本阶段的文件清单**不包含**新增 `factory/common.js`，因此把这份
     共用件放在列表页里由其它页面 import —— 集中一处才能避免四个页面
     各抄一份，改一个枚举时漏改其余三处。
   ============================================================ */

import api from '/shared/core/api.js';
import auth, { PERM } from '/shared/core/auth.js';
import { esc, formatDay, html, raw } from '/shared/ui/dom.js';
import { progress, statusTag } from '/shared/ui/components.js';
import { createListPage } from '/shared/app/page.js';
import toast from '/shared/ui/toast.js';

/* ------------------------------------------------------------
   一、共用件：状态 / 选项 / 错误提示
   ------------------------------------------------------------ */

/**
 * 工单状态映射。
 * 与后端 `FactoryOrderStatus` / `FACTORY_ORDER_STATUS_LABELS` 一一对应：
 *   PENDING → PRODUCING → COMPLETED → SHIPPED（终态）
 * 后端只给中文 label，列表需要 tone 着色，因此配色在前端定义。
 */
export const FACTORY_ORDER_STATUS_MAP = {
  PENDING: { text: '待生产', tone: 'warning' },
  PRODUCING: { text: '生产中', tone: 'info' },
  COMPLETED: { text: '烧录完成', tone: 'teal' },
  SHIPPED: { text: '已出货', tone: 'success' },
};

/** 状态下拉：内置「全部」项，避免每个页面各自拼一次 */
export const FACTORY_ORDER_STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  ...Object.entries(FACTORY_ORDER_STATUS_MAP).map(([value, { text }]) => ({ value, label: text })),
];

/** 可继续烧录的工单状态：只有这两态才允许上报（后端也会拒） */
export const BURNABLE_STATUSES = ['PENDING', 'PRODUCING'];

/** 可抽检的工单状态：PENDING 还没有烧录记录，抽检必然查不到 SN，故不列出 */
export const INSPECTABLE_STATUSES = ['PRODUCING', 'COMPLETED'];

/**
 * 抽检结果映射。
 * 与后端 `InspectionResult` 一致，刻意只有「合格 / 不合格」两态：
 * 抽检是判定动作，细分处置（复检、降级）不属于结果枚举。
 */
export const INSPECTION_RESULT_MAP = {
  PASS: { text: '合格', tone: 'success' },
  FAIL: { text: '不合格', tone: 'danger' },
};

/** 抽检结果下拉 */
export const INSPECTION_RESULT_OPTIONS = Object.entries(INSPECTION_RESULT_MAP).map(
  ([value, { text }]) => ({ value, label: text }),
);

/** 进度条配色：与状态色一致，让「哪些单快跑完」在列表里一眼可见 */
const PROGRESS_TONE = {
  PENDING: 'warning',
  PRODUCING: 'brand',
  COMPLETED: 'success',
  SHIPPED: 'success',
};

/**
 * 工厂端错误码 → 处理建议。
 *
 * BURN_COUNT_EXCEEDED 单独提出来的原因：这是工厂端最高频的误操作
 * （多报了一批），后端在 details 里给了 remaining，页面必须把它
 * 翻译成「本单还剩 N 台」而不是只弹一句「数量超限」。
 */
export const FACTORY_ERROR_HINTS = {
  BURN_COUNT_EXCEEDED: '上报数量不能超过本单剩余数量，请按提示调整后重新提交。',
  DEVICE_NOT_FOUND: '该 SN 不在平台设备库中，请核对机身标签或扫码结果后重试。',
  VALIDATION_ERROR: '请检查填写内容；若 SN 属于其它工单，请改选正确的工单。',
  INVALID_STATE_TRANSITION: '当前工单状态不允许该操作，请刷新后按最新状态操作。',
  RESOURCE_NOT_FOUND: '目标工单不存在，可能已被删除或尚未派单。',
};

/** 统一的错误提示（工厂端各页共用，避免提示口径不一致） */
export function notifyFactoryError(error, fallback = '操作失败，请稍后重试') {
  const hint = FACTORY_ERROR_HINTS[error?.code] || '';
  const message = error?.message || fallback;
  toast.error(hint ? `${message}　${hint}` : message, { traceId: error?.traceId || '' });
}

/**
 * 按下拉用途拉取工单。
 *
 * 为什么一次拿 200 条而不是做「搜索式下拉」：工厂同时开工的工单量在
 * 演示与真实产线都属于几十这个量级，一次拉全比每次输入都打一次接口
 * 更省事；同时保持**按状态过滤**（例如烧录页不该出现已出货的工单），
 * 避免用户选到一个点了必然被拒的工单。
 *
 * @param {string[]} statuses 允许的状态（空数组表示不过滤）
 * @returns {Promise<Array>}
 */
export async function fetchFactoryOrdersByStatus(statuses = []) {
  const targets = statuses.length ? statuses : [''];
  const payloads = await Promise.all(
    targets.map((status) =>
      api
        .get('/factory/orders', {
          params: { status: status || undefined, pageSize: 200, sortBy: 'createdAt', order: 'desc' },
          silent: true,
        })
        .catch(() => null),
    ),
  );

  // 多个状态分头请求后合并去重（同一个 id 只保留一次）
  const seen = new Set();
  const rows = [];
  payloads.forEach((payload) => {
    (payload?.records || []).forEach((row) => {
      const id = String(row.id);
      if (seen.has(id)) return;
      seen.add(id);
      rows.push(row);
    });
  });
  return rows;
}

/** 下拉项文案：工单号 + 型号 + 剩余，选单时不用再点进详情确认 */
export function factoryOrderOptionLabel(row) {
  const parts = [row.factoryOrderNo || row.id];
  if (row.productModel) parts.push(row.productModel);
  if (row.remaining !== undefined && row.remaining !== null) parts.push(`剩余 ${row.remaining}`);
  return parts.join(' · ');
}

/** 读取单个工单详情 */
export function loadFactoryOrder(id) {
  return api.get(`/factory/orders/${id}`);
}

/* ------------------------------------------------------------
   二、列表列定义
   ------------------------------------------------------------ */

const COLUMNS = [
  {
    key: 'factory_order_no',
    title: '工单号',
    sortable: true,
    render: (row) => html`<div class="cell-stack">
      <button type="button" class="btn-link text-left" data-action="open-order" data-id="${esc(row.id)}">
        ${row.factoryOrderNo || row.id}
      </button>
      <span class="cell-sub mono">${row.orderNo || '—'}</span>
    </div>`,
  },
  {
    // 列头明确写「脱敏」：让使用者一眼知道这不是完整客户名，
    // 而不是误以为工厂端漏了数据（这是 P6 的设计意图，不是缺陷）
    key: 'customer_name_masked',
    title: '客户（脱敏）',
    render: (row) => html`<div class="cell-stack">
      <span>${row.customerNameMasked || '—'}</span>
      <span class="cell-sub">服务端已脱敏，金额 / 联系方式不下发</span>
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
  {
    key: 'quantity',
    title: '数量',
    align: 'num',
    width: '80px',
    render: (row) => esc(row.quantity ?? '—'),
  },
  {
    key: 'burned_count',
    title: '已烧录',
    align: 'num',
    width: '110px',
    // 已烧录与剩余并列：只给「已烧录」时用户还要自己做减法
    render: (row) => html`<span class="num">${row.burnedCount ?? 0}</span>
      <span class="text-secondary"> / 剩 ${row.remaining ?? 0}</span>`,
  },
  {
    key: 'progress',
    title: '进度',
    width: '150px',
    render: (row) => {
      const percent = Number(row.progressPercent ?? 0);
      return html`<div class="flex items-center gap-2">
        ${raw(progress(percent, { tone: PROGRESS_TONE[row.status] || 'brand' }))}
        <span class="text-xs text-secondary num">${percent.toFixed(0)}%</span>
      </div>`;
    },
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
    width: '230px',
    nowrap: true,
    render: (row) => {
      const buttons = [
        `<button type="button" class="btn btn-sm btn-ghost" data-action="open-order" data-id="${esc(row.id)}">详情</button>`,
      ];
      // 按钮只在状态允许时出现：生产已完成的单不该再引导用户去上报
      // （点了也只会收到 409，属于「点了才知道不行」）
      if (BURNABLE_STATUSES.includes(row.status) && auth.hasPerm(PERM.factory.burnWrite)) {
        buttons.push(
          `<button type="button" class="btn btn-sm btn-primary" data-action="goto-burn" data-id="${esc(row.id)}">烧录上报</button>`,
        );
      }
      if (INSPECTABLE_STATUSES.includes(row.status) && auth.hasPerm(PERM.factory.inspectWrite)) {
        buttons.push(
          `<button type="button" class="btn btn-sm" data-action="goto-inspect" data-id="${esc(row.id)}">抽检</button>`,
        );
      }
      return html`<div class="btn-group">${raw(buttons.join(''))}</div>`;
    },
  },
];

/* ------------------------------------------------------------
   三、页面
   ------------------------------------------------------------ */

/**
 * 渲染生产订单列表。
 *
 * @param {HTMLElement} container
 * @param {{router: object}} ctx
 */
export async function renderFactoryOrders(container, ctx) {
  const { router } = ctx;

  return createListPage({
    container,
    title: '生产订单',
    desc: '本工厂承接的生产工单。客户信息由服务端脱敏后下发（如「中**动」），金额与联系方式不下发；进度按累计烧录数量计算。',
    actions: [{ label: '刷新', icon: 'refresh', action: 'reload-list' }],
    columns: COLUMNS,
    fetcher: (params) =>
      api.get('/factory/orders', {
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
      { key: 'keyword', placeholder: '工单号 / 平台订单号', width: '220px' },
      { key: 'status', type: 'select', options: FACTORY_ORDER_STATUS_OPTIONS },
    ],
    onAction: async (action, target, { table, reload }) => {
      const row = target.dataset.id ? table.findRowById(target.dataset.id) : null;

      if (action === 'reload-list') {
        table.load();
        return;
      }

      if (action === 'open-order') {
        if (row) router.goByName('factoryOrderDetail', { id: row.id });
        return;
      }

      // 烧录 / 抽检各自是独立的页面（都要选工单、填表单），
      // 因此用 query 把工单 id 带过去，落地即预选好，省一次手动选择
      if (action === 'goto-burn') {
        if (!row) return;
        if (!BURNABLE_STATUSES.includes(row.status)) {
          toast.warning('该工单已不在生产中，请刷新后查看最新状态');
          reload();
          return;
        }
        router.go('/burn', { orderId: row.id });
        return;
      }

      if (action === 'goto-inspect') {
        if (!row) return;
        router.go('/inspect', { orderId: row.id });
      }
    },
  });
}

export default {
  renderFactoryOrders,
  FACTORY_ORDER_STATUS_MAP,
  FACTORY_ORDER_STATUS_OPTIONS,
  BURNABLE_STATUSES,
  INSPECTABLE_STATUSES,
  INSPECTION_RESULT_MAP,
  INSPECTION_RESULT_OPTIONS,
  FACTORY_ERROR_HINTS,
  notifyFactoryError,
  fetchFactoryOrdersByStatus,
  factoryOrderOptionLabel,
  loadFactoryOrder,
};
