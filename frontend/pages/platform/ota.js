/* ============================================================
   平台端 · OTA 管理
   ------------------------------------------------------------
   路由：`#/ota`

   对应后端：
     GET    /platform/ota/packages              固件包列表（状态 / 模板 / 关键字）
     POST   /platform/ota/packages              multipart 上传（file 可选）
     GET    /platform/ota/packages/{id}         详情 + pushStats + 最近 20 条记录
     PUT    /platform/ota/packages/{id}         编辑（发布说明 / 最低版本 / 强制 / 状态）
     DELETE /platform/ota/packages/{id}         删除
     POST   /platform/ota/packages/{id}/push    推送给某个客户产品
     GET    /platform/ota/records               推送记录（按包 / 状态 / 设备筛）
     GET    /platform/templates                 上传时的模板下拉
     GET    /platform/client-products           推送目标的产品下拉

   「Wi-Fi 产品不能推送」必须以方案级提示呈现
   -----------------------------------------
   京东 JoyInside（Wi-Fi）方案**本身不支持平台侧推送**（只能端侧升级）。
   后端返回 409 `OTA_NOT_SUPPORTED`，`details` 里带 `otaSupport` /
   `cloudVendor` / `cloudProviderName`。如果只弹一句「推送失败」，
   用户会以为是自己选错了设备或网络抖动，反复重试；因此这里把
   「哪个联网方案、为什么不行」直接写在弹窗里。

   为什么上传的文件是**可选**的
   --------------------------
   契约里 `file` 可选：只登记版本信息（`hasFile=false`）也是合法状态，
   用于「先在系统里建好版本、稍后再传包」的场景。因此提交按钮不会
   因为没选文件而被禁用，只在提交成功后提示 hasFile 的真实结果。

   为什么推送结果要逐台展示
   ----------------------
   OTA 的现实是「大部分成功、少数失败」。只报一个批次状态无法回答
   「哪几台没升上去、为什么」——所以结果弹窗逐台列出
   `status / fromVersion → toVersion / errorMessage / vendorMessage`。
   ============================================================ */

import api from '/shared/core/api.js';
import auth, { PERM } from '/shared/core/auth.js';
import { formatBytes, formatDate, fromHtml, h, html, raw } from '/shared/ui/dom.js';
import {
  alert,
  card,
  emptyState,
  kvList,
  loadingState,
  statGrid,
  tag,
} from '/shared/ui/components.js';
import { createListPage } from '/shared/app/page.js';
import { DataTable, bindTableEvents, table } from '/shared/ui/table.js';
import { collect, form } from '/shared/ui/form.js';
import { confirmDialog, modal } from '/shared/ui/modal.js';
import toast from '/shared/ui/toast.js';
import { fetchRecords, notifyError, toOptions } from './common.js';

/** 固件包状态（取值同后端 EnableStatus；后端另给 statusLabel 时以它为准） */
const PACKAGE_STATUS_MAP = {
  ENABLED: { text: '已启用', tone: 'success' },
  DISABLED: { text: '已停用', tone: 'default' },
};

/** 推送记录状态（取值同后端 OtaPushStatus） */
const PUSH_STATUS_MAP = {
  PENDING: { text: '待推送', tone: 'warning' },
  PUSHING: { text: '推送中', tone: 'info' },
  SUCCESS: { text: '成功', tone: 'success' },
  PARTIAL_FAILED: { text: '部分失败', tone: 'warning' },
  FAILED: { text: '失败', tone: 'danger' },
};

const PUSH_STATUS_OPTIONS = [
  { value: '', label: '全部推送状态' },
  ...Object.entries(PUSH_STATUS_MAP).map(([value, item]) => ({ value, label: item.text })),
];

