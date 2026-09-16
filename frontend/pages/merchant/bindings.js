/* ============================================================
   商户端 · 绑定管理
   ------------------------------------------------------------
   对应后端 `/merchant/bindings`：
     GET   列表（status / deviceId / keyword）
     POST  /merchant/devices/{id}/unbind  解绑（在设备列表里触发，本页也提供入口）

   为什么绑定记录要单独一页
   ------------------------
   设备列表回答的是「这台设备现在怎么样」，绑定记录回答的是
   「这台设备被谁绑过、什么时候绑的、为什么解绑」。
   把两者混在一张表里会让列数失控（设备四维 + 绑定审计），
   所以绑定记录（尤其是解绑原因与操作者）单独成页，供售后与纠纷追溯。

   绑定动作本身**不在本页发起**：绑定必须扫码（两步：预检 → 确认），
   入口放在「我的设备」里，本页只做查询与解绑。
   ============================================================ */

import api from '/shared/core/api.js';
import auth, { PERM } from '/shared/core/auth.js';
import { esc, formatDate, fromHtml, h, html } from '/shared/ui/dom.js';
import { alert, statusTag, tag } from '/shared/ui/components.js';
import { createListPage } from '/shared/app/page.js';
import { confirmDialog } from '/shared/ui/modal.js';
import toast from '/shared/ui/toast.js';
import {
  ASSET_STATUS_MAP,
  BINDING_RECORD_STATUS_MAP,
  BINDING_RECORD_STATUS_OPTIONS,
  P5_ERROR_HINTS,
  notifyError,
} from '/pages/platform/common.js';

/** P5 错误码优先给出动作建议，其余交给既有提示表 */
function notifyP5Error(error, fallback = '操作失败，请稍后重试') {
  const hint = P5_ERROR_HINTS[error?.code];
  if (hint) {
    toast.error(`${error?.message || fallback}　${hint}`, { traceId: error?.traceId || '' });
    return;
  }
  notifyError(error, fallback);
}

/** 表格列定义 */
const COLUMNS = [
  {
    key: 'sn',
    title: 'SN',
    sortable: true,
    render: (row) => html`<div class="cell-stack">
      <span class="cell-strong mono">${row.sn || row.deviceId || '-'}</span>
      <span class="cell-sub">${row.qrFormat ? `二维码格式 ${row.qrFormat}` : '-'}</span>
    </div>`,
  },
  {
    key: 'device_label',
    title: '设备标签',
    width: '120px',
    render: (row) =>
      tag(row.deviceLabel || '-', ASSET_STATUS_MAP[row.assetStatus]?.tone || 'default'),
  },
  {
    key: 'status',
    title: '绑定状态',
    width: '110px',
    render: (row) => statusTag(row.status, BINDING_RECORD_STATUS_MAP),
  },
  {
    key: 'end_user_id',
    title: '终端用户',
    width: '140px',
    render: (row) => esc(row.endUserId || '-'),
  },
  {
    key: 'bound',
    title: '绑定时间 / 操作者',
    width: '180px',
    render: (row) => html`<div class="cell-stack">
      <span>${row.boundAt ? formatDate(row.boundAt) : '-'}</span>
      <span class="cell-sub">${row.boundBy || '-'}</span>
    </div>`,
  },
  {
    key: 'unbound',
    title: '解绑时间 / 原因 / 操作者',
    render: (row) => {
      if (!row.unboundAt) return html`<span class="text-secondary">—</span>`;
      return html`<div class="cell-stack">
        <span>${formatDate(row.unboundAt)}</span>
        <span class="cell-sub text-danger">${row.unbindReason || '未填写原因'}</span>
        <span class="cell-sub">${row.unboundBy || '-'}</span>
      </div>`;
    },
  },
  {
    key: 'bind_count',
    title: '绑定次数',
    align: 'num',
    width: '90px',
    render: (row) => esc(row.bindCount ?? 0),
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
    width: '100px',
    nowrap: true,
    render: (row) => {
      const canBind = auth.hasPerm(PERM.merchant.bindingWrite);
      // 只有「已绑定」的记录才谈得上解绑：
      // 待确认（PENDING）的令牌会自己过期，已解绑（UNBOUND）无需再解
      if (!canBind || row.status !== 'BOUND') return html`<span class="text-secondary">—</span>`;
      return html`<div class="btn-group">
        <button type="button" class="btn btn-sm btn-danger" data-action="unbind-binding" data-id="${esc(row.id)}">解绑</button>
      </div>`;
    },
  },
];

