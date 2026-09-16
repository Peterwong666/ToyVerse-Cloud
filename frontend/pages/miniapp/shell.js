/* ============================================================
   终端用户端（H5 小程序模拟）外壳
   ------------------------------------------------------------
   本模块从 P2 的 main.js 抽出，职责与原实现完全一致：

     手机壳 → 屏幕状态机（registerScreen / navigate）→ 底部 Tab

   与后台三端的差异：终端用户端没有侧边导航与 URL 路由，而是
   「一屏一屏推进」的状态机：

     扫码 → 登录 → 配网/激活 → 激活完成 → 首页 ⇄ 对话 / 充值 / 设置

   为什么外壳要单独成模块
   ----------------------
   9 个业务屏幕各自一个文件，它们都需要 navigate / state；
   若这些机制留在 main.js，屏幕模块就得反向 import main.js，
   而 main.js 又要 import 屏幕模块——形成循环依赖。把外壳独立出来
   后依赖是单向的：屏幕 → shell，main → (屏幕 + shell)。

   终端用户身份与后台身份**完全隔离**（见 shared/core/end-user.js）：
   本模块的 mpApi 用终端用户令牌，不碰 shared/core/auth.js 的后台令牌。
   ============================================================ */

import { apiUrl } from '/shared/core/config.js';
import endUser from '/shared/core/end-user.js';
import { ApiError, ERROR_CODES } from '/shared/core/api.js';
import { clear, h } from '/shared/ui/dom.js';
import { icon, iconNode } from '/shared/ui/icons.js';
import toast from '/shared/ui/toast.js';

/* ------------------------------------------------------------
   一、屏幕注册与状态
   ------------------------------------------------------------ */

/** 底部 Tab（仅主界面显示） */
export const TABS = [
  { key: 'home', label: '首页', icon: 'dashboard' },
  { key: 'chat', label: '对话', icon: 'chat' },
  { key: 'settings', label: '设置', icon: 'settings' },
];

/** 屏幕注册表：key → { title, tab, render } */
const screens = new Map();

/**
 * 注册一个屏幕。
 * @param {string} key
 * @param {object} o
 * @param {string} o.title        顶部标题（空则显示渐变头图）
 * @param {string} [o.tab]        所属 Tab
 * @param {Function} o.render     (bodyEl, ctx) => void
 * @param {Function} [o.onLeave]  离开本屏时调用（对话页用它关闭 WebSocket）
 * @param {boolean} [o.showTab]   是否显示底部 Tab
 * @param {boolean} [o.hero]      是否使用渐变头图
 * @param {string} [o.backTo]     显示返回按钮并指定返回目标
 */
export function registerScreen(key, o) {
  screens.set(key, { showTab: false, hero: false, onLeave: null, ...o });
}

/** 当前状态 */
export const state = {
  screen: 'scan',
  tab: 'home',
  /** 我的设备列表（登录响应里的 devices，或 /miniapp/devices 的 records） */
  devices: [],
  /** 待激活设备信息（由扫码/配网流程填充） */
  pendingDevice: null,
  /** 已激活/当前选中的设备 */
  device: null,
  /** 最近一次激活结果（激活完成页展示用） */
  lastActivation: null,
};

/**
 * 切换当前设备。
 *
 * 同时写入 endUser 的 deviceId：刷新页面后仍能回到同一台设备，
 * 而令牌与设备绑定关系是两件事——所以用 endUser.setDeviceId 而不是 save。
 */
export function setCurrentDevice(device) {
  state.device = device || null;
  if (device?.id) endUser.setDeviceId(device.id);
}

/**
 * 设备名称。
 *
 * 字段优先级：nickname（P9 会开放重命名，当前恒为 null）→
 * productName（只有设备详情接口下发，列表摘要里没有）→ 兜底文案。
 * 摘要里确实没有名称时宁可显示兜底，也不要拿 SN 冒充名字——
 * SN 已经在设备卡的第二行单独展示了。
 */
