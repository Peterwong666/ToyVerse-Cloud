/* ============================================================
   平台端 · 租户管理（客户开通）
   ------------------------------------------------------------
   对应后端 `/platform/tenants`：
     GET    列表             POST   开通租户
     PUT    更新资料          DELETE 删除（有关联时 409 CASCADE_CONFLICT）
     POST   /{id}/status    启用 / 禁用

   职责边界（刻意划分）
   --------------------
   列表页只承担「浏览 + 开通 + 改资料 + 启停」；
   **账号与密码、授权与产品清单、删除** 都在详情页（tenant_detail.js）。
   原因：把一个租户的 6 个低频操作全塞进表格操作列，会既拥挤又容易误触，
   而「点进详情再操作」是用户对「管理后台」的既有心智。

   本页也是遗留缺陷 P-06（删除无级联校验）的验收入口：
   删除被引用的租户必须 409 并说明「还被什么挡着」。
   ============================================================ */

import api from '/shared/core/api.js';
import { PERM } from '/shared/core/auth.js';
import { esc, formatDay, html, raw } from '/shared/ui/dom.js';
import { statusTag } from '/shared/ui/components.js';
import { createListPage } from '/shared/app/page.js';
import { formModal } from '/shared/ui/form.js';
import toast from '/shared/ui/toast.js';
import { TENANT_STATUS_MAP, confirmThenRun, fetchRecords } from './common.js';

/** 表格列定义 */
const COLUMNS = [
  {
    key: 'name',
    title: '租户',
    sortable: true,
    render: (row) => html`<div class="cell-stack">
      <span class="cell-strong">${row.name}</span>
      <span class="cell-sub mono">${row.code}</span>
    </div>`,
  },
  {
    key: 'contact_name',
    title: '联系人',
    render: (row) =>
      row.contactName || row.contactPhone
        ? html`<div class="cell-stack">
            <span>${row.contactName || '-'}</span>
            <span class="cell-sub">${row.contactPhone || '-'}</span>
          </div>`
        : '-',
  },
  { key: 'industry', title: '行业', render: (row) => esc(row.industry || '-') },
  { key: 'status', title: '状态', render: (row) => statusTag(row.status, TENANT_STATUS_MAP) },
  {
    key: 'created_at',
    title: '创建日期',
    width: '110px',
    render: (row) => esc(formatDay(row.createdAt)),
  },
  {
    key: 'actions',
    title: '操作',
    align: 'right',
    width: '190px',
    nowrap: true,
    render: (row) => html`<div class="btn-group">
      ${raw(
        `<button type="button" class="btn btn-sm btn-ghost" data-action="open-tenant" data-id="${esc(row.id)}">详情</button>`,
      )}
      ${raw(
        `<button type="button" class="btn btn-sm" data-action="edit-tenant" data-id="${esc(row.id)}">编辑</button>`,
      )}
      ${raw(
        row.status === 'ACTIVE'
          ? `<button type="button" class="btn btn-sm" data-action="toggle-tenant" data-id="${esc(row.id)}" data-status="DISABLED">禁用</button>`
          : `<button type="button" class="btn btn-sm" data-action="toggle-tenant" data-id="${esc(row.id)}" data-status="ACTIVE">启用</button>`,
      )}
    </div>`,
  },
];

/** 租户表单字段（新建与编辑复用；编辑时编码只读） */
function tenantFields({ forEdit = false } = {}) {
  const fields = [
    {
      key: 'code',
      label: '租户编码',
      required: !forEdit,
      maxLength: 64,
      placeholder: '如 DEMO-BRAND（全局唯一，自动转大写）',
      disabled: forEdit,
      hint: forEdit ? '编码是稳定标识，创建后不可修改' : '',
    },
    { key: 'name', label: '租户名称', required: true, maxLength: 128 },
    { key: 'contact_name', label: '联系人', maxLength: 64 },
    { key: 'contact_phone', label: '联系电话', maxLength: 32, hint: '通常兼作商户端登录账号' },
    { key: 'email', label: '邮箱', maxLength: 128 },
    { key: 'industry', label: '所属行业', maxLength: 64 },
  ];

  if (forEdit) {
    fields.push({
      key: 'status',
      label: '状态',
      type: 'select',
      options: [
        { value: 'ACTIVE', label: '启用' },
        { value: 'DISABLED', label: '禁用' },
      ],
      hint: '禁用后该租户下所有账号无法登录；账号本身状态不变，解禁即恢复',
    });
  }

  fields.push({ key: 'remark', label: '备注', type: 'textarea', maxLength: 2000, span: 2 });
  return fields;
}

