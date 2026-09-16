/* ============================================================
   平台端 · 产品模板
   ------------------------------------------------------------
   对应后端 `/platform/templates`：
     GET    列表          POST   新增
     PUT    更新          DELETE 删除（有授权/客户产品时 409）
     POST   /{id}/authorize          授权给租户（幂等）
     GET    /{id}/authorizations     已授权租户清单
     DELETE /authorizations/{id}     撤销授权（有产品时 409）

   这一页是「平台配置能力」的核心：平台在这里定义「可销售的智能玩具」
   （型号 / 芯片方案 / 联网方式 / 云服务商 / 固件版本 / AI 能力），
   再授权给租户。**授权是租户能否创建客户产品的唯一依据**
   （对应遗留缺陷 P-03 的修复：产品必须绑定租户且有授权）。
   ============================================================ */

import api from '/shared/core/api.js';
import { PERM } from '/shared/core/auth.js';
import { esc, formatDay, fromHtml, html, raw } from '/shared/ui/dom.js';
import { emptyState, statusTag, tag } from '/shared/ui/components.js';
import { table } from '/shared/ui/table.js';
import { createListPage } from '/shared/app/page.js';
import { formModal } from '/shared/ui/form.js';
import { modal } from '/shared/ui/modal.js';
import toast from '/shared/ui/toast.js';
import {
  ENABLE_OPTIONS,
  ENABLE_STATUS_MAP,
  NETWORK_MAP,
  NETWORK_OPTIONS,
  VENDOR_LABELS,
  confirmThenRun,
  fetchRecords,
  notifyError,
  toOptions,
} from './common.js';

/** 表格列 */
const COLUMNS = [
  {
    key: 'name',
    title: '产品模板',
    sortable: true,
    render: (row) => html`<div class="cell-stack">
      <span class="cell-strong">${row.name}</span>
      <span class="cell-sub mono">${row.code}</span>
    </div>`,
  },
  { key: 'category', title: '品类', render: (row) => esc(row.category || '-') },
  {
    key: 'network_type',
    title: '联网方式',
    render: (row) => statusTag(row.networkType, NETWORK_MAP),
  },
  {
    key: 'cloud_provider_name',
    title: '云服务商',
    render: (row) =>
      row.cloudProviderName
        ? esc(row.cloudProviderName)
        : tag('未关联', 'warning'),
  },
  {
    key: 'hardware',
    title: '型号 / 芯片',
    render: (row) =>
      row.model || row.chip
        ? html`<div class="cell-stack">
            <span>${row.model || '-'}</span>
            <span class="cell-sub">${row.chip || '-'}</span>
          </div>`
        : '-',
  },
  {
    key: 'coverage',
    title: '授权 / 产品',
    width: '110px',
    render: (row) => html`<span class="num">${row.authorizationCount}</span>
      <span class="text-secondary"> / </span>
      <span class="num">${row.clientProductCount}</span>`,
  },
  { key: 'status', title: '状态', render: (row) => statusTag(row.status, ENABLE_STATUS_MAP) },
  {
    key: 'actions',
    title: '操作',
    align: 'right',
    width: '250px',
    nowrap: true,
    render: (row) => html`<div class="btn-group">
      ${raw(`<button type="button" class="btn btn-sm btn-primary" data-action="authorize-template" data-id="${esc(row.id)}">授权</button>`)}
      ${raw(`<button type="button" class="btn btn-sm" data-action="view-authorizations" data-id="${esc(row.id)}">授权清单</button>`)}
      ${raw(`<button type="button" class="btn btn-sm btn-ghost" data-action="edit-template" data-id="${esc(row.id)}">编辑</button>`)}
      ${raw(`<button type="button" class="btn btn-sm btn-ghost" data-action="delete-template" data-id="${esc(row.id)}">删除</button>`)}
    </div>`,
  },
];

/**
 * 模板表单字段。
 *
 * `aiFeatures` / `specs` 用 textarea 收 JSON 字符串：
 * 后端 schema 对这两个字段接「对象或 JSON 字符串」两种入参，
 * 因此前端无需自己解析（解析失败会由后端返回 400 + 字段级错误）。
 */
