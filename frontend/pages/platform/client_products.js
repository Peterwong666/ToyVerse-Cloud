/* ============================================================
   平台端 · 客户产品
   ------------------------------------------------------------
   对应后端 `/platform/client-products`：
     GET / POST / PUT / DELETE   +   /{id}/miniapp-config

   这一页是遗留缺陷 **P-03 的核心修复点**：
   创建时必须同时指定「租户」与「产品模板」，且服务端会校验该租户
   已获得该模板的授权（否则 409 `PRODUCT_NOT_AUTHORIZED`）。
   因此不可能再出现「产品建好了却没绑定客户」——
   创建成功后到「租户详情 → 客户产品」一定能看到它。

   另一个容易困惑的点（页面上直接说明）：
   联网方式 / 云服务商 / 固件版本在创建时**从模板快照**到产品上，
   之后改模板不会追溯影响已交付的产品。
   ============================================================ */

import api from '/shared/core/api.js';
import { PERM } from '/shared/core/auth.js';
import { esc, formatDay, html, raw } from '/shared/ui/dom.js';
import { statusTag, tag } from '/shared/ui/components.js';
import { createListPage } from '/shared/app/page.js';
import { formModal } from '/shared/ui/form.js';
import toast from '/shared/ui/toast.js';
import {
  ENABLE_OPTIONS,
  ENABLE_STATUS_MAP,
  NETWORK_MAP,
  confirmThenRun,
  fetchRecords,
  toOptions,
} from './common.js';

/** 表格列 */
const COLUMNS = [
  {
    key: 'name',
    title: '客户产品',
    sortable: true,
    render: (row) => html`<div class="cell-stack">
      <span class="cell-strong">${row.name}</span>
      <span class="cell-sub mono">${row.code}</span>
    </div>`,
  },
  {
    key: 'tenant_name',
    title: '归属租户',
    render: (row) => html`<div class="cell-stack">
      <span>${row.tenantName || '-'}</span>
      <span class="cell-sub mono">${row.tenantCode || row.tenantId}</span>
    </div>`,
  },
  {
    key: 'template_name',
    title: '来源模板',
    render: (row) => html`<div class="cell-stack">
      <span>${row.templateName || '-'}</span>
      <span class="cell-sub mono">${row.templateCode || row.templateId}</span>
    </div>`,
  },
  {
    key: 'network_type',
    title: '联网方式',
    render: (row) => statusTag(row.networkType, NETWORK_MAP),
  },
  {
    key: 'cloud_provider_name',
    title: '云服务商',
    render: (row) => (row.cloudProviderName ? esc(row.cloudProviderName) : tag('未关联', 'warning')),
  },
  { key: 'status', title: '状态', render: (row) => statusTag(row.status, ENABLE_STATUS_MAP) },
  {
    key: 'miniapp',
    title: '小程序',
    render: (row) =>
      row.hasMiniappConfig ? tag('已配置', 'success') : tag('未配置', 'default'),
  },
  {
    key: 'created_at',
    title: '创建日期',
    width: '105px',
    render: (row) => esc(formatDay(row.createdAt)),
  },
  {
    key: 'actions',
    title: '操作',
    align: 'right',
    width: '190px',
    nowrap: true,
    render: (row) => html`<div class="btn-group">
      ${raw(`<button type="button" class="btn btn-sm btn-primary" data-action="open-product" data-id="${esc(row.id)}">详情 / 小程序</button>`)}
      ${raw(`<button type="button" class="btn btn-sm btn-ghost" data-action="delete-product" data-id="${esc(row.id)}">删除</button>`)}
    </div>`,
  },
];

/**
 * 创建 / 编辑客户产品。
 *
 * @param {object} o
 * @param {object[]} o.tenantOptions
 * @param {object[]} o.templateOptions
 * @param {object|null} [o.row] 传入则为编辑
 */
