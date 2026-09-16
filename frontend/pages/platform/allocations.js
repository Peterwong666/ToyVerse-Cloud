/* ============================================================
   平台端 · 分配管理
   ------------------------------------------------------------
   对应后端 `/platform/allocations`：
     GET   列表（status / tenantId / keyword）
     POST  新建分配单（设备清单在**创建时**就固定，支持 Idempotency-Key）
     GET   /{id}/items   明细行（分页）
     POST  /{id}/execute 执行分配

   为什么「选完设备再建单」而不是「先建单后期再挑设备」
   ----------------------------------------------------
   分配单是有审计意义的凭据：它记录「某时刻把哪几台给谁」。
   允许先建单后补设备，会让审计记录指向一个当时并不存在的事实。
   因此弹窗里必须一次选好租户、产品与设备，前端先拦下不完整的提交
   （而不是等后端报 400 —— 用户不会喜欢「点了才知道缺什么」）。

   为什么执行结果要把 failures 逐条列出来
   --------------------------------------
   一张单里失败三台时只报「执行失败」等于没说：用户不知道是哪三台、
   为什么失败。与 P4 批次导入的错误报告同一口径——**失败必须可解释**。
   ============================================================ */

import api from '/shared/core/api.js';
import auth, { PERM } from '/shared/core/auth.js';
import { esc, formatDate, fromHtml, h, html, raw } from '/shared/ui/dom.js';
import {
  alert,
  descList,
  emptyState,
  loadingState,
  statGrid,
  statusTag,
  tag,
} from '/shared/ui/components.js';
import { table } from '/shared/ui/table.js';
import { createListPage } from '/shared/app/page.js';
import { confirmDialog, modal } from '/shared/ui/modal.js';
import toast from '/shared/ui/toast.js';
import {
  ACTIVATION_STATUS_MAP,
  ALLOCATION_ITEM_STATUS_MAP,
  ALLOCATION_ITEM_STATUS_OPTIONS,
  ALLOCATION_STATUS_MAP,
  ALLOCATION_STATUS_OPTIONS,
  ASSET_STATUS_MAP,
  BIND_STATUS_MAP,
  NETWORK_MAP,
  ONLINE_STATUS_MAP,
  P5_ERROR_HINTS,
  fetchRecords,
  notifyError,
  toOptions,
} from './common.js';

/** 可执行的分配单状态：草稿与执行失败都可以执行（失败后允许修正再跑） */
const EXECUTABLE = new Set(['DRAFT', 'FAILED']);

/** 一次弹窗里最多展示的可选设备数（与后端单张分配单 500 台上限同一考虑） */
const DEVICE_PICK_LIMIT = 200;

/* ------------------------------------------------------------
   一、小工具
   ------------------------------------------------------------ */

/** 统一错误提示：P5 错误码优先用 P5_ERROR_HINTS 给出动作建议 */
function notifyP5Error(error, fallback = '操作失败，请稍后重试') {
  const hint = P5_ERROR_HINTS[error?.code];
  if (hint) {
    toast.error(`${error?.message || fallback}　${hint}`, { traceId: error?.traceId || '' });
    return;
  }
  notifyError(error, fallback);
}

/** 包一层 label + 控件 + 提示的表单字段 */
function formField(label, control, hint = '') {
  const wrap = h('div', { class: 'field' });
  wrap.append(h('label', { class: 'field-label', text: label }));
  wrap.append(control);
  if (hint) wrap.append(h('div', { class: 'field-hint', text: hint }));
  return wrap;
}

/** 构造一个带占位项 select */
function buildSelect(placeholder, options = []) {
  const el = h('select', { class: 'select' });
  el.append(h('option', { value: '' }, placeholder));
  options.forEach((opt) => el.append(h('option', { value: String(opt.value) }, opt.label)));
  return el;
}

/* ------------------------------------------------------------
   二、新建分配单弹窗
   ------------------------------------------------------------ */

/** 可选设备行：复选框 + SN + 联网方式 + 四维标签 */
function devicePickerRow(device, checked, onToggle) {
  const wrap = h('label', { class: 'flex items-center gap-3 p-2' });

  const box = h('input', { type: 'checkbox' });
  if (checked) box.checked = true;
  box.addEventListener('change', () => onToggle(box.checked));

  const info = h('div', { class: 'flex-1' });
  info.append(h('div', { class: 'mono text-sm', text: device.sn || device.id }));
  info.append(
    fromHtml(
      html`<div class="text-xs text-secondary flex items-center gap-2 flex-wrap">
        <span>${NETWORK_MAP[device.networkType]?.text || device.networkType || '-'}</span>
        ${raw(statusTag(device.assetStatus, ASSET_STATUS_MAP))}
        ${raw(statusTag(device.activationStatus, ACTIVATION_STATUS_MAP))}
        ${raw(statusTag(device.onlineStatus, ONLINE_STATUS_MAP))}
        ${raw(statusTag(device.bindStatus, BIND_STATUS_MAP))}
      </div>`,
    ),
  );

  wrap.append(box, info);
  return wrap;
}

