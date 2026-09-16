/* ============================================================
   终端用户端 · 激活完成
   ------------------------------------------------------------
   展示激活结果（来自 state.lastActivation，即后端返回的激活响应）：

     { deviceId, sn, networkType, activationStatus, bindStatus,
       activatedAt, vendorMessage, mock }

   为什么要显示 vendorMessage 与 mock 标记
   ---------------------------------------
   这一屏是全流程里唯一「可以向用户宣布成功」的地方。
   如果只显示一个大对勾，就没法区分：

     * 厂商真实返回成功；
     * 厂商通道是模拟实现（mock=true）——演示/离线环境；
     * 厂商返回了别的话（vendorMessage，例如「套餐待生效」）。

   把这三者原样摆出来，是对「不伪造成功」这条纪律的延伸：
   界面上写的成功，必须和后端拿到的东西一致。
   ============================================================ */

import { fromHtml, h } from '/shared/ui/dom.js';
import { alert, descList, tag } from '/shared/ui/components.js';
import { deviceName, navigate, networkLabel, registerScreen, state } from './shell.js';
import { icon } from '/shared/ui/icons.js';

registerScreen('activate_done', {
  title: '',
  render: (body) => {
    const activation = state.lastActivation;
    const device = state.device;

    const stage = h('div', { class: 'mp-scan-stage' });

    /* ---- 成功勾选（沿用 P2 的视觉，图标用 DOM 构造避免 innerHTML 拼接） ---- */
    const checkMark = h('div', { class: 'mp-success-check' });
    const checkTpl = document.createElement('template');
    checkTpl.innerHTML = icon('check', { size: 34 }).trim();
    checkMark.append(checkTpl.content);

    const sn = activation?.sn || device?.sn || '-';
    const networkType = activation?.networkType || device?.networkType || '';

    stage.append(
      checkMark,
      h(
        'div',
        {},
        h('div', { class: 'text-lg font-semibold text-primary', text: '激活成功' }),
        h('div', {
          class: 'text-xs text-secondary mt-1',
          text: activation?.bindStatus === 'BOUND' ? '设备已绑定到你的账号，可以开始对话了' : '设备已激活，可以开始对话了',
        }),
      ),
    );

    /* ---- 激活详情 ---- */
    const detailCard = h('div', { class: 'mp-card', style: { width: '100%', textAlign: 'left' } });
    detailCard.append(
      fromHtml(
        descList(
          [
            ['设备名称', deviceName(device, activation ? '智能玩具' : '-')],
            ['设备 SN', sn],
            ['联网方式', networkLabel(networkType)],
            ['激活时间', activation?.activatedAt],
            ['厂商返回', activation?.vendorMessage],
          ],
          { cols: 1 },
        ),
      ),
    );

    /* mock=true：厂商密钥未配置时走的是模拟通道，界面上必须标注，
       否则演示者会把「模拟成功」当成「真机已激活」。 */
    if (activation?.mock) {
      detailCard.append(
        fromHtml(
          `<div class="mt-3">${tag('演示环境：厂商通道为模拟实现', 'warning')}</div>`,
        ),
      );
    }
    stage.append(detailCard);

    /* ---- 进入首页 ---- */
    const homeBtn = h('button', {
      class: 'mp-btn',
      type: 'button',
      text: '进入首页',
      style: { maxWidth: '240px' },
    });
    homeBtn.addEventListener('click', () => navigate('home'));
    stage.append(homeBtn);

    if (!activation) {
      // 直接进本屏（例如刷新后靠缓存回到这里）：说明数据来源，别让人以为刚激活成功
      stage.append(
        fromHtml(
          alert({
            tone: 'neutral',
            text: '本次会话没有可回显的激活结果——你可能是直接进入了本页。设备状态以首页展示的实时数据为准。',
          }),
        ),
      );
    } else if (activation.activationStatus && activation.activationStatus !== 'ACTIVATED') {
      stage.append(
        fromHtml(
          alert({
            tone: 'warning',
            title: '激活状态并非「已激活」',
            text: `后端返回的激活状态是 ${activation.activationStatus}，请以首页显示的状态为准，必要时联系客服。`,
          }),
        ),
      );
    }

    body.append(stage);
  },
});