export function deviceName(device, fallback = '我的玩具') {
  return device?.nickname || device?.productName || fallback;
}

/** 联网方式展示文案 */
const NETWORK_LABELS = { '4G': '4G 流量', WIFI: 'Wi-Fi' };

export function networkLabel(networkType) {
  return NETWORK_LABELS[networkType] || networkType || '-';
}

/* ------------------------------------------------------------
   二、外壳渲染
   ------------------------------------------------------------ */

let shellRefs = null;

function mountShell() {
  const statusbar = h(
    'div',
    { class: 'mp-statusbar' },
    h('span', { class: 'mp-statusbar-time', text: '9:41' }),
    h(
      'span',
      { class: 'mp-statusbar-icons' },
      h('span', { text: '▮▮▮' }),
      h('span', { text: '5G' }),
      h('span', { text: '▮' }),
    ),
  );

  const navbar = h('div', { class: 'mp-navbar' });
  const hero = h('div', { class: 'mp-hero' });
  const body = h('div', { class: 'mp-body' });
  const tabbar = h('div', { class: 'mp-tabbar' });

  const screen = h('div', { class: 'mp-screen' }, navbar, hero, body, tabbar);

  const frame = h(
    'div',
    { class: 'phone-frame' },
    h('div', { class: 'phone-notch' }),
    statusbar,
    screen,
  );

  const stage = h('div', { class: 'phone-stage' }, frame);
  document.body.replaceChildren(stage);

  shellRefs = { screen, statusbar, navbar, hero, body, tabbar };
  return shellRefs;
}

function renderTabbar() {
  const { tabbar } = shellRefs;
  clear(tabbar);

  const def = screens.get(state.screen);
  if (!def?.showTab) {
    tabbar.classList.add('hidden');
    return;
  }
  tabbar.classList.remove('hidden');

  TABS.forEach((tab) => {
    const node = h(
      'div',
      { class: `mp-tab ${state.tab === tab.key ? 'active' : ''}`, 'data-tab': tab.key },
      iconNode(tab.icon, { size: 20, class: 'mp-tab-icon' }),
      h('span', { text: tab.label }),
    );
    node.addEventListener('click', () => navigate(tab.key));
    tabbar.append(node);
  });
}

/* ------------------------------------------------------------
   三、导航
   ------------------------------------------------------------ */

/**
 * 导航到指定屏幕。
 *
 * @param {string} key
 * @param {object} [options] 传给屏幕 render 的附加参数（如 { preset: '讲个故事' }）
 */
export function navigate(key, options = {}) {
  const def = screens.get(key);
  if (!def) {
    toast.error(`未注册的屏幕：${key}`);
    return;
  }

  /* 离开钩子：对话页借此关闭 WebSocket，避免「切走了连接还开着」 */
  if (state.screen !== key) screens.get(state.screen)?.onLeave?.();

  state.screen = key;
  if (def.tab) state.tab = def.tab;

  /* ---- 顶部导航栏 ---- */
  const { navbar, hero, body } = shellRefs;
  clear(navbar);
  clear(hero);
  hero.classList.add('hidden');
  navbar.classList.add('hidden');

  if (def.hero) {
    hero.classList.remove('hidden');
    navbar.classList.remove('hidden');
    navbar.style.background = 'transparent';
    navbar.style.borderBottom = 'none';
  } else if (def.title) {
    navbar.classList.remove('hidden');
    navbar.style.background = '';
    navbar.style.borderBottom = '';

    if (def.backTo) {
      const backBtn = h('button', {
        class: 'mp-navbar-back',
        type: 'button',
        'aria-label': '返回',
      });
      const tpl = document.createElement('template');
      tpl.innerHTML = icon('arrowLeft', { size: 18 }).trim();
      backBtn.append(tpl.content);
      backBtn.addEventListener('click', () => navigate(def.backTo));
      navbar.append(backBtn);
    }

    navbar.append(h('div', { class: 'mp-navbar-title', text: def.title }));
  }

  /* ---- 正文 ----
     hero 一并交给屏幕：首页需要往渐变头图里写设备名，
     而 hero 在外壳里，不通过 ctx 传出去屏幕模块拿不到。 */
  clear(body);
  def.render(body, { navigate, state, options, hero });

  /* ---- Tab 栏 ---- */
  renderTabbar();

  /* ---- 滚动置顶 ---- */
  body.scrollTop = 0;
}

