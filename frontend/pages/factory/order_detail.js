/* ============================================================
   工厂端 · 工单详情
   ------------------------------------------------------------
   路由：`#/orders/:id`（参数化路由，刷新与分享都不丢状态）

   三个标签页：
     概览       工单字段 + 进度 + 脱敏说明
     烧录记录   分批上报的历史（可核对「谁在什么时候报了多少」）
     抽检记录   合格 / 不合格统计 + 明细

   页头动作按**工单当前状态**动态出现（配合 createDetailPage 的 setActions）：
     待生产 / 生产中 → 烧录上报
     生产中 / 烧录完成 → 抽检确认
     烧录完成       → 出货登记
     任何状态       → 导出二维码
   为什么必须用 setActions 而不是首屏算一次：在详情页做完一次烧录上报后
   工单可能从「生产中」变成「烧录完成」，页头应立即变成「出货登记」，
   否则用户得手动 F5 才能看到下一步操作。
   ============================================================ */

import api from '/shared/core/api.js';
import { PERM } from '/shared/core/auth.js';
import { downloadCsv, esc, formatDate, fromHtml, h, html, raw } from '/shared/ui/dom.js';
import {
  alert,
  descList,
  emptyState,
  statGrid,
  statusTag,
} from '/shared/ui/components.js';
import { table } from '/shared/ui/table.js';
import { createDetailPage } from '/shared/app/page.js';
import { confirmDialog } from '/shared/ui/modal.js';
import toast from '/shared/ui/toast.js';
import {
  BURNABLE_STATUSES,
  FACTORY_ORDER_STATUS_MAP,
  INSPECTION_RESULT_MAP,
  INSPECTABLE_STATUSES,
  loadFactoryOrder,
  notifyFactoryError,
} from './orders.js';

const TABS = [
  { key: 'overview', label: '概览' },
  { key: 'burns', label: '烧录记录' },
  { key: 'inspections', label: '抽检记录' },
];

/** 展示时间（空值由 descList / 表格统一显示为 '-'） */
function showTime(value) {
  return value ? formatDate(value) : '';
}

/* ------------------------------------------------------------
   一、概览
   ------------------------------------------------------------ */

/** 脱敏规则说明卡（P6 的核心卖点，文案需与后端序列化行为完全一致） */
function maskingCard() {
  return html`${raw(
    `<div class="kv-list">
      <div class="kv-row"><span class="kv-key">客户名称</span><span class="kv-value">首字符 + 星号 + 尾字符（如 <span class="mono">中**动</span>）</span></div>
      <div class="kv-row"><span class="kv-key">订单金额</span><span class="kv-value text-disabled">服务端不下发</span></div>
      <div class="kv-row"><span class="kv-key">联系方式</span><span class="kv-value text-disabled">服务端不下发</span></div>
      <div class="kv-row"><span class="kv-key">邮箱</span><span class="kv-value text-disabled">服务端不下发</span></div>
    </div>`,
  )}`;
}