/**
 * 打开「新建分配单」弹窗。
 *
 * @param {object} o
 * @param {Array} o.tenantOptions 租户下拉项
 * @param {Function} [o.onDone] 创建成功后的回调（用于刷新列表）
 */
export async function openCreateAllocation({ tenantOptions, onDone }) {
  /** 已选设备：id -> device。用 Map 保留跨多次搜索的选择 */
  const selected = new Map();
  let devices = [];
  let lastKeyword = '';

  const body = h('div');

  /* ---- 第一段：租户 / 产品 / 备注 ---- */
  const tenantSelect = buildSelect('请选择租户', tenantOptions);
  const productSelect = buildSelect('请先选择租户');
  productSelect.disabled = true;
  const remarkInput = h('textarea', { class: 'textarea', rows: '2', placeholder: '可选：分配说明' });

  const productHint = h('div', { class: 'field-hint' });

  const headGrid = h('div', { class: 'form-grid' });
  headGrid.append(formField('租户', tenantSelect));
  const productField = formField('客户产品', productSelect);
  productField.append(productHint);
  headGrid.append(productField);
  const remarkField = h('div', { class: 'field form-grid-full' });
  remarkField.append(h('label', { class: 'field-label', text: '备注' }), remarkInput);
  headGrid.append(remarkField);
  body.append(headGrid);

  /* ---- 第二段：选设备 ---- */
  body.append(
    fromHtml(
      html`<div class="mt-4 mb-2 flex items-center gap-3 flex-wrap">
        <span class="font-semibold">选择设备</span>
        <span class="text-xs text-secondary">仅列出平台库存中「已入库待生产」且未分配给任何租户的设备</span>
      </div>`,
    ),
  );

  const keywordInput = h('input', {
    class: 'input',
    type: 'search',
    placeholder: '按 SN / IMEI / MAC 过滤',
    style: { maxWidth: '260px' },
  });
  const searchBtn = h('button', { class: 'btn btn-sm', type: 'button', text: '查询设备' });
  const counter = h('span');

  const searchRow = h('div', { class: 'flex items-center gap-2 flex-wrap mb-2' });
  searchRow.append(keywordInput, searchBtn, counter);
  body.append(searchRow);

  const listHost = h('div', {
    style: { maxHeight: '320px', overflow: 'auto', border: '1px solid var(--border-1)', borderRadius: 'var(--radius-2)' },
  });
  body.append(listHost);

  const paintCounter = () => {
    counter.replaceChildren(fromHtml(tag(`已选 ${selected.size} 台`, selected.size ? 'brand' : 'default')));
  };

  const paintDevices = () => {
    if (!devices.length) {
      listHost.replaceChildren(
        fromHtml(
          emptyState({
            icon: 'device',
            title: lastKeyword ? '没有匹配的设备' : '没有可选设备',
            desc: lastKeyword
              ? '换个 SN / IMEI / MAC 关键字再试，或清空关键字查看全部可选设备。'
              : '平台库存中没有「已入库待生产」且未分配给任何租户的设备。可先通过批次导入或订单生成补充库存。',
          }),
        ),
      );
      return;
    }

    const frag = document.createDocumentFragment();
    devices.forEach((device) => {
      const id = String(device.id);
      frag.append(
        devicePickerRow(device, selected.has(id), (checked) => {
          if (checked) selected.set(id, device);
          else selected.delete(id);
          // 只更新计数、不重绘列表：重绘会把用户正在点的复选框换掉，勾选体验会跳
          paintCounter();
        }),
      );
    });
    listHost.replaceChildren(frag);
  };

  const loadDevices = async (keyword) => {
    lastKeyword = keyword || '';
    listHost.replaceChildren(fromHtml(loadingState('正在读取可选设备…')));
    try {
      const payload = await api.get('/platform/devices', {
        params: {
          unallocated: true,
          assetStatus: 'IN_STOCK',
          pageSize: DEVICE_PICK_LIMIT,
          keyword: lastKeyword || undefined,
        },
      });
      devices = Array.isArray(payload?.records) ? payload.records : [];
    } catch (error) {
      devices = [];
      notifyP5Error(error, '可选设备读取失败');
    }
    paintDevices();
  };

  /** 拉取指定租户下可用的客户产品（产品必须按租户过滤） */
  const loadProducts = async (tenantId) => {
    if (!tenantId) return;
    const records = await fetchRecords('/platform/client-products', { tenantId });
    productSelect.replaceChildren();
    productSelect.append(
      h('option', { value: '' }, records.length ? '请选择产品' : '该租户暂无可用产品'),
    );
    records.forEach((item) =>
      productSelect.append(h('option', { value: String(item.id) }, `${item.name}（${item.code}）`)),
    );
    productSelect.disabled = records.length === 0;
    productHint.textContent = records.length
      ? ''
      : '该租户暂无已授权的客户产品。请先在「客户产品」页为该租户创建产品后再分配。';
  };

  /* 换租户：产品与已勾选的设备都必须清空 ——
     产品属于租户，留着上一个租户的产品会把设备分到错误的组合上；
     已勾选的设备同理，避免用户以为「换了租户但设备没变」还是同一张单。

     ★ 但**不能清空设备列表本身**：可选设备是「平台库存中已入库待生产、
     且尚未分配给任何租户」的那批，与目标租户无关，选哪个租户都一样。
     早期实现顺手把列表也清成了空数组且没有重新加载，导致「先选租户」
     这个必然动作之后，可选设备永远是空的、分配单无法创建——功能不可用。 */
  tenantSelect.addEventListener('change', async () => {
    selected.clear();
    paintCounter();
    // 保留 devices，只把复选框重绘为「全部未勾选」
    paintDevices();
    productSelect.replaceChildren(h('option', { value: '' }, '请选择产品'));
    productSelect.disabled = true;
    productHint.textContent = '';
    await loadProducts(tenantSelect.value);
  });

  searchBtn.addEventListener('click', () => loadDevices(keywordInput.value.trim()));
  keywordInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      loadDevices(keywordInput.value.trim());
    }
  });

  const submit = async (dlg, btn) => {
    const tenantId = tenantSelect.value;
    const clientProductId = productSelect.value;
    const remark = remarkInput.value.trim();

    // 前端先拦：这三项缺失时后端一定会报 400，不如在这里把话说清楚
    if (!tenantId) {
      toast.warning('请先选择租户');
      tenantSelect.focus();
      return;
    }
    if (!clientProductId) {
      toast.warning('请先选择客户产品');
      productSelect.focus();
      return;
    }
    if (selected.size === 0) {
      toast.warning('请至少选择 1 台设备');
      return;
    }

    btn.disabled = true;
    btn.textContent = '提交中…';
    try {
      const created = await api.post(
        '/platform/allocations',
        {
          tenantId,
          clientProductId,
          deviceIds: [...selected.keys()],
          remark: remark || null,
        },
        { idempotencyKey: api.newIdempotencyKey() },
      );
      dlg.close(created);
    } catch (error) {
      btn.disabled = false;
      btn.textContent = '创建分配单';
      notifyP5Error(error, '创建分配单失败');
    }
  };

  const dialog = modal({
    title: '新建分配单',
    subtitle: '创建后为草稿状态，需要点列表里的「执行」才会真正把设备分配给租户',
    size: 'xl',
    // 表单内容较多，误点遮罩就丢掉已选设备代价太高
    closeOnMask: false,
    body,
    footer: (dlg) => {
      const cancel = h('button', { class: 'btn', type: 'button', text: '取消' });
      cancel.addEventListener('click', () => dlg.close(null));
      const ok = h('button', { class: 'btn btn-primary', type: 'button', text: '创建分配单' });
      ok.addEventListener('click', () => submit(dlg, ok));
      return [cancel, ok];
    },
  });

  paintCounter();
  await loadDevices('');

  const created = await dialog.result;
  if (created) {
    toast.success(
      `分配单 ${created.allocationNo || ''} 已创建（${created.totalCount ?? selected.size} 台），请执行以完成分配`,
    );
    onDone?.();
  }
  return created;
}