/** 把一次推送结果里的单台记录渲染成表格 */
function pushRecordsTable(records = []) {
  return table({
    columns: [
      {
        key: 'device',
        title: '设备',
        render: (row) => html`<div class="cell-stack">
          <span class="cell-strong mono">${row.sn || '-'}</span>
          <span class="cell-sub mono">${row.deviceId || ''}</span>
        </div>`,
      },
      {
        key: 'status',
        title: '结果',
        width: '110px',
        render: (row) => {
          const tone = PUSH_STATUS_MAP[row.status]?.tone || 'default';
          return tag(PUSH_STATUS_MAP[row.status]?.text || row.status || '-', tone);
        },
      },
      {
        key: 'version',
        title: '版本变化',
        width: '150px',
        render: (row) => html`<span class="mono">${row.fromVersion || '-'} → ${row.toVersion || '-'}</span>`,
      },
      {
        key: 'error',
        title: '失败原因',
        // 后端给的原因（含厂商原话）必须原样展示，这是排查的唯一线索
        render: (row) =>
          row.errorMessage || row.vendorMessage
            ? html`<span class="text-danger text-xs">${row.errorMessage || ''}
                ${row.vendorMessage ? `（厂商：${row.vendorMessage}）` : ''}</span>`
            : html`<span class="text-secondary">-</span>`,
      },
    ],
    rows: records,
    emptyText: '本次推送没有逐台记录',
  });
}

/** 展示推送结果（独立弹窗，带逐台明细） */
function showPushResult(result) {
  const body = h('div');
  body.append(
    fromHtml(
      statGrid([
        { label: '请求台数', value: result?.requested ?? 0, unit: '台', icon: 'device', tone: 'brand' },
        { label: '推送成功', value: result?.pushed ?? 0, unit: '台', icon: 'checkCircle', tone: 'success' },
        { label: '推送失败', value: result?.failed ?? 0, unit: '台', icon: 'xCircle', tone: 'danger' },
        { label: '已跳过', value: result?.skipped ?? 0, unit: '台', icon: 'minus', tone: 'default' },
      ]),
    ),
  );

  if (result?.otaSupport && result.otaSupport !== 'SUPPORTED') {
    body.append(
      fromHtml(
        alert({
          tone: 'warning',
          title: '该联网方案仅支持端侧升级',
          text: `方案的推送能力为 ${result.otaSupport}，平台不会向设备下发固件；请通过端侧（设备自身）升级流程处理。`,
        }),
      ),
    );
  }

  const wrap = h('div', { class: 'mt-4' });
  wrap.append(fromHtml(pushRecordsTable(result?.records || [])));
  body.append(wrap);

  const dialog = modal({
    title: '推送结果',
    subtitle: `目标客户产品 ${result?.clientProductId || '-'}`,
    size: 'xl',
    body,
    footer: (dlg) => {
      const close = h('button', { class: 'btn btn-primary', type: 'button', text: '知道了' });
      close.addEventListener('click', () => dlg.close(true));
      return [close];
    },
  });

  return dialog.result;
}

/**
 * 打开推送弹窗。
 *
 * @param {object} row 固件包行
 * @param {object} ctx { productOptions, reload }
 */
