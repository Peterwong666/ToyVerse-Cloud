/* ============================================================
   终端用户端 · 流量充值（仅 4G 设备）
   ------------------------------------------------------------
   真实接口：

     GET  /miniapp/recharge/plans?deviceId=       套餐列表
     POST /miniapp/recharge/orders                下单
     POST /miniapp/recharge/orders/{id}/pay       支付
     GET  /miniapp/recharge/orders?deviceId=      充值记录（分页）

   supported=false 不是错误
   ------------------------
   Wi-Fi 设备走家庭宽带、没有物联卡，后端会返回 supported=false + reason。
   前端据此渲染**空态**而不是报错弹窗：用户并没有做错什么，
   报错只会让人以为平台坏了。只有真正的请求失败才走 notifyError。

   「按钮文案与实际一致」
   ----------------------
   本页只有一个支付按钮，它做两件事：下单 → 支付。所以文案写
   「下单并支付 ¥x」，不写「立即充值」这种含糊说法；支付通道是模拟的
   时候，界面上会明确标注「演示环境……已被直接置为成功」，
   而不是让人以为真的扣了钱。
   ============================================================ */

import { formatDate, formatMoney, fromHtml, h } from '/shared/ui/dom.js';
import { alert, emptyState, loadingState } from '/shared/ui/components.js';
import toast from '/shared/ui/toast.js';
import { deviceName, mpApi, navigate, notifyError, registerScreen, state } from './shell.js';

/** 充值订单状态 → 展示文案（对应后端 RechargeOrderStatus 的四个取值） */
const ORDER_STATUS_LABELS = {
  PENDING: '待支付',
  PAID: '已支付',
  FAILED: '支付失败',
  REFUNDED: '已退款',
};

/** 流量数值展示：≥1024MB 换成 GB */
function dataText(dataMb) {
  const mb = Number(dataMb);
  if (!Number.isFinite(mb) || mb <= 0) return '';
  return mb >= 1024 ? `${(mb / 1024).toFixed(mb % 1024 === 0 ? 0 : 1)} GB` : `${mb} MB`;
}

/** 套餐副标题：优先用后端描述，否则由流量 + 有效期拼出来 */
function planDesc(plan) {
  if (plan?.description) return plan.description;
  const parts = [dataText(plan?.dataMb), plan?.validDays ? `${plan.validDays} 天有效` : ''].filter(Boolean);
  return parts.join(' · ') || '-';
}

