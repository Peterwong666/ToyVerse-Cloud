/* ============================================================
   工厂端 · 抽检确认
   ------------------------------------------------------------
   对应后端：
     POST /factory/orders/{id}/inspect  body { sn, result, defectCode?, note? }
     GET  /factory/inspections          （按工单 / SN / 结果分页查询）

   SN 归属校验为什么必须由后端做、前端只负责「说清楚」
   --------------------------------------------------
   SN 是否属于本工单只有后端知道（前端拿不到全量设备-工单映射），
   因此这里不猜测、不做本地校验，只把两种典型拒绝翻译成可操作的话：
     DEVICE_NOT_FOUND  → 「该 SN 不在平台设备库中，请核对机身标签」
     VALIDATION_ERROR  → 「SN 可能属于其它工单，请改选正确的工单」
   这正是 P3 以来坚持的原则：不要让用户点了才知道不行，
   但确实只能后端判定时，就把失败原因解释到「下一步该做什么」。

   页面结构：上半部分是登记表单（插在页头与列表之间），
   下半部分复用 createListPage 的抽检记录列表 —— 提交后 reload 即可
   看到刚登记的那一条，无需另写一套列表。
   ============================================================ */

import api from '/shared/core/api.js';
import { esc, formatDate, fromHtml, h, html } from '/shared/ui/dom.js';
import { statusTag } from '/shared/ui/components.js';
import { form, readAndValidate } from '/shared/ui/form.js';
import { createListPage } from '/shared/app/page.js';
import toast from '/shared/ui/toast.js';
import {
  INSPECTION_RESULT_MAP,
  INSPECTION_RESULT_OPTIONS,
  INSPECTABLE_STATUSES,
  fetchFactoryOrdersByStatus,
  factoryOrderOptionLabel,
  notifyFactoryError,
} from './orders.js';

/** 抽检结果筛选项：内置「全部」，与表单里的结果字段区分开 */
const RESULT_FILTER_OPTIONS = [{ value: '', label: '全部结果' }, ...INSPECTION_RESULT_OPTIONS];

/**
 * 生成抽检记录列定义。
 *
 * @param {Map<string,string>} orderLabels 工单 id → 工单号（列表接口只回 factoryOrderId）
 */
function buildColumns(orderLabels) {
  return [
    {
      key: 'sn',
      title: 'SN',
      render: (row) => html`<span class="mono text-sm">${row.sn || '—'}</span>`,
    },
    {
      key: 'factory_order_id',
      title: '所属工单',
      render: (row) => {
        const label = orderLabels.get(String(row.factoryOrderId));
        // 映射不到时退回显示 id（mono），绝不显示空白——否则用户无法定位这条记录
        return label
          ? html`<span class="mono text-sm">${label}</span>`
          : html`<span class="mono text-xs text-secondary">${row.factoryOrderId || '—'}</span>`;
      },
    },
    {
      key: 'result',
      title: '结果',
      width: '110px',
      render: (row) => statusTag(row.result, INSPECTION_RESULT_MAP),
    },
    {
      key: 'defect_code',
      title: '不良代码',
      width: '140px',
      render: (row) =>
        row.defectCode
          ? html`<span class="mono text-sm">${row.defectCode}</span>`
          : html`<span class="text-secondary">—</span>`,
    },
    { key: 'inspector', title: '抽检人', width: '120px', render: (row) => esc(row.inspector || '—') },
    { key: 'note', title: '备注', render: (row) => esc(row.note || '—') },
    {
      key: 'inspected_at',
      title: '抽检时间',
      width: '160px',
      render: (row) => esc(formatDate(row.inspectedAt || row.createdAt)),
    },
  ];
}

/* ------------------------------------------------------------
   页面
   ------------------------------------------------------------ */

/**
 * 渲染抽检确认页。
 *
 * @param {HTMLElement} container
 * @param {{router: object, query: object}} ctx
 */