async function openPushModal(row, ctx) {
  const { productOptions, reload } = ctx;
  if (!productOptions.length) {
    toast.warning('没有可推送的客户产品，无法推送');
    return;
  }

  const productSelect = h('select', { class: 'select' });
  productOptions.forEach((opt) => {
    productSelect.append(h('option', { value: opt.value, text: opt.label }));
  });

  const deviceInput = h('textarea', {
    class: 'textarea',
    rows: '4',
    placeholder: '每行一个设备 ID（也可用逗号分隔）；留空表示推送该产品下的全部设备',
  });

  const noteHost = h('div', { class: 'mb-3' });

  const body = h('div');
  const productField = h('div', { class: 'field' });
  productField.append(
    h('label', { class: 'field-label', text: '目标客户产品' }),
    productSelect,
    h('div', {
      class: 'field-hint',
      // 只描述**规则**，不点名厂商：判定读的是 `cloud_providers.ota_support`，
      // 写死厂商名会在换供应商或新接一家支持 OTA 的 Wi-Fi 厂商后变成误导文案。
      text: '推送能力由该产品的云服务商决定：`ota_support=SUPPORTED` 的方案可由平台推送，'
        + '`UNSUPPORTED`（目前所有 Wi-Fi 方案）只能端侧升级。',
    }),
  );
  const deviceField = h('div', { class: 'field mt-3' });
  deviceField.append(
    h('label', { class: 'field-label', text: '指定设备（可选）' }),
    deviceInput,
    h('div', {
      class: 'field-hint',
      text: '留空则推送该产品下所有在线且版本低于目标版本的设备；填了则只推送列出的设备。',
    }),
  );
  body.append(noteHost, productField, deviceField);

  const dialog = modal({
    title: `推送固件 ${row.version}`,
    subtitle: `${row.templateName || row.templateId || ''}　·　${row.fileName || '未上传固件文件'}`,
    size: 'lg',
    body,
    footer: (dlg) => {
      const cancel = h('button', { class: 'btn', type: 'button', text: '取消' });
      cancel.addEventListener('click', () => dlg.close(false));

      const push = h('button', { class: 'btn btn-primary', type: 'button', text: '开始推送' });
      push.addEventListener('click', async () => {
        const deviceIds = deviceInput.value
          .split(/[\s,，、;；]+/)
          .map((item) => item.trim())
          .filter(Boolean);

        push.disabled = true;
        push.textContent = '推送中…';
        noteHost.replaceChildren();
        try {
          const result = await api.post(
            `/platform/ota/packages/${row.id}/push`,
            { clientProductId: productSelect.value, deviceIds: deviceIds.length ? deviceIds : null },
            { idempotencyKey: api.newIdempotencyKey() },
          );
          dlg.close(true);
          await showPushResult(result);
          reload();
        } catch (error) {
          push.disabled = false;
          push.textContent = '开始推送';

          if (error?.code === 'OTA_NOT_SUPPORTED') {
            // 方案级提示：说清楚「哪个方案、为什么不行」，而不是笼统的失败
            const details = error.details || {};
            const scheme = details.cloudProviderName || details.cloudVendor || '该联网方案';
            noteHost.replaceChildren(
              fromHtml(
                alert({
                  tone: 'warning',
                  title: '该联网方案仅支持端侧升级，平台无法推送',
                  text:
                    `${scheme} 的 OTA 推送能力为 ${details.otaSupport || 'UNSUPPORTED'}，` +
                    '平台不会向设备下发固件。请改用设备端侧升级流程；' +
                    '这不是设备选择错误，换成其它设备也不会成功。',
                }),
              ),
            );
            return;
          }
          notifyError(error, '推送失败');
        }
      });
      dlg.submit = () => push.click();
      return [cancel, push];
    },
  });

  return dialog.result;
}

/** 打开固件包详情（含最近 20 条推送记录） */
async function openPackageDetail(packageId) {
  const body = h('div');
  body.append(fromHtml(loadingState('正在读取固件包详情…')));
  const dialog = modal({ title: '固件包详情', size: 'xl', body });

  try {
    const data = await api.get(`/platform/ota/packages/${packageId}`);
    const stats = data.pushStats || {};

    body.replaceChildren(
      fromHtml(
        statGrid([
          { label: '推送总数', value: stats.total ?? 0, unit: '台', icon: 'device', tone: 'brand' },
          { label: '成功', value: stats.success ?? 0, unit: '台', icon: 'checkCircle', tone: 'success' },
          { label: '失败', value: stats.failed ?? 0, unit: '台', icon: 'xCircle', tone: 'danger' },
          { label: '待推送', value: stats.pending ?? 0, unit: '台', icon: 'clock', tone: 'warning' },
        ]),
      ),
      fromHtml(
        kvList([
          ['版本', data.version],
          ['产品模板', data.templateName || data.templateId],
          ['文件名', data.fileName],
          ['文件大小', data.fileSize === null || data.fileSize === undefined ? '-' : formatBytes(data.fileSize)],
          ['校验和（SHA-256）', data.checksum],
          ['最低可升级版本', data.minVersion],
          ['强制升级', data.isForced ? '是' : '否'],
          ['状态', PACKAGE_STATUS_MAP[data.status]?.text || data.statusLabel || data.status],
          ['是否已上传文件', data.hasFile ? '是' : '否'],
          ['发布时间', data.publishedAt ? formatDate(data.publishedAt) : '-'],
          ['创建时间', formatDate(data.createdAt)],
        ]),
      ),
    );

    const note = h('div', { class: 'mt-4' });
    note.append(
      fromHtml(`<div class="card-head"><div class="card-title">发布说明</div></div>`),
      fromHtml(html`<div class="text-body">${data.releaseNotes || '（无）'}</div>`),
    );
    body.append(note);

    const recordsWrap = h('div', { class: 'mt-4' });
    recordsWrap.append(
      fromHtml(`<div class="card-head"><div class="card-title">最近 20 条推送记录</div></div>`),
      fromHtml(pushRecordsTable(Array.isArray(data.records) ? data.records : [])),
    );
    body.append(recordsWrap);
  } catch (error) {
    body.replaceChildren(
      fromHtml(
        emptyState({ icon: 'alertTriangle', title: '固件包详情读取失败', desc: error?.message || '请稍后重试。' }),
      ),
    );
  }

  return dialog.result;
}