/** 返回上一屏（简化的栈式返回） */
export function goBack(fallback = 'home') {
  navigate(fallback);
}

/* ------------------------------------------------------------
   四、终端用户 HTTP 客户端
   ------------------------------------------------------------
   为什么不用 shared/core/api.js
   -----------------------------
   api.js 会自动注入 `auth.accessToken`——那是**后台管理端**的令牌，
   且它在 extraHeaders 之后无条件覆盖 authorization 头。
   本端需要的是终端用户令牌（endUser.token），两者是两套身份体系：

     * 后台令牌发给 /miniapp/* → 后端按终端用户令牌解析 → UNAUTHENTICATED；
     * 更糟的是 api.js 的 401 分支会用后台 refreshToken 去刷新，
       刷新失败即 auth.clear()，把正在用的管理后台登录态一起清掉。

   共享模块不允许改（本轮纪律），因此这里做一层**薄封装**：
   复用 api.js 导出的 ApiError / ERROR_CODES（错误契约与全站一致），
   但令牌来源、URL 拼接换成终端用户这一套。
   ------------------------------------------------------------ */

/** 生成 traceId（与后端格式一致：32 位十六进制），便于与后端日志串联 */
function newTraceId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID().replace(/-/g, '');
  return Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join('');
}

/** 构造查询串（跳过空值，避免 ?deviceId=null 这类噪声） */
function buildQuery(params) {
  if (!params) return '';
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === '') continue;
    search.append(key, String(value));
  }
  const query = search.toString();
  return query ? `?${query}` : '';
}

/** 401：清掉终端用户会话并回到登录屏（**不影响**后台管理端会话） */
function handleUnauthorized() {
  endUser.clear();
  state.device = null;
  state.devices = [];
  toast.error('登录态已失效，请重新登录');
  if (state.screen !== 'login') navigate('login');
}

/**
 * 发起终端用户请求。
 *
 * @param {string} method
 * @param {string} path
 * @param {object} [options]
 * @param {object} [options.params] 查询参数
 * @param {any}    [options.body]
 * @param {number} [options.timeout] 毫秒；流式接口需要放宽
 * @param {boolean}[options.raw]     true → 返回未消费的 Response（读 SSE 流用）
 * @param {AbortSignal} [options.signal]
 */
