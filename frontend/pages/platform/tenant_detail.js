/* ============================================================
   平台端 · 租户详情
   ------------------------------------------------------------
   路由：`#/tenants/:id`（**参数化路由**，刷新与分享都不丢状态 ——
   P-01 的修复在这里被真正使用）

   一页承载四块内容（用标签页切换）：
     概览        基本资料 + 关联计数 + 危险操作（启用/禁用/删除）
     授权        该租户已获得的「产品模板」授权（可撤销）
     客户产品    该租户名下的客户产品（★ P-03 的验收视角：
                 「建后客户详情可见」）
     账号        登录账号（开通账号 / 重置密码，一次性密码展示）

   为什么账号与密码放在详情页而不是列表页
   --------------------------------------
   「重置密码」是不可逆地踢掉客户在线的登录态的操作，属于高风险低频动作，
   放在需要「先点进某个租户」的位置，可以显著降低误操作概率。
   ============================================================ */

import api from '/shared/core/api.js';
import { PERM } from '/shared/core/auth.js';
import {
  esc,
  formatDay,
  formatRelative,
  fromHtml,
  h,
  html,
} from '/shared/ui/dom.js';
import {
  descList,
  emptyState,
  loadingState,
  statGrid,
  statusTag,
} from '/shared/ui/components.js';
import { table } from '/shared/ui/table.js';
import { createDetailPage } from '/shared/app/page.js';
import { formModal } from '/shared/ui/form.js';
import { confirmDialog } from '/shared/ui/modal.js';
import toast from '/shared/ui/toast.js';
import {
  ENABLE_STATUS_MAP,
  NETWORK_MAP,
  TENANT_STATUS_MAP,
  confirmThenRun,
  notifyError,
  showOneTimePassword,
} from './common.js';

/** 取租户详情 */
async function loadTenant(id) {
  return api.get(`/platform/tenants/${id}`);
}

/* ------------------------------------------------------------
   概览
   ------------------------------------------------------------ */