/* ------------------------------------------------------------
   三、查看明细
   ------------------------------------------------------------ */

/**
 * 打开「分配明细」弹窗。
 *
 * 用「加载更多」累加而不是页码控件：一单最多 500 台，
 * 用户关心的是「哪几台失败」，逐页翻页反而增加操作成本。
 */
export async function openAllocationItems(allocation) {
  const items = [];
  let total = 0;
  let lastPage = 0;

  const body = h('div');
  // 单级失败原因（failureReason）与执行人只在有值时出现：
  // 逐台原因在下方明细里，这里给的是「整单为什么没跑完」的结论
  const summaryRows = [
    ['分配单号', allocation.allocationNo],
    ['状态', ALLOCATION_STATUS_MAP[allocation.status]?.text || allocation.status],
    ['总台数', allocation.totalCount ?? 0],
    ['已分配', allocation.allocatedCount ?? 0],
    ['失败', allocation.failedCount ?? 0],
  ];
  if (allocation.executedBy) summaryRows.push(['执行人', allocation.executedBy]);
  if (allocation.failureReason) summaryRows.push(['整单失败原因', allocation.failureReason]);

  body.append(fromHtml(descList(summaryRows, { cols: 2 })));

  const listHost = h('div', { class: 'mt-3' });
  const footHost = h('div', { class: 'mt-3 text-center' });

  /* 明细状态筛选：失败一多时，「只看失败的」比翻页找红字有效得多 */
  const statusFilter = h('select', { class: 'select', style: { maxWidth: '180px' } });
  ALLOCATION_ITEM_STATUS_OPTIONS.forEach((opt) =>
    statusFilter.append(h('option', { value: String(opt.value) }, opt.label)),
  );
  statusFilter.addEventListener('change', () => {
    items.length = 0;
    total = 0;
    lastPage = 0;
    loadPage(1, statusFilter.value);
  });

  const filterRow = h('div', { class: 'flex items-center gap-2 mt-3' });
  filterRow.append(
    h('span', { class: 'text-xs text-secondary', text: '明细状态' }),
    statusFilter,
  );
  body.append(filterRow, listHost, footHost);

  const paint = () => {
    listHost.replaceChildren(
      fromHtml(
        table({
          columns: [
            {
              key: 'device_sn',
              title: 'SN',
              render: (row) => html`<span class="mono text-sm">${row.deviceSn || row.deviceId || '-'}</span>`,
            },
            {
              key: 'status',
              title: '状态',
              width: '110px',
              render: (row) => statusTag(row.status, ALLOCATION_ITEM_STATUS_MAP),
            },
            {
              key: 'error_message',
              title: '失败原因',
              render: (row) =>
                row.errorMessage ? html`<span class="text-danger">${row.errorMessage}</span>` : '—',
            },
            {
              key: 'allocated_at',
              title: '分配时间',
              width: '150px',
              render: (row) => esc(row.allocatedAt ? formatDate(row.allocatedAt) : '-'),
            },
          ],
          rows: items,
          emptyText: '暂无明细行',
        }),
      ),
    );

    if (items.length < total) {
      const more = h('button', {
        class: 'btn btn-sm',
        type: 'button',
        text: `加载更多（已显示 ${items.length} / ${total} 条）`,
      });
      more.addEventListener('click', () => loadPage(lastPage + 1, statusFilter.value));
      footHost.replaceChildren(more);
    } else {
      footHost.replaceChildren(
        h('div', { class: 'text-xs text-secondary', text: `已显示全部 ${total} 条明细` }),
      );
    }
  };

  const loadPage = async (targetPage, status = '') => {
    listHost.replaceChildren(fromHtml(loadingState('正在读取分配明细…')));
    try {
      const payload = await api.get(`/platform/allocations/${allocation.id}/items`, {
        params: { page: targetPage, pageSize: 50, status: status || undefined },
      });
      lastPage = targetPage;
      total = Number(payload?.total ?? 0);
      const records = Array.isArray(payload?.records) ? payload.records : [];
      records.forEach((row) => items.push(row));
      paint();
    } catch (error) {
      listHost.replaceChildren(
        fromHtml(
          emptyState({
            icon: 'alertTriangle',
            title: '分配明细读取失败',
            desc: error?.message || '请稍后重试',
          }),
        ),
      );
      footHost.replaceChildren();
    }
  };

  const dialog = modal({
    title: `分配明细 ${allocation.allocationNo || allocation.id}`,
    size: 'xl',
    body,
  });

  await loadPage(1);
  return dialog;
}