function templateFields({ forEdit = false, cloudOptions = [] } = {}) {
  const fields = [
    {
      key: 'code',
      label: '模板编码',
      required: !forEdit,
      maxLength: 64,
      disabled: forEdit,
      placeholder: '如 TPL-ESP32S3-TOY（全局唯一，自动转大写）',
      hint: forEdit ? '编码创建后不可修改' : '',
    },
    { key: 'name', label: '模板名称', required: true, maxLength: 128 },
    { key: 'category', label: '产品品类', maxLength: 64, placeholder: '如 智能玩具 / 故事机' },
    { key: 'model', label: '型号', maxLength: 64, placeholder: '如 ESP32-S3' },
    { key: 'chip', label: '芯片 / 模组方案', maxLength: 64, placeholder: '如 乐鑫 ESP32-S3' },
    {
      key: 'network_type',
      label: '联网方式',
      type: 'select',
      options: NETWORK_OPTIONS,
      value: 'WIFI',
      hint: '决定激活路径与二维码格式（4G 走 JX，Wi-Fi 走 JD）',
    },
    {
      key: 'cloud_provider_id',
      label: '关联云服务商',
      type: 'select',
      options: [{ value: '', label: '暂不关联' }, ...cloudOptions],
      span: 1,
    },
    { key: 'firmware_version', label: '固件版本', maxLength: 64, placeholder: '如 1.0.0' },
    {
      key: 'reference_price',
      label: '参考单价（元）',
      type: 'number',
      min: 0,
      max: 9999999,
      hint: '仅平台端可见；工厂端响应不含金额（P6）',
    },
    { key: 'status', label: '状态', type: 'select', options: ENABLE_OPTIONS, value: 'ENABLED' },
    {
      key: 'ai_features',
      label: 'AI 能力开关（JSON）',
      type: 'textarea',
      rows: 3,
      span: 2,
      placeholder: '{"chat": true, "story": true, "music": true, "vision": false}',
    },
    {
      key: 'specs',
      label: '规格参数（JSON）',
      type: 'textarea',
      rows: 3,
      span: 2,
      placeholder: '{"尺寸": "80×80×90mm", "材质": "ABS", "电池": "1200mAh"}',
    },
    { key: 'description', label: '描述', type: 'textarea', maxLength: 2000, span: 2 },
  ];
  return fields;
}

/** 把 JSON 对象回填到 textarea（对象 → 格式化字符串） */
function toJsonText(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return '';
  }
}

/** 授权弹窗：勾选租户 + 设备上限 + 到期时间 */
async function openAuthorize(template, { reload }) {
  const tenants = await fetchRecords('/platform/tenants', { status: 'ACTIVE' });
  if (!tenants.length) {
    toast.warning('当前没有启用中的租户，请先到「租户管理」开通');
    return;
  }

  const result = await formModal({
    title: `授权模板「${template.name}」`,
    subtitle: '勾选需要授权的租户；已授权的租户会被自动跳过（幂等）',
    size: 'md',
    submitText: '确认授权',
    fields: [
      {
        key: 'tenant_ids',
        label: '目标租户',
        type: 'checkbox',
        required: true,
        span: 2,
        options: toOptions(tenants, (item) => `${item.name}（${item.code}）`),
      },
      {
        key: 'max_devices',
        label: '可创建设备上限',
        type: 'number',
        min: 0,
        placeholder: '留空表示不限',
        hint: '仅作为授权额度登记，实际设备数在 P4 订单阶段校验',
      },
      { key: 'expires_at', label: '授权到期日（可空）', type: 'date' },
      { key: 'remark', label: '备注', type: 'textarea', maxLength: 2000, span: 2 },
    ],
    onSubmit: (values) =>
      api.post(
        `/platform/templates/${template.id}/authorize`,
        {
          tenant_ids: values.tenant_ids,
          max_devices: values.max_devices,
          expires_at: values.expires_at ? `${values.expires_at}T23:59:59` : null,
          remark: values.remark,
        },
        { idempotencyKey: api.newIdempotencyKey() },
      ),
  });

  if (!result) return;

  const parts = [];
  if (result.created?.length) parts.push(`新增 ${result.created.length} 个`);
  if (result.skipped?.length) parts.push(`已授权跳过 ${result.skipped.length} 个`);
  if (result.denied?.length) parts.push(`租户不存在 ${result.denied.length} 个`);
  toast.success(
    parts.length ? `授权完成：${parts.join('，')}；当前共 ${result.totalAuthorized} 个租户已授权` : '授权完成',
  );
  reload();
}