function renderOverview(host, data, ctx) {
  const { router, reload } = ctx;

  host.append(
    fromHtml(
      statGrid([
        {
          label: '产品授权',
          value: data.counts?.authorizations ?? 0,
          unit: '条',
          icon: 'shield',
          tone: 'brand',
          foot: '可用的产品模板授权',
        },
        {
          label: '客户产品',
          value: data.counts?.clientProducts ?? 0,
          unit: '个',
          icon: 'package',
          tone: 'teal',
          foot: '该租户名下的产品',
        },
        {
          label: '登录账号',
          value: data.counts?.users ?? 0,
          unit: '个',
          icon: 'users',
          tone: 'accent',
          foot: '商户端登录账号',
        },
        {
          label: '租户状态',
          value: data.status === 'ACTIVE' ? '正常' : '已禁用',
          icon: data.status === 'ACTIVE' ? 'checkCircle' : 'xCircle',
          tone: data.status === 'ACTIVE' ? 'brand' : 'coral',
          foot: data.status === 'ACTIVE' ? '账号可登录' : '所有账号无法登录',
        },
      ]),
    ),
  );

  const detail = h('div', { class: 'card mt-4' });
  detail.append(
    fromHtml(
      `<div class="card-head"><div class="card-title">基本资料</div></div>`,
    ),
  );
  detail.append(
    fromHtml(
      descList([
        ['租户名称', data.name],
        ['租户编码', data.code],
        ['状态', data.status === 'ACTIVE' ? '启用' : '禁用'],
        ['时区', data.timezone],
        ['联系人', data.contactName],
        ['联系电话', data.contactPhone],
        ['邮箱', data.email],
        ['所属行业', data.industry],
        ['创建时间', formatDay(data.createdAt)],
        ['最后更新', formatRelative(data.updatedAt)],
        ['备注', data.remark],
      ]),
    ),
  );
  host.append(detail);

  /* ---- 危险操作区 ---- */
  const danger = h('div', { class: 'card mt-4' });
  danger.append(fromHtml(`<div class="card-head"><div class="card-title">状态与删除</div></div>`));
  const body = h('div', { class: 'p-4' });
  const desc = h('div', {
    class: 'text-sm text-secondary mb-3',
    text:
      data.status === 'ACTIVE'
        ? '禁用可立即阻断该租户下所有账号登录，且不会删除任何数据；删除会校验关联数据，存在关联时会被拒绝。'
        : '该租户当前处于禁用状态，其下所有账号无法登录。',
  });
  body.append(desc);
  const btns = h('div', { class: 'btn-group' });
  const toggle = h('button', {
    class: `btn ${data.status === 'ACTIVE' ? 'btn-danger' : 'btn-primary'}`,
    type: 'button',
    text: data.status === 'ACTIVE' ? '禁用该租户' : '启用该租户',
    'data-action': 'toggle-status',
  });
  const remove = h('button', {
    class: 'btn',
    type: 'button',
    text: '删除该租户',
    'data-action': 'delete-tenant',
  });
  btns.append(toggle, remove);
  body.append(btns);
  danger.append(body);
  host.append(danger);

  /* ---- 事件 ---- */
  toggle.addEventListener('click', async () => {
    const disabling = data.status === 'ACTIVE';
    await confirmThenRun({
      title: disabling ? '禁用租户' : '启用租户',
      description: `将把「${data.name}」切换为${disabling ? '禁用' : '启用'}状态。`,
      detail: disabling ? '禁用后该租户下所有账号无法登录（账号本身不受影响）。' : '',
      tone: disabling ? 'danger' : 'warning',
      confirmText: disabling ? '确认禁用' : '确认启用',
      run: () =>
        api.post(
          `/platform/tenants/${data.id}/status`,
          { status: disabling ? 'DISABLED' : 'ACTIVE', reason: '详情页手动切换' },
          { idempotencyKey: api.newIdempotencyKey() },
        ),
      successMessage: disabling ? '租户已禁用' : '租户已启用',
      onDone: reload,
    });
  });

  remove.addEventListener('click', async () => {
    const { confirmed } = await confirmDialog({
      title: `删除租户「${data.name}」`,
      description: '删除是不可逆操作，且会校验关联数据。',
      detail:
        (data.counts?.users || 0) + (data.counts?.authorizations || 0) + (data.counts?.clientProducts || 0) > 0
          ? `该租户当前有 ${data.counts?.users ?? 0} 个账号、${data.counts?.authorizations ?? 0} 条授权、${data.counts?.clientProducts ?? 0} 个客户产品——按设计会被拒绝（CASCADE_CONFLICT）。若目的是停服，请改用「禁用」。`
          : '该租户暂无关联数据，可以删除。',
      tone: 'danger',
      confirmText: '确认删除',
    });
    if (!confirmed) return;

    try {
      await api.del(`/platform/tenants/${data.id}`, undefined, {
        idempotencyKey: api.newIdempotencyKey(),
      });
      toast.success('租户已删除');
      router.go('/tenants');
    } catch (error) {
      // CASCADE_CONFLICT 不是「出错了」，而是设计生效：把挡路的数据说清楚
      notifyError(error);
    }
  });
}

/* ------------------------------------------------------------
   授权
   ------------------------------------------------------------ */

