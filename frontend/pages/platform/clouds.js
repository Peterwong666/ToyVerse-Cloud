/* ============================================================
   平台端 · 云服务商配置
   ------------------------------------------------------------
   对应后端 `/platform/clouds`：
     GET / POST / PUT / DELETE   +   POST /{id}/test（连通性检测）

   两件必须让使用者看懂的事（页面上直接写出来，而不是藏在文档里）
   ------------------------------------------------------------
   1. **密钥只进不出**：`accessKey` / `secretKey` 是明文入参，
      服务端加密落库；**响应永远只回前 4 位掩码**（`accessKeyHint`）。
      所以编辑时密钥输入框是空的，留空 = 保持原值 —— 这不是 bug。
   2. **连通性检测不验证密钥**：只探测「接口地址是否网络可达」。
      未配置地址或密钥时返回 `NOT_CONFIGURED` 且不发起任何真实调用
      （ADR-07：未配置的能力一律安全失败，绝不伪造成功）。
   ============================================================ */

import api from '/shared/core/api.js';
import { PERM } from '/shared/core/auth.js';
import { esc, formatRelative, fromHtml, html, raw } from '/shared/ui/dom.js';
import { alert, statusTag, tag } from '/shared/ui/components.js';
import { createListPage } from '/shared/app/page.js';
import { formModal } from '/shared/ui/form.js';
import toast from '/shared/ui/toast.js';
import {
  CLOUD_STATUS_MAP,
  NETWORK_MAP,
  VENDOR_LABELS,
  VENDOR_OPTIONS,
  NETWORK_OPTIONS,
  confirmThenRun,
  notifyError,
  renderSecretHint,
} from './common.js';

/** 表格列 */
const COLUMNS = [
  {
    key: 'name',
    title: '云服务商',
    sortable: true,
    render: (row) => html`<div class="cell-stack">
      <span class="cell-strong">${row.name}</span>
      <span class="cell-sub mono">${row.code}</span>
    </div>`,
  },
  {
    key: 'vendor',
    title: '厂商',
    render: (row) => esc(VENDOR_LABELS[row.vendor] || row.vendor),
  },
  {
    key: 'network_type',
    title: '联网方式',
    render: (row) => statusTag(row.networkType, NETWORK_MAP),
  },
  {
    key: 'access_key_hint',
    title: 'AccessKey',
    render: (row) => renderSecretHint(row.accessKeyHint),
  },
  {
    key: 'secret_key_hint',
    title: 'SecretKey',
    render: (row) => renderSecretHint(row.secretKeyHint),
  },
  {
    key: 'status',
    title: '接入状态',
    render: (row) => statusTag(row.status, CLOUD_STATUS_MAP),
  },
  {
    key: 'last_test',
    title: '最近检测',
    render: (row) => {
      if (!row.lastTestedAt) return tag('未检测', 'default');
      const ok = row.lastTestOk === true;
      return html`<div class="cell-stack">
        <span>${ok ? tag('通过', 'success') : tag('未通过', 'danger')}</span>
        <span class="cell-sub">${esc(formatRelative(row.lastTestedAt))}</span>
      </div>`;
    },
  },
  {
    key: 'template_count',
    title: '引用模板',
    width: '90px',
    render: (row) => String(row.templateCount ?? 0),
  },
  {
    key: 'actions',
    title: '操作',
    align: 'right',
    width: '190px',
    nowrap: true,
    render: (row) => html`<div class="btn-group">
      ${raw(`<button type="button" class="btn btn-sm btn-primary" data-action="test-cloud" data-id="${esc(row.id)}">连通性检测</button>`)}
      ${raw(`<button type="button" class="btn btn-sm" data-action="edit-cloud" data-id="${esc(row.id)}">编辑</button>`)}
      ${raw(`<button type="button" class="btn btn-sm btn-ghost" data-action="delete-cloud" data-id="${esc(row.id)}">删除</button>`)}
    </div>`,
  },
];

