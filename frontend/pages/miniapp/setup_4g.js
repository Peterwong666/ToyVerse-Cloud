/* ============================================================
   终端用户端 · 4G 开机激活（集贤方案）
   ------------------------------------------------------------
   真实流程（对应 POST /miniapp/devices/{id}/activate-4g）：

     用户给玩具上电 → 设备通过 4G 自动上线 → 平台调集贤接口激活 →
     返回 { deviceId, sn, networkType, activationStatus, bindStatus,
            activatedAt, vendorMessage, mock }

   为什么这一步必须由后端发起
   --------------------------
   激活要带厂商密钥（集贤的 appKey/appSecret），密钥只在服务端；
   设备端能自报的只有 SN/IMEI/ICCID。让前端「看起来激活成功了」
   是典型的假交付，所以成功判定完全以接口返回为准。

   厂商密钥未配置时后端返回 503 VENDOR_UNAVAILABLE（安全失败），
   本页必须把这条讲清楚：能力被禁用，而不是「重试一下就好」。

   重复激活是幂等的：已激活的设备再点一次也会返回 ACTIVATED，
   不额外报错（用户不该因为多按了一次看到红色弹窗）。
   ============================================================ */

import { fromHtml, h } from '/shared/ui/dom.js';
import { alert, descList, emptyState } from '/shared/ui/components.js';
import toast from '/shared/ui/toast.js';
import { networkLabel, mpApi, navigate, notifyError, registerScreen, setCurrentDevice, state } from './shell.js';