function renderAuthorizations(host, data, ctx) {
  const { reload } = ctx;
  const rows = data.authorizations || [];

  if (!rows.length) {
    host.append(
      fromHtml(
        emptyState({
          icon: 'shield',
          title: '尚未获得任何产品模板授权',
          desc: '请到「产品模板」页选择模板并授权给该租户，之后才能为其创建客户产品。',
        }),
      ),
    );
    return;
  }

  const host2 = h('div');
  host2.innerHTML = table({
    columns: [
      {
        key: 'template_name',
        title: '产品模板',
        render: (row) => html`<div class="cell-stack">
          <span class="cell-strong">${row.templateName || '-'}</span>
          <span class="cell-sub mono">${row.templateCode || '-'}</span>
        </div>`,
      },
      { key: 'status', title: '授权状态', render: (row) => statusTag(row.status, ENABLE_STATUS_MAP) },
      {
        key: 'max_devices',
        title: '设备上限',
        render: (row) => (row.maxDevices === null || row.maxDevices === undefined ? '不限' : String(row.maxDevices)),
      },
      {
        key: 'client_product_count',
        title: '已建产品',
        render: (row) => String(row.clientProductCount ?? 0),
      },
      {
        key: 'authorized_at',
        title: '授权时间',
        render: (row) => esc(row.authorizedAt ? formatDay(row.authorizedAt) : '-'),
      },
      { key: 'authorized_by', title: '授权人', render: (row) => esc(row.authorizedBy || '-') },
      {
        key: 'actions',
        title: '操作',
        align: 'right',
        nowrap: true,
        render: (row) => html`<button type="button" class="btn btn-sm" data-action="revoke-auth" data-id="${esc(row.id)}">撤销授权</button>`,
      },
    ],
    rows,
    emptyText: '暂无授权',
  });

  host.append(host2);

  host.addEventListener('click', async (event) => {
    const btn = event.target.closest('[data-action="revoke-auth"]');
    if (!btn) return;
    const row = rows.find((item) => String(item.id) === String(btn.dataset.id));
    if (!row) return;

    await confirmThenRun({
      title: '撤销模板授权',
      description: `撤销该租户对「${row.templateName || row.templateId}」的授权。`,
      detail:
        (row.clientProductCount ?? 0) > 0
          ? `该授权下已有 ${row.clientProductCount} 个客户产品，按设计会被拒绝（CASCADE_CONFLICT）。请先停用/删除这些产品。`
          : '撤销后该租户将无法再基于此模板创建客户产品。',
      tone: 'danger',
      confirmText: '确认撤销',
      run: () => api.del(`/platform/authorizations/${row.id}`, undefined, {
        idempotencyKey: api.newIdempotencyKey(),
      }),
      successMessage: '授权已撤销',
      onDone: reload,
    });
  });
}

/* ------------------------------------------------------------
   客户产品
   ------------------------------------------------------------ */

function renderClientProducts(host, data, ctx) {
  const { router } = ctx;
  const rows = data.clientProducts || [];

  if (!rows.length) {
    host.append(
      fromHtml(
        emptyState({
          icon: 'package',
          title: '该租户还没有客户产品',
          desc: '在「客户产品」页创建时选择该租户即可；创建前需要先完成产品模板授权。',
        }),
      ),
    );
    return;
  }

  const host2 = h('div');
  host2.innerHTML = table({
    columns: [
      {
        key: 'name',
        title: '客户产品',
        render: (row) => html`<div class="cell-stack">
          <span class="cell-strong">${row.name}</span>
          <span class="cell-sub mono">${row.code}</span>
        </div>`,
      },
      {
        key: 'network_type',
        title: '联网方式',
        render: (row) => statusTag(row.networkType, NETWORK_MAP),
      },
      { key: 'status', title: '状态', render: (row) => statusTag(row.status, ENABLE_STATUS_MAP) },
      {
        key: 'ai_enabled',
        title: 'AI 已配置',
        render: (row) => (row.aiEnabled ? '是' : '否'),
      },
      { key: 'created_at', title: '创建日期', render: (row) => esc(formatDay(row.createdAt)) },
      {
        key: 'actions',
        title: '操作',
        align: 'right',
        nowrap: true,
        render: (row) => html`<button type="button" class="btn btn-sm" data-action="open-product" data-id="${esc(row.id)}">查看产品</button>`,
      },
    ],
    rows,
    emptyText: '暂无客户产品',
  });
  host.append(host2);

  host.addEventListener('click', (event) => {
    const btn = event.target.closest('[data-action="open-product"]');
    if (!btn) return;
    router.goByName('clientProductDetail', { id: btn.dataset.id });
  });
}

/* ------------------------------------------------------------
   账号
   ------------------------------------------------------------ */