/** 表单字段（编辑时密钥留空 = 保持原值） */
function cloudFields({ forEdit = false } = {}) {
  const fields = [
    {
      key: 'code',
      label: '厂商编码',
      required: !forEdit,
      maxLength: 64,
      disabled: forEdit,
      placeholder: '如 VOLCANO-HW（全局唯一，自动转大写）',
      hint: forEdit ? '编码创建后不可修改' : '',
    },
    { key: 'name', label: '名称', required: true, maxLength: 128 },
    {
      key: 'vendor',
      label: '厂商',
      type: 'select',
      options: VENDOR_OPTIONS,
      value: 'VOLCANO',
      hint: '决定设备生成方、激活路径与二维码格式',
    },
    {
      key: 'network_type',
      label: '联网方式',
      type: 'select',
      options: NETWORK_OPTIONS,
      value: 'WIFI',
    },
    {
      key: 'api_base',
      label: '接口基址',
      maxLength: 256,
      span: 2,
      placeholder: '如 https://rtc.volcengineapi.com',
      hint: '连通性检测会向该地址发起一次轻量 HTTP 探测',
    },
    {
      key: 'access_key',
      label: 'AccessKey',
      maxLength: 256,
      span: 1,
      placeholder: forEdit ? '留空表示保持原值' : '明文录入，服务端加密存储',
      hint: '响应中只会返回前 4 位掩码，明文不回显',
    },
    {
      key: 'secret_key',
      label: 'SecretKey',
      maxLength: 512,
      span: 1,
      placeholder: forEdit ? '留空表示保持原值' : '明文录入，服务端加密存储',
      hint: '任何接口都不会返回 SecretKey',
    },
  ];

  fields.push({
    key: 'ota_support',
    label: 'OTA 支持',
    type: 'select',
    options: [
      { value: 'SUPPORTED', label: '支持（平台可推送）' },
      { value: 'UNSUPPORTED', label: '不支持（仅端侧升级）' },
    ],
    value: 'SUPPORTED',
  });
  fields.push({
    key: 'extra_config',
    label: '厂商特有参数（JSON）',
    type: 'textarea',
    rows: 3,
    span: 2,
    placeholder: '{"vendor_id": "...", "app_id": "..."}',
  });
  fields.push({ key: 'remark', label: '备注', type: 'textarea', maxLength: 2000, span: 2 });
  return fields;
}

function toJsonText(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return '';
  }
}

/** 执行连通性检测并给出**准确**的结论（不夸大） */
async function runConnectivityTest(row, { reload }) {
  try {
    const result = await api.post(
      `/platform/clouds/${row.id}/test`,
      undefined,
      { idempotencyKey: api.newIdempotencyKey() },
    );
    reload();

    if (result.result === 'NOT_CONFIGURED') {
      // 这是「安全失败」，不是错误：必须说清为什么，否则用户以为功能坏了
      toast.warning(`未执行真实调用：${result.message}`);
      return;
    }
    if (result.ok) {
      toast.success(
        `检测完成：${result.message}${result.latencyMs !== null ? `（${result.latencyMs}ms）` : ''}`,
      );
      return;
    }
    toast.error(`检测失败：${result.message}`);
  } catch (error) {
    notifyError(error);
  }
}

/**
 * 渲染云服务商配置页。
 *
 * @param {HTMLElement} container
 * @param {{router: object}} ctx
 */
