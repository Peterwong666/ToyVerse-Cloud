/* ============================================================
   平台端 · 设备库存
   ------------------------------------------------------------
   对应后端 `/platform/devices`：
     GET  列表（tenantId / clientProductId / 四维状态 / keyword）
     POST /{id}/freeze  /{id}/thaw  /{id}/retire

   ★ ADR-03：设备状态是**四个正交维度**，不是单一枚举。
     因此列表把「资产 / 激活 / 在线 / 绑定」拆成 4 个小标签并列，
     同时保留后端派生好的中文 `label` 作为主状态列 ——
     label 便于快速浏览，四维标签用于定位「到底哪一维不对」。

   冻结 / 解冻 / 报废都是**有审计要求的写操作**（后端记录原因与操作者），
   因此它们都必须填写原因：用 confirmDialog 的 requireReason，
   而不是先提交再由后端报「原因必填」。
   ============================================================ */

import api from '/shared/core/api.js';
import auth, { PERM } from '/shared/core/auth.js';
import { esc, formatDate, fromHtml, h, html, raw } from '/shared/ui/dom.js';
import { statGrid, statusTag, tag } from '/shared/ui/components.js';
import { createListPage } from '/shared/app/page.js';
import { confirmDialog } from '/shared/ui/modal.js';
import { countSafe } from '/shared/app/dashboard.js';
import toast from '/shared/ui/toast.js';
import {
  ACTIVATION_STATUS_MAP,
  ACTIVATION_STATUS_OPTIONS,
  ASSET_STATUS_MAP,
  ASSET_STATUS_OPTIONS,
  BIND_STATUS_MAP,
  BIND_STATUS_OPTIONS,
  NETWORK_MAP,
  ONLINE_STATUS_MAP,
  ONLINE_STATUS_OPTIONS,
  fetchRecords,
  notifyError,
  toOptions,
} from './common.js';

/**
 * 可冻结的资产状态。
 *
 * P5 修正（P4 遗留缺陷）：P4 这里写的是 `GENERATED` / `IN_STOCK`，但后端
 * 已把冻结范围收紧为**只允许 `IN_STOCK`**（见 app/models/enums.py 的
 * `FREEZABLE_ASSET_STATUSES`）。前端集合比后端宽会让「冻结」按钮出现在
 * 一定会被拒绝的设备上，用户点完只能收到 400 —— 这正是 P3 反复强调的
 * 「不要让用户点了才知道不行」。此处与后端 FREEZABLE_ASSET_STATUSES 对齐。
 */
const FREEZABLE = new Set(['IN_STOCK']);

/** 已知的资产状态 → 主状态列配色 */
const LABEL_TONE_BY_ASSET = {
  PENDING_GEN: 'default',
  GENERATED: 'info',
  IN_STOCK: 'brand',
  PRODUCING: 'warning',
  PRODUCED: 'teal',
  SHIPPED: 'teal',
  ALLOCATED: 'info',
  BOUND: 'success',
  FROZEN: 'coral',
  RETIRED: 'default',
};

/**
 * 四维状态小标签（ADR-03）。
 * @param {object} row DeviceResponse
 */
function fourDimTags(row) {
  return html`${raw(
    [
      statusTag(row.assetStatus, ASSET_STATUS_MAP),
      statusTag(row.activationStatus, ACTIVATION_STATUS_MAP),
      statusTag(row.onlineStatus, ONLINE_STATUS_MAP),
      statusTag(row.bindStatus, BIND_STATUS_MAP),
    ].join(''),
  )}`;
}