function renderAccounts(host, data, ctx) {
  const { reload } = ctx;
  const rows = data.accounts || [];

  const head = h('div', { class: 'flex-between mb-3' });
  head.append(
    h('div', {
      class: 'text-sm text-secondary',
      text: '密码由服务端随机生成，只显示一次，并强制客户首次登录时修改。',
    }),
  );
  const addBtn = h('button', {
    class: 'btn btn-primary btn-sm',
    type: 'button',
    text: '开通登录账号',
    'data-action': 'add-account',
  });
  head.append(addBtn);
  host.append(head);

  if (!rows.length) {
    host.append(
      fromHtml(
        emptyState({
          icon: 'users',
          title: '该租户还没有登录账号',
          desc: '点击上方「开通登录账号」，系统会生成一次性初始密码。',
        }),
      ),
    );
  } else {
    const host2 = h('div');
    host2.innerHTML = table({
      columns: [
        { key: 'account', title: '账号' },
        { key: 'nickname', title: '昵称', render: (row) => esc(row.nickname || '-') },
        { key: 'role_code', title: '角色', render: (row) => esc(row.roleCode) },
        { key: 'status', title: '状态', render: (row) => statusTag(row.status, TENANT_STATUS_MAP) },
        {
          key: 'must_change_password',
          title: '待改密',
          render: (row) => (row.mustChangePassword ? '是' : '否'),
        },
        {
          key: 'last_login_at',
          title: '最近登录',
          render: (row) => esc(row.lastLoginAt ? formatRelative(row.lastLoginAt) : '从未登录'),
        },
        {
          key: 'actions',
          title: '操作',
          align: 'right',
          nowrap: true,
          render: (row) =>
            html`<button type="button" class="btn btn-sm" data-action="reset-password" data-account="${esc(row.account)}">重置密码</button>`,
        },
      ],
      rows,
      emptyText: '暂无账号',
    });
    host.append(host2);
  }

  /* ---- 开通账号 ---- */
  addBtn.addEventListener('click', async () => {
    const created = await formModal({
      title: '为租户开通登录账号',
      subtitle: '密码留空则由服务端生成强随机密码，仅在提交后显示一次',
      size: 'md',
      submitText: '开通',
      fields: [
        {
          key: 'account',
          label: '登录账号',
          required: true,
          maxLength: 64,
          placeholder: '建议填写手机号',
        },
        { key: 'nickname', label: '昵称', maxLength: 128 },
        {
          key: 'role_code',
          label: '角色',
          type: 'select',
          options: [
            { value: 'MERCHANT_ADMIN', label: '商户管理员' },
            { value: 'MERCHANT_OPERATOR', label: '商户运营' },
          ],
          value: 'MERCHANT_ADMIN',
        },
        { key: 'password', label: '初始密码', maxLength: 128, placeholder: '留空自动生成' },
      ],
      onSubmit: (values) =>
        api.post(`/platform/tenants/${data.id}/accounts`, values, {
          idempotencyKey: api.newIdempotencyKey(),
        }),
    });

    if (!created) return;
    reload();
    if (created.generated && created.password) {
      await showOneTimePassword({
        account: created.account?.account || '',
        password: created.password,
      });
    } else {
      toast.success('账号已创建（首次登录会强制改密）');
    }
  });

  /* ---- 重置密码 ---- */
  host.addEventListener('click', async (event) => {
    const btn = event.target.closest('[data-action="reset-password"]');
    if (!btn) return;
    const account = btn.dataset.account;

    const result = await formModal({
      title: `重置账号 ${account} 的密码`,
      subtitle: '重置后该账号的全部登录态会被吊销，并强制下次登录修改密码',
      size: 'md',
      submitText: '重置',
      fields: [
        { key: 'account', label: '账号', value: account, disabled: true },
        { key: 'new_password', label: '新密码', maxLength: 128, placeholder: '留空自动生成' },
      ],
      onSubmit: (values) =>
        api.post(
          `/platform/tenants/${data.id}/reset-password`,
          { account, new_password: values.new_password },
          { idempotencyKey: api.newIdempotencyKey() },
        ),
    });

    if (!result) return;
    reload();
    // 无论自动生成还是人工指定，都必须让操作者确认「已保存」——
    // 自动生成时这是唯一一次看到明文的机会。
    if (result.generated && result.password) {
      await showOneTimePassword({
        account: result.account,
        password: result.password,
        title: '新密码（仅显示一次）',
      });
    } else {
      toast.success('密码已重置，客户下次登录需修改密码');
    }
  });
}