/**
 * 打开上传固件包弹窗（multipart）。
 *
 * 为什么不用 formModal：formModal 只收集字段值，不能构造 FormData
 * （文件必须走 multipart），因此这里用 modal + form 自己拼 FormData。
 */
async function openUploadModal(templateOptions) {
  const FIELDS = [
    {
      key: 'templateId',
      label: '产品模板',
      type: 'select',
      required: true,
      span: 2,
      options: templateOptions,
      hint: '固件包按模板维度管理，同一模板下版本号唯一。',
    },
    { key: 'version', label: '版本号', required: true, maxLength: 64, placeholder: '如 1.2.0' },
    {
      key: 'minVersion',
      label: '最低可升级版本',
      maxLength: 64,
      placeholder: '留空表示不限制',
      hint: '低于该版本的设备才允许升级（避免把设备降级）。',
    },
    {
      key: 'file',
      label: '固件文件',
      type: 'custom',
      span: 2,
      control: '<input class="input" type="file" name="file" />',
      hint: '可选：不选文件则只登记版本信息（hasFile=false），稍后再传包。',
    },
    {
      key: 'isForced',
      label: '强制升级',
      type: 'switch',
      span: 2,
      hint: '强制升级会在设备下次联网时自动下发，请确认固件已通过验证。',
    },
    { key: 'releaseNotes', label: '发布说明', type: 'textarea', rows: 3, maxLength: 2000, span: 2 },
  ];

  const dialog = modal({
    title: '上传固件包',
    subtitle: '固件文件可选；上传后可在列表里推送给客户产品',
    size: 'lg',
    body: () => fromHtml(form({ fields: FIELDS, columns: 2, id: 'otaPackageForm' })),
    footer: (dlg) => {
      const cancel = h('button', { class: 'btn', type: 'button', text: '取消' });
      cancel.addEventListener('click', () => dlg.close(null));

      const submit = h('button', { class: 'btn btn-primary', type: 'button', text: '上传' });
      submit.addEventListener('click', async () => {
        const formEl = dlg.el.querySelector('#otaPackageForm');
        const values = collect(formEl, FIELDS);
        if (!values.templateId || !values.version) {
          toast.warning('请填写产品模板与版本号');
          return;
        }

        const fileInput = formEl.querySelector('[name="file"]');
        const file = fileInput?.files?.[0] || null;

        const formData = new FormData();
        formData.append('templateId', values.templateId);
        formData.append('version', values.version);
        formData.append('isForced', values.isForced ? 'true' : 'false');
        if (values.minVersion) formData.append('minVersion', values.minVersion);
        if (values.releaseNotes) formData.append('releaseNotes', values.releaseNotes);
        if (file) formData.append('file', file);

        submit.disabled = true;
        submit.textContent = '上传中…';
        try {
          const created = await api.upload('/platform/ota/packages', formData, {
            idempotencyKey: api.newIdempotencyKey(),
          });
          dlg.close(created);
        } catch (error) {
          submit.disabled = false;
          submit.textContent = '上传';
          notifyError(error, '上传失败');
        }
      });
      dlg.submit = () => submit.click();
      return [cancel, submit];
    },
  });

  return dialog.result;
}

/**
 * 渲染 OTA 管理页。
 *
 * @param {HTMLElement} container
 * @param {{router: object}} ctx
 */