/**
 * 渲染租户管理页。
 *
 * @param {HTMLElement} container
 * @param {{router: object}} ctx
 * @returns {Promise<object>}
 */
export async function renderTenants(container, ctx) {
  const { router } = ctx;

  // 行业筛选的候选值来自现有数据（静默容错：失败就退化为「全部行业」）
  const industries = await fetchRecords('/platform/tenant-industries');
  const industryOptions = [
    { value: '', label: '全部行业' },
    ...industries.map((item) => ({ value: item.value, label: item.label })),
  ];

  return createListPage({
    container,
    title: '租户管理',
    desc: '客户开通入口：开通租户 → 在「产品模板」授权 → 创建客户产品',
    actions: [
      { label: '刷新', icon: 'refresh', action: 'reload-list' },
      {
        label: '开通租户',
        icon: 'plus',
        variant: 'primary',
        action: 'new-tenant',
        perm: PERM.platform.tenantWrite,
      },
    ],
    columns: COLUMNS,
    fetcher: (params) =>
      api.get('/platform/tenants', {
        params: {
          page: params.page,
          pageSize: params.pageSize,
          sortBy: params.sortBy,
          order: params.order,
          keyword: params.keyword,
          status: params.status,
          industry: params.industry,
        },
      }),
    filters: [
      { key: 'keyword', placeholder: '名称 / 编码 / 联系人 / 电话', width: '230px' },
      {
        key: 'status',
        type: 'select',
        options: [
          { value: '', label: '全部状态' },
          { value: 'ACTIVE', label: '启用' },
          { value: 'DISABLED', label: '已禁用' },
        ],
      },
      { key: 'industry', type: 'select', options: industryOptions },
    ],
    onAction: async (action, target, { table, reload }) => {
      const row = target.dataset.id ? table.findRowById(target.dataset.id) : null;

      if (action === 'reload-list') {
        table.load();
        return;
      }

      if (action === 'new-tenant') {
        const created = await formModal({
          title: '开通租户',
          subtitle: '编码全局唯一；开通后需到「产品模板」页为其授权，才能创建客户产品',
          submitText: '开通',
          fields: tenantFields(),
          onSubmit: (values) =>
            api.post('/platform/tenants', values, { idempotencyKey: api.newIdempotencyKey() }),
        });
        if (created) {
          toast.success(`租户「${created.name}」已开通`);
          reload();
        }
        return;
      }

      if (action === 'open-tenant') {
        if (row) router.goByName('tenantDetail', { id: row.id });
        return;
      }

      if (action === 'edit-tenant') {
        if (!row) return;
        const updated = await formModal({
          title: `编辑租户「${row.name}」`,
          submitText: '保存',
          fields: tenantFields({ forEdit: true }),
          values: {
            code: row.code,
            name: row.name,
            contact_name: row.contactName,
            contact_phone: row.contactPhone,
            email: row.email,
            industry: row.industry,
            status: row.status,
            remark: row.remark,
          },
          // 状态有独立端点（便于记录变更原因与审计），这里拆开提交
          onSubmit: async (values) => {
            const { status, code: _code, ...profile } = values;
            const result = await api.put(`/platform/tenants/${row.id}`, profile, {
              idempotencyKey: api.newIdempotencyKey(),
            });
            if (status && status !== row.status) {
              await api.post(
                `/platform/tenants/${row.id}/status`,
                { status, reason: '平台端编辑租户时调整' },
                { idempotencyKey: api.newIdempotencyKey() },
              );
            }
            return result;
          },
        });
        if (updated) {
          toast.success('租户资料已更新');
          reload();
        }
        return;
      }

      if (action === 'toggle-tenant') {
        if (!row) return;
        const nextStatus = target.dataset.status;
        const disabling = nextStatus === 'DISABLED';
        await confirmThenRun({
          title: disabling ? '禁用租户' : '启用租户',
          description: disabling
            ? `禁用后「${row.name}」下所有账号将无法登录。`
            : `启用后「${row.name}」下账号可恢复正常登录。`,
          detail: disabling
            ? '账号本身状态不会改变，解禁即恢复；这比删除更安全，推荐用它替代删除。'
            : '',
          tone: disabling ? 'danger' : 'warning',
          confirmText: disabling ? '确认禁用' : '确认启用',
          run: () =>
            api.post(
              `/platform/tenants/${row.id}/status`,
              { status: nextStatus, reason: '平台端手动切换' },
              { idempotencyKey: api.newIdempotencyKey() },
            ),
          successMessage: disabling ? '租户已禁用' : '租户已启用',
          onDone: reload,
        });
      }
    },
  });
}

export default { renderTenants };
