/* ============================================================
   终端用户端 · 扫码识别
   ------------------------------------------------------------
   真实流程（对应 POST /miniapp/scan/resolve）：

     用户扫玩具底部二维码 → 平台解析载荷 → 判定这台设备
     「能不能绑」以及「该走哪条激活路径」→ 前端据此路由：

       bindable + 4G   → 4G 开机激活（集贤）
       bindable + WIFI → Wi-Fi 配网激活（京东 JoyInside）
       已激活/已绑定    → 直接进首页（重复激活幂等，不报错）
       不可绑定         → 页内说明原因（**不是错误**）

   为什么「不可绑定」不弹错误框
   ----------------------------
   bindable=false 是业务上的正常状态（已激活、已绑到别人账号、
   设备未入库……），后端为此专门给了 reason 字段。把它做成红色
   错误弹窗会让家长以为「玩具坏了」。只有 QR_INVALID /
   DEVICE_NOT_FOUND 这类才是真错误——那会走到 notifyError。

   H5 没有真实摄像头扫码能力（零构建、无依赖），因此本页提供
   「粘贴载荷」入口；演示按钮填入的是**种子数据里的真实设备**
   （SN-DEMO-4G-001 / SN-DEMO-WIFI-001），让激活两条路径都能走通。
   二维码图形用 shared/ui/qrcode.js 渲染，仅作装饰。
   ============================================================ */

import { fromHtml, h } from '/shared/ui/dom.js';
import { alert, descList } from '/shared/ui/components.js';
import { hydrateQr, qrHtml } from '/shared/ui/qrcode.js';
import toast from '/shared/ui/toast.js';
import { mpApi, notifyError, registerScreen, setCurrentDevice, state, navigate, networkLabel } from './shell.js';

/** 激活状态展示文案（仅本页用，故不放进 shell） */
const ACTIVATION_LABELS = {
  NOT_ACTIVATED: '未激活',
  ACTIVATING: '激活中',
  ACTIVATED: '已激活',
  BIND_FAILED: '激活校验失败（SN 不一致）',
};

/** 二维码格式说明 */
const FORMAT_LABELS = {
  JX: 'JX（集贤 4G 方案）',
  JD: 'JD（京东 Wi-Fi 方案）',
};

/* ------------------------------------------------------------
   演示载荷：来自后端种子数据 backend/app/db/seed.py
   ------------------------------------------------------------ */

/**
 * 4G 演示载荷：JX|{SN}|{IMEI}|{ICCID}|{deviceId}
 *
 * 第 5 段（厂商设备 ID）在本地首次生成时可能为空，种子数据里已有值，
 * 这里照抄种子数据，保证「演示按钮」解析出的就是那台待激活设备。
 */
const DEMO_JX_PAYLOAD = 'JX|SN-DEMO-4G-001|866000000000001|8986000000000000001|jx-demo-device-001';

/**
 * Wi-Fi 演示载荷：JD|{tenantId}|{productId}|{sn}|{sign}
 *
 * 最后一段 sign 是 HMAC-SHA256("{tenantId}|{productId}|{sn}", QR_SIGN_SECRET)，
 * 密钥只在服务端（见 app/services/qrcode_service.py）——**前端算不出来**，
 * 硬编码到前端更是把签名密钥送出去。因此这里留空，并在界面上明确
 * 告诉用户去哪里复制完整载荷（平台端订单的「二维码 / SN」导出）。
 */
const DEMO_JD_PREFIX = 'JD|t-001|prod-t001-cube|SN-DEMO-WIFI-001|';

/* ------------------------------------------------------------
   屏幕
   ------------------------------------------------------------ */