function renderOverview(host, data) {
  const total = Number(data.quantity ?? 0);
  const burned = Number(data.burnedCount ?? 0);
  const remaining = Number(data.remaining ?? Math.max(0, total - burned));
  const percent = Number(data.progressPercent ?? (total ? (burned / total) * 100 : 0));

  host.append(
    fromHtml(
      statGrid([
        {
          label: '需求数量',
          value: total,
          unit: '台',
          icon: 'package',
          tone: 'brand',
          foot: `平台订单数量 ${data.orderQuantity ?? '—'}`,
        },
        {
          label: '已烧录',
          value: burned,
          unit: '台',
          icon: 'fire',
          tone: 'accent',
          foot: `上报记录 ${(data.burnReports || []).length} 次`,
        },
        {
          label: '剩余待烧录',
          value: remaining,
          unit: '台',
          icon: 'clipboard',
          tone: remaining ? 'warning' : 'success',
          foot: remaining ? '烧录上报的默认数量' : '本单已烧录达标',
        },
        {
          label: '进度',
          value: `${percent.toFixed(0)}%`,
          icon: 'chartBar',
          tone: percent >= 100 ? 'success' : 'teal',
          foot: `累计烧录 ${burned} / ${total}`,
        },
      ]),
    ),
  );

  /* ---- 状态提示：把「下一步能做什么」说清楚 ---- */
  if (data.status === 'PENDING') {
    host.append(
      fromHtml(
        alert({
          tone: 'info',
          title: '本单尚未开始烧录',
          text: '点击页头「烧录上报」登记首批烧录数量，工单会自动进入「生产中」。',
        }),
      ),
    );
  } else if (data.status === 'PRODUCING') {
    host.append(
      fromHtml(
        alert({
          tone: 'info',
          title: '烧录进行中',
          text: `还需烧录 ${remaining} 台。累计烧录数达标后工单会转为「烧录完成」，届时页头出现「出货登记」。`,
        }),
      ),
    );
  } else if (data.status === 'COMPLETED') {
    host.append(
      fromHtml(
        alert({
          tone: 'success',
          title: '烧录已完成，可以出货了',
          text: '请核对实物数量与抽检结果后再点页头「出货登记」；出货后工单进入终态。',
        }),
      ),
    );
  } else if (data.status === 'SHIPPED') {
    host.append(
      fromHtml(
        alert({
          tone: 'success',
          title: '本单已出货',
          text: `出货时间：${showTime(data.shippedAt) || '—'}。出货为工单终态，不可再修改。`,
        }),
      ),
    );
  }

  const infoCard = h('div', { class: 'card mt-4' });
  infoCard.append(fromHtml(`<div class="card-head"><div class="card-title">工单信息</div></div>`));
  infoCard.append(
    fromHtml(
      descList([
        ['工单号', data.factoryOrderNo],
        ['平台订单号', data.orderNo],
        ['客户（脱敏）', data.customerNameMasked],
        ['型号', data.productModel],
        ['固件版本', data.firmwareVersion],
        ['数量', data.quantity],
        ['已烧录', data.burnedCount],
        ['剩余', data.remaining],
        ['工单状态', FACTORY_ORDER_STATUS_MAP[data.status]?.text || data.statusLabel || data.status],
        ['平台订单状态', data.orderStatusLabel || data.orderStatus],
        ['派单时间', showTime(data.assignedAt)],
        ['交期', showTime(data.dueAt)],
        ['出货时间', showTime(data.shippedAt)],
        ['生产要求', data.productionNote],
      ]),
    ),
  );
  host.append(infoCard);

  const maskCard = h('div', { class: 'card mt-4' });
  maskCard.append(fromHtml(`<div class="card-head"><div class="card-title">字段脱敏说明</div></div>`));
  maskCard.append(
    fromHtml(
      alert({
        tone: 'warning',
        text: '以下字段在服务端即被处理，工厂端接口**不会返回**真实值。',
      }),
    ),
  );
  maskCard.append(fromHtml(maskingCard()));
  host.append(maskCard);
}

/* ------------------------------------------------------------
   二、烧录记录
   ------------------------------------------------------------ */

function renderBurns(host, data) {
  const records = Array.isArray(data.burnReports) ? data.burnReports : [];

  if (!records.length) {
    host.append(
      fromHtml(
        emptyState({
          icon: 'fire',
          title: '还没有烧录记录',
          desc: '首次上报成功后，这里会按批次列出上报数量、SN 区间与操作人。',
        }),
      ),
    );
    return;
  }

  host.append(
    fromHtml(
      descList(
        [
          ['累计已烧录', `${data.burnedCount ?? 0} 台`],
          ['需求数量', `${data.quantity ?? '—'} 台`],
          ['剩余', `${data.remaining ?? 0} 台`],
          ['上报批次', `${records.length} 次`],
        ],
        { cols: 2 },
      ),
    ),
  );

  host.append(
    fromHtml(
      table({
        columns: [
          {
            key: 'burned_count',
            title: '本次上报',
            align: 'num',
            width: '100px',
            render: (row) => html`<span class="num font-semibold">${row.burnedCount ?? 0}</span>`,
          },
          {
            key: 'sn_range',
            title: 'SN 区间',
            render: (row) =>
              row.snFrom || row.snTo
                ? html`<span class="mono text-sm">${row.snFrom || '—'} ~ ${row.snTo || '—'}</span>`
                : html`<span class="text-secondary">未填写</span>`,
          },
          { key: 'operator', title: '操作人', width: '120px', render: (row) => esc(row.operator || '—') },
          { key: 'note', title: '备注', render: (row) => esc(row.note || '—') },
          {
            key: 'reported_at',
            title: '上报时间',
            width: '160px',
            render: (row) => esc(formatDate(row.reportedAt || row.createdAt)),
          },
        ],
        rows: records,
        emptyText: '暂无烧录记录',
      }),
    ),
  );
}

/* ------------------------------------------------------------
   三、抽检记录
   ------------------------------------------------------------ */