registerScreen('recharge', {
  title: '流量充值',
  backTo: 'home',
  render: (body) => {
    const device = state.device;
    const section = h('div', { class: 'mp-section' });

    if (!device?.id) {
      section.append(
        fromHtml(
          emptyState({
            icon: 'device',
            title: '还没有设备',
            desc: '充值需要指定一台设备。请先扫码激活你的玩具。',
          }),
        ),
      );
      const toScan = h('button', { class: 'mp-btn mt-4', type: 'button', text: '去扫码' });
      toScan.addEventListener('click', () => navigate('scan'));
      section.append(toScan);
      body.append(section);
      return;
    }

    const planHost = h('div');
    const recordHost = h('div');
    section.append(planHost, recordHost);

    /* ---- 选中态与下单 ---- */
    let selected = null;
    let paying = false;

    const payBtn = h('button', {
      class: 'mp-btn',
      type: 'button',
      text: '请先选择套餐',
      style: { marginTop: 'var(--space-4)' },
    });
    payBtn.disabled = true;

    function paintPayButton() {
      if (!selected) {
        payBtn.disabled = true;
        payBtn.textContent = '请先选择套餐';
        return;
      }
      payBtn.disabled = paying;
      payBtn.textContent = paying ? '支付中…' : `下单并支付 ${formatMoney(selected.price)}`;
    }

    /* ---- 充值记录 ---- */
    let recordPage = 1;
    const recordList = h('div', { style: { display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' } });
    const moreBtn = h('button', {
      class: 'mp-btn mp-btn-outline mt-3',
      type: 'button',
      text: '加载更多记录',
    });
    moreBtn.style.display = 'none';

    function paintRecords(payload, { append = false } = {}) {
      const records = Array.isArray(payload?.records) ? payload.records : [];
      const total = Number(payload?.total ?? records.length);

      if (!append) recordList.replaceChildren();

      if (!records.length && !append) {
        recordList.append(
          fromHtml(
            emptyState({
              icon: 'clipboard',
              title: '还没有充值记录',
              desc: '给这台设备购买套餐后，记录会显示在这里。',
            }),
          ),
        );
        moreBtn.style.display = 'none';
        return;
      }

      records.forEach((order) => {
        const status = order?.status || '';
        const quota = [dataText(order?.dataMb), order?.validDays ? `${order.validDays} 天有效` : '']
          .filter(Boolean)
          .join(' · ');

        const row = h(
          'div',
          { class: 'mp-card' },
          h(
            'div',
            { class: 'flex-between' },
            h('span', { class: 'font-semibold', text: order?.planName || order?.code || '流量套餐' }),
            h('span', { class: 'font-semibold', text: formatMoney(order?.amount ?? order?.price) }),
          ),
          h('div', {
            class: 'text-xs text-secondary mt-1',
            text: `${ORDER_STATUS_LABELS[status] || status || '-'}${quota ? `　${quota}` : ''}　${
              order?.createdAt ? formatDate(order.createdAt) : ''
            }`,
          }),
          order?.orderNo
            ? h('div', { class: 'text-xs text-secondary mono', text: `订单号 ${order.orderNo}` })
            : null,
          order?.failedReason
            ? h('div', { class: 'text-xs text-danger mt-1', text: `失败原因：${order.failedReason}` })
            : null,
        );
        recordList.append(row);
      });

      // 记录是分页的：还有剩余就给出「加载更多」，不静默截断历史
      const loaded = recordList.children.length;
      moreBtn.style.display = loaded < total ? '' : 'none';
      moreBtn.textContent = `加载更多记录（已显示 ${loaded} / ${total}）`;
    }

    async function loadRecords({ append = false } = {}) {
      try {
        const payload = await mpApi.get('/miniapp/recharge/orders', {
          params: { deviceId: device.id, page: recordPage, pageSize: 10 },
        });
        paintRecords(payload, { append });
      } catch (error) {
        if (!append) {
          recordList.replaceChildren(
            fromHtml(alert({ tone: 'danger', text: error?.message || '充值记录加载失败' })),
          );
        } else {
          notifyError(error, '充值记录加载失败');
        }
      }
    }

    moreBtn.addEventListener('click', () => {
      recordPage += 1;
      loadRecords({ append: true });
    });

    /* ---- 套餐列表 ---- */
    async function loadPlans() {
      planHost.replaceChildren(fromHtml(loadingState('正在获取套餐…')));

      try {
        const payload = await mpApi.get('/miniapp/recharge/plans', { params: { deviceId: device.id } });
        planHost.replaceChildren();

        /* supported=false：Wi-Fi 设备 / 未开通充值 —— 正常状态，给空态 */
        if (!payload?.supported) {
          planHost.append(
            fromHtml(
              emptyState({
                icon: 'wifi',
                title: '当前设备无需充值',
                desc: payload?.reason || '该设备不消耗流量，因此没有可购买的套餐。',
              }),
            ),
          );
          return;
        }

        const plans = Array.isArray(payload?.records) ? payload.records : [];
        if (!plans.length) {
          planHost.append(
            fromHtml(
              emptyState({
                icon: 'package',
                title: '暂无可购买套餐',
                desc: '平台的流量套餐尚未配置，请稍后再来。',
              }),
            ),
          );
          return;
        }

        planHost.append(
          h('div', { class: 'mp-section-title', text: `为 ${deviceName(device)} 选择套餐` }),
        );

        const list = h('div', { style: { display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' } });

        plans.forEach((plan) => {
          const node = h(
            'div',
            { class: `mp-plan ${plan?.isRecommended ? 'mp-plan-popular' : ''}` },
            h(
              'div',
              {},
              h('div', { class: 'mp-plan-name', text: plan?.name || plan?.code || '流量套餐' }),
              h('div', { class: 'mp-plan-desc', text: planDesc(plan) }),
            ),
            h('div', { class: 'mp-plan-price' }, h('small', { text: '¥' }), String(plan?.price ?? '-')),
          );

          node.addEventListener('click', () => {
            list.querySelectorAll('.mp-plan').forEach((el) => el.classList.remove('selected'));
            node.classList.add('selected');
            selected = plan;
            paintPayButton();
          });

          list.append(node);
        });

        planHost.append(list, payBtn);

        // 默认标记推荐套餐（仅高亮，不自动下单）
        const recommended = plans.find((plan) => plan?.isRecommended);
        if (recommended) {
          const index = plans.indexOf(recommended);
          list.children[index]?.classList.add('selected');
          selected = recommended;
          paintPayButton();
          toast.info(`已为你选中推荐套餐：${recommended.name || recommended.code || ''}`);
        }
      } catch (error) {
        planHost.replaceChildren();
        planHost.append(
          fromHtml(
            alert({
              tone: 'danger',
              title: '套餐获取失败',
              text: error?.message || '请检查网络后重试',
            }),
          ),
        );
        const retry = h('button', { class: 'mp-btn mp-btn-outline mt-4', type: 'button', text: '重新获取' });
        retry.addEventListener('click', loadPlans);
        planHost.append(retry);
      }
    }

    /* ---- 下单 + 支付 ---- */
    payBtn.addEventListener('click', async () => {
      if (!selected || paying) return;
      paying = true;
      paintPayButton();

      try {
        const order = await mpApi.post('/miniapp/recharge/orders', {
          deviceId: device.id,
          planId: selected.id,
        });

        const paid = await mpApi.post(`/miniapp/recharge/orders/${order.id}/pay`);
        const paidOrder = paid?.order || order;

        recordHost.replaceChildren();
        recordHost.append(
          fromHtml(`<div class="mt-4">${alert({
            tone: 'success',
            title: '充值成功',
            text: `已为 ${deviceName(device)} 开通「${selected.name || selected.code || '流量套餐'}」。`,
          })}</div>`),
        );

        /* mock=true：支付通道是模拟的，必须如实标注，别让人以为真扣款了 */
        if (paid?.mock) {
          recordHost.append(
            fromHtml(`<div class="mt-3">${alert({
              tone: 'info',
              title: '演示环境：支付通道为模拟，已被直接置为成功',
              text: `订单 ${paidOrder?.orderNo || order.id} 未发生真实扣款。接入真实支付渠道后，这里会返回支付渠道的流水号与结果。`,
            })}</div>`),
          );
        }

        toast.success('充值成功');
        // 重新拉记录：以服务端落库结果为准，不用本地拼一条
        recordPage = 1;
        await loadRecords();
      } catch (error) {
        notifyError(error, '充值失败');
      } finally {
        paying = false;
        paintPayButton();
      }
    });

    /* ---- 记录区标题 ---- */
    recordHost.append(h('div', { class: 'mp-section-title mt-4', text: '充值记录' }));
    recordHost.append(recordList, moreBtn);

    body.append(section);

    // 先套餐后记录：两个请求互不依赖，不需要串行等待
    loadPlans();
    loadRecords();
  },
});
