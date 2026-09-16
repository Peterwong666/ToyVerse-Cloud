/* ============================================================
   商户端 · 知识库
   ------------------------------------------------------------
   路由：`#/knowledge-bases`（菜单里的「知识库」入口）

   对应后端：
     GET    /merchant/knowledge-bases           列表（分页 + keyword）
     POST   /merchant/knowledge-bases           新建
     PUT    /merchant/knowledge-bases/{id}      编辑
     DELETE /merchant/knowledge-bases/{id}      删除（被 AI 配置引用时 409）
     GET    /merchant/knowledge-bases/{id}/files        文件列表（可筛状态）
     POST   /merchant/knowledge-bases/{id}/files        multipart 上传（字段名 file）
     DELETE /merchant/knowledge-bases/{id}/files/{fid}  删除文件
     POST   /merchant/knowledge-bases/{id}/files/{fid}/parse  触发解析

   为什么文件列表做成「同页展开」而不是跳详情页
   ------------------------------------------
   知识库的日常操作是「传一批文件 → 看哪些解析失败 → 删掉重传」。
   跳详情页会丢掉列表的筛选与分页状态，来回几次很烦；同页展开
   则能一边看知识库列表（文件数 / 已解析数的变化）一边处理文件。

   为什么解析失败必须原样展示 errorMessage
   --------------------------------------
   二进制格式（PDF 等）的文本抽取尚未实现，后端会返回
   `status=FAILED` 且 `errorMessage` 写明原因。如果 UI 只显示
   「解析失败」，用户会反复重试同一个必然失败的文件；
   把后端的原话摆出来，用户才知道「要换格式」而不是「再点一次」。

   为什么删除知识库要单独处理 CASCADE_CONFLICT
   -----------------------------------------
   被产品的 AI 配置引用时后端拒绝删除，并在 `details.products`
   里说明「被谁引用」。只说「冲突」等于没说——用户需要知道
   去改哪几个产品的配置。
   ============================================================ */

import api from '/shared/core/api.js';
import auth, { PERM } from '/shared/core/auth.js';
import { formatBytes, formatDate, formatRelative, fromHtml, h, html, raw } from '/shared/ui/dom.js';
import {
  button,
  card,
  kvList,
  statusTag,
  tag,
} from '/shared/ui/components.js';
import { createListPage } from '/shared/app/page.js';
import { DataTable, bindTableEvents } from '/shared/ui/table.js';
import { formModal } from '/shared/ui/form.js';
import { confirmDialog } from '/shared/ui/modal.js';
import toast from '/shared/ui/toast.js';
import { ENABLE_OPTIONS, fetchRecords, notifyError, toOptions } from '/pages/platform/common.js';

/** 知识库状态（取值同后端 EnableStatus） */
const KB_STATUS_MAP = {
  ENABLED: { text: '启用', tone: 'success' },
  DISABLED: { text: '停用', tone: 'default' },
};

/** 文件解析状态（取值同后端 KbFileStatus） */
const FILE_STATUS_MAP = {
  PENDING: { text: '待解析', tone: 'warning' },
  PARSING: { text: '解析中', tone: 'info' },
  PARSED: { text: '已解析', tone: 'success' },
  FAILED: { text: '解析失败', tone: 'danger' },
};

const FILE_STATUS_OPTIONS = [
  { value: '', label: '全部解析状态' },
  ...Object.entries(FILE_STATUS_MAP).map(([value, item]) => ({ value, label: item.text })),
];

/** 知识库表单字段（新建 / 编辑共用） */
function kbFields(productOptions, { forEdit = false } = {}) {
  return [
    { key: 'name', label: '知识库名称', required: true, maxLength: 128, span: 2 },
    {
      key: 'description',
      label: '描述',
      type: 'textarea',
      rows: 3,
      maxLength: 1000,
      span: 2,
      placeholder: '这个知识库放什么内容，例如「产品说明书与常见问题」',
    },
    {
      key: 'clientProductId',
      label: '关联客户产品',
      type: 'select',
      span: 2,
      options: [{ value: '', label: '不限定产品（本租户通用）' }, ...productOptions],
      hint: '关联后该知识库只用于这个产品；不限定则本租户的产品都可以引用。',
    },
    {
      key: 'status',
      label: '状态',
      type: 'select',
      options: ENABLE_OPTIONS,
      value: 'ENABLED',
      hint: forEdit ? '' : '停用后 AI 配置仍可引用，但检索不会命中。',
    },
  ];
}

/**
 * 渲染知识库页。
 *
 * @param {HTMLElement} container
 * @param {{router: object}} ctx
 */
