/* ============================================================
   平台端 · 订单详情
   ------------------------------------------------------------
   路由：`#/orders/:id`（参数化路由，刷新与分享都不丢状态）

   三个标签页：
     概览       订单字段 + 状态说明 + 审核信息
     设备清单   本单已生成的设备（四维状态），可跳设备详情
     二维码     真实可扫二维码（P2 的 Canvas 编码器）+ CSV 导出

   页头动作按**状态**动态出现（而不是全部渲染再在点击时报 409）：
     待审核 → 审核通过 / 驳回
     已审核 → 生成设备
     始终   → 导出二维码

   为什么「生成设备」放在详情页而不是列表页
   ----------------------------------------
   生成设备会向厂商云发起真实调用（失败会返回各厂商的错误），
   属于有副作用、需要看结果明细的动作；放在「先点进某一单」的位置，
   既降低误触，也让生成结果（成功/失败/厂商消息）有地方完整展示。
   ============================================================ */

import api from '/shared/core/api.js';
import { PERM } from '/shared/core/auth.js';
import {
  downloadCsv,
  esc,
  formatDate,
  formatMoney,
  formatRelative,
  fromHtml,
  h,
  html,
  raw,
} from '/shared/ui/dom.js';
import {
  alert,
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
import { renderToCanvas } from '/shared/ui/qrcode.js';
import toast from '/shared/ui/toast.js';
import {
  ACTIVATION_STATUS_MAP,
  ASSET_STATUS_MAP,
  BIND_STATUS_MAP,
  NETWORK_MAP,
  ONLINE_STATUS_MAP,
  ORDER_STATUS_MAP,
  confirmThenRun,
  notifyError,
} from './common.js';

const TABS = [
  { key: 'overview', label: '概览' },
  { key: 'devices', label: '设备清单' },
  { key: 'qrcodes', label: '二维码' },
];

/** 读取订单详情 */
function loadOrder(id) {
  return api.get(`/platform/orders/${id}`);
}

/* ------------------------------------------------------------
   一、概览
   ------------------------------------------------------------ */

function renderOverview(host, data, ctx) {
  const { reload } = ctx;

  host.append(
    fromHtml(
      statGrid([
        {
          label: '订购数量',
          value: data.quantity ?? '—',
          unit: '台',
          icon: 'package',
          tone: 'brand',
          foot: `单价 ${formatMoney(data.unitPrice)}`,
        },
        {
          label: '已生成设备',
          value: data.generatedCount ?? 0,
          unit: '台',
          icon: 'device',
          tone: 'teal',
          foot: `订单下设备 ${data.deviceCount ?? 0} 台`,
        },
        {
          label: '订单金额',
          value: formatMoney(data.totalAmount),
          icon: 'chartBar',
          tone: 'accent',
          foot: '数量 × 单价',
        },
        {
          label: '当前状态',
          value: ORDER_STATUS_MAP[data.status]?.text || data.status || '—',
          icon: 'clipboard',
          tone: data.status === 'REJECTED' ? 'coral' : 'brand',
          foot: data.generatedAt ? `生成于 ${formatRelative(data.generatedAt)}` : '尚未生成设备',
        },
      ]),
    ),
  );

  /* ---- 状态提示：把「下一步能做什么」说清楚 ---- */
  if (data.status === 'PENDING_AUDIT') {
    host.append(
      fromHtml(
        alert({
          tone: 'warning',
          title: '审核通过后才能生成设备',
          text: '本单仍在待审核状态。请先用页头「审核通过」或「驳回」给出结论。',
        }),
      ),
    );
  } else if (data.status === 'APPROVED') {
    host.append(
      fromHtml(
        alert({
          tone: 'info',
          title: '已通过审核，可以生成设备了',
          text: '点击页头「生成设备」向厂商云申请设备；生成结果（成功 / 失败 / 厂商消息）会在此提示。',
        }),
      ),
    );
  } else if (data.status === 'REJECTED') {
    host.append(
      fromHtml(
        alert({
          tone: 'danger',
          title: '本单已被驳回',
          text: data.rejectReason || '未填写驳回原因。',
        }),
      ),
    );
  }

  const card = h('div', { class: 'card mt-4' });
  card.append(fromHtml(`<div class="card-head"><div class="card-title">订单信息</div></div>`));
  card.append(
    fromHtml(
      descList([
        ['订单号', data.orderNo],
        ['状态', ORDER_STATUS_MAP[data.status]?.text || data.status],
        ['归属租户', `${data.tenantName || '-'}（${data.tenantCode || data.tenantId || '-'}）`],
        ['客户产品', `${data.clientProductName || '-'}（${data.clientProductCode || data.clientProductId || '-'}）`],
        ['联网方式', data.networkType === '4G' ? '4G（集贤方案）' : 'Wi-Fi（火山 / JoyInside）'],
        ['数量', data.quantity],
        ['单价', formatMoney(data.unitPrice)],
        ['订单金额', formatMoney(data.totalAmount)],
        ['申请人', data.applicantName],
        ['联系电话', data.applicantPhone],
        ['下单时间', formatDate(data.createdAt)],
        ['最后更新', formatRelative(data.updatedAt)],
        ['备注', data.remark],
      ]),
    ),
  );
  host.append(card);

  const auditCard = h('div', { class: 'card mt-4' });
  auditCard.append(fromHtml(`<div class="card-head"><div class="card-title">审核与生成</div></div>`));
  auditCard.append(
    fromHtml(
      descList([
        ['审核人', data.auditedBy],
        ['审核时间', data.auditedAt ? formatDate(data.auditedAt) : ''],
        ['审核备注', data.auditRemark],
        ['驳回原因', data.rejectReason],
        ['已生成数量', data.generatedCount],
        ['生成时间', data.generatedAt ? formatDate(data.generatedAt) : ''],
        ['订单下设备数', data.deviceCount],
      ]),
    ),
  );
  host.append(auditCard);
}

/* ------------------------------------------------------------
   二、设备清单
   ------------------------------------------------------------ */

/**
 * 取「本订单的设备」。
 *
 * 首选 `/platform/devices?orderId=<id>`（后端 device_service 支持按订单筛选，
 * 且列表项已含四维状态，是唯一能同时给出「本单范围 + 四维状态」的来源）。
 * 若该接口不可用或返回空，再退到 `/orders/{id}/qrcodes`（同样按订单返回，
 * 但只有 SN / 联网方式，没有状态）——保证页面至少能列出 SN，
 * 而不是因为一个接口挂掉就整页空白。
 *
 * @param {object} order
 * @returns {Promise<{rows:Array, scope:'order'|'qr'}>}
 */
async function loadOrderDevices(order) {
  const scoped = await api
    .get('/platform/devices', {
      params: { orderId: order.id, pageSize: 200 },
      silent: true,
    })
    .catch(() => null);

  const deviceRows = Array.isArray(scoped?.records) ? scoped.records : [];
  if (deviceRows.length) return { rows: deviceRows, scope: 'order' };

  const qr = await api
    .get(`/platform/orders/${order.id}/qrcodes`, { silent: true })
    .catch(() => null);

  return {
    rows: (Array.isArray(qr?.records) ? qr.records : []).map((item) => ({
      id: item.deviceId,
      sn: item.sn,
      networkType: item.networkType,
    })),
    scope: 'qr',
  };
}

/** 设备四维状态：四个小标签并排（ADR-03 要求分维度展示） */
function deviceStateTags(row) {
  if (!row.assetStatus && !row.activationStatus && !row.onlineStatus && !row.bindStatus) {
    return html`<span class="text-secondary">—</span>`;
  }
  return html`${raw(
    [
      statusTag(row.assetStatus, ASSET_STATUS_MAP),
      statusTag(row.activationStatus, ACTIVATION_STATUS_MAP),
      statusTag(row.onlineStatus, ONLINE_STATUS_MAP),
      statusTag(row.bindStatus, BIND_STATUS_MAP),
    ].join(''),
  )}`;
}

async function renderDevices(host, data, ctx) {
  const { router } = ctx;
  host.append(fromHtml(loadingState('正在读取设备…')));

  const { rows, scope } = await loadOrderDevices(data);
  host.replaceChildren();

  if (!rows.length) {
    host.append(
      fromHtml(
        emptyState({
          icon: 'device',
          title: '本单还没有设备',
          desc:
            data.status === 'APPROVED'
              ? '订单已通过审核，可回到「概览」并点击页头「生成设备」。'
              : '设备在「审核通过 → 生成设备」之后才会出现在这里。',
        }),
      ),
    );
    return;
  }

  if (scope !== 'order') {
    host.append(
      fromHtml(
        alert({
          tone: 'neutral',
          title: '展示范围说明',
          text: '设备列表接口暂不可用，以下仅展示本单二维码对应的 SN 与联网方式；四维状态请进入设备详情页查看。',
        }),
      ),
    );
  }

  const tableHost = h('div', { class: 'mt-3' });
  tableHost.innerHTML = table({
    columns: [
      {
        key: 'sn',
        title: 'SN',
        render: (row) => html`<div class="cell-stack">
          <span class="cell-strong mono">${row.sn || '-'}</span>
          <span class="cell-sub mono">${row.imei || row.mac || row.id}</span>
        </div>`,
      },
      { key: 'four_dim', title: '四维状态（资产 / 激活 / 在线 / 绑定）', render: (row) => deviceStateTags(row) },
      {
        key: 'network_type',
        title: '联网方式',
        width: '110px',
        render: (row) => statusTag(row.networkType, NETWORK_MAP),
      },
      {
        key: 'actions',
        title: '操作',
        align: 'right',
        width: '110px',
        nowrap: true,
        render: (row) =>
          row.id
            ? html`<button type="button" class="btn btn-sm btn-ghost" data-action="open-device" data-id="${esc(row.id)}">设备详情</button>`
            : '',
      },
    ],
    rows,
    emptyText: '暂无设备',
  });
  host.append(tableHost);

  // 监听器绑在本次渲染新建的 host 上（不是 container），随页面切换一起回收
  host.addEventListener('click', (event) => {
    const target = event.target.closest('[data-action="open-device"]');
    if (!target) return;
    // 设备详情页不在本阶段的路由清单内：路由缺失时给出明确指引，而不是抛错白屏
    const hasRoute = (router.routes || []).some((item) => item.name === 'deviceDetail');
    if (hasRoute) router.goByName('deviceDetail', { id: target.dataset.id });
    else toast.info('设备详情页尚未注册路由，可先到「设备库存」页查看该设备');
  });
}

/* ------------------------------------------------------------
   三、二维码
   ------------------------------------------------------------ */

/** 拉取本单二维码（供标签页与页头导出共用） */
async function fetchQrcodes(orderId) {
  return api.get(`/platform/orders/${orderId}/qrcodes`);
}

function renderQrcodes(host, data, ctx) {
  const { setCache } = ctx;
  host.append(fromHtml(loadingState('正在生成二维码…')));

  fetchQrcodes(data.id)
    .then((payload) => {
      const records = Array.isArray(payload?.records) ? payload.records : [];
      setCache(records);
      host.replaceChildren();

      if (!records.length) {
        host.append(
          fromHtml(
            emptyState({
              icon: 'qrcode',
              title: '还没有可导出的二维码',
              desc: '设备生成之后，每台设备的二维码会出现在这里，可直接扫码激活。',
            }),
          ),
        );
        return;
      }

      const bar = h('div', { class: 'flex-between mb-3' });
      bar.append(
        h('div', {
          class: 'text-sm text-secondary',
          text: `共 ${records.length} 张。二维码内容由厂商与设备身份共同决定，扫码即可进入激活流程。`,
        }),
      );
      const exportBtn = h('button', {
        class: 'btn btn-sm',
        type: 'button',
        text: '导出 CSV',
        'data-action': 'export-qrcodes',
      });
      bar.append(exportBtn);
      host.append(bar);

      const grid = h('div', { class: 'qr-grid' });
      records.forEach((record) => {
        const card = h('div', { class: 'card p-4 text-center' });
        const canvas = h('canvas');
        card.append(canvas);

        // 真二维码：P2 的纯实现编码器，不是 CSS 画的假码
        try {
          renderToCanvas(canvas, record.payload || '', { size: 140 });
        } catch (error) {
          canvas.remove();
          card.append(h('div', { class: 'text-xs text-danger', text: '二维码生成失败：内容过长或为空' }));
          console.error('[order_detail] 二维码生成失败', error);
        }

        card.append(h('div', { class: 'mono text-xs mt-2', text: record.sn || record.deviceId || '-' }));
        card.append(
          h('div', {
            class: 'text-xs text-secondary',
            text: `联网方式：${record.networkType || '-'}　格式：${record.format || '-'}`,
          }),
        );
        grid.append(card);
      });
      host.append(grid);
    })
    .catch((error) => {
      host.replaceChildren();
      host.append(
        fromHtml(
          emptyState({
            icon: 'alertTriangle',
            title: '二维码获取失败',
            desc: error?.message || '请确认订单是否已生成设备，然后重试。',
          }),
        ),
      );
    });
}

/* ------------------------------------------------------------
   四、页头动作
   ------------------------------------------------------------ */

/** 审核通过（可选填写备注） */
async function approveOrder(order, reload) {
  await confirmThenRun({
    title: `审核通过订单 ${order.orderNo || order.id}`,
    description: `将把订单状态改为「已审核」，之后即可生成设备。`,
    detail: '通过后订单进入「已审核」，下一步即可生成设备；驳回才是不可逆的终态操作。',
    tone: 'warning',
    confirmText: '确认通过',
    run: () =>
      api.post(
        `/platform/orders/${order.id}/audit`,
        { decision: 'APPROVED', remark: null, rejectReason: null },
        { idempotencyKey: api.newIdempotencyKey() },
      ),
    successMessage: '订单已通过审核，可以生成设备了',
    onDone: reload,
  });
}

/** 驳回（必须填写原因，会展示给商户端） */
async function rejectOrder(order, reload) {
  const rejected = await formModal({
    title: `驳回订单 ${order.orderNo || order.id}`,
    subtitle: '驳回后订单进入终态，商户需要重新下单；原因会展示给商户端',
    size: 'md',
    submitText: '确认驳回',
    fields: [
      {
        key: 'rejectReason',
        label: '驳回原因',
        type: 'textarea',
        required: true,
        maxLength: 500,
        span: 2,
        hint: '请写清楚需要商户修改什么，避免来回沟通',
      },
      { key: 'remark', label: '审核备注', type: 'textarea', maxLength: 1000, span: 2 },
    ],
    onSubmit: (values) =>
      api.post(
        `/platform/orders/${order.id}/audit`,
        { decision: 'REJECTED', remark: values.remark || null, rejectReason: values.rejectReason },
        { idempotencyKey: api.newIdempotencyKey() },
      ),
  });
  if (rejected) {
    toast.success('订单已驳回');
    reload();
  }
}

/** 生成设备（有副作用：会调用厂商云） */
async function generateDevices(order) {
  // 这里不用 confirmThenRun：它只回传「是否成功」，而生成结果
  // （requested / generated / failed / vendorMessage）必须完整展示给用户。
  const { confirmed } = await confirmDialog({
    title: `为订单 ${order.orderNo || order.id} 生成设备`,
    description: `将向厂商云申请生成 ${order.quantity ?? ''} 台设备，成功后订单进入「已生成」。`,
    detail: '生成会调用真实厂商接口；若厂商密钥未配置或额度不足，会返回具体失败原因。',
    tone: 'warning',
    confirmText: '确认生成',
  });
  if (!confirmed) return null;

  const handle = toast.loading('正在向厂商申请生成设备…');
  try {
    const result = await api.post(`/platform/orders/${order.id}/generate`, undefined, {
      idempotencyKey: api.newIdempotencyKey(),
    });
    handle.close();
    return result;
  } catch (error) {
    handle.close();
    // 生成失败的原因（厂商不可用 / 状态不允许）必须让用户看到，不吞错误
    notifyError(error, '生成设备失败');
    return null;
  }
}

/* ------------------------------------------------------------
   页面入口
   ------------------------------------------------------------ */

/**
 * 渲染订单详情页。
 *
 * @param {HTMLElement} container
 * @param {{router: object, params: {id: string}}} ctx
 */
export async function renderOrderDetail(container, ctx) {
  const { router, params } = ctx;
  const orderId = params?.id;
  if (!orderId) {
    router.go('/orders');
    return;
  }

  let data;
  try {
    data = await loadOrder(orderId);
  } catch (error) {
    notifyError(error, '订单不存在或已被删除');
    router.go('/orders');
    return;
  }

  /** 二维码缓存：标签页加载后页头导出可直接复用，避免重复请求 */
  let qrcodeCache = null;

  /**
   * 按**订单当前状态**构建页头动作。
   *
   * 抽成函数的原因：页头动作是状态相关的（待审核才有审核按钮、
   * 已审核才有生成设备按钮）。若只在首屏算一次，页面内完成审核后
   * 按钮不会更新，用户必须手动 F5 才能看到「生成设备」。
   * 因此 `reload()` 里会重新调用本函数并通过 `detail.setActions()` 刷新。
   */
  const buildActions = (status) => {
    const list = [{ label: '刷新', icon: 'refresh', variant: 'ghost', action: 'reload-detail' }];

    if (status === 'PENDING_AUDIT') {
      list.push({
        label: '审核通过',
        variant: 'primary',
        action: 'audit-approve',
        perm: PERM.platform.orderWrite,
      });
      list.push({ label: '驳回', variant: 'ghost', action: 'audit-reject', perm: PERM.platform.orderWrite });
    }
    if (status === 'APPROVED') {
      list.push({
        label: '生成设备',
        variant: 'primary',
        action: 'generate-devices',
        perm: PERM.platform.orderWrite,
      });
    }
    list.push({ label: '导出二维码', icon: 'download', variant: 'ghost', action: 'export-qrcodes' });
    return list;
  };

  const detail = createDetailPage(container, {
    title: data.orderNo || `订单 ${orderId}`,
    desc: `${data.tenantName || data.tenantId}　·　${data.clientProductName || data.clientProductId}　·　数量 ${data.quantity}`,
    backPath: '/orders',
    router,
    actions: buildActions(data.status),
    tabs: TABS,
    activeTab: 'overview',
  });

  const reload = async () => {
    try {
      data = await loadOrder(orderId);
      // 状态可能已变化，页头动作必须跟着变（否则审核完看不到「生成设备」）
      detail.setActions(buildActions(data.status));
      renderTab(detail.activeTab);
    } catch (error) {
      notifyError(error);
    }
  };

  const renderTab = (key) => {
    const host = h('div');
    detail.body.replaceChildren(host);

    if (key === 'devices') {
      renderDevices(host, data, { router }).catch((error) => notifyError(error));
    } else if (key === 'qrcodes') {
      renderQrcodes(host, data, {
        setCache: (records) => {
          qrcodeCache = records;
        },
      });
    } else {
      renderOverview(host, data, { reload });
    }
  };

  renderTab('overview');
  container.addEventListener('tabchange', (event) => renderTab(event.detail.tab));

  /** 导出二维码 CSV（需要鉴权，走 api 客户端而不是 <a href>） */
  const exportQrcodes = async () => {
    try {
      const records = qrcodeCache ?? (await fetchQrcodes(orderId))?.records ?? [];
      if (!records.length) {
        toast.warning('本单还没有二维码可导出，请先生成设备');
        return;
      }
      downloadCsv(`订单-${data.orderNo || orderId}-二维码.csv`, [
        ['设备ID', 'SN', '联网方式', '二维码内容', '格式'],
        ...records.map((item) => [
          item.deviceId,
          item.sn,
          item.networkType,
          item.payload,
          item.format,
        ]),
      ]);
      toast.success(`已导出 ${records.length} 张二维码`);
    } catch (error) {
      notifyError(error, '二维码导出失败');
    }
  };

  /* ---- 页头按钮 ----
     绑在 detail.head（本次渲染新建）而不是 container：
     container 由应用壳长期持有，绑在上面会随切页叠加监听器。 */
  detail.head.addEventListener('click', async (event) => {
    const target = event.target.closest('[data-action]');
    if (!target) return;
    const { action } = target.dataset;

    if (action === 'reload-detail') {
      await reload();
      toast.info('已刷新');
      return;
    }

    if (action === 'audit-approve') {
      await approveOrder(data, reload);
      return;
    }

    if (action === 'audit-reject') {
      await rejectOrder(data, reload);
      return;
    }

    if (action === 'generate-devices') {
      const result = await generateDevices(data);
      if (!result) return;
      const summary = `生成完成：请求 ${result.requested ?? 0} 台，成功 ${result.generated ?? 0} 台，失败 ${result.failed ?? 0} 台`;
      if (result.failed) toast.warning(summary);
      else toast.success(summary);
      // 厂商不可用等错误必须让用户看到：后端会放在 vendorMessage 里
      if (result.vendorMessage) {
        notifyError({ code: 'VENDOR_UNAVAILABLE', message: result.vendorMessage });
      }
      await reload();
      return;
    }

    if (action === 'export-qrcodes') {
      await exportQrcodes();
    }
  });
}

export default { renderOrderDetail };
