/* ============================================================
   工厂端 · 烧录上报
   ------------------------------------------------------------
   对应后端 `POST /factory/orders/{id}/burn`：
     body { burnedCount, snFrom?, snTo?, note? } → 返回工单详情

   两个容易被忽略、但决定好不好用的点
   ----------------------------------
   1. 数量默认填 `remaining`：产线多数场景是「一次把剩下的全烧完」，
      默认值给 remaining 比给 1 少一次输入，也天然不会超出上限。
   2. 超限（400 BURN_COUNT_EXCEEDED）时必须把 details.remaining
      说出来。后端已经在错误详情里给了剩余台数，只回一句「数量超限」
      等于让用户自己去算 —— 这里把「本单还剩 N 台」直接写进提示，
      并把数量输入框改成剩余值，用户改一个字就能重提。

   为什么是独立页面而不是工单详情里的弹窗
   ------------------------------------
   一轮烧录要填数量 + SN 区间 + 备注，弹窗里放不下也不便来回核对
   进度；独立页面有位置同时展示「当前进度」与「历次上报记录」，
   提交后立刻能看到新进度与新增的那一条记录。
   ============================================================ */

import api from '/shared/core/api.js';
import { clear, esc, formatDate, fromHtml, h, html } from '/shared/ui/dom.js';
import { alert, emptyState, progress, statGrid } from '/shared/ui/components.js';
import { table } from '/shared/ui/table.js';
import { form, readAndValidate } from '/shared/ui/form.js';
import { renderPageHead } from '/shared/app/page.js';
import toast from '/shared/ui/toast.js';
import {
  BURNABLE_STATUSES,
  FACTORY_ORDER_STATUS_MAP,
  fetchFactoryOrdersByStatus,
  factoryOrderOptionLabel,
  loadFactoryOrder,
  notifyFactoryError,
} from './orders.js';

/** 表单字段定义（orderId 的 options 在运行时注入） */
function buildFields(orderOptions) {
  return [
    {
      key: 'orderId',
      label: '生产工单',
      type: 'select',
      required: true,
      span: 2,
      options: orderOptions,
      hint: '只列出「待生产 / 生产中」的工单；已完成或已出货的工单不再出现在这里。',
    },
    {
      key: 'burnedCount',
      label: '本次烧录数量',
      type: 'number',
      required: true,
      min: 1,
      span: 1,
      hint: '默认填本单剩余台数；超过剩余数量后端会拒绝。',
    },
    {
      key: 'snFrom',
      label: 'SN 起始（选填）',
      type: 'text',
      maxLength: 64,
      span: 1,
      placeholder: '如 SN2026090001',
      hint: '本批烧录的 SN 区间，便于日后追溯。',
    },
    {
      key: 'snTo',
      label: 'SN 结束（选填）',
      type: 'text',
      maxLength: 64,
      span: 1,
      placeholder: '如 SN2026090100',
    },
    {
      key: 'note', label: '备注', type: 'textarea', maxLength: 500, span: 2, placeholder: '如：本批使用 3 号线烧录台' },
  ];
}

/* ------------------------------------------------------------
   页面
   ------------------------------------------------------------ */

/**
 * 渲染烧录上报页。
 *
 * @param {HTMLElement} container
 * @param {{router: object, query: object}} ctx
 */
