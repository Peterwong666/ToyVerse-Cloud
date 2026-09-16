/* ============================================================
   平台端 · 设备批次导入
   ------------------------------------------------------------
   对应后端 `/platform/batches`：
     GET   列表
     POST  /              multipart 上传（字段名 file，可选 query orderId）
     POST  /{id}/import  执行导入
     GET   /{id}/lines   明细行（分页）
     GET   /{id}/error-report  错误报告（**CSV 文件流**）

   为什么流程拆成「上传 → 预检 → 导入」两步
   ----------------------------------------
   CSV 由厂商/客户提供，格式错误极常见。先预检（校验 SN/IMEI 合法性、
   库内是否重复）给出 总行数/有效/无效/重复，再允许导入，
   可以避免「导入一半失败」导致库存里出现半截数据。
   因此列表里「导入」按钮只在预检完成后出现。
   ============================================================ */

import api from '/shared/core/api.js';
import auth, { PERM } from '/shared/core/auth.js';
import { downloadFile, esc, formatDate, fromHtml, h, html, raw } from '/shared/ui/dom.js';
import { descList, emptyState, statusTag } from '/shared/ui/components.js';
import { table } from '/shared/ui/table.js';
import { createListPage } from '/shared/app/page.js';
import { drawer } from '/shared/ui/modal.js';
import toast from '/shared/ui/toast.js';
import {
  BATCH_LINE_STATUS_MAP,
  BATCH_STATUS_MAP,
  BATCH_STATUS_OPTIONS,
  NETWORK_MAP,
  notifyError,
} from './common.js';

/** 可以执行导入的状态（已上传或预检完成；失败后可重试） */
const IMPORTABLE = new Set(['UPLOADED', 'PRE_CHECKED', 'FAILED']);

/** 表格列定义 */
const COLUMNS = [
  {
    key: 'batch_no',
    title: '批次号',
    sortable: true,
    render: (row) => html`<div class="cell-stack">
      <span class="cell-strong mono">${row.batchNo || row.id}</span>
      <span class="cell-sub">${row.fileName || '-'}</span>
    </div>`,
  },
  {
    key: 'network_type',
    title: '联网方式',
    width: '105px',
    render: (row) => statusTag(row.networkType, NETWORK_MAP),
  },
  { key: 'status', title: '状态', width: '130px', render: (row) => statusTag(row.status, BATCH_STATUS_MAP) },
  {
    key: 'rows',
    title: '总行数 / 有效 / 无效 / 重复',
    align: 'num',
    width: '190px',
    render: (row) => html`<span class="num">${row.totalRows ?? 0}</span>
      <span class="text-secondary"> / ${row.validRows ?? 0} / </span>
      <span class="text-danger">${row.invalidRows ?? 0}</span>
      <span class="text-secondary"> / </span>
      <span class="text-warning">${row.duplicatedRows ?? 0}</span>`,
  },
  {
    key: 'imported_rows',
    title: '已导入',
    align: 'num',
    width: '90px',
    render: (row) => esc(row.importedRows ?? 0),
  },
  {
    key: 'created_at',
    title: '创建时间',
    sortable: true,
    width: '150px',
    render: (row) => esc(formatDate(row.createdAt)),
  },
  {
    key: 'actions',
    title: '操作',
    align: 'right',
    width: '240px',
    nowrap: true,
    render: (row) => {
      const canWrite = auth.hasPerm(PERM.platform.batchWrite);
      const buttons = [
        `<button type="button" class="btn btn-sm btn-ghost" data-action="view-lines" data-id="${esc(row.id)}">明细</button>`,
      ];
      if (canWrite && IMPORTABLE.has(row.status)) {
        buttons.push(
          `<button type="button" class="btn btn-sm btn-primary" data-action="import-batch" data-id="${esc(row.id)}">导入</button>`,
        );
      }
      // 错误报告是只读下载，不需要 batchWrite；只要批次有可能存在错误行就展示
      if (
        row.errorReportAvailable ||
        (row.invalidRows ?? 0) > 0 ||
        (row.duplicatedRows ?? 0) > 0
      ) {
        buttons.push(
          `<button type="button" class="btn btn-sm" data-action="download-errors" data-id="${esc(row.id)}">错误报告 CSV</button>`,
        );
      }
      return html`<div class="btn-group">${raw(buttons.join(''))}</div>`;
    },
  },
];