async function request(method, path, options = {}) {
  const { params, body, timeout = 20000, raw = false, signal } = options;

  const headers = { 'x-trace-id': newTraceId() };
  if (body !== undefined && body !== null) headers['content-type'] = 'application/json';

  const token = endUser.token;
  if (token) headers.authorization = `Bearer ${token}`;

  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort('timeout');
  }, timeout);

  if (signal) {
    if (signal.aborted) controller.abort(signal.reason);
    else signal.addEventListener('abort', () => controller.abort(signal.reason), { once: true });
  }

  let response;
  try {
    response = await fetch(apiUrl(path) + buildQuery(params), {
      method,
      headers,
      body: body === undefined || body === null ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
  } catch {
    clearTimeout(timer);
    /* 调用方主动取消（例如离开对话页时中断 SSE 流）：这是正常控制流，
       不能当成「超时」弹提示，因此原样抛出 AbortError 交给调用方忽略。 */
    if (signal?.aborted) {
      const aborted = new Error('请求已取消');
      aborted.name = 'AbortError';
      throw aborted;
    }
    throw new ApiError({
      code: timedOut ? ERROR_CODES.TIMEOUT : ERROR_CODES.NETWORK_ERROR,
      message: timedOut ? '请求超时，请检查网络后重试' : '网络异常，请检查网络连接',
      traceId: headers['x-trace-id'],
    });
  } finally {
    clearTimeout(timer);
  }

  if (response.ok) {
    // raw 时必须跳过 body 解析：读掉之后 SSE 流的 reader 就拿不到数据了
    if (raw) return response;
    if (response.status === 204) return null;
    return await response.json().catch(() => null);
  }

  const payload = await response.json().catch(() => null);
  const error = new ApiError({
    code: payload?.code || (response.status === 404 ? ERROR_CODES.RESOURCE_NOT_FOUND : ERROR_CODES.INTERNAL_ERROR),
    message: payload?.message || `请求失败（HTTP ${response.status}）`,
    traceId: payload?.traceId || response.headers.get('x-trace-id'),
    details: payload?.details ?? null,
    status: response.status,
  });

  if (response.status === 401) handleUnauthorized();
  throw error;
}

/** 终端用户接口客户端（令牌取自 endUser，而不是后台 auth） */
export const mpApi = {
  get: (path, options) => request('GET', path, options),
  post: (path, body, options) => request('POST', path, { ...options, body }),
  put: (path, body, options) => request('PUT', path, { ...options, body }),
  del: (path, body, options) => request('DELETE', path, { ...options, body }),
  /** 返回未消费的 Response（SSE 流式降级用） */
  raw: (method, path, options) => request(method, path, { ...options, raw: true }),
};

/* ------------------------------------------------------------
   五、错误提示
   ------------------------------------------------------------ */

/**
 * 终端用户端错误码 → 可执行提示。
 *
 * 为什么单独一张表：后台三端的提示词是「去找谁处理」，
 * 而这里面对的是消费者——必须说清「现在该做什么」，
 * 且 VENDOR_UNAVAILABLE / BIND_FAILED 属**安全失败**，
 * 要明确告诉用户「平台没有伪造成功」，而不是让人反复重试。
 */
export const END_USER_ERROR_HINTS = {
  VENDOR_UNAVAILABLE: '厂商密钥未配置，该能力已被安全禁用（不会伪造成功）',
  BIND_FAILED: '请核对机身标签上的 SN 后重新填写',
  QR_INVALID: '请确认这是平台签发的设备二维码',
  QR_EXPIRED: '二维码/验证码已过期，请重新获取',
  DEVICE_NOT_FOUND: '设备不存在或尚未入库，请联系卖家',
  DEVICE_ALREADY_BOUND: '该设备已绑定过账号，请先在原账号解绑',
  DEVICE_NOT_IN_TENANT: '该设备不属于当前商户，请联系卖家',
  PERMISSION_DENIED: '当前账号无权操作该设备',
  DEVICE_FROZEN: '设备已被冻结，请联系客服',
  UNAUTHENTICATED: '请重新登录后再试',
};

/** 取错误码对应的提示（无映射则返回空串） */
function errorHint(code) {
  return END_USER_ERROR_HINTS[code] || '';
}

/** 统一错误提示：后端 message 为主，附上本端可执行建议 */
export function notifyError(error, fallback = '操作失败，请稍后重试') {
  const hint = errorHint(error?.code);
  const message = error?.message || fallback;
  toast.error(hint ? `${message}　${hint}` : message, { traceId: error?.traceId || '' });
}

/* ------------------------------------------------------------
   六、启动
   ------------------------------------------------------------ */

/** 挂载外壳并进入首屏 */
export function startApp() {
  mountShell();

  /* 终端用户会话独立于后台管理端会话（见 shared/core/end-user.js）：
     * 已登录 → 交给登录屏判断「有设备去首页 / 没设备去扫码」
     * 未登录 → 扫码引导
     注意：后台管理端的登录态不影响这里。 */
  navigate(endUser.isLoggedIn ? 'login' : 'scan');
}