function renderInspections(host, data) {
  const summary = data.inspectionSummary || {};
  const passCount = Number(summary.passCount ?? 0);
  const failCount = Number(summary.failCount ?? 0);
  const totalInspected = Number(summary.total ?? passCount + failCount);
  const passRate = totalInspected ? (passCount / totalInspected) * 100 : 0;

  host.append(
    fromHtml(
      statGrid([
        {
          label: '抽检总数',
          value: totalInspected,
          unit: '台',
          icon: 'checkCircle',
          tone: 'brand',
          foot: '本工单已登记的抽检记录',
        },
        {
          label: '合格',
          value: passCount,
          unit: '台',
          icon: 'checkCircle',
          tone: 'success',
          foot: `合格率 ${passRate.toFixed(0)}%`,
        },
        {
          label: '不合格',
          value: failCount,
          unit: '台',
          icon: 'xCircle',
          tone: failCount ? 'coral' : 'default',
          foot: failCount ? '需要按不良代码定位问题批次' : '暂无不合格记录',
        },
      ]),
    ),
  );

  const records = Array.isArray(data.inspections) ? data.inspections : [];
  if (!records.length) {
    host.append(
      fromHtml(
        emptyState({
          icon: 'checkCircle',
          title: '还没有抽检记录',
          desc: '到「抽检确认」页选择本工单并扫描 SN，即可登记合格 / 不合格结果。',
        }),
      ),
    );
    return;
  }

  // 不合格记录未必在首屏：用 descList 明确提示「下面的列表里有没有红字」，
  // 避免用户只看到统计卡就以为全部合格
  if (failCount) {
    host.append(
      fromHtml(
        alert({
          tone: 'danger',
          title: `本单有 ${failCount} 台抽检不合格`,
          text: '请在下表中查看对应 SN 与不良代码，必要时回炉重烧后重新抽检。',
        }),
      ),
    );
  }

  host.append(
    fromHtml(
      table({
        columns: [
          { key: 'sn', title: 'SN', render: (row) => html`<span class="mono text-sm">${row.sn || '—'}</span>` },
          {
            key: 'result',
            title: '结果',
            width: '110px',
            render: (row) => statusTag(row.result, INSPECTION_RESULT_MAP),
          },
          {
            key: 'defect_code',
            title: '不良代码',
            width: '140px',
            render: (row) =>
              row.defectCode
                ? html`<span class="mono text-sm">${row.defectCode}</span>`
                : html`<span class="text-secondary">—</span>`,
          },
          { key: 'note', title: '备注', render: (row) => esc(row.note || '—') },
          { key: 'inspector', title: '抽检人', width: '120px', render: (row) => esc(row.inspector || '—') },
          {
            key: 'inspected_at',
            title: '抽检时间',
            width: '160px',
            render: (row) => esc(formatDate(row.inspectedAt || row.createdAt)),
          },
        ],
        rows: records,
        emptyText: '暂无抽检记录',
      }),
    ),
  );
}

/* ------------------------------------------------------------
   四、动作
   ------------------------------------------------------------ */

/** 出货登记：不可逆 → 必须确认 */
async function shipOrder(order, reload) {
  const { confirmed } = await confirmDialog({
    title: `登记出货：${order.factoryOrderNo || order.id}`,
    description: `本单 ${order.quantity ?? 0} 台将标记为「已出货」，工单进入终态。`,
    detail: '出货后不可撤销。请先确认实物数量与抽检结果均已核对无误。',
    tone: 'warning',
    confirmText: '确认出货',
  });
  if (!confirmed) return;

  const handle = toast.loading('正在登记出货…');
  try {
    await api.post(`/factory/orders/${order.id}/ship`, undefined, {
      idempotencyKey: api.newIdempotencyKey(),
    });
    handle.close();
    toast.success('出货登记完成');
    await reload();
  } catch (error) {
    handle.close();
    notifyFactoryError(error, '出货登记失败');
  }
}

/** 导出本工单二维码清单（CSV，带 BOM，Excel 打开不乱码） */
async function exportQrcodes(order) {
  try {
    const payload = await api.get(`/factory/orders/${order.id}/qrcodes`);
    const records = Array.isArray(payload?.records) ? payload.records : [];
    if (!records.length) {
      toast.warning('本工单还没有可导出的二维码，请先确认平台已生成设备');
      return;
    }
    downloadCsv(`工单-${order.factoryOrderNo || order.id}-二维码.csv`, [
      ['设备ID', 'SN', '联网方式', '二维码内容', '格式'],
      ...records.map((item) => [item.deviceId, item.sn, item.networkType, item.payload, item.format]),
    ]);
    toast.success(`已导出 ${records.length} 条二维码数据`);
  } catch (error) {
    notifyFactoryError(error, '二维码导出失败');
  }
}