/** 授权清单弹窗（含撤销） */
async function openAuthorizationList(template) {
  let result;
  try {
    result = await api.get(`/platform/templates/${template.id}/authorizations`, {
      params: { pageSize: 200 },
    });
  } catch (error) {
    notifyError(error);
    return;
  }

  const rows = result?.records || [];
  const body = document.createElement('div');

  const renderList = (list) => {
    body.replaceChildren();
    if (!list.length) {
      body.append(
        fromHtml(
          emptyState({
            icon: 'shield',
            title: '该模板尚未授权给任何租户',
            desc: '点击「授权」按钮选择目标租户。未授权的租户无法创建对应的客户产品。',
          }),
        ),
      );
      return;
    }
    const host = document.createElement('div');
    host.innerHTML = table({
      columns: [
        {
          key: 'tenant_name',
          title: '租户',
          render: (row) => html`<div class="cell-stack">
            <span class="cell-strong">${row.tenantName || '-'}</span>
            <span class="cell-sub mono">${row.tenantCode || '-'}</span>
          </div>`,
        },
        { key: 'status', title: '状态', render: (row) => statusTag(row.status, ENABLE_STATUS_MAP) },
        {
          key: 'max_devices',
          title: '设备上限',
          render: (row) =>
            row.maxDevices === null || row.maxDevices === undefined ? '不限' : String(row.maxDevices),
        },
        { key: 'client_product_count', title: '已建产品', render: (row) => String(row.clientProductCount ?? 0) },
        {
          key: 'authorized_at',
          title: '授权时间',
          render: (row) => esc(row.authorizedAt ? formatDay(row.authorizedAt) : '-'),
        },
        {
          key: 'actions',
          title: '操作',
          align: 'right',
          nowrap: true,
          render: (row) =>
            html`<button type="button" class="btn btn-sm" data-action="revoke" data-id="${esc(row.id)}">撤销</button>`,
        },
      ],
      rows: list,
      emptyText: '暂无授权',
    });
    body.append(host);
  };

  renderList(rows);

  body.addEventListener('click', async (event) => {
    const btn = event.target.closest('[data-action="revoke"]');
    if (!btn) return;
    const row = rows.find((item) => String(item.id) === String(btn.dataset.id));
    if (!row) return;

    const { confirmDialog } = await import('/shared/ui/modal.js');
    const { confirmed } = await confirmDialog({
      title: '撤销授权',
      description: `撤销「${row.tenantName || row.tenantId}」对该模板的授权。`,
      detail:
        (row.clientProductCount ?? 0) > 0
          ? `该授权下已有 ${row.clientProductCount} 个客户产品，按设计会被拒绝（CASCADE_CONFLICT）。`
          : '撤销后该租户无法再基于此模板创建客户产品。',
      tone: 'danger',
      confirmText: '确认撤销',
    });
    if (!confirmed) return;

    try {
      await api.del(`/platform/authorizations/${row.id}`, undefined, {
        idempotencyKey: api.newIdempotencyKey(),
      });
      toast.success('授权已撤销');
      const fresh = await api.get(`/platform/templates/${template.id}/authorizations`, {
        params: { pageSize: 200 },
      });
      renderList(fresh?.records || []);
    } catch (error) {
      notifyError(error);
    }
  });

  modal({
    title: `「${template.name}」的授权租户`,
    subtitle: '列表同时显示每个租户已创建的客户产品数——有产品时无法撤销授权',
    size: 'lg',
    body,
  });
}

/**
 * 渲染产品模板页。
 *
 * @param {HTMLElement} container
 * @param {{router: object}} ctx
 */