export async function renderBurn(container, ctx) {
  const { query } = ctx;

  /* ---- 前置：拉取可上报的工单 ---- */
  let orders = [];
  try {
    orders = await fetchFactoryOrdersByStatus(BURNABLE_STATUSES);
  } catch (error) {
    orders = [];
  }

  clear(container);
  const root = h('div', { class: 'page-body' });
  container.append(root);

  renderPageHead(root, {
    title: '烧录上报',
    desc: '按批次上报烧录数量，累计数量达标后工单自动转为「烧录完成」。上报数量不得超过本单剩余台数。',
  });

  if (!orders.length) {
    const host = h('div', { class: 'card' });
    host.append(
      fromHtml(
        emptyState({
          icon: 'fire',
          title: '当前没有可上报的工单',
          desc: '只有「待生产 / 生产中」的工单可以上报。若确认有工单，请检查平台是否已派单，或到「生产订单」页刷新后重试。',
        }),
      ),
    );
    root.append(host);
    return;
  }

  /* ---- 工单下拉 + 表单 ---- */
  const fields = buildFields(
    orders.map((row) => ({ value: String(row.id), label: factoryOrderOptionLabel(row) })),
  );

  const summaryHost = h('div', { class: 'mb-4' });
  const noticeHost = h('div', { class: 'mb-3' });

  const formEl = fromHtml(
    form({
      fields,
      values: { orderId: query?.orderId ? String(query.orderId) : String(orders[0].id) },
      columns: 2,
      id: 'burnForm',
    }),
  );

  const selectEl = formEl.querySelector('[name="orderId"]');
  const countInput = formEl.querySelector('[name="burnedCount"]');
  // 动态的数量上限提示：随所选工单变化，比写死在 field-hint 里更有用
  const countHint = h('div', { class: 'field-hint' });
  formEl.querySelector('[data-field-wrap="burnedCount"]')?.append(countHint);

  const submitBtn = h('button', { class: 'btn btn-primary', type: 'button', text: '提交烧录上报' });
  const submitBar = h('div', { class: 'btn-group mt-4' }, submitBtn);

  const formCard = h('div', { class: 'card mb-4' });
  formCard.append(fromHtml(`<div class="card-head"><div class="card-title">上报表单</div></div>`));
  formCard.append(noticeHost, formEl, submitBar);
  root.append(summaryHost, formCard);

  /* ---- 最近上报记录 ---- */
  const recordsHost = h('div', { class: 'card' });
  recordsHost.append(fromHtml(`<div class="card-head"><div class="card-title">本单上报记录</div></div>`));
  root.append(recordsHost);

  /** 当前工单（用于提交时取 remaining 等信息做前端提示） */
  let current = null;

  const paintSummary = (order) => {
    current = order;
    const total = Number(order.quantity ?? 0);
    const burned = Number(order.burnedCount ?? 0);
    const remaining = Number(order.remaining ?? Math.max(0, total - burned));
    const percent = Number(order.progressPercent ?? (total ? (burned / total) * 100 : 0));

    summaryHost.replaceChildren(
      fromHtml(
        statGrid([
          {
            label: '需求数量',
            value: total,
            unit: '台',
            icon: 'package',
            tone: 'brand',
            foot: `工单 ${order.factoryOrderNo || order.id}`,
          },
          {
            label: '已烧录',
            value: burned,
            unit: '台',
            icon: 'fire',
            tone: 'accent',
            foot: `固件 ${order.firmwareVersion || '—'}`,
          },
          {
            label: '剩余',
            value: remaining,
            unit: '台',
            icon: 'clipboard',
            tone: remaining ? 'warning' : 'success',
            foot: remaining ? '本次上报的上限' : '本单已烧录达标',
          },
          {
            label: '状态 / 进度',
            value: `${percent.toFixed(0)}%`,
            icon: 'chartBar',
            tone: order.status === 'COMPLETED' ? 'success' : 'teal',
            foot: FACTORY_ORDER_STATUS_MAP[order.status]?.text || order.status,
          },
        ]),
      ),
    );

    const bar = h('div', { class: 'card mt-3' });
    bar.append(
      fromHtml(
        html`<div class="flex-between mb-2">
          <span class="text-sm text-secondary">烧录进度</span>
          <span class="text-sm num">${burned} / ${total}</span>
        </div>`,
      ),
    );
    bar.append(fromHtml(progress(percent, { tone: remaining ? 'brand' : 'success' })));
    if (order.productionNote) {
      bar.append(
        fromHtml(
          alert({ tone: 'neutral', title: '生产要求', text: order.productionNote }),
        ),
      );
    }
    summaryHost.append(bar);

    countHint.textContent = `本单还剩 ${remaining} 台（需求量 ${total}，已烧录 ${burned}）。`;
    // 默认把数量填成剩余值：一次烧完是产线最常见的情形
    countInput.value = remaining > 0 ? String(remaining) : '';
    countInput.max = String(Math.max(remaining, 1));
  };

  const paintRecords = (order) => {
    const records = Array.isArray(order.burnReports) ? order.burnReports : [];
    const head = recordsHost.firstElementChild;
    recordsHost.replaceChildren(head);
    if (!records.length) {
      recordsHost.append(
        fromHtml(
          emptyState({
            icon: 'activity',
            title: '还没有上报记录',
            desc: '提交成功后，本单的每一批上报都会按时间倒序列在这里。',
          }),
        ),
      );
      return;
    }
    recordsHost.append(
      fromHtml(
        table({
          columns: [
            {
              key: 'burned_count',
              title: '本次上报',
              align: 'num',
              width: '110px',
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
          emptyText: '暂无上报记录',
        }),
      ),
    );
  };

  /** 切换工单：拉详情以拿到最新的 remaining 与上报记录 */
  const selectOrder = async (orderId) => {
    if (!orderId) {
      summaryHost.replaceChildren();
      return;
    }
    noticeHost.replaceChildren();
    try {
      const order = await loadFactoryOrder(orderId);
      paintSummary(order);
      paintRecords(order);
    } catch (error) {
      // 详情取不到时不阻塞填表：提示后仍允许提交（提交会给出真正的错误）
      notifyFactoryError(error, '工单详情读取失败');
    }
  };

  if (selectEl) {
    selectEl.addEventListener('change', () => selectOrder(selectEl.value));
  }

  /* ---- 提交 ---- */
  const submit = async () => {
    const { ok, values } = readAndValidate(formEl, fields);
    if (!ok) {
      toast.warning('请检查表单填写');
      return;
    }
    if (!values.orderId) {
      toast.warning('请先选择生产工单');
      return;
    }

    // 前端先拦一次：能在这里说清楚就不让用户去换一个 400
    if (current && values.burnedCount > Number(current.remaining ?? 0)) {
      noticeHost.replaceChildren(
        fromHtml(
          alert({
            tone: 'danger',
            title: '上报数量超过剩余数量',
            text: `本单还剩 ${current.remaining ?? 0} 台（需求量 ${current.quantity ?? '—'}，已烧录 ${current.burnedCount ?? 0}）。请把数量改为不超过剩余台数。`,
          }),
        ),
      );
      toast.warning(`本单还剩 ${current.remaining ?? 0} 台，请调整数量`);
      countInput.focus();
      return;
    }

    submitBtn.disabled = true;
    submitBtn.textContent = '提交中…';
    noticeHost.replaceChildren();

    try {
      const order = await api.post(
        `/factory/orders/${values.orderId}/burn`,
        {
          burnedCount: values.burnedCount,
          // 空串转 null：后端把缺省字段视为「未填写」，传 '' 会被当成非法值
          snFrom: values.snFrom || null,
          snTo: values.snTo || null,
          note: values.note || null,
        },
        { idempotencyKey: api.newIdempotencyKey() },
      );

      paintSummary(order);
      paintRecords(order);

      const percent = Number(order.progressPercent ?? 0);
      if (order.status === 'COMPLETED') {
        toast.success(`已上报 ${values.burnedCount} 台，本单烧录完成（100%），可进行出货登记`);
      } else {
        toast.success(`已上报 ${values.burnedCount} 台，当前进度 ${percent.toFixed(0)}%`);
      }
    } catch (error) {
      const details = error?.details || {};
      if (error?.code === 'BURN_COUNT_EXCEEDED') {
        // 把后端给的 remaining 显式说出来，并把输入框改成合法值
        const remaining = details.remaining ?? current?.remaining ?? 0;
        noticeHost.replaceChildren(
          fromHtml(
            alert({
              tone: 'danger',
              title: '上报数量超过剩余数量',
              text: `本单还剩 ${remaining} 台（需求量 ${details.quantity ?? current?.quantity ?? '—'}，已烧录 ${details.currentBurnedCount ?? current?.burnedCount ?? '—'}）。已把数量改为剩余台数，请确认后重新提交。`,
            }),
          ),
        );
        countInput.value = String(remaining);
        countHint.textContent = `本单还剩 ${remaining} 台。`;
      }
      notifyFactoryError(error, '烧录上报失败');
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = '提交烧录上报';
    }
  };

  submitBtn.addEventListener('click', submit);

  await selectOrder(selectEl?.value);
}

export default { renderBurn };