export async function renderOta(container, ctx) {
  void ctx;
  const canWrite = auth.hasPerm(PERM.platform.otaWrite);

  /* ---- 下拉数据（静默容错） ---- */
  const templates = await fetchRecords('/platform/templates');
  const templateOptions = toOptions(templates, (item) => `${item.name}（${item.code}）`);
  const products = await fetchRecords('/platform/client-products');
  const productOptions = toOptions(products, (item) => `${item.name}（${item.code}）`);

  const COLUMNS = [
    {
      key: 'version',
      title: '版本',
      sortable: true,
      render: (row) => html`<div class="cell-stack">
        <span class="cell-strong mono">${row.version || '-'}</span>
        <span class="cell-sub">${row.templateName || row.templateId || ''}</span>
      </div>`,
    },
    {
      key: 'file',
      title: '固件文件',
      render: (row) =>
        row.hasFile === false || !row.fileName
          ? tag('未上传文件', 'warning')
          : html`<div class="cell-stack">
              <span class="mono">${row.fileName}</span>
              <span class="cell-sub">${formatBytes(row.fileSize)}</span>
            </div>`,
    },
    {
      key: 'min_version',
      title: '最低版本',
      width: '110px',
      render: (row) => html`<span class="mono">${row.minVersion || '不限制'}</span>`,
    },
    {
      key: 'is_forced',
      title: '强制',
      width: '80px',
      render: (row) => (row.isForced ? tag('强制', 'warning') : tag('可选', 'default')),
    },
    {
      key: 'status',
      title: '状态',
      width: '100px',
      render: (row) => {
        const item = PACKAGE_STATUS_MAP[row.status] || {};
        return tag(row.statusLabel || item.text || row.status || '-', item.tone || 'default');
      },
    },
    {
      key: 'push_stats',
      title: '推送统计',
      width: '180px',
      render: (row) => {
        const stats = row.pushStats || {};
        return html`<div class="cell-stack">
          <span>${raw(tag(`成功 ${stats.success ?? 0}`, 'success'))} ${raw(tag(`失败 ${stats.failed ?? 0}`, stats.failed ? 'danger' : 'default'))}</span>
          <span class="cell-sub">共 ${stats.total ?? 0} 台 · 待推送 ${stats.pending ?? 0} 台</span>
        </div>`;
      },
    },
    {
      key: 'published_at',
      title: '发布时间',
      width: '140px',
      render: (row) => (row.publishedAt ? formatDate(row.publishedAt) : '-'),
    },
    {
      key: 'actions',
      title: '操作',
      align: 'right',
      width: '220px',
      nowrap: true,
      render: (row) => {
        const buttons = [
          `<button type="button" class="btn btn-sm" data-action="open-package" data-id="${row.id}">详情</button>`,
        ];
        if (canWrite) {
          buttons.push(
            `<button type="button" class="btn btn-sm btn-primary" data-action="push-package" data-id="${row.id}">推送</button>`,
          );
          buttons.push(
            `<button type="button" class="btn btn-sm btn-ghost" data-action="delete-package" data-id="${row.id}">删除</button>`,
          );
        }
        return html`<div class="btn-group">${raw(buttons.join(''))}</div>`;
      },
    },
  ];

  /* ---- 推送记录区（同页第二个列表） ---- */
  const recordsHost = h('div');
  let recordsTable = null;

  const recordsSelect = h('select', { class: 'select', style: { width: '180px' } });
  PUSH_STATUS_OPTIONS.forEach((opt) => {
    recordsSelect.append(h('option', { value: opt.value, text: opt.label }));
  });

  const recordsCard = fromHtml(
    card({
      title: '推送记录',
      subtitle: '逐台留痕：只看批次状态无法回答「哪几台没升上去、为什么」。',
    }),
  );
  recordsCard.append(
    h('div', { class: 'flex items-center gap-2 flex-wrap mb-3' }, recordsSelect),
    recordsHost,
  );

  const page = createListPage({
    container,
    title: 'OTA 管理',
    desc:
      '固件包按产品模板管理，推送给客户产品时由该产品的云服务商决定能否推送：' +
      '`ota_support=SUPPORTED` 的方案可由平台推送；`UNSUPPORTED`（目前所有 Wi-Fi 方案）'
      + '只能端侧升级——按产品的云服务商判定，不因选择哪台设备而改变。',
    actions: [
      { label: '刷新', icon: 'refresh', action: 'reload-list' },
      {
        label: '上传固件包',
        icon: 'upload',
        variant: 'primary',
        action: 'upload-package',
        perm: PERM.platform.otaWrite,
      },
    ],
    columns: COLUMNS,
    fetcher: (params) =>
      api.get('/platform/ota/packages', {
        params: {
          page: params.page,
          pageSize: params.pageSize,
          keyword: params.keyword,
          status: params.status,
          templateId: params.templateId,
        },
      }),
    filters: [
      { key: 'keyword', placeholder: '版本 / 文件名', width: '200px' },
      {
        key: 'templateId',
        type: 'select',
        options: [{ value: '', label: '全部模板' }, ...templateOptions],
      },
      {
        key: 'status',
        type: 'select',
        options: [
          { value: '', label: '全部状态' },
          { value: 'ENABLED', label: '已启用' },
          { value: 'DISABLED', label: '已停用' },
        ],
      },
    ],
    onAction: async (action, target, { table: tbl, reload }) => {
      const row = target.dataset.id ? tbl.findRowById(target.dataset.id) : null;

      if (action === 'reload-list') {
        tbl.load();
        recordsTable?.load();
        return;
      }

      if (action === 'upload-package') {
        if (!templateOptions.length) {
          toast.warning('还没有产品模板，无法上传固件包');
          return;
        }
        const created = await openUploadModal(templateOptions);
        if (created) {
          toast.success(
            created.hasFile === false
              ? `版本 ${created.version} 已登记（未上传固件文件）`
              : `固件包 ${created.version} 已上传`,
          );
          reload();
        }
        return;
      }

      if (action === 'open-package') {
        if (row) await openPackageDetail(row.id);
        return;
      }

      if (action === 'push-package') {
        if (row) await openPushModal(row, { productOptions, reload });
        return;
      }

      if (action === 'delete-package') {
        if (!row) return;
        const { confirmed } = await confirmDialog({
          title: `删除固件包 ${row.version}`,
          description: '固件包与它的推送记录会一并删除，删除后无法恢复。',
          detail: '如需保留历史推送记录，请改为「停用」而不是删除。',
          tone: 'danger',
          confirmText: '确认删除',
        });
        if (!confirmed) return;
        try {
          await api.del(`/platform/ota/packages/${row.id}`, undefined, {
            idempotencyKey: api.newIdempotencyKey(),
          });
          toast.success('固件包已删除');
          reload();
        } catch (error) {
          notifyError(error, '删除失败');
        }
      }
    },
  });

  /* ---- 把推送记录区插在固件包列表之后 ---- */
  page.root.append(h('div', { class: 'mt-4' }, recordsCard));

  recordsTable = new DataTable({
    container: recordsHost,
    columns: [
      {
        key: 'device',
        title: '设备',
        render: (row) => html`<div class="cell-stack">
          <span class="cell-strong mono">${row.sn || row.deviceId || '-'}</span>
          <span class="cell-sub mono">${row.deviceId || ''}</span>
        </div>`,
      },
      {
        key: 'package',
        title: '固件包',
        render: (row) => html`<div class="cell-stack">
          <span class="mono">${row.toVersion || row.version || '-'}</span>
          <span class="cell-sub mono">${row.otaPackageId || row.packageId || ''}</span>
        </div>`,
      },
      {
        key: 'status',
        title: '结果',
        width: '110px',
        render: (row) => {
          const tone = PUSH_STATUS_MAP[row.status]?.tone || 'default';
          return tag(PUSH_STATUS_MAP[row.status]?.text || row.status || '-', tone);
        },
      },
      {
        key: 'version',
        title: '版本变化',
        width: '150px',
        render: (row) => html`<span class="mono">${row.fromVersion || '-'} → ${row.toVersion || '-'}</span>`,
      },
      {
        key: 'error',
        title: '失败原因',
        render: (row) =>
          row.errorMessage || row.vendorMessage
            ? html`<span class="text-danger text-xs">${row.errorMessage || ''}
                ${row.vendorMessage ? `（厂商：${row.vendorMessage}）` : ''}</span>`
            : html`<span class="text-secondary">-</span>`,
      },
      {
        key: 'created_at',
        title: '时间',
        width: '150px',
        render: (row) => formatDate(row.createdAt || row.pushedAt),
      },
    ],
    pageSize: 10,
    emptyText: '还没有推送记录',
    fetcher: (params) =>
      api.get('/platform/ota/records', {
        params: {
          page: params.page,
          pageSize: params.pageSize,
          status: params.status,
          packageId: params.packageId,
          deviceId: params.deviceId,
        },
      }),
  });
  bindTableEvents(recordsHost, recordsTable);
  recordsSelect.addEventListener('change', () => {
    recordsTable.setFilters({ status: recordsSelect.value || null });
  });
  recordsTable.load();

  return page;
}

export default { renderOta };
