/* ============================================================
   终端用户端 · 首页
   ------------------------------------------------------------
   数据来源：GET /miniapp/devices → { records, total }

   首屏策略：先用登录响应里的 devices（state.devices）渲染，
   再拉真实列表覆盖。原因是手机端首屏空白最刺眼，而登录响应里
   已经带了设备摘要（DeviceBrief），没必要让用户等第二次请求。

   关于「流量充值仅 4G 设备显示」
   ------------------------------
   Wi-Fi 方案走家庭宽带，不消耗流量，所以充值入口对 Wi-Fi 设备
   是**无意义的**——它只会在充值页得到一个空态。既然如此就不要在
   首页给出这个入口：入口本身就是一种承诺。因此这里按
   networkType === '4G' 条件渲染宫格项。

   在线状态一律用 `online`（心跳窗口内派生）而不是状态枚举：
   枚举是落库投影，两次心跳之间不会自己变，设备掉线了还显示「在线」。
   ============================================================ */

import endUser from '/shared/core/end-user.js';
import { clear, fromHtml, h } from '/shared/ui/dom.js';
import { alert, emptyState, loadingState } from '/shared/ui/components.js';
import { icon, iconNode } from '/shared/ui/icons.js';
import toast from '/shared/ui/toast.js';
import {
  deviceName,
  mpApi,
  navigate,
  notifyError,
  registerScreen,
  setCurrentDevice,
  state,
} from './shell.js';

/** 功能宫格（流量充值项只在 4G 设备上出现，见下方条件拼接） */
const BASE_TILES = [
  { label: '开始对话', icon: 'chat', tone: 'brand', to: 'chat' },
  { label: '听故事', icon: 'book', tone: 'accent', to: 'chat', preset: '讲个故事' },
  { label: '听音乐', icon: 'volume', tone: 'teal', to: 'chat', preset: '唱首歌' },
];

const RECHARGE_TILE = { label: '流量充值', icon: 'signal', tone: 'coral', to: 'recharge' };