/** 表格列定义（需要租户 / 产品的名称映射，故用工厂函数生成） */
function buildColumns({ tenantLabels, productLabels }) {
  return [
    {
      key: 'sn',
      title: 'SN / IMEI / MAC',
      sortable: true,
      render: (row) => html`<div class="cell-stack">
      <span class="cell-strong mono">${row.sn || '-'}</span>
      <span class="cell-sub mono">${row.imei || '-'}</span>
      <span class="cell-sub mono">${row.mac || '-'}</span>
    </div>`,
    },
    {
      key: 'tenant_name',
      title: '归属租户',
      // 还没有分配给任何租户的设备属于平台自有库存，用 tag 明确区分，
      // 避免用户把「平台库存」误读成「某租户名为空」
      //
      // DeviceResponse（列表项）**不含** tenantName/tenantCode，
      // 只有详情才带名称。因此这里用租户下拉的数据在前端做名称映射，
      // 映射不到时退回显示 tenantId（mono），绝不显示成空。
      render: (row) => {
        if (!row.tenantId) return tag('平台库存', 'info');
        const known = tenantLabels.get(String(row.tenantId));
        return html`<div class="cell-stack">
          <span>${row.tenantName || known?.name || '-'}</span>
          <span class="cell-sub mono">${row.tenantCode || known?.code || row.tenantId}</span>
        </div>`;
      },
    },
    {
      key: 'client_product_name',
      title: '客户产品',
      render: (row) => {
        if (!row.clientProductId) return '-';
        const known = productLabels.get(String(row.clientProductId));
        return html`<div class="cell-stack">
          <span>${row.clientProductName || known?.name || '-'}</span>
          <span class="cell-sub mono">${row.clientProductCode || known?.code || row.clientProductId}</span>
        </div>`;
      },
    },
    {
      key: 'label',
      title: '状态',
      width: '120px',
      // label 由后端按四维状态派生（derive_device_label），前端只负责着色
      render: (row) => tag(row.label || '-', LABEL_TONE_BY_ASSET[row.assetStatus] || 'default'),
    },
    {
      key: 'four_dim',
      title: '四维（资产 / 激活 / 在线 / 绑定）',
      render: (row) => fourDimTags(row),
    },
    {
      key: 'network_type',
      title: '联网方式',
      width: '105px',
      render: (row) => statusTag(row.networkType, NETWORK_MAP),
    },
    {
      key: 'online',
      title: '在线',
      width: '90px',
      // ★ P5：本列一律以布尔 `online`（按 180 秒心跳窗口派生）为准。
      // 为什么不用 `onlineStatus`：那是**落库的投影**，只在下一次心跳或
      // 模拟心跳时才改写；设备掉线后它仍停留在「在线」，而 `online`
      // 会随窗口失效自动变 false。四维标签里的 onlineStatus 只用于筛选。
      render: (row) => tag(row.online ? '在线' : '离线', row.online ? 'success' : 'warning'),
    },
    {
      key: 'generated_at',
      title: '生成时间',
      sortable: true,
      width: '150px',
      render: (row) => esc(formatDate(row.generatedAt || row.createdAt)),
    },
    {
      key: 'actions',
      title: '操作',
      align: 'right',
      width: '320px',
      nowrap: true,
      render: (row) => {
        const canWrite = auth.hasPerm(PERM.platform.deviceWrite);
        const buttons = [
          `<button type="button" class="btn btn-sm btn-ghost" data-action="open-device" data-id="${esc(row.id)}">详情</button>`,
        ];
        // 模拟心跳是演示用动作（后端响应里 simulated=true），
        // 已报废设备不再产生任何状态流转，因此不提供该按钮。
        if (canWrite && row.assetStatus !== 'RETIRED') {
          buttons.push(
            `<button type="button" class="btn btn-sm btn-ghost" data-action="simulate-heartbeat" data-id="${esc(row.id)}">模拟心跳</button>`,
          );
        }
        if (canWrite && FREEZABLE.has(row.assetStatus)) {
          buttons.push(
            `<button type="button" class="btn btn-sm" data-action="freeze-device" data-id="${esc(row.id)}">冻结</button>`,
          );
        }
        if (canWrite && row.assetStatus === 'FROZEN') {
          buttons.push(
            `<button type="button" class="btn btn-sm btn-primary" data-action="thaw-device" data-id="${esc(row.id)}">解冻</button>`,
          );
        }
        if (canWrite && row.assetStatus !== 'RETIRED') {
          buttons.push(
            `<button type="button" class="btn btn-sm btn-danger" data-action="retire-device" data-id="${esc(row.id)}">报废</button>`,
          );
        }
        return html`<div class="btn-group">${raw(buttons.join(''))}</div>`;
      },
    },
  ];
}

/**
 * 需要填写原因的写操作封装（冻结 / 报废）。
 *
 * 不复用 common.confirmThenRun：那个封装不支持「必填原因」，
 * 而冻结与报废在后端都要记录 freezeReason / retireReason。
 */