registerScreen('scan', {
  title: '',
  hero: true,
  render: (body) => {
    const stage = h('div', { class: 'mp-scan-stage' });

    /* ---- 取景框：内嵌一张真实可扫的示例二维码（装饰） ---- */
    const scanner = h(
      'div',
      { class: 'mp-scanner' },
      h('span', { class: 'mp-scanner-corner tl' }),
      h('span', { class: 'mp-scanner-corner tr' }),
      h('span', { class: 'mp-scanner-corner bl' }),
      h('span', { class: 'mp-scanner-corner br' }),
      h('span', { class: 'mp-scanner-line' }),
    );

    // 二维码只是「看起来像个扫码页」的装饰：真正的识别入口是下方的粘贴框，
    // 因此这里垫一层绝对定位容器把它居中，且不影响扫描线/四角标识的层级。
    const qrHolder = h(
      'div',
      {
        style: {
          position: 'absolute',
          inset: '0',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        },
      },
      fromHtml(qrHtml(DEMO_JX_PAYLOAD, { size: 160 })),
    );
    scanner.append(qrHolder);

    stage.append(
      scanner,
      h(
        'div',
        {},
        h('div', { class: 'mp-hero-title', text: '扫描玩具底部的二维码' }),
        h('div', {
          class: 'mp-hero-sub',
          text: '扫码后将自动识别产品与网络方式，并引导完成激活',
        }),
      ),
      h('div', {
        class: 'text-xs',
        style: { color: 'rgba(255,255,255,.7)', maxWidth: '260px' },
        text: '二维码承载设备身份信息（集贤 4G 方案 / 京东 Wi-Fi 方案格式不同）',
      }),
    );

    /* ---- 结果区（识别后回显，不跳走的场景留在这里说明原因） ---- */
    const resultHost = h('div', { style: { width: '100%', maxWidth: '300px' } });

    /* ---- 手动粘贴入口：H5 无摄像头扫码，这是**真实可用**的识别入口 ---- */
    const payloadInput = h('textarea', {
      class: 'textarea',
      rows: '3',
      placeholder: '把二维码内容粘贴到这里（JX|… 或 JD|…）',
      'aria-label': '二维码内容',
    });

    const resolveBtn = h('button', { class: 'mp-btn', type: 'button', text: '识别二维码' });

    /* ---- 演示按钮（载荷来自种子数据，不是编造的假数据） ---- */
    const demo4gBtn = h('button', {
      class: 'mp-btn mp-btn-outline',
      type: 'button',
      text: '4G 演示设备（SN-DEMO-4G-001）',
    });

    const demoWifiBtn = h('button', {
      class: 'mp-btn mp-btn-outline',
      type: 'button',
      text: 'Wi-Fi 演示设备（SN-DEMO-WIFI-001）',
    });

    const wifiHint = h('div', {
      class: 'text-xs',
      style: { color: 'rgba(255,255,255,.7)', maxWidth: '300px' },
      text: 'Wi-Fi 载荷的签名段需从平台端「订单 → 二维码 / SN」复制后粘贴（密钥在服务端，前端无法计算）',
    });

    const actions = h(
      'div',
      { style: { width: '100%', maxWidth: '300px', display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' } },
      payloadInput,
      resolveBtn,
      demo4gBtn,
      demoWifiBtn,
      wifiHint,
    );

    /**
     * 调后端解析载荷。
     * @param {string} payload
     */
    async function resolve(payload) {
      const value = String(payload ?? '').trim();
      if (!value) {
        toast.warning('请先粘贴二维码内容');
        payloadInput.focus();
        return;
      }

      resolveBtn.disabled = true;
      resolveBtn.textContent = '识别中…';
      resultHost.replaceChildren();

      try {
        const result = await mpApi.post('/miniapp/scan/resolve', { payload: value });
        applyResolveResult(result, value);
      } catch (error) {
        /* QR_INVALID / DEVICE_NOT_FOUND 才是真错误：给明确原因 + traceId */
        notifyError(error, '二维码识别失败');
      } finally {
        resolveBtn.disabled = false;
        resolveBtn.textContent = '识别二维码';
      }
    }

    /**
     * 按识别结果决定下一步。
     * @param {object} result
     * @param {string} payload
     */
    function applyResolveResult(result, payload) {
      const device = result?.device || null;
      const info = {
        id: device?.id,
        sn: device?.sn || '',
        networkType: result?.networkType || device?.networkType || '',
        qrPayload: payload,
        /* 扫码解析结果里已经带了 IMEI / 固件 / MAC（ScanDeviceInfo），
           一并带进激活页，省掉一次设备详情请求；详情只在字段缺失时才补 */
        imei: device?.imei,
        mac: device?.mac,
        firmwareVersion: device?.firmwareVersion,
        deviceLabel: device?.deviceLabel,
        activationStatus: result?.activationStatus,
        bindStatus: result?.bindStatus,
        tenantName: result?.tenantName,
        productName: result?.product?.name || '',
        cloudVendorLabel: result?.cloudVendorLabel,
        product: result?.product || null,
      };

      const alreadyActivated = result?.activationStatus === 'ACTIVATED' || result?.bindStatus === 'BOUND';

      /* ---- 不可绑定：正常状态，页内说明 ---- */
      if (!result?.bindable) {
        paintResult(result, info);

        if (alreadyActivated) {
          // 重复激活是幂等的：不报错，直接把人送到首页
          if (device) setCurrentDevice(device);
          toast.info(result?.reason || '设备已激活并绑定，直接进入首页');
          navigate('home');
          return;
        }

        resultHost.append(
          fromHtml(
            alert({
              tone: 'warning',
              title: '这台设备暂时不能绑定',
              text: result?.reason || '平台未给出具体原因，请联系卖家核实设备状态。',
            }),
          ),
        );
        return;
      }

      /* ---- 可绑定：带上下文进入对应激活路径 ---- */
      if (!info.id) {
        paintResult(result, info);
        resultHost.append(
          fromHtml(
            alert({
              tone: 'danger',
              title: '二维码有效，但缺少设备 ID',
              text: '无法发起激活。请把该载荷与截图反馈给平台，便于定位二维码签发问题。',
            }),
          ),
        );
        return;
      }

      state.pendingDevice = info;

      if (info.networkType === '4G') {
        toast.success(`已识别 4G 设备 ${info.sn}，请按提示开机激活`);
        navigate('setup_4g');
        return;
      }

      toast.success(`已识别 Wi-Fi 设备 ${info.sn}，请按提示配网`);
      navigate('setup_wifi');
    }

    /** 回显「扫到的是哪一台」，让用户在跳转前有机会核对 */
    function paintResult(result, info) {
      resultHost.replaceChildren();

      resultHost.append(
        fromHtml(
          descList(
            [
              ['二维码格式', FORMAT_LABELS[result?.format] || result?.format],
              ['联网方式', networkLabel(info.networkType)],
              ['设备 SN', info.sn],
              ['客户产品', info.productName || '-'],
              ['归属商户', result?.tenantName || '-'],
              ['云服务商', result?.cloudVendorLabel || '-'],
              ['激活状态', ACTIVATION_LABELS[result?.activationStatus] || result?.activationStatus],
              ['绑定状态', result?.bindStatus === 'BOUND' ? '已绑定' : '未绑定'],
              ['设备标签', info.deviceLabel || '-'],
            ],
            { cols: 2 },
          ),
        ),
      );
    }

    resolveBtn.addEventListener('click', () => resolve(payloadInput.value));

    demo4gBtn.addEventListener('click', () => {
      // JX 载荷无签名，可直接识别
      payloadInput.value = DEMO_JX_PAYLOAD;
      resolve(DEMO_JX_PAYLOAD);
    });

    demoWifiBtn.addEventListener('click', () => {
      // 只填骨架不自动提交：没有签名段的载荷必然被后端拒绝，硬提交只会演示失败
      payloadInput.value = DEMO_JD_PREFIX;
      payloadInput.focus();
      toast.info('已填入 Wi-Fi 载荷骨架，请补上从平台端复制的签名段');
    });

    stage.append(resultHost, actions);
    body.append(stage);

    // 二维码需在插入 DOM 后渲染（qrHtml 只输出 canvas 占位）
    hydrateQr(scanner);
  },
});