/* ------------------------------------------------------------
   一、上传与预检
   ------------------------------------------------------------ */

/** 上传区域（DOM 构造：含 input[type=file]，不适合用 HTML 字符串拼） */
function buildUploadCard(page) {
  const card = h('div', { class: 'card mb-4' });
  card.append(fromHtml(`<div class="card-head"><div class="card-title">上传 CSV 批次</div></div>`));

  const body = h('div', { class: 'p-4' });
  const row = h('div', { class: 'flex items-center gap-3 flex-wrap' });

  const fileInput = h('input', {
    type: 'file',
    accept: '.csv,text/csv',
    class: 'input',
    style: { maxWidth: '320px' },
  });
  // 关联订单：本阶段后端用 query 参数接收，留空则建一个「无订单」的库存批次
  const orderInput = h('input', {
    type: 'text',
    class: 'input',
    placeholder: '可选：关联订单 ID',
    style: { maxWidth: '240px' },
  });
  const uploadBtn = h('button', { class: 'btn btn-primary', type: 'button', text: '上传并预检' });

  row.append(fileInput, orderInput, uploadBtn);
  body.append(row);
  body.append(
    h('div', {
      class: 'field-hint mt-2',
      text: 'CSV 表头需包含 sn，可选 imei / iccid / mac。上传后先做预检（不写库），确认无误再点列表里的「导入」。',
    }),
  );

  const resultHost = h('div', { class: 'mt-3' });
  body.append(resultHost);
  card.append(body);

  uploadBtn.addEventListener('click', async () => {
    const file = fileInput.files?.[0];
    if (!file) {
      toast.warning('请先选择 CSV 文件');
      return;
    }

    const formData = new FormData();
    formData.append('file', file);
    const orderId = orderInput.value.trim();

    uploadBtn.disabled = true;
    uploadBtn.textContent = '上传中…';
    try {
      const created = await api.upload('/platform/batches', formData, {
        params: orderId ? { orderId } : undefined,
        idempotencyKey: api.newIdempotencyKey(),
      });
      resultHost.replaceChildren(
        fromHtml(
          descList(
            [
              ['批次号', created?.batchNo || '-'],
              ['总行数', created?.totalRows ?? '—'],
              ['有效行', created?.validRows ?? '—'],
              ['无效行', created?.invalidRows ?? '—'],
              ['重复行', created?.duplicatedRows ?? '—'],
            ],
            { cols: 2 },
          ),
        ),
      );
      toast.success('上传完成，已预检；确认无误后点列表里的「导入」');
      page.reload();
    } catch (error) {
      notifyError(error, '上传失败，请检查 CSV 格式');
    } finally {
      uploadBtn.disabled = false;
      uploadBtn.textContent = '上传并预检';
    }
  });

  return card;
}

/* ------------------------------------------------------------
   二、明细抽屉
   ------------------------------------------------------------ */