export async function renderInspect(container, ctx) {
  const { query } = ctx;

  const orders = await fetchFactoryOrdersByStatus(INSPECTABLE_STATUSES).catch(() => []);
  const orderLabels = new Map(orders.map((row) => [String(row.id), row.factoryOrderNo || String(row.id)]));

  const page = createListPage({
    container,
    title: '抽检确认',
    desc: '逐台登记抽检结果。合格与不合格都会写入工单的抽检记录，并汇总为合格率；不合格请填写不良代码，便于定位问题批次。',
    actions: [{ label: '刷新', icon: 'refresh', action: 'reload-list' }],
    columns: buildColumns(orderLabels),
    fetcher: (params) =>
      api.get('/factory/inspections', {
        params: {
          page: params.page,
          pageSize: params.pageSize,
          factoryOrderId: params.factoryOrderId,
          sn: params.sn,
          result: params.result,
        },
      }),
    filters: [
      { key: 'sn', placeholder: 'SN', width: '200px' },
      { key: 'result', type: 'select', options: RESULT_FILTER_OPTIONS },
    ],
    // 用 query 预置列表范围，避免首屏加载完成后再多打一次请求
    tableOptions: { filters: { factoryOrderId: query?.orderId ? String(query.orderId) : null } },
    onAction: (action, target, { table }) => {
      if (action === 'reload-list') table.load();
    },
  });

  /* ---- 登记表单：插在页头与表格之间 ---- */
  const formHost = h('div', { class: 'mb-4' });
  page.root.insertBefore(formHost, page.root.children[1] || null);

  if (!orders.length) {
    // 没有可抽检的工单时，不渲染表单（渲染了也选不出工单），
    // 只说明原因，并保留下方列表供查看历史记录
    formHost.append(
      fromHtml(
        html`<div class="alert alert-warning" role="alert">
          <span class="alert-icon">!</span>
          <div class="alert-body">
            <div class="alert-title">当前没有可抽检的工单</div>
            <div>只有「生产中 / 烧录完成」的工单可以抽检（待生产的工单还没有 SN 可扫）。若确认有工单，请刷新后重试；下方仍可查看历史抽检记录。</div>
          </div>
        </div>`,
      ),
    );
    return page;
  }

  const fields = [
    {
      key: 'orderId',
      label: '生产工单',
      type: 'select',
      required: true,
      span: 2,
      options: orders.map((row) => ({ value: String(row.id), label: factoryOrderOptionLabel(row) })),
      hint: '只列出「生产中 / 烧录完成」的工单。',
    },
    {
      key: 'sn',
      label: '设备 SN',
      type: 'text',
      required: true,
      maxLength: 128,
      span: 2,
      placeholder: '扫描或输入设备 SN',
      hint: 'SN 必须属于所选工单；不属于本工单时后端会拒绝并说明原因。',
    },
    {
      key: 'result',
      label: '抽检结果',
      type: 'radio',
      required: true,
      span: 2,
      value: 'PASS',
      options: INSPECTION_RESULT_OPTIONS,
    },
    {
      key: 'defectCode',
      label: '不良代码（选填）',
      type: 'text',
      maxLength: 64,
      span: 1,
      placeholder: '如 NO_BOOT / AUDIO_FAIL',
    },
    { key: 'note', label: '备注（选填）', type: 'textarea', maxLength: 500, span: 2 },
  ];

  const noticeHost = h('div', { class: 'mb-3' });
  const formEl = fromHtml(
    form({
      fields,
      values: {
        orderId: query?.orderId ? String(query.orderId) : String(orders[0].id),
        result: 'PASS',
      },
      columns: 2,
      id: 'inspectForm',
    }),
  );

  const selectEl = formEl.querySelector('[name="orderId"]');
  const defectInput = formEl.querySelector('[name="defectCode"]');
  const defectHint = h('div', { class: 'field-hint' });
  formEl.querySelector('[data-field-wrap="defectCode"]')?.append(defectHint);

  /** 判定为不合格时，不良代码从「选填」变成实际必填的沟通信息 */
  const paintDefectHint = () => {
    const fail = formEl.querySelector('input[name="result"]:checked')?.value === 'FAIL';
    defectHint.textContent = fail
      ? '判定为不合格时请务必填写不良代码，便于平台按批次复盘。'
      : '只有在判定为不合格时才需要填写。';
  };
  formEl.querySelectorAll('input[name="result"]').forEach((el) => {
    el.addEventListener('change', paintDefectHint);
  });
  paintDefectHint();

  const submitBtn = h('button', { class: 'btn btn-primary', type: 'button', text: '提交抽检结果' });
  const formCard = h('div', { class: 'card' });
  formCard.append(fromHtml(`<div class="card-head"><div class="card-title">抽检登记</div></div>`));
  formCard.append(noticeHost, formEl, h('div', { class: 'btn-group mt-4' }, submitBtn));
  formHost.append(formCard);

  /* 选工单 → 把列表范围切到该工单，登记完就能在下方看到结果 */
  const applyOrderFilter = (orderId) => page.table.setFilters({ factoryOrderId: orderId || null });
  if (selectEl) {
    selectEl.addEventListener('change', () => applyOrderFilter(selectEl.value));
  }

  const submit = async () => {
    const { ok, values } = readAndValidate(formEl, fields);
    if (!ok) {
      toast.warning('请检查表单填写');
      return;
    }
    if (values.result === 'FAIL' && !values.defectCode) {
      // 后端不强制不良代码，但「不合格却不写原因」的记录对批次复盘毫无价值。
      // 这里选择**阻断提交**：填一个代码的成本远低于一次无效抽检，
      // 而放行后再补是补不回来的（记录已落库）。
      noticeHost.replaceChildren(
        fromHtml(
          html`<div class="alert alert-warning" role="alert">
            <span class="alert-icon">!</span>
            <div class="alert-body">
              <div class="alert-title">判定为不合格时请填写不良代码</div>
              <div>不良代码用于按批次定位问题（如 NO_BOOT、AUDIO_FAIL）。</div>
            </div>
          </div>`,
        ),
      );
      defectInput?.focus();
      return;
    }

    submitBtn.disabled = true;
    submitBtn.textContent = '提交中…';
    noticeHost.replaceChildren();

    try {
      const record = await api.post(
        `/factory/orders/${values.orderId}/inspect`,
        {
          sn: values.sn,
          result: values.result,
          defectCode: values.defectCode || null,
          note: values.note || null,
        },
        { idempotencyKey: api.newIdempotencyKey() },
      );

      // 成功：清空 SN 与备注，保留工单与结果（同一工单常连续抽检多台）
      const snInput = formEl.querySelector('[name="sn"]');
      const noteInput = formEl.querySelector('[name="note"]');
      if (snInput) snInput.value = '';
      if (noteInput) noteInput.value = '';
      if (defectInput) defectInput.value = '';
      snInput?.focus();

      const resultText = INSPECTION_RESULT_MAP[record?.result]?.text || values.result;
      if (values.result === 'FAIL') {
        toast.warning(`已登记：${record?.sn || values.sn} 抽检${resultText}`);
      } else {
        toast.success(`已登记：${record?.sn || values.sn} 抽检${resultText}`);
      }
      page.reload();
    } catch (error) {
      notifyFactoryError(error, '抽检登记失败');
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = '提交抽检结果';
    }
  };

  submitBtn.addEventListener('click', submit);

  return page;
}

export default { renderInspect };