export function renderClouds(container, ctx) {
  void ctx;

  return createListPage({
    container,
    title: '云服务商配置',
    desc:
      '设备生成与激活所依赖的厂商账号（火山引擎智能云 / 集贤 / 京东云 JoyInside）。' +
      '密钥加密落库且只回掩码；连通性检测只验证「网络可达」，不代表密钥有效。',
    actions: [
      { label: '刷新', icon: 'refresh', action: 'reload-list' },
      {
        label: '新增云服务商',
        icon: 'plus',
        variant: 'primary',
        action: 'new-cloud',
        perm: PERM.platform.cloudWrite,
      },
    ],
    columns: COLUMNS,
    fetcher: (params) =>
      api.get('/platform/clouds', {
        params: {
          page: params.page,
          pageSize: params.pageSize,
          sortBy: params.sortBy,
          order: params.order,
          keyword: params.keyword,
          vendor: params.vendor,
          status: params.status,
        },
      }),
    filters: [
      { key: 'keyword', placeholder: '名称 / 编码', width: '200px' },
      {
        key: 'vendor',
        type: 'select',
        options: [{ value: '', label: '全部厂商' }, ...VENDOR_OPTIONS],
      },
      {
        key: 'status',
        type: 'select',
        options: [
          { value: '', label: '全部接入状态' },
          { value: 'CONNECTED', label: '已接入' },
          { value: 'NOT_CONNECTED', label: '未接入' },
        ],
      },
    ],
    // 安全说明常驻在表格上方。注意：DataTable 每次 load 都会清空并重建
    // 自己的容器，而 onRendered 回调在每次重建后都会触发，
    // 因此这里 prepend 的提示条会在每次刷新后自动补回（不会丢）。
    onReady: (host) => {
      const tip = fromHtml(
        alert({
          tone: 'neutral',
          title: '关于密钥与连通性检测',
          text:
            '密钥为明文录入、加密存储，响应只回前 4 位掩码（因此编辑时输入框为空属正常，留空即保持原值）。' +
            '「连通性检测」只探测接口地址是否网络可达；未配置地址或密钥时会明确返回「未配置」并跳过真实调用。',
        }),
      );
      host.prepend(tip);
    },
    onAction: async (action, target, { table, reload }) => {
      const row = target.dataset.id ? table.findRowById(target.dataset.id) : null;

      if (action === 'reload-list') {
        table.load();
        return;
      }

      if (action === 'new-cloud') {
        const created = await formModal({
          title: '新增云服务商',
          subtitle: '密钥将加密落库，接口响应永远不会包含明文密钥',
          size: 'lg',
          submitText: '创建',
          fields: cloudFields(),
          onSubmit: (values) =>
            api.post('/platform/clouds', values, { idempotencyKey: api.newIdempotencyKey() }),
        });
        if (created) {
          toast.success(`云服务商「${created.name}」已创建，建议先执行一次连通性检测`);
          reload();
        }
        return;
      }

      if (action === 'edit-cloud') {
        if (!row) return;
        const updated = await formModal({
          title: `编辑云服务商「${row.name}」`,
          subtitle: '密钥留空表示保持原值；一旦填写新密钥，接入状态会回到「未接入」并要求重新检测',
          size: 'lg',
          submitText: '保存',
          fields: cloudFields({ forEdit: true }),
          values: {
            code: row.code,
            name: row.name,
            vendor: row.vendor,
            network_type: row.networkType,
            api_base: row.apiBase,
            ota_support: row.otaSupport,
            // 密钥刻意不回填：后端不回明文，前端也无从回填
            access_key: '',
            secret_key: '',
            extra_config: toJsonText(row.extraConfig),
            remark: row.remark,
          },
          onSubmit: (values) => {
            const { code: _code, vendor: _vendor, network_type: _network, ...patch } = values;
            return api.put(`/platform/clouds/${row.id}`, patch, {
              idempotencyKey: api.newIdempotencyKey(),
            });
          },
        });
        if (updated) {
          toast.success('云服务商已更新');
          reload();
        }
        return;
      }

      if (action === 'test-cloud') {
        if (row) await runConnectivityTest(row, { reload });
        return;
      }

      if (action === 'delete-cloud') {
        if (!row) return;
        await confirmThenRun({
          title: `删除云服务商「${row.name}」`,
          description: '删除前会校验是否被产品模板或客户产品引用。',
          detail:
            row.templateCount > 0
              ? `该云服务商已被 ${row.templateCount} 个产品模板引用——按设计会被拒绝（CASCADE_CONFLICT）。请先改绑或删除这些模板。`
              : '该云服务商暂无引用，可以删除。',
          tone: 'danger',
          confirmText: '确认删除',
          run: () =>
            api.del(`/platform/clouds/${row.id}`, undefined, {
              idempotencyKey: api.newIdempotencyKey(),
            }),
          successMessage: '云服务商已删除',
          onDone: reload,
        });
      }
    },
  });
}

export default { renderClouds };