export async function renderKnowledge(container, ctx) {
  void ctx;
  const canWrite = auth.hasPerm(PERM.merchant.kbWrite);

  const products = await fetchRecords('/merchant/products');
  const productOptions = toOptions(products, (item) => `${item.name}（${item.code}）`);
  const productLabels = new Map(products.map((item) => [String(item.id), item.name]));

  /* ---- 文件管理面板（同页展开） ---- */
  const panelHost = h('div', { class: 'mt-4' });
  let currentKb = null;
  let filesTable = null;

  const FILE_COLUMNS = [
    {
      key: 'filename',
      title: '文件',
      render: (row) => html`<div class="cell-stack">
        <span class="cell-strong mono">${row.filename || '-'}</span>
        <span class="cell-sub">${row.contentType || '未知类型'}</span>
      </div>`,
    },
    {
      key: 'size_bytes',
      title: '大小',
      align: 'num',
      width: '100px',
      render: (row) => formatBytes(row.sizeBytes),
    },
    {
      key: 'status',
      title: '解析状态',
      width: '110px',
      // 优先用后端给的 statusLabel（它就是权威文案），配色仍由前端映射决定
      render: (row) => {
        const tone = FILE_STATUS_MAP[row.status]?.tone || 'default';
        return tag(row.statusLabel || FILE_STATUS_MAP[row.status]?.text || row.status || '-', tone);
      },
    },
    {
      key: 'chunk_count',
      title: '分块数',
      align: 'num',
      width: '90px',
      render: (row) => String(row.chunkCount ?? 0),
    },
    {
      key: 'error_message',
      title: '错误原因',
      // 原样展示后端 errorMessage：这是用户判断「该换格式还是该重试」的唯一依据
      render: (row) =>
        row.errorMessage
          ? html`<span class="text-danger text-xs">${row.errorMessage}</span>`
          : html`<span class="text-secondary">-</span>`,
    },
    {
      key: 'created_at',
      title: '上传时间',
      width: '150px',
      render: (row) => formatDate(row.createdAt),
    },
    {
      key: 'actions',
      title: '操作',
      align: 'right',
      width: '150px',
      nowrap: true,
      render: (row) => {
        const buttons = [];
        if (canWrite) {
          buttons.push(
            `<button type="button" class="btn btn-sm" data-action="parse-file" data-id="${row.id}">解析</button>`,
          );
          buttons.push(
            `<button type="button" class="btn btn-sm btn-ghost" data-action="delete-file" data-id="${row.id}">删除</button>`,
          );
        }
        return html`<div class="btn-group">${raw(buttons.join(''))}</div>`;
      },
    },
  ];

  /** 打开 / 刷新某个知识库的文件面板 */
  function openFiles(kb) {
    currentKb = kb;

    const cardEl = fromHtml(
      card({
        title: `文件管理 · ${kb.name}`,
        subtitle: '上传后需要点「解析」才会切块进检索索引；解析失败会显示后端给出的具体原因。',
        actions: button({ label: '关闭', variant: 'ghost', size: 'sm', action: 'close-files' }),
      }),
    );

    const body = h('div');
    body.append(
      fromHtml(
        kvList([
          ['关联产品', kb.clientProductId ? productLabels.get(String(kb.clientProductId)) || kb.clientProductId : '不限定产品'],
          ['文件数', kb.fileCount ?? kb.docCount ?? '-'],
          ['已解析文件数', kb.parsedFileCount ?? '-'],
          ['分块数', kb.chunkCount ?? '-'],
        ]),
      ),
    );

    /* ---- 上传区 ---- */
    const uploadRow = h('div', { class: 'flex items-center gap-3 flex-wrap mt-4' });
    const fileInput = h('input', { type: 'file', class: 'input', dataset: { fileInput: 'true' }, style: { maxWidth: '360px' } });
    const uploadBtn = fromHtml(
      button({
        label: '上传文件',
        icon: 'upload',
        variant: 'primary',
        size: 'sm',
        action: 'upload-file',
        disabled: !canWrite,
      }),
    );
    uploadRow.append(fileInput, uploadBtn);
    body.append(uploadRow);
    body.append(
      fromHtml(
        html`<div class="field-hint mt-1">
          字段以 multipart 的 <span class="mono">file</span> 上传。
          文本 / Markdown 等可抽取格式能解析成功；PDF 等二进制格式的文本抽取尚未实现，
          解析会返回失败并写明原因（请把原话转给平台，而不是反复重试）。
        </div>`,
      ),
    );

    /* ---- 状态筛选（直接接到 DataTable，无需额外的「查询」按钮） ---- */
    const statusSelect = h('select', { class: 'select', style: { width: '160px' } });
    FILE_STATUS_OPTIONS.forEach((opt) => {
      statusSelect.append(h('option', { value: opt.value, text: opt.label }));
    });

    const filesHost = h('div', { class: 'mt-4' });
    body.append(h('div', { class: 'flex items-center gap-2 mt-4' }, statusSelect), filesHost);
    cardEl.append(body);
    panelHost.replaceChildren(cardEl);

    filesTable = new DataTable({
      container: filesHost,
      columns: FILE_COLUMNS,
      pageSize: 10,
      emptyText: '该知识库还没有文件',
      fetcher: (params) =>
        api.get(`/merchant/knowledge-bases/${kb.id}/files`, {
          params: { page: params.page, pageSize: params.pageSize, status: params.status },
        }),
    });
    bindTableEvents(filesHost, filesTable);

    statusSelect.addEventListener('change', () => {
      filesTable.setFilters({ status: statusSelect.value || null });
    });

    filesTable.load();

    // 滚动到面板，避免「点了没反应」的错觉
    cardEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  /* ---- 面板内的操作（panelHost 每次渲染只绑一次） ---- */
  panelHost.addEventListener('click', async (event) => {
    const target = event.target.closest('[data-action]');
    if (!target || !currentKb) return;
    const action = target.dataset.action;

    if (action === 'close-files') {
      currentKb = null;
      filesTable = null;
      panelHost.replaceChildren();
      return;
    }

    if (action === 'upload-file') {
      const input = panelHost.querySelector('[data-file-input]');
      const file = input?.files?.[0];
      if (!file) {
        toast.warning('请先选择要上传的文件');
        return;
      }
      const formData = new FormData();
      formData.append('file', file);
      target.disabled = true;
      try {
        await api.upload(`/merchant/knowledge-bases/${currentKb.id}/files`, formData, {
          idempotencyKey: api.newIdempotencyKey(),
        });
        toast.success(`文件「${file.name}」已上传，请点「解析」使其进入检索索引`);
        filesTable?.load();
        page.reload();
      } catch (error) {
        notifyError(error, '上传失败');
      } finally {
        target.disabled = false;
      }
      return;
    }

    if (action === 'parse-file') {
      const fileId = target.dataset.id;
      target.disabled = true;
      try {
        const result = await api.post(
          `/merchant/knowledge-bases/${currentKb.id}/files/${fileId}/parse`,
          undefined,
          { idempotencyKey: api.newIdempotencyKey() },
        );
        if (result?.status === 'FAILED') {
          // 后端已经把原因写清楚了，这里原样透出（不吞、不美化）
          toast.warning(`解析失败：${result.errorMessage || '后端未返回具体原因'}`, { duration: 9000 });
        } else {
          toast.success('解析完成，已切块进入检索索引');
        }
        filesTable?.load();
        page.reload();
      } catch (error) {
        notifyError(error, '触发解析失败');
      } finally {
        target.disabled = false;
      }
      return;
    }

    if (action === 'delete-file') {
      const fileId = target.dataset.id;
      const { confirmed } = await confirmDialog({
        title: '删除文件',
        description: '删除后该文件的分块会一并从检索索引移除，AI 将无法再引用其中的内容。',
        tone: 'danger',
        confirmText: '确认删除',
      });
      if (!confirmed) return;
      try {
        await api.del(`/merchant/knowledge-bases/${currentKb.id}/files/${fileId}`, undefined, {
          idempotencyKey: api.newIdempotencyKey(),
        });
        toast.success('文件已删除');
        filesTable?.load();
        page.reload();
      } catch (error) {
        notifyError(error, '删除失败');
      }
    }
  });

  /* ---- 知识库列表 ---- */
  const COLUMNS = [
    {
      key: 'name',
      title: '知识库',
      sortable: true,
      render: (row) => html`<div class="cell-stack">
        <span class="cell-strong">${row.name || '-'}</span>
        <span class="cell-sub">${row.description || '—'}</span>
      </div>`,
    },
    {
      key: 'client_product',
      title: '关联产品',
      render: (row) =>
        row.clientProductId
          ? productLabels.get(String(row.clientProductId)) || row.clientProductId
          : '不限定产品',
    },
    {
      key: 'file_count',
      title: '文件 / 已解析',
      align: 'num',
      width: '120px',
      render: (row) => html`<span class="num">${row.fileCount ?? '-'}</span>
        <span class="text-secondary"> / ${row.parsedFileCount ?? '-'}</span>`,
    },
    {
      key: 'chunk_count',
      title: '分块数',
      align: 'num',
      width: '90px',
      render: (row) => String(row.chunkCount ?? '-'),
    },
    { key: 'status', title: '状态', width: '90px', render: (row) => statusTag(row.status, KB_STATUS_MAP) },
    {
      key: 'updated_at',
      title: '最后更新',
      width: '140px',
      render: (row) => formatRelative(row.updatedAt),
    },
    {
      key: 'actions',
      title: '操作',
      align: 'right',
      width: '200px',
      nowrap: true,
      render: (row) => {
        const buttons = [
          `<button type="button" class="btn btn-sm btn-primary" data-action="manage-files" data-id="${row.id}">文件</button>`,
        ];
        if (canWrite) {
          buttons.push(`<button type="button" class="btn btn-sm" data-action="edit-kb" data-id="${row.id}">编辑</button>`);
          buttons.push(
            `<button type="button" class="btn btn-sm btn-ghost" data-action="delete-kb" data-id="${row.id}">删除</button>`,
          );
        }
        return html`<div class="btn-group">${raw(buttons.join(''))}</div>`;
      },
    },
  ];

  const page = createListPage({
    container,
    title: '知识库',
    desc: '为产品的 AI 提供可检索的私有内容。上传文件后需要点「解析」才会切块进索引；被产品 AI 配置引用的知识库不能删除。',
    actions: [
      { label: '刷新', icon: 'refresh', action: 'reload-list' },
      {
        label: '新建知识库',
        icon: 'plus',
        variant: 'primary',
        action: 'new-kb',
        perm: PERM.merchant.kbWrite,
      },
    ],
    columns: COLUMNS,
    fetcher: (params) =>
      api.get('/merchant/knowledge-bases', {
        params: {
          page: params.page,
          pageSize: params.pageSize,
          keyword: params.keyword,
          sortBy: params.sortBy,
          order: params.order,
        },
      }),
    filters: [{ key: 'keyword', placeholder: '名称 / 描述', width: '220px' }],
    onAction: async (action, target, { table: tbl, reload }) => {
      const row = target.dataset.id ? tbl.findRowById(target.dataset.id) : null;

      if (action === 'reload-list') {
        tbl.load();
        return;
      }

      if (action === 'new-kb') {
        const created = await formModal({
          title: '新建知识库',
          subtitle: '创建后可在列表里点「文件」上传并解析内容',
          size: 'lg',
          submitText: '创建',
          fields: kbFields(productOptions),
          onSubmit: (values) =>
            api.post('/merchant/knowledge-bases', values, { idempotencyKey: api.newIdempotencyKey() }),
        });
        if (created) {
          toast.success(`知识库「${created.name || ''}」已创建，请继续上传文件`);
          reload();
        }
        return;
      }

      if (action === 'edit-kb') {
        if (!row) return;
        const updated = await formModal({
          title: `编辑知识库「${row.name}」`,
          size: 'lg',
          submitText: '保存',
          fields: kbFields(productOptions, { forEdit: true }),
          values: {
            name: row.name,
            description: row.description,
            clientProductId: row.clientProductId ?? '',
            status: row.status,
          },
          onSubmit: (values) =>
            api.put(`/merchant/knowledge-bases/${row.id}`, values, {
              idempotencyKey: api.newIdempotencyKey(),
            }),
        });
        if (updated) {
          toast.success('知识库已更新');
          reload();
          // 面板里展示的是旧值，重新打开一次以保持同步
          if (currentKb?.id === row.id) openFiles({ ...currentKb, ...updated });
        }
        return;
      }

      if (action === 'delete-kb') {
        if (!row) return;
        const { confirmed } = await confirmDialog({
          title: `删除知识库「${row.name}」`,
          description: '知识库下的文件与分块会一并删除，AI 将无法再引用其中的内容。',
          detail: '若该知识库仍被某个产品的 AI 配置引用，后端会拒绝删除并列出引用它的产品。',
          tone: 'danger',
          confirmText: '确认删除',
        });
        if (!confirmed) return;

        try {
          await api.del(`/merchant/knowledge-bases/${row.id}`, undefined, {
            idempotencyKey: api.newIdempotencyKey(),
          });
          toast.success('知识库已删除');
          if (currentKb?.id === row.id) {
            currentKb = null;
            panelHost.replaceChildren();
          }
          reload();
        } catch (error) {
          if (error?.code === 'CASCADE_CONFLICT') {
            // details.products 的形状可能是对象数组也可能是字符串数组，两种都兼容
            const refs = Array.isArray(error.details?.products) ? error.details.products : [];
            const names = refs
              .map((item) =>
                typeof item === 'string'
                  ? item
                  : item?.name || item?.productName || item?.code || item?.id || '',
              )
              .filter(Boolean)
              .join('、');
            toast.error(
              `${error.message || '该知识库仍被引用，无法删除。'}${
                names ? `　引用它的产品：${names}。` : ''
              }请先到这些产品的「AI 配置」里改绑其它知识库。`,
              { traceId: error?.traceId || '', duration: 9000 },
            );
            return;
          }
          notifyError(error, '删除失败');
        }
        return;
      }

      if (action === 'manage-files') {
        if (row) openFiles(row);
      }
    },
  });

  page.root.append(panelHost);

  return page;
}

export default { renderKnowledge };