async function openProductForm({ tenantOptions, templateOptions, row = null }) {
  const editing = Boolean(row);

  const fields = editing
    ? [
        // 编辑时不允许改归属与模板：它们是「产品归属」与「授权依据」
        { key: 'tenant', label: '归属租户', type: 'static', value: row.tenantName || row.tenantId },
        { key: 'template', label: '来源模板', type: 'static', value: row.templateName || row.templateId },
        { key: 'code', label: '产品编码', type: 'static', value: row.code },
        { key: 'name', label: '产品名称', required: true, maxLength: 128 },
        { key: 'firmware_version', label: '固件版本', maxLength: 64 },
        { key: 'status', label: '状态', type: 'select', options: ENABLE_OPTIONS },
        {
          key: 'ai_enabled',
          label: 'AI 已配置',
          type: 'switch',
          switchLabel: '开启表示该产品已完成 AI 配置（P9 落地具体配置项）',
          span: 2,
        },
        { key: 'remark', label: '备注', type: 'textarea', maxLength: 2000, span: 2 },
      ]
    : [
        {
          key: 'tenant_id',
          label: '归属租户',
          type: 'select',
          required: true,
          options: [{ value: '', label: '请选择租户' }, ...tenantOptions],
          hint: '产品必须绑定租户（P-03 的架构性修复）',
        },
        {
          key: 'template_id',
          label: '来源模板',
          type: 'select',
          required: true,
          options: [{ value: '', label: '请选择模板' }, ...templateOptions],
          hint: '该租户必须已获得此模板的授权，否则会被拒绝',
        },
        {
          key: 'code',
          label: '产品编码',
          required: true,
          maxLength: 64,
          placeholder: '如 CP-T001-CUBE（全局唯一，自动转大写）',
        },
        {
          key: 'name',
          label: '产品名称',
          maxLength: 128,
          placeholder: '留空则沿用模板名称',
        },
        { key: 'firmware_version', label: '固件版本', maxLength: 64, placeholder: '留空则沿用模板版本' },
        { key: 'status', label: '状态', type: 'select', options: ENABLE_OPTIONS, value: 'ENABLED' },
        { key: 'remark', label: '备注', type: 'textarea', maxLength: 2000, span: 2 },
      ];

  return formModal({
    title: editing ? `编辑客户产品「${row.name}」` : '创建客户产品',
    subtitle: editing
      ? '归属租户与来源模板不可修改；联网方式、云服务商为创建时的快照，也不会随模板变化'
      : '联网方式 / 云服务商 / 固件版本会从所选模板快照到该产品上',
    size: 'lg',
    submitText: editing ? '保存' : '创建',
    fields,
    values: editing
      ? {
          name: row.name,
          firmware_version: row.firmwareVersion,
          status: row.status,
          ai_enabled: row.aiEnabled,
          remark: row.remark,
        }
      : {},
    onSubmit: (values) => {
      if (editing) {
        const { name, firmware_version, status, ai_enabled, remark } = values;
        return api.put(
          `/platform/client-products/${row.id}`,
          { name, firmware_version, status, ai_enabled, remark },
          { idempotencyKey: api.newIdempotencyKey() },
        );
      }
      return api.post('/platform/client-products', values, {
        idempotencyKey: api.newIdempotencyKey(),
      });
    },
  });
}

/**
 * 渲染客户产品页。
 *
 * @param {HTMLElement} container
 * @param {{router: object}} ctx
 */
export async function renderClientProducts(container, ctx) {
  const { router } = ctx;

  // 下拉数据：启用中的租户 + 启用中的模板（模板停用时后端也会拒绝）
  const [tenants, templates] = await Promise.all([
    fetchRecords('/platform/tenants', { status: 'ACTIVE' }),
    fetchRecords('/platform/templates', { status: 'ENABLED' }),
  ]);
  const tenantOptions = toOptions(tenants, (item) => `${item.name}（${item.code}）`);
  const templateOptions = toOptions(templates, (item) => `${item.name}（${item.code}）`);

  return createListPage({
    container,
    title: '客户产品',
    desc:
      '租户名下的具体产品，由「产品模板」派生。创建时必须选择租户与模板，' +
      '且该租户需已获得模板授权 —— 这是 P-03（产品未绑定客户）的修复点。',
    actions: [
      { label: '刷新', icon: 'refresh', action: 'reload-list' },
      {
        label: '创建客户产品',
        icon: 'plus',
        variant: 'primary',
        action: 'new-product',
        perm: PERM.platform.productWrite,
      },
    ],
    columns: COLUMNS,
    fetcher: (params) =>
      api.get('/platform/client-products', {
        params: {
          page: params.page,
          pageSize: params.pageSize,
          sortBy: params.sortBy,
          order: params.order,
          keyword: params.keyword,
          tenantId: params.tenantId,
          templateId: params.templateId,
          status: params.status,
        },
      }),
    filters: [
      { key: 'keyword', placeholder: '名称 / 编码', width: '180px' },
      { key: 'tenantId', type: 'select', options: [{ value: '', label: '全部租户' }, ...tenantOptions] },
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

      if (action === 'new-product') {
        if (!tenantOptions.length) {
          toast.warning('没有启用中的租户，请先到「租户管理」开通租户');
          return;
        }
        if (!templateOptions.length) {
          toast.warning('没有启用中的产品模板，请先到「产品模板」页新增');
          return;
        }
        const created = await openProductForm({ tenantOptions, templateOptions });
        if (created) {
          toast.success('客户产品已创建；可在「租户详情 → 客户产品」中确认归属');
          reload();
        }
        return;
      }

      if (action === 'open-product') {
        if (row) router.goByName('clientProductDetail', { id: row.id });
        return;
      }

      if (action === 'delete-product') {
        if (!row) return;
        await confirmThenRun({
          title: `删除客户产品「${row.name}」`,
          description: '产品的小程序配置会一并删除（1:1 从属关系）。',
          detail:
            'P4 设备表落地后，此处会追加「产品下已有设备则拒绝删除」的校验；当前阶段尚无设备数据。',
          tone: 'danger',
          confirmText: '确认删除',
          run: () =>
            api.del(`/platform/client-products/${row.id}`, undefined, {
              idempotencyKey: api.newIdempotencyKey(),
            }),
          successMessage: '客户产品已删除',
          onDone: reload,
        });
      }
    },
  });
}

export default { renderClientProducts };