/* ------------------------------------------------------------
   四、执行分配
   ------------------------------------------------------------ */

/** 执行结果弹窗：数量汇总 + **逐条失败原因** */
function showExecuteResult(result, allocation) {
  const allocated = result?.allocatedCount ?? 0;
  const skipped = result?.skippedCount ?? 0;
  const failed = result?.failedCount ?? 0;
  const failures = Array.isArray(result?.failures) ? result.failures : [];

  const body = h('div');
  body.append(
    fromHtml(
      statGrid([
        {
          label: '已分配',
          value: allocated,
          unit: '台',
          icon: 'checkCircle',
          tone: 'success',
          foot: '本次真正写入租户的台数',
        },
        {
          label: '已跳过',
          value: skipped,
          unit: '台',
          icon: 'minus',
          tone: 'default',
          foot: '重跑时已处于目标状态，无需再动',
        },
        {
          label: '失败',
          value: failed,
          unit: '台',
          icon: 'xCircle',
          tone: failed ? 'coral' : 'default',
          foot: '需要在下方按原因处理后重试',
        },
      ]),
    ),
  );

  if (failures.length) {
    body.append(
      fromHtml(
        alert({
          tone: 'danger',
          title: `${failures.length} 台设备没有分配成功`,
          text: '请按下面的原因逐台处理（例如先解冻设备、把设备补进当前租户、改选库存内可用的设备），处理后可以再次执行本单。',
        }),
      ),
    );
    body.append(
      fromHtml(
        table({
          columns: [
            {
              key: 'device_sn',
              title: 'SN',
              render: (row) => html`<span class="mono text-sm">${row.deviceSn || row.deviceId || '-'}</span>`,
            },
            {
              key: 'code',
              title: '错误码',
              width: '170px',
              render: (row) => html`<span class="mono text-xs">${row.code || '-'}</span>`,
            },
            {
              key: 'message',
              title: '失败原因',
              render: (row) => html`<span class="text-danger">${row.message || '-'}</span>`,
            },
          ],
          rows: failures,
          emptyText: '无失败明细',
        }),
      ),
    );
  } else {
    body.append(
      fromHtml(
        alert({
          tone: 'success',
          title: '没有失败的设备',
          text: '本单所有待分配设备都已处理完成。若设备数少于预期，请检查是否被其它分配单先分走了。',
        }),
      ),
    );
  }

  modal({ title: `执行结果 ${allocation.allocationNo || allocation.id}`, size: 'lg', body });
}