export async function renderTemplates(container, ctx) {
  const clouds = await fetchRecords('/platform/clouds');
  const cloudOptions = toOptions(clouds, (item) => `${item.name}（${VENDOR_LABELS[item.vendor] || item.vendor}）`);

  return createListPage({
    container,
    title: '产品模板',
    desc: '平台级产品定义：型号 / 芯片方案 / 联网方式 / 云服务商 / AI 能力；授权给租户后才能创建客户产品',
    actions: [
      { label: '刷新', icon: 'refresh', action: 'reload-list' },
      {
        label: '新增模板',
        icon: 'plus',
        variant: 'primary',
        action: 'new-template',
        perm: PERM.platform.templateWrite,
      },
    ],
    columns: COLUMNS,
    fetcher: (params) =>
      api.get('/platform/templates', {
        params: {
          page: params.page,
          pageSize: params.pageSize,
          sortBy: params.sortBy,
          order: params.order,
          keyword: params.keyword,
          category: params.category,
          networkType: params.networkType,
          status: params.status,
        },
      }),
    filters: [
      { key: 'keyword', placeholder: '名称 / 编码 / 型号', width: '200px' },
      { key: 'category', placeholder: '品类', width: '140px' },
      {
        key: 'networkType',
        type: 'select',
        options: [{ value: '', label: '全部联网方式' }, ...NETWORK_OPTIONS],
      },
      {
        key: 'status',
        type: 'select',
        options: [
          { value: '', label: '全部状态' },
          { value: 'ENABLED', label: '启用' },
          { value: 'DISABLED', label: '停用' },
        ],
      },
    ],
    onAction: async (action, target, { table, reload }) => {
      const row = target.dataset.id ? table.findRowById(target.dataset.id) : null;

      if (action === 'reload-list') {
        table.load();
        return;
      }

      if (action === 'new-template') {
        const created = await formModal({
          title: '新增产品模板',
          subtitle: '模板是「可销售的智能玩具」定义，创建后需授权给租户',
          size: 'lg',
          submitText: '创建',
          fields: templateFields({ cloudOptions }),
          onSubmit: (values) =>
            api.post('/platform/templates', values, { idempotencyKey: api.newIdempotencyKey() }),
        });
        if (created) {
          toast.success(`产品模板「${created.name}」已创建`);
          reload();
        }
        return;
      }

      if (action === 'edit-template') {
        if (!row) return;
        const updated = await formModal({
          title: `编辑模板「${row.name}」`,
          subtitle: '模板变更不会追溯修改已创建的客户产品（产品持有快照）',
          size: 'lg',
          submitText: '保存',
          fields: templateFields({ forEdit: true, cloudOptions }),
          values: {
            code: row.code,
            name: row.name,
            category: row.category,
            model: row.model,
            chip: row.chip,
            network_type: row.networkType,
            cloud_provider_id: row.cloudProviderId || '',
            firmware_version: row.firmwareVersion,
            reference_price: row.referencePrice,
            status: row.status,
            ai_features: toJsonText(row.aiFeatures),
            specs: toJsonText(row.specs),
            description: row.description,
          },
          onSubmit: (values) => {
            const { code: _code, ...patch } = values;
            return api.put(`/platform/templates/${row.id}`, patch, {
              idempotencyKey: api.newIdempotencyKey(),
            });
          },
        });
        if (updated) {
          toast.success('模板已更新');
          reload();
        }
        return;
      }

      if (action === 'authorize-template') {
        if (row) await openAuthorize(row, { reload });
        return;
      }

      if (action === 'view-authorizations') {
        if (row) await openAuthorizationList(row);
        return;
      }

      if (action === 'delete-template') {
        if (!row) return;
        await confirmThenRun({
          title: `删除模板「${row.name}」`,
          description: '删除前会校验是否存在授权或客户产品。',
          detail:
            row.authorizationCount || row.clientProductCount
              ? `该模板当前有 ${row.authorizationCount} 条授权、${row.clientProductCount} 个客户产品——按设计会被拒绝（CASCADE_CONFLICT）。请先撤销授权并处理产品，或改为「停用」。`
              : '该模板暂无关联数据，可以删除。',
          tone: 'danger',
          confirmText: '确认删除',
          run: () =>
            api.del(`/platform/templates/${row.id}`, undefined, {
              idempotencyKey: api.newIdempotencyKey(),
            }),
          successMessage: '模板已删除',
          onDone: reload,
        });
      }
    },
  });
}

export default { renderTemplates };