/* ------------------------------------------------------------
   页面入口
   ------------------------------------------------------------ */

const TABS = [
  { key: 'overview', label: '概览' },
  { key: 'authorizations', label: '产品授权' },
  { key: 'products', label: '客户产品' },
  { key: 'accounts', label: '登录账号' },
];

/**
 * 渲染租户详情页。
 *
 * @param {HTMLElement} container
 * @param {{router: object, params: {id: string}}} ctx
 */
export async function renderTenantDetail(container, ctx) {
  const { router, params } = ctx;
  const tenantId = params?.id;
  if (!tenantId) {
    router.go('/tenants');
    return;
  }

  let data;
  try {
    data = await loadTenant(tenantId);
  } catch (error) {
    notifyError(error, '租户不存在或已被删除');
    router.go('/tenants');
    return;
  }

  const detail = createDetailPage(container, {
    title: data.name,
    desc: `租户编码 ${data.code}　·　${data.status === 'ACTIVE' ? '启用中' : '已禁用'}`,
    backPath: '/tenants',
    router,
    actions: [
      { label: '编辑资料', action: 'edit-tenant', perm: PERM.platform.tenantWrite },
      { label: '刷新', icon: 'refresh', variant: 'ghost', action: 'reload-detail' },
    ],
    tabs: TABS,
    activeTab: 'overview',
  });

  const reload = async () => {
    try {
      data = await loadTenant(tenantId);
      renderTab(detail.activeTab);
    } catch (error) {
      notifyError(error);
    }
  };

  const renderTab = (key) => {
    detail.body.replaceChildren(fromHtml(loadingState()));
    const host = h('div');
    detail.body.replaceChildren(host);

    if (key === 'overview') renderOverview(host, data, { router, reload });
    else if (key === 'authorizations') renderAuthorizations(host, data, { reload });
    else if (key === 'products') renderClientProducts(host, data, { router });
    else if (key === 'accounts') renderAccounts(host, data, { reload });
  };

  renderTab('overview');
  container.addEventListener('tabchange', (event) => renderTab(event.detail.tab));

  /* ---- 页头按钮 ----
     绑在 detail.head 上而不是 container：container 由应用壳长期持有，
     绑在上面会随「进入本页的次数」叠加监听器（表现为一次点击弹多个弹窗）。 */
  detail.head.addEventListener('click', async (event) => {
    const target = event.target.closest('[data-action]');
    if (!target) return;

    if (target.dataset.action === 'reload-detail') {
      await reload();
      toast.info('已刷新');
      return;
    }

    if (target.dataset.action === 'edit-tenant') {
      const updated = await formModal({
        title: `编辑租户「${data.name}」`,
        submitText: '保存',
        fields: [
          { key: 'name', label: '租户名称', required: true, maxLength: 128 },
          { key: 'contact_name', label: '联系人', maxLength: 64 },
          { key: 'contact_phone', label: '联系电话', maxLength: 32 },
          { key: 'email', label: '邮箱', maxLength: 128 },
          { key: 'industry', label: '所属行业', maxLength: 64 },
          { key: 'remark', label: '备注', type: 'textarea', maxLength: 2000, span: 2 },
        ],
        values: {
          name: data.name,
          contact_name: data.contactName,
          contact_phone: data.contactPhone,
          email: data.email,
          industry: data.industry,
          remark: data.remark,
        },
        onSubmit: (values) =>
          api.put(`/platform/tenants/${tenantId}`, values, {
            idempotencyKey: api.newIdempotencyKey(),
          }),
      });
      if (updated) {
        toast.success('租户资料已更新');
        await reload();
      }
    }
  });
}