registerScreen('setup_4g', {
  title: '开机激活',
  backTo: 'scan',
  render: (body) => {
    const pending = state.pendingDevice;
    const section = h('div', { class: 'mp-section' });

    /* 直接进本屏（未经过扫码）时不该假装有设备：给出去扫码的出口 */
    if (!pending?.id) {
      section.append(
        fromHtml(
          emptyState({
            icon: 'qrcode',
            title: '还没有待激活的设备',
            desc: '请先扫描玩具底部的二维码，识别出设备后再开机激活。',
          }),
        ),
      );
      const toScan = h('button', { class: 'mp-btn mt-4', type: 'button', text: '去扫码' });
      toScan.addEventListener('click', () => navigate('scan'));
      section.append(toScan);
      body.append(section);
      return;
    }

    const resultHost = h('div', { class: 'mb-3' });

    /* ---- 步骤说明（沿用 P2 的视觉：圆环数字 + 标题 + 说明） ---- */
    const stepCard = h(
      'div',
      { class: 'mp-card', style: { textAlign: 'center' } },
      h('div', { class: 'mp-step-ring', style: { margin: '0 auto var(--space-4)' }, text: '1' }),
      h('div', { class: 'mp-section-title', style: { justifyContent: 'center' }, text: '请打开玩具电源' }),
      h('div', {
        class: 'text-xs text-secondary',
        text: '设备开机后将通过 4G 网络自动上线，平台随后调用集贤接口完成激活',
      }),
    );

    /* ---- 设备信息：IMEI / 固件要等设备详情拉回来才有，先落占位 ---- */
    const infoCard = h('div', { class: 'mp-card mt-4' });
    const infoHost = h('div');
    infoCard.append(h('div', { class: 'mp-section-title', text: '待激活设备' }), infoHost);

    function paintInfo(extra = {}) {
      const merged = { ...pending, ...extra };
      infoHost.replaceChildren(
        fromHtml(
          descList(
            [
              ['设备 SN', merged.sn],
              ['IMEI', merged.imei],
              ['设备型号', merged.productName],
              ['固件版本', merged.firmwareVersion],
              ['归属商户', merged.tenantName],
              ['云服务商', merged.cloudVendorLabel],
            ],
            { cols: 2 },
          ),
        ),
      );
    }
    paintInfo();

    /* 详情接口可能在 P8 后端尚未就绪时 404 —— 静默容错：
       IMEI/固件属于「有更好、没有也能继续」的信息，不该拦住激活。 */
    (async () => {
      try {
        const detail = await mpApi.get(`/miniapp/devices/${pending.id}`);
        paintInfo({
          // 详情只补齐扫码结果里缺的字段（?? 而不是覆盖：扫码结果更权威）
          imei: detail?.imei ?? pending.imei,
          productName: detail?.productName || pending.productName,
          firmwareVersion: detail?.firmwareVersion ?? pending.firmwareVersion,
          tenantName: detail?.tenantName || pending.tenantName,
          networkType: detail?.networkType || pending.networkType,
        });
      } catch {
        /* 拉不到详情就保留扫码结果，不打扰用户 */
      }
    })();

    const activateBtn = h('button', { class: 'mp-btn mt-4', type: 'button', text: '我已完成开机，开始激活' });

    activateBtn.addEventListener('click', async () => {
      activateBtn.disabled = true;
      activateBtn.textContent = '激活中…';
      resultHost.replaceChildren();

      try {
        const result = await mpApi.post(`/miniapp/devices/${pending.id}/activate-4g`);

        state.lastActivation = result;
        setCurrentDevice({
          id: result?.deviceId || pending.id,
          sn: result?.sn || pending.sn,
          networkType: result?.networkType || '4G',
          activationStatus: result?.activationStatus,
          bindStatus: result?.bindStatus,
          productName: pending.productName,
          tenantName: pending.tenantName,
          online: false,
        });

        // 幂等：已激活的设备再次激活仍返回 ACTIVATED，同样算成功
        if (result?.activationStatus === 'ACTIVATED' || result?.bindStatus === 'BOUND') {
          toast.success(
            result?.activationStatus === 'ACTIVATED' && result?.bindStatus === 'BOUND'
              ? '设备已激活并绑定'
              : '激活成功',
          );
          navigate('activate_done');
          return;
        }

        // 后端有返回但状态不是「已激活」：如实展示，不自行判定成功
        resultHost.append(
          fromHtml(
            alert({
              tone: 'warning',
              title: '激活未完成',
              text: `厂商返回：${result?.vendorMessage || '状态未知'}（当前激活状态 ${
                result?.activationStatus || '-'
              }）。可稍后重试；若持续失败请联系客服。`,
            }),
          ),
        );
      } catch (error) {
        if (error?.code === 'VENDOR_UNAVAILABLE') {
          resultHost.replaceChildren(
            fromHtml(
              alert({
                tone: 'warning',
                title: '厂商密钥未配置，激活能力已安全禁用（不会伪造成功）',
                text:
                  '平台未配置集贤 4G 云服务的密钥，无法调用厂商接口。' +
                  '此时**不会**把设备标记为已激活——设备状态保持原样，配置密钥后重试即可。' +
                  '请联系平台管理员。',
              }),
            ),
          );
          return;
        }

        if (error?.code === 'INVALID_STATE_TRANSITION' || error?.code === 'DEVICE_ALREADY_BOUND') {
          resultHost.replaceChildren(
            fromHtml(
              alert({
                tone: 'info',
                title: '设备当前状态无需再次激活',
                text: error?.message || '该设备可能已被激活或已绑定账号。',
              }),
            ),
          );
          return;
        }

        notifyError(error, '激活失败');
      } finally {
        activateBtn.disabled = false;
        activateBtn.textContent = '我已完成开机，开始激活';
      }
    });

    section.append(
      resultHost,
      stepCard,
      infoCard,
      activateBtn,
      fromHtml(
        `<div class="mt-4">${alert({
          tone: 'neutral',
          title: '这条路径在做什么',
          text: `集贤 4G 方案：二维码格式为 JX|SN|IMEI|ICCID|deviceId。联网方式 ${networkLabel(
            pending.networkType,
          )}，激活由平台携带厂商密钥调用集贤接口完成，成功与否以厂商返回为准。`,
        })}</div>`,
      ),
    );

    body.append(section);
  },
});