/* ------------------------------------------------------------
   页面入口
   ------------------------------------------------------------ */

/**
 * 渲染工单详情页。
 *
 * @param {HTMLElement} container
 * @param {{router: object, params: {id: string}}} ctx
 */
export async function renderFactoryOrderDetail(container, ctx) {
  const { router, params } = ctx;
  const orderId = params?.id;
  if (!orderId) {
    router.go('/orders');
    return;
  }

  let data;
  try {
    data = await loadFactoryOrder(orderId);
  } catch (error) {
    notifyFactoryError(error, '工单不存在或已被删除');
    router.go('/orders');
    return;
  }

  /**
   * 按工单当前状态构建页头动作。
   *
   * 抽成函数的原因：动作是状态相关的，而 `reload()` 会在页面内操作
   * （烧录上报后的状态流转）后重新调用它并交给 setActions 刷新，
   * 否则用户必须手动刷新才能看到「出货登记」。
   *
   * 出货登记的权限：后端工厂端只提供 `factory:burn:write` 与
   * `factory:inspect:write` 两个写权限，没有单独的出货权限码，
   * 因此这里沿用生产写权限 burnWrite 作为门禁。
   */
  const buildActions = (status) => {
    const list = [{ label: '刷新', icon: 'refresh', variant: 'ghost', action: 'reload-detail' }];

    if (BURNABLE_STATUSES.includes(status)) {
      list.push({
        label: '烧录上报',
        variant: 'primary',
        action: 'goto-burn',
        perm: PERM.factory.burnWrite,
      });
    }
    if (INSPECTABLE_STATUSES.includes(status)) {
      list.push({
        label: '抽检确认',
        variant: 'ghost',
        action: 'goto-inspect',
        perm: PERM.factory.inspectWrite,
      });
    }
    if (status === 'COMPLETED') {
      list.push({
        label: '出货登记',
        variant: 'primary',
        action: 'ship-order',
        perm: PERM.factory.burnWrite,
      });
    }
    list.push({ label: '导出二维码', icon: 'download', variant: 'ghost', action: 'export-qrcodes' });
    return list;
  };

  const detail = createDetailPage(container, {
    title: data.factoryOrderNo || `工单 ${orderId}`,
    desc: `平台订单 ${data.orderNo || '—'}　·　${data.productModel || '—'}　·　固件 ${data.firmwareVersion || '—'}　·　数量 ${data.quantity ?? '—'}`,
    backPath: '/orders',
    router,
    actions: buildActions(data.status),
    tabs: TABS,
    activeTab: 'overview',
  });

  const reload = async () => {
    try {
      data = await loadFactoryOrder(orderId);
      // 状态可能已变化，页头动作必须跟着变
      detail.setActions(buildActions(data.status));
      renderTab(detail.activeTab);
    } catch (error) {
      notifyFactoryError(error);
    }
  };

  function renderTab(key) {
    const host = h('div');
    detail.body.replaceChildren(host);

    if (key === 'burns') renderBurns(host, data);
    else if (key === 'inspections') renderInspections(host, data);
    else renderOverview(host, data);
  }

  renderTab('overview');
  container.addEventListener('tabchange', (event) => renderTab(event.detail.tab));

  /* ---- 页头按钮 ----
     绑在 detail.head（本次渲染新建）而不是 container：
     container 由应用壳长期持有，绑在上面会随切页不断叠加监听器。 */
  detail.head.addEventListener('click', async (event) => {
    const target = event.target.closest('[data-action]');
    if (!target) return;
    const { action } = target.dataset;

    if (action === 'reload-detail') {
      await reload();
      toast.info('已刷新');
      return;
    }

    if (action === 'goto-burn') {
      // 用 query 把工单 id 带过去，烧录页落地即预选好本单
      router.go('/burn', { orderId: data.id });
      return;
    }

    if (action === 'goto-inspect') {
      router.go('/inspect', { orderId: data.id });
      return;
    }

    if (action === 'ship-order') {
      await shipOrder(data, reload);
      return;
    }

    if (action === 'export-qrcodes') {
      await exportQrcodes(data);
    }
  });
}

export default { renderFactoryOrderDetail };