/** 解绑（原因必填）：本页按绑定记录发起，用记录里的 deviceId 调设备侧解绑接口 */
async function unbindFromBinding(binding, reload) {
  const { confirmed, reason } = await confirmDialog({
    title: `解绑设备 ${binding.sn || binding.deviceId}`,
    description: '解绑后终端用户将无法继续使用该设备，可再次扫码重新绑定。',
    detail: '解绑原因会写入绑定记录与审计日志，用于后续的责任追溯。',
    tone: 'danger',
    confirmText: '确认解绑',
    requireReason: true,
    reasonLabel: '解绑原因',
  });
  if (!confirmed) return;

  try {
    await api.post(
      `/merchant/devices/${binding.deviceId}/unbind`,
      { reason },
      { idempotencyKey: api.newIdempotencyKey() },
    );
    toast.success('设备已解绑');
    reload();
  } catch (error) {
    notifyP5Error(error, '解绑失败');
  }
}

/**
 * 渲染绑定管理页。
 *
 * @param {HTMLElement} container
 * @param {{router: object}} ctx
 */
export async function renderBindings(container, ctx) {
  const { router } = ctx;

  const page = createListPage({
    container,
    title: '绑定管理',
    desc: '本租户的绑定记录（服务端强制按租户隔离）。包含绑定 / 解绑时间、操作者与解绑原因，供售后与纠纷追溯。',
    actions: [
      { label: '刷新', icon: 'refresh', action: 'reload-list' },
      {
        label: '去绑定设备',
        icon: 'qrcode',
        variant: 'primary',
        action: 'go-devices',
        perm: PERM.merchant.bindingWrite,
      },
    ],
    columns: COLUMNS,
    fetcher: (params) =>
      api.get('/merchant/bindings', {
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
      { key: 'keyword', placeholder: 'SN', width: '200px' },
      { key: 'status', type: 'select', options: BINDING_RECORD_STATUS_OPTIONS },
    ],
    onAction: async (action, target, { table: tbl, reload }) => {
      const row = target.dataset.id ? tbl.findRowById(target.dataset.id) : null;

      if (action === 'reload-list') {
        tbl.load();
        return;
      }

      if (action === 'go-devices') {
        // 绑定必须扫码：本页只做查询与解绑，发起绑定统一走设备列表
        router.go('/devices');
        return;
      }

      if (action === 'unbind-binding') {
        if (row) await unbindFromBinding(row, reload);
      }
    },
  });

  /* ---- 页头指引：绑定由扫码发起，不要把用户留在本页找「绑定」按钮 ---- */
  const note = h('div', { class: 'mb-4' });
  note.append(
    fromHtml(
      alert({
        tone: 'neutral',
        title: '绑定由扫码发起，请到「我的设备」操作',
        text:
          '本页用于查询绑定记录与执行解绑。要绑定新设备：进入「我的设备」，' +
          '找到状态为「已分配 / 未绑定」的设备，点击「绑定」并扫描设备机身或包装上的二维码。' +
          '扫码后会先回显识别结果供你核对，确认后才真正绑定（确认码 5 分钟内有效）。',
      }),
    ),
  );
  page.root.insertBefore(note, page.root.children[1] || null);

  return page;
}

export default { renderBindings };