async function openLines(batch) {
  let payload = null;
  try {
    payload = await api.get(`/platform/batches/${batch.id}/lines`, {
      params: { pageSize: 200 },
      silent: true,
    });
  } catch (error) {
    notifyError(error, '批次明细获取失败');
    return;
  }

  const rows = Array.isArray(payload?.records) ? payload.records : [];
  const body = h('div');

  if (!rows.length) {
    body.append(
      fromHtml(
        emptyState({
          icon: 'file',
          title: '暂无明细行',
          desc: '该批次还没有明细数据，或明细超出本阶段一次可加载的条数。',
        }),
      ),
    );
  } else {
    const host = h('div');
    host.innerHTML = table({
      columns: [
        { key: 'row_no', title: '行号', width: '70px', align: 'num', render: (row) => esc(row.rowNo ?? '-') },
        {
          key: 'sn',
          title: 'SN / IMEI / MAC',
          render: (row) => html`<div class="cell-stack">
            <span class="cell-strong mono">${row.sn || '-'}</span>
            <span class="cell-sub mono">${row.imei || row.mac || '-'}</span>
          </div>`,
        },
        { key: 'status', title: '状态', width: '100px', render: (row) => statusTag(row.status, BATCH_LINE_STATUS_MAP) },
        {
          key: 'error_message',
          title: '错误信息',
          render: (row) => (row.errorMessage ? html`<span class="text-danger">${row.errorMessage}</span>` : '—'),
        },
      ],
      rows,
      emptyText: '暂无明细',
    });
    body.append(host);
  }

  drawer({
    title: `批次明细 ${batch.batchNo || batch.id}`,
    wide: true,
    body,
  });
}

/* ------------------------------------------------------------
   三、错误报告下载
   ------------------------------------------------------------ */

async function downloadErrorReport(batch) {
  try {
    // 为什么不用 <a href>：那样不会带 Authorization 头，后端会直接 401。
    // 为什么不用 api.raw() 再 blob()：当前 api 的 raw 选项返回的是**已解析的
    // 响应体**（不是 Response），拿不到 blob()；而错误报告是 text/csv，
    // api.get 的解析逻辑会原样返回 CSV 文本，直接落盘即可（且自动带鉴权头）。
    const content = await api.get(`/platform/batches/${batch.id}/error-report`, { silent: true });
    if (typeof content !== 'string' || !content.trim()) {
      toast.warning('错误报告为空，可能所有行都已通过校验');
      return;
    }
    downloadFile(`批次-${batch.batchNo || batch.id}-错误行.csv`, content, 'text/csv;charset=utf-8');
    toast.success('错误报告已开始下载');
  } catch (error) {
    notifyError(error, '错误报告下载失败');
  }
}

/* ------------------------------------------------------------
   页面入口
   ------------------------------------------------------------ */

/**
 * 渲染批次页。
 *
 * @param {HTMLElement} container
 */
export async function renderBatches(container) {
  const page = createListPage({
    container,
    title: '设备批次',
    desc: '通过 CSV 批量导入设备（SN 等）。先上传预检，再执行导入；错误行可下载报告后修正重传。',
    actions: [{ label: '刷新', icon: 'refresh', action: 'reload-list' }],
    columns: COLUMNS,
    fetcher: (params) =>
      api.get('/platform/batches', {
        params: {
          page: params.page,
          pageSize: params.pageSize,
          sortBy: params.sortBy,
          order: params.order,
          status: params.status,
          keyword: params.keyword,
        },
      }),
    filters: [
      { key: 'keyword', placeholder: '批次号 / 文件名', width: '200px' },
      { key: 'status', type: 'select', options: BATCH_STATUS_OPTIONS },
    ],
    onAction: async (action, target, { table: tbl, reload }) => {
      const row = target.dataset.id ? tbl.findRowById(target.dataset.id) : null;

      if (action === 'reload-list') {
        tbl.load();
        return;
      }

      if (action === 'view-lines') {
        if (row) await openLines(row);
        return;
      }

      if (action === 'import-batch') {
        if (!row) return;
        try {
          const result = await api.post(
            `/platform/batches/${row.id}/import`,
            undefined,
            { idempotencyKey: api.newIdempotencyKey() },
          );
          const summary = `导入完成：已导入 ${result?.importedRows ?? 0} 行，失败 ${result?.invalidRows ?? 0} 行`;
          if (result?.invalidRows) toast.warning(summary);
          else toast.success(summary);
          reload();
        } catch (error) {
          notifyError(error, '导入失败');
        }
        return;
      }

      if (action === 'download-errors') {
        if (row) await downloadErrorReport(row);
      }
    },
  });

  // 上传区插在页头与表格之间（与设备库存的统计卡同一位置策略）
  page.root.insertBefore(buildUploadCard(page), page.root.children[1] || null);

  return page;
}

export default { renderBatches };