async function runWithReason(o) {
  const { confirmed, reason } = await confirmDialog({
    title: o.title,
    description: o.description,
    detail: o.detail,
    tone: o.tone || 'warning',
    confirmText: o.confirmText,
    requireReason: true,
    reasonLabel: o.reasonLabel,
  });
  if (!confirmed) return;
  try {
    await o.run(reason);
    toast.success(o.successMessage);
    o.onDone?.();
  } catch (error) {
    notifyError(error);
  }
}

/**
 * 渲染设备库存页。
 *
 * @param {HTMLElement} container
 * @param {{router: object}} ctx
 */
export async function renderDevices(container, ctx) {
  const { router } = ctx;

  // 租户 / 客户产品下拉静默容错：取不到只影响名称展示（会退回显示 ID），不影响列表浏览
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

  const page = createListPage({
    container,
    title: '设备库存',
    desc: '平台自有库存与已分配给租户的设备。状态按「资产 / 激活 / 在线 / 绑定」四个维度分别展示（ADR-03）；「在线」列以 180 秒心跳窗口派生的 online 为准。',
    actions: [{ label: '刷新', icon: 'refresh', action: 'reload-list' }],
    columns: buildColumns({ tenantLabels, productLabels }),
    fetcher: (params) =>
      api.get('/platform/devices', {
        params: {
          page: params.page,
          pageSize: params.pageSize,
          sortBy: params.sortBy,
          order: params.order,
          keyword: params.keyword,
          tenantId: params.tenantId,
          clientProductId: params.clientProductId,
          assetStatus: params.assetStatus,
          activationStatus: params.activationStatus,
          onlineStatus: params.onlineStatus,
          bindStatus: params.bindStatus,
        },
      }),
    filters: [
      { key: 'keyword', placeholder: 'SN / IMEI / MAC', width: '200px' },
      { key: 'tenantId', type: 'select', options: [{ value: '', label: '全部租户' }, ...tenantOptions] },
      { key: 'assetStatus', type: 'select', options: ASSET_STATUS_OPTIONS },
      { key: 'activationStatus', type: 'select', options: ACTIVATION_STATUS_OPTIONS },
      { key: 'onlineStatus', type: 'select', options: ONLINE_STATUS_OPTIONS },
      { key: 'bindStatus', type: 'select', options: BIND_STATUS_OPTIONS },
    ],
    onReady: (host, instance) => {
      // 只在「加载完成」时刷新统计，避免 loading 渲染触发无谓的 4 个请求
      if (!instance.state.loading) refreshStats(instance.filters);
    },
    onAction: async (action, target, { table: tbl, reload }) => {
      const row = target.dataset.id ? tbl.findRowById(target.dataset.id) : null;

      if (action === 'reload-list') {
        tbl.load();
        return;
      }

      if (action === 'open-device') {
        if (!row) return;
        // P5：设备详情页已注册路由，这里走真正的跳转（把设备 id 写进 URL，
        // 刷新与分享都不丢状态）。保留路由缺失的兜底分支，避免路由表被
        // 改动时整页抛错白屏。
        const hasRoute = (router.routes || []).some((item) => item.name === 'deviceDetail');
        if (hasRoute) router.goByName('deviceDetail', { id: row.id });
        else toast.info('设备详情页尚未注册路由，可先在本列表中查看该设备的四维状态');
        return;
      }

      if (action === 'simulate-heartbeat') {
        if (!row) return;
        const handle = toast.loading('正在模拟设备心跳…');
        try {
          const result = await api.post(
            `/platform/devices/${row.id}/simulate-heartbeat`,
            { online: true },
            { idempotencyKey: api.newIdempotencyKey() },
          );
          handle.close();
          // 后端会回 simulated=true 表示这是平台演示心跳，绝不冒充真实上报
          toast.success(
            `已写入演示心跳：${result?.online === false ? '设备离线' : '设备在线'}` +
              `${result?.simulated ? '（模拟数据）' : ''}`,
          );
          reload();
        } catch (error) {
          handle.close();
          notifyError(error, '模拟心跳失败');
        }
        return;
      }

      if (action === 'freeze-device') {
        if (!row) return;
        await runWithReason({
          title: `冻结设备 ${row.sn}`,
          description: '冻结后该设备不可被分配、绑定或激活，用于处理异常与纠纷。',
          detail: '冻结会记录原因与操作者；解冻后恢复冻结前的资产状态。',
          confirmText: '确认冻结',
          reasonLabel: '冻结原因',
          run: (reason) =>
            api.post(
              `/platform/devices/${row.id}/freeze`,
              { reason },
              { idempotencyKey: api.newIdempotencyKey() },
            ),
          successMessage: '设备已冻结',
          onDone: reload,
        });
        return;
      }

      if (action === 'thaw-device') {
        if (!row) return;
        await runWithReason({
          title: `解冻设备 ${row.sn}`,
          description: '解冻后设备恢复到冻结前的资产状态。',
          detail: '后端会校验当前是否为冻结态，若不是会拒绝并说明原因。',
          tone: 'warning',
          confirmText: '确认解冻',
          reasonLabel: '解冻原因',
          run: (reason) =>
            api.post(
              `/platform/devices/${row.id}/thaw`,
              { reason },
              { idempotencyKey: api.newIdempotencyKey() },
            ),
          successMessage: '设备已解冻',
          onDone: reload,
        });
        return;
      }

      if (action === 'retire-device') {
        if (!row) return;
        await runWithReason({
          title: `报废设备 ${row.sn}`,
          description: '报废是不可逆操作，设备将进入终态，不能再分配或绑定。',
          detail: '请确认该设备已停止使用；报废会记录原因与操作者，供审计追溯。',
          tone: 'danger',
          confirmText: '确认报废',
          reasonLabel: '报废原因',
          run: (reason) =>
            api.post(
              `/platform/devices/${row.id}/retire`,
              { reason },
              { idempotencyKey: api.newIdempotencyKey() },
            ),
          successMessage: '设备已报废',
          onDone: reload,
        });
      }
    },
  });

  /* ---- 统计卡：插在页头与表格之间 ----
     数字一律取自后端真实计数（countSafe 失败显示「—」），
     绝不估算或沿用上一页的数字。 */
  const statsHost = h('div', { class: 'mb-4' });
  page.root.insertBefore(statsHost, page.root.children[1] || null);

  let latest = { total: '—', inStock: '—', activated: '—', online: '—' };

  const paintStats = () => {
    statsHost.replaceChildren(
      fromHtml(
        statGrid([
          {
            label: '设备总数',
            value: latest.total,
            unit: '台',
            icon: 'device',
            tone: 'brand',
            foot: '当前筛选条件下的设备',
          },
          {
            label: '已入库',
            value: latest.inStock,
            unit: '台',
            icon: 'database',
            tone: 'teal',
            foot: '资产状态 = 已入库待生产',
          },
          {
            label: '已激活',
            value: latest.activated,
            unit: '台',
            icon: 'checkCircle',
            tone: 'accent',
            foot: '激活状态 = 已激活',
          },
          {
            label: '在线',
            value: latest.online,
            unit: '台',
            icon: 'activity',
            tone: 'coral',
            foot: '180 秒心跳窗口内（与列表「在线」列同口径）',
          },
        ]),
      ),
    );
  };

  async function refreshStats(filters = {}) {
    // 「在线」卡的口径说明（P5）：
    //   列表「在线」列读的是布尔 `online`（180 秒心跳窗口派生）；
    //   统计卡因为要一次拿总数，只能用列表接口的 `onlineStatus=ONLINE` 过滤。
    //   后端已把该过滤条件改为**同一窗口口径**，因此两者的数字一致。
    //   若哪天这里与列表列出现偏差，优先怀疑这个过滤器口径被改回去了。
    const [total, inStock, activated, online] = await Promise.all([
      countSafe('/platform/devices', { ...filters }),
      countSafe('/platform/devices', { ...filters, assetStatus: 'IN_STOCK' }),
      countSafe('/platform/devices', { ...filters, activationStatus: 'ACTIVATED' }),
      countSafe('/platform/devices', { ...filters, onlineStatus: 'ONLINE' }),
    ]);
    latest = { total, inStock, activated, online };
    paintStats();
  }

  paintStats();

  return page;
}

export default { renderDevices };
