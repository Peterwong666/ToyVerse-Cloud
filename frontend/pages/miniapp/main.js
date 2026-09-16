/* ============================================================
   终端用户端（H5 小程序模拟）启动入口
   ------------------------------------------------------------
   与后台三端不同，这里没有侧边导航，而是「屏幕状态机」：

     扫码 → 登录 → 配网/激活 → 激活完成 → 首页 ⇄ 对话 / 充值 / 设置

   P2 阶段交付：手机壳、屏幕路由、状态机、Tab 栏、导航。
   业务屏幕（扫码激活、对话、充值）在 P7–P8 阶段接入真实接口。
   ============================================================ */

import endUser from '/shared/core/end-user.js';
import { clear, fromHtml, h } from '/shared/ui/dom.js';
import { icon, iconNode } from '/shared/ui/icons.js';
import { alert, emptyState } from '/shared/ui/components.js';
import toast from '/shared/ui/toast.js';

/* ------------------------------------------------------------
   屏幕定义
   ------------------------------------------------------------ */

/** 底部 Tab（仅主界面显示） */
const TABS = [
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
 * @param {boolean} [o.showTab]   是否显示底部 Tab
 * @param {boolean} [o.hero]      是否使用渐变头图
 * @param {string} [o.backTo]     显示返回按钮并指定返回目标
 */
export function registerScreen(key, o) {
  screens.set(key, { showTab: false, hero: false, ...o });
}

/** 当前状态 */
export const state = {
  screen: 'scan',
  tab: 'home',
  /** 待激活设备信息（由扫码/配网流程填充） */
  pendingDevice: null,
  /** 已激活设备 */
  device: null,
};

/* ------------------------------------------------------------
   外壳渲染
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
   导航
   ------------------------------------------------------------ */

/** 导航到指定屏幕 */
export function navigate(key, options = {}) {
  const def = screens.get(key);
  if (!def) {
    toast.error(`未注册的屏幕：${key}`);
    return;
  }

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

  /* ---- 正文 ---- */
  clear(body);
  def.render(body, { navigate, state, options });

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
   内置屏幕
   ------------------------------------------------------------ */

/* ---- 扫码 ---- */
registerScreen('scan', {
  title: '',
  hero: true,
  render: (body) => {
    const stage = h('div', { class: 'mp-scan-stage' });

    const scanner = h(
      'div',
      { class: 'mp-scanner' },
      h('span', { class: 'mp-scanner-corner tl' }),
      h('span', { class: 'mp-scanner-corner tr' }),
      h('span', { class: 'mp-scanner-corner bl' }),
      h('span', { class: 'mp-scanner-corner br' }),
      h('span', { class: 'mp-scanner-line' }),
    );

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

    const actions = h('div', { style: { width: '100%', maxWidth: '280px', display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' } });

    const demoBtn = h('button', { class: 'mp-btn', type: 'button', text: '模拟扫码（演示）' });
    demoBtn.addEventListener('click', () => {
      state.pendingDevice = {
        qrPayload: 'JD|t-001|cprod-demo|SN-DEMO-0001|demo-sign',
        networkType: 'WIFI',
      };
      toast.info('已识别：京东 Wi-Fi 方案（演示数据）');
      navigate('login');
    });
    actions.append(demoBtn);

    const hint = h('button', {
      class: 'mp-btn mp-btn-outline',
      type: 'button',
      text: '跳过激活，直接体验',
    });
    hint.addEventListener('click', () => {
      state.device = { name: '演示玩具', sn: 'SN-DEMO-0001', online: true };
      navigate('home');
    });
    actions.append(hint);

    stage.append(actions);
    body.append(stage);
  },
});

/* ---- 登录 ---- */
registerScreen('login', {
  title: '手机号登录',
  backTo: 'scan',
  render: (body) => {
    const section = h('div', { class: 'mp-section' });

    section.append(
      h(
        'div',
        { class: 'mp-card' },
        h('div', { class: 'mp-section-title', text: '登录后即可绑定设备' }),
        h('div', { class: 'text-xs text-secondary mb-4', text: '登录用于关联设备与你的账号，便于后续管理' }),
        (() => {
          const form = h('div', { style: { display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' } });
          const phoneInput = h('input', {
            class: 'input',
            type: 'tel',
            placeholder: '请输入手机号',
            value: '13800000000',
            inputmode: 'numeric',
          });
          const code = h(
            'div',
            { class: 'flex gap-2' },
            h('input', { class: 'input', type: 'text', placeholder: '验证码', value: '123456' }),
            h('button', { class: 'btn', type: 'button', text: '获取验证码', style: { flexShrink: '0' } }),
          );
          const submit = h('button', { class: 'mp-btn', type: 'button', text: '登录并继续' });
          submit.addEventListener('click', () => {
            const phone = phoneInput.value.trim();
            if (!phone) {
              toast.warning('请输入手机号');
              return;
            }
            if (!/^\d{11}$/.test(phone)) {
              toast.warning('请输入 11 位手机号');
              return;
            }

            /* 真实校验在 P8 阶段接入 /miniapp/auth/login；
               此处先落地独立的终端用户会话，保证与后台登录态隔离。 */
            endUser.save({
              token: `demo-enduser-${Date.now()}`,
              profile: { phone, nickname: `用户${phone.slice(-4)}` },
            });

            toast.success('登录成功（演示）');
            navigate('setup_wifi');
          });
          form.append(phoneInput, code, submit);
          return form;
        })(),
      ),
      fromHtml(`<div class="mt-4">${alert({
        tone: 'info',
        text: '当前为演示环境：终端用户登录接口将在 P8 阶段交付，此处不校验真实验证码。终端用户会话与后台管理端完全独立。',
      })}</div>`),
    );

    body.append(section);
  },
});

/* ---- Wi-Fi 配网 ---- */
registerScreen('setup_wifi', {
  title: '连接 Wi-Fi',
  backTo: 'login',
  render: (body) => {
    const section = h('div', { class: 'mp-section' });
    section.append(
      h(
        'div',
        { class: 'mp-card' },
        h('div', { class: 'mp-section-title', text: '为玩具配置网络' }),
        h('div', {
          class: 'text-xs text-secondary mb-4',
          text: '玩具需要通过 Wi-Fi 连接云端，配置完成后会自动上报设备信息完成激活',
        }),
        (() => {
          const form = h('div', { style: { display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' } });
          form.append(
            h('input', { class: 'input', type: 'text', placeholder: 'Wi-Fi 名称', value: 'Home_WiFi_5G' }),
            h('input', { class: 'input', type: 'password', placeholder: 'Wi-Fi 密码', value: '12345678' }),
          );
          const submit = h('button', { class: 'mp-btn', type: 'button', text: '开始配网' });
          submit.addEventListener('click', () => {
            toast.info('正在配网…');
            setTimeout(() => navigate('activate_done'), 600);
          });
          form.append(submit);
          return form;
        })(),
      ),
      fromHtml(`<div class="mt-4">${alert({
        tone: 'warning',
        text: '真实配网流程由设备端完成：设备联网后向平台上报 SN 与 MAC，平台校验与二维码一致后才调用厂商接口激活。',
      })}</div>`),
    );
    body.append(section);
  },
});

/* ---- 4G 激活 ---- */
registerScreen('setup_4g', {
  title: '开机激活',
  backTo: 'login',
  render: (body) => {
    const section = h('div', { class: 'mp-section' });
    section.append(
      h(
        'div',
        { class: 'mp-card', style: { textAlign: 'center' } },
        h('div', { class: 'mp-step-ring', style: { margin: '0 auto var(--space-4)' }, text: '1' }),
        h('div', { class: 'mp-section-title', style: { justifyContent: 'center' }, text: '请打开玩具电源' }),
        h('div', {
          class: 'text-xs text-secondary',
          text: '设备开机后将通过 4G 网络自动上线，平台随后调用集贤接口完成激活',
        }),
        (() => {
          const submit = h('button', { class: 'mp-btn mt-4', type: 'button', text: '我已完成开机' });
          submit.addEventListener('click', () => {
            toast.info('等待设备上线…');
            setTimeout(() => navigate('activate_done'), 800);
          });
          return submit;
        })(),
      ),
      fromHtml(`<div class="mt-4">${alert({
        tone: 'info',
        text: '集贤 4G 方案：二维码格式为 JX|SN|IMEI|ICCID|deviceId，设备生成由厂商 API 返回。',
      })}</div>`),
    );
    body.append(section);
  },
});

/* ---- 激活完成 ---- */
registerScreen('activate_done', {
  title: '',
  render: (body) => {
    const stage = h('div', { class: 'mp-scan-stage' });
    stage.append(
      h('div', { class: 'mp-success-check' }, (() => {
        const tpl = document.createElement('template');
        tpl.innerHTML = icon('check', { size: 34 }).trim();
        return tpl.content;
      })()),
      h(
        'div',
        {},
        h('div', { class: 'text-lg font-semibold text-primary', text: '激活成功' }),
        h('div', { class: 'text-xs text-secondary mt-1', text: '设备已绑定到你的账号，可以开始对话了' }),
      ),
      (() => {
        const btn = h('button', { class: 'mp-btn', type: 'button', text: '进入首页', style: { maxWidth: '240px' } });
        btn.addEventListener('click', () => {
          state.device = { name: '智能玩具', sn: 'SN-DEMO-0001', online: true };
          navigate('home');
        });
        return btn;
      })(),
    );
    body.append(stage);
  },
});

/* ---- 首页 ---- */
registerScreen('home', {
  tab: 'home',
  showTab: true,
  hero: true,
  render: (body) => {
    const { hero } = shellRefs;
    const device = state.device;

    hero.append(
      h('div', { class: 'mp-hero-title', text: device ? (device.name || '我的玩具') : '还没有设备' }),
      h('div', {
        class: 'mp-hero-sub',
        text: device ? `SN ${device.sn || '-'}` : '请先扫码激活你的玩具',
      }),
    );

    /* ---- 设备状态卡（上浮） ---- */
    if (device) {
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
            h('div', { class: 'mp-device-name', text: device.name || '智能玩具' }),
            h('div', { class: 'mp-device-meta', text: device.sn || '-' }),
            h(
              'div',
              { class: 'mp-device-status' },
              h('span', { class: `mp-dot ${device.online ? 'pulse' : 'offline'}` }),
              h('span', { class: 'text-success', text: device.online ? '在线' : '离线' }),
            ),
          ),
        ),
      );
      body.append(card);
    }

    /* ---- 功能宫格 ---- */
    const grid = h('div', { class: 'mp-section' });
    grid.append(h('div', { class: 'mp-section-title', text: '常用功能' }));

    const items = [
      { label: '开始对话', icon: 'chat', tone: 'brand', to: 'chat' },
      { label: '听故事', icon: 'book', tone: 'accent', to: 'chat' },
      { label: '听音乐', icon: 'volume', tone: 'teal', to: 'chat' },
      { label: '流量充值', icon: 'signal', tone: 'coral', to: 'recharge' },
    ];

    const gridEl = h('div', { class: 'mp-grid' });
    items.forEach((item) => {
      const gridIcon = iconNode(item.icon, { size: 22, class: 'mp-grid-icon' });
      gridIcon.setAttribute('data-tone', item.tone);

      const cell = h(
        'div',
        { class: 'mp-grid-item' },
        gridIcon,
        h('span', { class: 'mp-grid-label', text: item.label }),
      );
      cell.addEventListener('click', () => navigate(item.to));
      gridEl.append(cell);
    });
    grid.append(gridEl);
    body.append(grid);

    /* ---- 提示 ---- */
    const notice = h('div', { class: 'mp-section' });
    notice.append(
      fromHtml(
        alert({
          tone: 'info',
          text: '对话与充值功能将在后续阶段接入真实接口；当前处于 P2 前端地基阶段。',
        }),
      ),
    );
    body.append(notice);
  },
});

/* ---- 对话 ---- */
registerScreen('chat', {
  title: '和玩具说话',
  backTo: 'home',
  tab: 'chat',
  showTab: true,
  render: (body) => {
    const messages = h('div', { class: 'mp-chat-body' });

    const addMessage = (text, role = 'assistant') => {
      const avatarTpl = document.createElement('template');
      avatarTpl.innerHTML = icon(role === 'assistant' ? 'sparkles' : 'user', { size: 15 }).trim();

      const node = h(
        'div',
        { class: `mp-msg mp-msg-${role}` },
        h('span', { class: 'mp-msg-avatar' }, avatarTpl.content),
        h('div', { class: 'mp-bubble', text }),
      );
      messages.append(node);
      body.scrollTop = body.scrollHeight;
    };

    addMessage('你好呀！我是你的小伙伴，想听故事、听儿歌，还是聊聊天？');

    /* ---- 快捷提问 ---- */
    const quick = h('div', { class: 'mp-quick-replies' });
    ['讲个故事', '唱首歌', '今天天气怎么样', '你叫什么名字'].forEach((text) => {
      const chip = h('div', { class: 'mp-quick-reply', text });
      chip.addEventListener('click', () => send(text));
      quick.append(chip);
    });

    /* ---- 输入栏 ---- */
    const input = h('input', {
      class: 'input',
      type: 'text',
      placeholder: '说点什么…',
      'aria-label': '消息输入',
    });

    const sendBtn = h('button', { class: 'mp-send', type: 'button', 'aria-label': '发送', text: '↑' });

    function send(text) {
      const value = String(text ?? '').trim();
      if (!value) return;
      addMessage(value, 'user');
      input.value = '';

      // 打字中指示
      const typing = h(
        'div',
        { class: 'mp-msg mp-msg-assistant' },
        h('span', { class: 'mp-msg-avatar' }),
        h('div', { class: 'mp-bubble mp-typing' }, h('span'), h('span'), h('span')),
      );
      messages.append(typing);
      body.scrollTop = body.scrollHeight;

      // P7/P8 接入真实 WebSocket 后，这里替换为流式回复
      setTimeout(() => {
        typing.remove();
        addMessage('对话能力将在后续阶段接入（连接设备云与 AI 供应商）。当前为前端演示。');
      }, 900);
    }

    sendBtn.addEventListener('click', () => send(input.value));
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') send(input.value);
    });

    const inputbar = h('div', { class: 'mp-inputbar' }, input, sendBtn);

    body.append(messages, quick, inputbar);
  },
});

/* ---- 充值 ---- */
registerScreen('recharge', {
  title: '流量充值',
  backTo: 'home',
  render: (body) => {
    /* 仅 4G 方案需要流量充值；Wi-Fi 方案不显示此入口 */
    const isWifi = state.pendingDevice?.networkType === 'WIFI';

    const section = h('div', { class: 'mp-section' });

    if (isWifi) {
      section.append(
        emptyState({
          icon: 'wifi',
          title: '当前设备无需充值',
          desc: 'Wi-Fi 方案通过家庭网络联网，不消耗流量；如需购买流量套餐，请使用 4G 版设备。',
        }),
      );
      body.append(section);
      return;
    }

    section.append(
      fromHtml(
        alert({
          tone: 'info',
          text: '充值功能将在 P8 阶段接入真实套餐与订单接口。以下为界面预览。',
        }),
      ),
    );

    const plans = [
      { name: '体验包', desc: '1GB · 30 天有效', price: '9.9', popular: false },
      { name: '标准包', desc: '5GB · 90 天有效', price: '29.9', popular: true },
      { name: '畅玩包', desc: '20GB · 180 天有效', price: '89.9', popular: false },
    ];

    const list = h('div', { style: { display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' } });
    plans.forEach((plan) => {
      const node = h(
        'div',
        { class: `mp-plan ${plan.popular ? 'mp-plan-popular' : ''}` },
        h(
          'div',
          {},
          h('div', { class: 'mp-plan-name', text: plan.name }),
          h('div', { class: 'mp-plan-desc', text: plan.desc }),
        ),
        h('div', { class: 'mp-plan-price' }, h('small', { text: '¥' }), plan.price),
      );
      node.addEventListener('click', () => {
        list.querySelectorAll('.mp-plan').forEach((el) => el.classList.remove('selected'));
        node.classList.add('selected');
        toast.info(`已选择 ${plan.name}`);
      });
      list.append(node);
    });

    section.append(list);

    const submit = h('button', {
      class: 'mp-btn',
      type: 'button',
      text: '确认支付（演示）',
      style: { marginTop: 'var(--space-4)' },
    });
    submit.addEventListener('click', () => toast.info('支付功能将在后续阶段接入'));
    section.append(submit);

    body.append(section);
  },
});

/* ---- 设置 ---- */
registerScreen('settings', {
  tab: 'settings',
  showTab: true,
  title: '设置',
  render: (body) => {
    const section = h('div', { class: 'mp-section' });
    const list = h('div', { class: 'mp-list' });

    const rows = [
      { icon: 'device', title: '设备信息', value: state.device?.sn || '未绑定', to: null },
      { icon: 'wifi', title: '网络状态', value: '良好', to: null },
      { icon: 'volume', title: '音量', value: '中', to: null },
      { icon: 'shield', title: '儿童模式', value: '未开启', to: null },
      { icon: 'refresh', title: '固件版本', value: 'v1.0.0', to: null },
    ];

    rows.forEach((row) => {
      const tpl = document.createElement('template');
      tpl.innerHTML = icon(row.icon, { size: 16 }).trim();

      const node = h(
        'div',
        { class: 'mp-list-item' },
        h('span', { class: 'mp-list-icon' }, tpl.content),
        h('span', { class: 'mp-list-title', text: row.title }),
        h('span', { class: 'mp-list-value', text: row.value }),
        h('span', { class: 'mp-list-arrow', text: '›' }),
      );
      node.addEventListener('click', () => toast.info('该设置项将在后续阶段接入'));
      list.append(node);
    });

    section.append(list);

    const unbind = h('button', {
      class: 'mp-btn mp-btn-outline',
      type: 'button',
      text: '解绑设备',
      style: { marginTop: 'var(--space-4)', color: 'var(--danger-600)' },
    });
    unbind.addEventListener('click', () => {
      endUser.reset();
      state.device = null;
      toast.info('已解绑设备（演示）');
      navigate('scan');
    });
    section.append(unbind);

    const toLogin = h('button', {
      class: 'mp-btn mp-btn-outline',
      type: 'button',
      text: '返回管理后台登录',
      style: { marginTop: 'var(--space-3)' },
    });
    toLogin.addEventListener('click', () => {
      window.location.href = '/login';
    });
    section.append(toLogin);

    body.append(section);
  },
});

/* ------------------------------------------------------------
   启动
   ------------------------------------------------------------ */

function boot() {
  mountShell();

  /* 终端用户会话独立于后台管理端会话（见 shared/core/end-user.js）：
     * 未登录            → 扫码
     * 已登录但未绑设备  → 登录流程
     * 已登录且已绑设备  → 首页
     注意：后台管理端的登录态不影响这里。 */
  if (state.device) {
    navigate('home');
    return;
  }

  navigate(endUser.isLoggedIn ? 'login' : 'scan');
}

boot();