registerScreen('home', {
  tab: 'home',
  showTab: true,
  hero: true,
  render: (body, ctx) => {
    const { hero } = ctx;

    /** 当前设备：优先用 state.device，其次用会话里记住的 deviceId，最后取第一台 */
    function pickCurrent(records) {
      const list = records || state.devices || [];
      const wanted = state.device?.id || endUser.deviceId || null;
      return list.find((item) => item?.id === wanted) || list[0] || state.device || null;
    }

    function paintHero(device) {
      clear(hero);
      hero.append(
        h('div', { class: 'mp-hero-title', text: device ? deviceName(device) : '还没有设备' }),
        h('div', {
          class: 'mp-hero-sub',
          text: device ? `SN ${device.sn || '-'}` : '请先扫码激活你的玩具',
        }),
      );
    }

    /* ---- 设备卡 ---- */
    const cardHost = h('div', { class: 'mp-section' });
    const gridHost = h('div', { class: 'mp-section' });
    const listHost = h('div'); // 多设备时的切换列表

    /** 加载中占位（有缓存时不走这里，避免首屏闪一下骨架） */
    function paintLoading() {
      clear(cardHost);
      cardHost.append(fromHtml(loadingState('正在加载设备…')));
    }

    /** 无设备空态（**只在列表确实为空时**出现，见下面对「加载失败」的区分） */
    function paintNoDevice() {
      clear(cardHost);
      const empty = h('div', { class: 'mp-card' });
      empty.append(
        fromHtml(
          emptyState({
            icon: 'qrcode',
            title: '还没有绑定设备',
            desc: '扫描玩具底部的二维码，按引导完成激活后即可开始对话。',
          }),
        ),
      );
      const toScan = h('button', { class: 'mp-btn mt-4', type: 'button', text: '扫码添加设备' });
      toScan.addEventListener('click', () => navigate('scan'));
      empty.append(toScan);
      cardHost.append(empty);
    }

    /** 加载失败且没有缓存：说清是「没拉到」而不是「没有设备」 */
    function paintLoadError(message) {
      clear(cardHost);
      const box = h('div', { class: 'mp-card' });
      box.append(
        fromHtml(
          alert({
            tone: 'danger',
            title: '设备列表加载失败',
            text: `设备列表暂时取不到（${message}）。这**不代表**你没有设备——请检查网络后重试。`,
          }),
        ),
      );
      const retry = h('button', { class: 'mp-btn mp-btn-outline mt-4', type: 'button', text: '重新加载' });
      retry.addEventListener('click', () => {
        paintLoading();
        loadDevices();
      });
      box.append(retry);
      cardHost.append(box);
    }

    /** 渲染设备卡（上浮在头图上） */
    function paintDevice(device) {
      clear(cardHost);
      if (!device) {
        paintNoDevice();
        return;
      }

      const refreshBtn = h('button', {
        class: 'btn-icon btn-sm',
        type: 'button',
        'aria-label': '刷新设备状态',
        title: '刷新设备状态',
      });
      const refreshTpl = document.createElement('template');
      refreshTpl.innerHTML = icon('refresh', { size: 16 }).trim();
      refreshBtn.append(refreshTpl.content);
      refreshBtn.addEventListener('click', (event) => {
        event.stopPropagation();
        refreshBtn.disabled = true;
        loadDevices({ silent: true }).finally(() => {
          refreshBtn.disabled = false;
        });
      });

      const online = Boolean(device.online);
      const card = h(
        'div',
        { class: 'mp-float-card' },
        h(
          'div',
          { class: 'mp-device' },
          h('div', { class: 'mp-device-figure', text: '🐶' }),
          h(
            'div',
            { class: 'mp-device-info' },
            h('div', { class: 'mp-device-name', text: deviceName(device) }),
            h('div', { class: 'mp-device-meta', text: device.sn || '-' }),
            h(
              'div',
              { class: 'mp-device-status' },
              h('span', { class: `mp-dot ${online ? 'pulse' : 'offline'}` }),
              h('span', { class: online ? 'text-success' : 'text-secondary', text: online ? '在线' : '离线' }),
            ),
          ),
          refreshBtn,
        ),
      );
      // 点卡片进设置页（设备信息 / 音量 / 儿童模式都在那里）
      card.style.cursor = 'pointer';
      card.addEventListener('click', () => navigate('settings'));
      cardHost.append(card);
    }

    /** 渲染功能宫格 */
    function paintGrid(device) {
      clear(gridHost);
      gridHost.append(h('div', { class: 'mp-section-title', text: '常用功能' }));

      const tiles = [...BASE_TILES];
      // 仅 4G 设备展示充值入口（Wi-Fi 不消耗流量，给了入口也只能看到空态）
      if (device?.networkType === '4G') tiles.push(RECHARGE_TILE);

      const gridEl = h('div', { class: 'mp-grid' });
      tiles.forEach((item) => {
        const gridIcon = iconNode(item.icon, { size: 22, class: 'mp-grid-icon' });
        gridIcon.setAttribute('data-tone', item.tone);

        const cell = h(
          'div',
          { class: 'mp-grid-item' },
          gridIcon,
          h('span', { class: 'mp-grid-label', text: item.label }),
        );
        cell.addEventListener('click', () => navigate(item.to, item.preset ? { preset: item.preset } : {}));
        gridEl.append(cell);
      });
      gridHost.append(gridEl);
    }

    /** 多设备时的切换列表 */
    function paintDeviceList(devices, current) {
      clear(listHost);
      if (!devices || devices.length < 2) return;

      const section = h('div', { class: 'mp-section' });
      section.append(h('div', { class: 'mp-section-title', text: '切换设备' }));

      const list = h('div', { class: 'mp-list' });
      devices.forEach((device) => {
        const active = device.id === current?.id;
        const iconTpl = document.createElement('template');
        iconTpl.innerHTML = icon('device', { size: 16 }).trim();

        const node = h(
          'div',
          { class: 'mp-list-item' },
          h('span', { class: 'mp-list-icon' }, iconTpl.content),
          h('span', { class: 'mp-list-title', text: deviceName(device, device.sn || '未命名设备') }),
          h('span', { class: 'mp-list-value', text: active ? '当前' : device.sn || '' }),
          h('span', { class: 'mp-list-arrow', text: active ? '✓' : '›' }),
        );
        node.addEventListener('click', () => {
          if (active) return;
          setCurrentDevice(device);
          paintHero(device);
          paintDevice(device);
          paintGrid(device);
          paintDeviceList(devices, device);
          toast.info(`已切换到 ${deviceName(device, device.sn)}`);
        });
        list.append(node);
      });

      section.append(list);
      listHost.append(section);
    }

    /** 统一刷新入口：拉列表 → 重新渲染三块 */
    async function loadDevices({ silent = false } = {}) {
      try {
        const result = await mpApi.get('/miniapp/devices');
        const records = Array.isArray(result?.records) ? result.records : [];
        state.devices = records;

        const current = pickCurrent(records);
        // 设备列表是空的（例如已在别处解绑）→ 清掉当前设备，
        // 否则对话页还会拿旧设备去建连接
        if (current) setCurrentDevice(current);
        else setCurrentDevice(null);

        paintHero(current);
        paintDevice(current);
        paintGrid(current);
        paintDeviceList(records, current);
        if (!silent) toast.success('设备状态已刷新');
      } catch (error) {
        if (!silent) notifyError(error, '设备列表加载失败');
        const current = pickCurrent();
        if (current) {
          // 有缓存：继续展示缓存，不打断使用
          paintHero(current);
          paintDevice(current);
          paintGrid(current);
          paintDeviceList(state.devices, current);
        } else {
          paintLoadError(error?.message || '请检查网络后重试');
        }
      }
    }

    /* ---- 首屏：缓存先渲染 → 再拉真实列表 ---- */
    const cached = pickCurrent();
    paintHero(cached);
    paintGrid(cached);
    paintDeviceList(state.devices, cached);
    if (cached) paintDevice(cached);
    else paintLoading();

    body.append(cardHost, gridHost, listHost);

    /* ---- 说明条 ---- */
    const notice = h('div', { class: 'mp-section' });
    notice.append(
      fromHtml(
        alert({
          tone: 'neutral',
          text: '设备在线状态按心跳窗口（默认 180 秒）派生；刚断电的设备会在一小段时间后转为「离线」。',
        }),
      ),
    );
    body.append(notice);

    // 缓存在先、请求在后：有缓存时不阻塞渲染，直接静默刷新
    loadDevices({ silent: Boolean(cached) });
  },
});