async function executeAllocation(allocation, reload) {
  const { confirmed } = await confirmDialog({
    title: `执行分配单 ${allocation.allocationNo || allocation.id}`,
    description: `将把本单 ${allocation.totalCount ?? 0} 台设备分配到目标租户。`,
    detail:
      '执行会逐台校验设备状态；单台失败不影响其它设备，失败原因会逐条列出。' +
      '已完成的单不需要也不允许重复执行。',
    tone: 'warning',
    confirmText: '确认执行',
  });
  if (!confirmed) return;

  const handle = toast.loading('正在执行分配…');
  try {
    const result = await api.post(`/platform/allocations/${allocation.id}/execute`, undefined, {
      idempotencyKey: api.newIdempotencyKey(),
    });
    handle.close();
    showExecuteResult(result, allocation);
    reload();
  } catch (error) {
    handle.close();
    notifyP5Error(error, '执行分配失败');
  }
}

/* ------------------------------------------------------------
   五、列表
   ------------------------------------------------------------ */

/** 表格列定义（租户 / 产品名称靠下拉数据映射，故用工厂函数生成） */
function buildColumns({ tenantLabels, productLabels }) {
  return [
    {
      key: 'allocation_no',
      title: '分配单号',
      sortable: true,
      render: (row) => html`<div class="cell-stack">
        <span class="cell-strong mono">${row.allocationNo || row.id}</span>
        <span class="cell-sub">${row.remark || '—'}</span>
      </div>`,
    },
    {
      key: 'tenant_name',
      title: '归属租户',
      render: (row) => {
        const known = tenantLabels.get(String(row.tenantId));
        return html`<div class="cell-stack">
          <span>${known?.name || '-'}</span>
          <span class="cell-sub mono">${known?.code || row.tenantId || '-'}</span>
        </div>`;
      },
    },
    {
      key: 'client_product_name',
      title: '客户产品',
      render: (row) => {
        const known = productLabels.get(String(row.clientProductId));
        return html`<div class="cell-stack">
          <span>${known?.name || '-'}</span>
          <span class="cell-sub mono">${known?.code || row.clientProductId || '-'}</span>
        </div>`;
      },
    },
    {
      key: 'status',
      title: '状态',
      width: '110px',
      render: (row) => statusTag(row.status, ALLOCATION_STATUS_MAP),
    },
    {
      key: 'total_count',
      title: '总台数',
      align: 'num',
      width: '90px',
      render: (row) => esc(row.totalCount ?? 0),
    },
    {
      key: 'allocated_count',
      title: '已分配',
      align: 'num',
      width: '90px',
      render: (row) => esc(row.allocatedCount ?? 0),
    },
    {
      key: 'failed_count',
      title: '失败',
      align: 'num',
      width: '80px',
      // 失败数非 0 时标红：这是用户唯一需要立刻介入的信号
      render: (row) =>
        (row.failedCount ?? 0) > 0
          ? html`<span class="text-danger font-semibold">${row.failedCount}</span>`
          : esc(row.failedCount ?? 0),
    },
    {
      key: 'created_by',
      title: '创建人',
      width: '120px',
      render: (row) => esc(row.createdBy || '-'),
    },
    {
      key: 'executed_at',
      title: '执行时间',
      sortable: true,
      width: '150px',
      render: (row) => esc(row.executedAt ? formatDate(row.executedAt) : '-'),
    },
    {
      key: 'actions',
      title: '操作',
      align: 'right',
      width: '190px',
      nowrap: true,
      render: (row) => {
        const canWrite = auth.hasPerm(PERM.platform.allocationWrite);
        const buttons = [
          `<button type="button" class="btn btn-sm btn-ghost" data-action="view-items" data-id="${esc(row.id)}">查看明细</button>`,
        ];
        // 已完成的单不渲染「执行」：再点一次只会得到幂等回放，
        // 给用户一个必然无效果的按钮只会徒增困惑。
        if (canWrite && EXECUTABLE.has(row.status)) {
          buttons.push(
            `<button type="button" class="btn btn-sm btn-primary" data-action="execute-allocation" data-id="${esc(row.id)}">执行</button>`,
          );
        }
        return html`<div class="btn-group">${raw(buttons.join(''))}</div>`;
      },
    },
  ];
}

/**
 * 渲染分配管理页。
 *
 * @param {HTMLElement} container
 * @param {{router: object}} ctx
 */
export async function renderAllocations(container) {
  // 名称映射：列表响应只给 tenantId / clientProductId，名称靠下拉数据补
  const [tenants, products] = await Promise.all([
    fetchRecords('/platform/tenants'),
    fetchRecords('/platform/client-products'),
  ]);
  const tenantOptions = toOptions(tenants, (item) => `${item.name}（${item.code}）`);
  const tenantLabels = new Map(
    tenants.map((item) => [String(item.id), { name: item.name, code: item.code }]),
  );
  const productLabels = new Map(
    products.map((item) => [String(item.id), { name: item.name, code: item.code }]),
  );

  return createListPage({
    container,
    title: '分配管理',
    desc: '把平台库存中的设备分配给租户。分配单创建时即固定设备清单，执行后设备归属租户；失败原因逐条可见。',
    actions: [
      { label: '刷新', icon: 'refresh', action: 'reload-list' },
      {
        label: '新建分配单',
        icon: 'plus',
        variant: 'primary',
        action: 'new-allocation',
        perm: PERM.platform.allocationWrite,
      },
    ],
    columns: buildColumns({ tenantLabels, productLabels }),
    fetcher: (params) =>
      api.get('/platform/allocations', {
        params: {
          page: params.page,
          pageSize: params.pageSize,
          sortBy: params.sortBy,
          order: params.order,
          status: params.status,
          tenantId: params.tenantId,
          keyword: params.keyword,
        },
      }),
    filters: [
      { key: 'keyword', placeholder: '分配单号', width: '200px' },
      { key: 'status', type: 'select', options: ALLOCATION_STATUS_OPTIONS },
      {
        key: 'tenantId',
        type: 'select',
        options: [{ value: '', label: '全部租户' }, ...tenantOptions],
      },
    ],
    onAction: async (action, target, { table: tbl, reload }) => {
      const row = target.dataset.id ? tbl.findRowById(target.dataset.id) : null;

      if (action === 'reload-list') {
        tbl.load();
        return;
      }

      if (action === 'new-allocation') {
        await openCreateAllocation({ tenantOptions, onDone: reload });
        return;
      }

      if (action === 'view-items') {
        if (row) await openAllocationItems(row);
        return;
      }

      if (action === 'execute-allocation') {
        if (row) await executeAllocation(row, reload);
      }
    },
  });
}

export default { renderAllocations, openCreateAllocation, openAllocationItems };
