/* ============================================================
   应用外壳（App Shell）
   ------------------------------------------------------------
   四端共用的框架装配。负责：
     * 登录态校验与守卫接入（修复 P-05）
     * 侧边导航（浅色导轨，可折叠，移动端抽屉）
     * 顶部条（面包屑 / 命令面板入口 / 用户菜单）
     * 路由装配与页面渲染
     * 命令面板（⌘K）
     * 首次登录强制改密
     * 全局错误提示接入

   各端只需提供菜单与页面映射，调用 createShell() 即可启动。
   ============================================================ */

import { config, currentEnd, isDev } from '../core/config.js';
import auth, { redirectToLogin } from '../core/auth.js';
import api, { setErrorHandler, setUnauthorizedHandler } from '../core/api.js';
import { Router } from '../core/router.js';
import { guard, GUARD_RESULT, HOME_PATHS, PASSWORD_CHANGE_PATH, renderForbidden } from '../core/guard.js';
import { store } from '../core/store.js';
import { clear, delegate, esc, fromHtml, h, raw, uid } from '../ui/dom.js';
import { icon, iconNode } from '../ui/icons.js';
import { avatar, button, dropdown, emptyState, loadingState } from '../ui/components.js';
import { confirmDialog, modal } from '../ui/modal.js';
import toast, { toastApiError } from '../ui/toast.js';
import { CommandPalette, bindCommandShortcut, commandsFromMenus } from '../ui/command.js';

const COLLAPSE_KEY = `${config.storagePrefix}_sidebar_collapsed`;

/* ------------------------------------------------------------
   一、外壳
   ------------------------------------------------------------ */

/**
 * 创建并启动应用外壳。
 *
 * @param {object} options
 * @param {string} options.end          端标识：platform / merchant / factory
 * @param {string} [options.title]      端标题
 * @param {Array}  options.menus        菜单定义
 * @param {Array}  options.routes       路由定义（{ pattern, name, handler, perm, title }）
 * @param {string} [options.home]       首页路径（默认按端推导）
 * @returns {Promise<{router:Router, palette:CommandPalette, refreshBadges:Function}>}
 */
export async function createShell(options) {
  const {
    end = currentEnd(),
    menus = [],
    routes = [],
    home = HOME_PATHS[end] || '/dashboard',
  } = options;

  const endMeta = config.ends[end] || config.ends.platform;
  const title = options.title || endMeta.title;

  /* ---- 1. 登录态前置校验 ---- */
  // 走到这里说明页面已加载，但令牌可能不存在或已过期
  if (!auth.accessToken || !auth.isAuthenticated) {
    auth.clear();
    redirectToLogin({ reason: 'unauthenticated' });
    // 返回 null 表示「已发起跳转，不再装配外壳」
    return null;
  }

  /* ---- 2. 全局错误处理接入 ---- */
  setErrorHandler((error) => {
    if (isDev) console.warn('[api]', error.code, error.message, error.traceId);
    toastApiError(error);
  });
  setUnauthorizedHandler(() => {
    auth.clear();
    redirectToLogin({ reason: 'expired' });
  });

  /* ---- 3. 装配布局 ---- */
  const shell = mountLayout({ end, title, endMeta });

  /* ---- 4. 路由装配 ---- */
  // 注意顺序：先把菜单补全为路由，再一次性注册，
  // 否则补全出来的路由不会被注册（菜单点进去是空白）。
  syncMenusWithRoutes(menus, routes);

  const router = new Router({
    fallback: home,
    onNavigate: (context) => renderRoute(context),
  });

  router.register({
    pattern: PASSWORD_CHANGE_PATH,
    name: 'changePassword',
    title: '修改密码',
    handler: (container) => renderChangePasswordPage(container),
  });

  router.register(routes);

  /* ---- 5. 命令面板 ---- */
  const palette = new CommandPalette({
    placeholder: `搜索 ${endMeta.label} 的页面与操作…`,
  });
  palette.register(commandsFromMenus(menus, (path) => router.go(path)));
  palette.register([
    {
      id: 'action:refresh',
      label: '刷新当前页面',
      group: '操作',
      icon: 'refresh',
      keywords: ['reload', 'shuaxin'],
      run: () => router.dispatch(),
    },
    {
      id: 'action:profile',
      label: '个人信息',
      group: '操作',
      icon: 'user',
      run: () => showProfile(),
    },
    {
      id: 'action:change-password',
      label: '修改密码',
      group: '操作',
      icon: 'key',
      keywords: ['password', 'mima'],
      run: () => showChangePassword(),
    },
    {
      id: 'action:logout',
      label: '退出登录',
      group: '操作',
      icon: 'logout',
      keywords: ['logout', 'tuichu'],
      run: () => doLogout(),
    },
  ]);
  bindCommandShortcut(palette);

  /* ---- 6. 启动路由 ---- */
  router.start();

  return { router, palette, shell, refreshBadges };

  /* ==========================================================
     内部实现
     ========================================================== */

  /** 渲染一次路由 */
  async function renderRoute(context) {
    const { path, query, route, params, notFound } = context;

    /* ---- 未匹配到路由 ---- */
    if (notFound || !route) {
      renderNotFound(shell.content, home);
      updateBreadcrumb([{ label: '页面不存在' }]);
      return;
    }

    /* ---- 守卫 ---- */
    const decision = guard({ end, route, params, query });

    if (decision.result === GUARD_RESULT.REDIRECTED) {
      return; // 正在跳转
    }

    if (decision.result === GUARD_RESULT.PASSWORD_CHANGE) {
      router.go(PASSWORD_CHANGE_PATH, null, { replace: true });
      return;
    }

    if (decision.result === GUARD_RESULT.FORBIDDEN) {
      updateBreadcrumb([{ label: route.title || '无权限' }]);
      renderForbidden(shell.content, {
        required: decision.required,
        onBack: () => router.go(home),
      });
      return;
    }

    /* ---- 更新导航高亮与面包屑 ---- */
    highlightNav(path);
    updateBreadcrumb([{ label: route.title || route.name || path }]);
    store.set('pageTitle', route.title || '');

    /* ---- 渲染页面 ---- */
    clear(shell.content);
    shell.content.append(fromHtml(loadingState()));

    try {
      const result = route.handler(shell.content, { router, params, query, shell });
      if (result instanceof Promise) await result;
    } catch (error) {
      console.error('[shell] 页面渲染失败', error);
      clear(shell.content);
      const tpl = document.createElement('template');
      tpl.innerHTML = emptyState({
        icon: 'alertTriangle',
        title: '页面加载失败',
        desc: error?.message || '发生了未预期的错误',
        action: `<button class="btn btn-primary mt-2" data-action="retry-route">重试</button>
                 <button class="btn mt-2" data-action="go-home">返回工作台</button>`,
      }).trim();
      shell.content.append(tpl.content);
      shell.content.addEventListener('click', (event) => {
        const target = event.target.closest('[data-action]');
        if (target?.dataset.action === 'retry-route') router.dispatch();
        if (target?.dataset.action === 'go-home') router.go(home);
      });
    }
  }
}

/* ------------------------------------------------------------
   二、布局装配
   ------------------------------------------------------------ */

function mountLayout({ end, title, endMeta }) {
  const collapsed =
    window.localStorage.getItem(COLLAPSE_KEY) === 'true' || window.innerWidth <= 1200;

  const brand = h(
    'div',
    { class: 'sidebar-brand', 'data-action': 'go-home' },
    h('span', { class: 'sidebar-brand-logo', text: 'T' }),
    h(
      'div',
      { class: 'sidebar-brand-text' },
      h('span', { class: 'sidebar-brand-name', text: config.appName }),
      h('span', { class: 'sidebar-brand-sub', text: endMeta.label }),
    ),
  );

  const nav = h('nav', { class: 'sidebar-nav', id: 'appNav' });

  const sidebar = h(
    'aside',
    { class: 'app-sidebar', id: 'appSidebar' },
    brand,
    nav,
    h(
      'div',
      { class: 'sidebar-footer' },
      h('div', { class: 'sidebar-footer-text', text: 'v0.1.0' }),
      h('div', { class: 'sidebar-footer-text', text: '© ToyVerse Cloud' }),
    ),
  );

  const toggleBtn = h('button', {
    class: 'topbar-toggle',
    type: 'button',
    'aria-label': '折叠或展开导航',
    'data-action': 'toggle-sidebar',
  });
  toggleBtn.innerHTML = icon('collapse', { size: 18 });

  const breadcrumb = h('div', { class: 'topbar-breadcrumb', id: 'appBreadcrumb' });

  const searchBtn = h(
    'button',
    { class: 'topbar-search', type: 'button', 'data-action': 'open-command', 'aria-label': '打开命令面板' },
    iconNode('search', { size: 15 }),
    h('span', { text: '搜索页面或操作' }),
    h('span', { class: 'topbar-kbd', text: isMac() ? '⌘K' : 'Ctrl K' }),
  );

  const user = auth.user || {};
  const avatarEl = h('span', { class: 'avatar' });
  avatarEl.textContent = (user.nickname || user.account || 'U').slice(0, 1).toUpperCase();

  const userChip = h(
    'div',
    { class: 'topbar-user', 'data-action': 'toggle-user-menu', role: 'button', tabindex: '0' },
    h(
      'div',
      { class: 'topbar-user-meta' },
      h('span', { class: 'topbar-user-name', text: user.nickname || user.account || '用户' }),
      h('span', { class: 'topbar-user-role', text: roleLabel(user.role) }),
    ),
    avatarEl,
  );

  const topbar = h(
    'header',
    { class: 'app-topbar' },
    toggleBtn,
    breadcrumb,
    h('div', { class: 'topbar-spacer' }),
    searchBtn,
    userChip,
  );

  const content = h('main', { class: 'app-content', id: 'appContent' });

  const main = h('div', { class: 'app-main' }, topbar, content);
  const shellEl = h('div', { class: 'app-shell' }, sidebar, main);

  // 用 replaceChildren 而非 innerHTML，避免破坏已绑定的事件
  document.body.dataset.end = end;
  document.body.replaceChildren(shellEl, h('div', { class: 'toast-stack', id: 'toastStack' }));

  shellEl.dataset.collapsed = String(collapsed);

  /* ---- 侧栏折叠 ---- */
  function toggleSidebar() {
    if (window.innerWidth <= 768) {
      sidebar.classList.toggle('open');
      if (sidebar.classList.contains('open')) addScrim();
      else removeScrim();
      return;
    }
    const next = shellEl.dataset.collapsed !== 'true';
    shellEl.dataset.collapsed = String(next);
    window.localStorage.setItem(COLLAPSE_KEY, String(next));
    toggleBtn.innerHTML = icon(next ? 'expand' : 'collapse', { size: 18 });
  }

  let scrim = null;
  function addScrim() {
    if (scrim) return;
    scrim = h('div', { class: 'sidebar-scrim', 'data-action': 'close-sidebar' });
    shellEl.append(scrim);
  }
  function removeScrim() {
    scrim?.remove();
    scrim = null;
  }

  /* ---- 用户菜单 ---- */
  let userMenu = null;
  function toggleUserMenu(anchor) {
    if (userMenu) {
      userMenu.remove();
      userMenu = null;
      return;
    }
    const rect = anchor.getBoundingClientRect();
    userMenu = h('div', { style: { position: 'fixed', zIndex: '500' } });
    const menuTpl = document.createElement('template');
    menuTpl.innerHTML = dropdown(
      [
        { key: 'profile', label: '个人信息', icon: 'user' },
        { key: 'password', label: '修改密码', icon: 'key' },
        { divider: true },
        { key: 'logout', label: '退出登录', icon: 'logout', danger: true },
      ],
      { action: 'user-menu' },
    ).trim();
    userMenu.append(menuTpl.content);
    userMenu.style.top = `${rect.bottom + 6}px`;
    userMenu.style.right = `${window.innerWidth - rect.right}px`;
    document.body.append(userMenu);

    const off = delegate(userMenu, 'click', '[data-action="user-menu"]', (event, target) => {
      off();
      userMenu?.remove();
      userMenu = null;
      switch (target.dataset.key) {
        case 'profile':
          showProfile();
          break;
        case 'password':
          showChangePassword();
          break;
        case 'logout':
          doLogout();
          break;
        default:
          break;
      }
    });
  }

  /* ---- 全局事件委托 ---- */
  shellEl.addEventListener('click', (event) => {
    const target = event.target.closest('[data-action]');
    if (!target) {
      userMenu?.remove();
      userMenu = null;
      return;
    }
    switch (target.dataset.action) {
      case 'toggle-sidebar':
        toggleSidebar();
        break;
      case 'close-sidebar':
        sidebar.classList.remove('open');
        removeScrim();
        break;
      case 'toggle-user-menu':
        toggleUserMenu(target);
        break;
      case 'go-home':
        window.location.hash = `#${HOME_PATHS[end] || '/dashboard'}`;
        break;
      default:
        break;
    }
  });

  window.addEventListener('resize', () => {
    if (window.innerWidth > 768) {
      sidebar.classList.remove('open');
      removeScrim();
    }
  });

  return { el: shellEl, sidebar, nav, content, breadcrumb, toggleBtn };
}

/* ------------------------------------------------------------
   三、导航渲染
   ------------------------------------------------------------ */

/**
 * 由菜单定义渲染侧边导航。
 * @param {HTMLElement} navEl
 * @param {Array} menus
 * @param {(path:string)=>void} navigate
 */
export function renderNav(navEl, menus, navigate) {
  clear(navEl);

  menus.forEach((group) => {
    const items = (group.items || []).filter((item) => item.path && !item.hidden);
    if (!items.length) return;

    if (group.groupTitle) {
      navEl.append(h('div', { class: 'nav-group-title', text: group.groupTitle }));
    }

    const groupEl = h('div', { class: 'nav-group' });

    items.forEach((item) => {
      const itemIcon = iconNode(item.icon || 'chevronRight', { size: 18 });

      const node = h(
        'div',
        {
          class: 'nav-item',
          'data-path': item.path,
          role: 'link',
          tabindex: '0',
          title: item.label,
        },
        h('span', { class: 'nav-item-icon' }, itemIcon),
        h('span', { class: 'nav-item-label', text: item.label }),
        item.badge
          ? h('span', {
              class: 'nav-item-badge',
              'data-tone': item.badgeTone || 'brand',
              text: String(item.badge),
              'data-badge-key': item.badgeKey || '',
            })
          : null,
      );

      node.addEventListener('click', () => navigate(item.path));
      node.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          navigate(item.path);
        }
      });

      groupEl.append(node);
    });

    navEl.append(groupEl);
  });
}

/* ------------------------------------------------------------
   四、辅助渲染
   ------------------------------------------------------------ */

/** 高亮当前导航项 */
function highlightNav(path) {
  document.querySelectorAll('.nav-item').forEach((el) => {
    const itemPath = el.dataset.path;
    const active = itemPath === path || (itemPath !== '/' && path.startsWith(`${itemPath}/`));
    el.classList.toggle('active', active);
  });
}

/** 更新顶部面包屑 */
function updateBreadcrumb(items) {
  const el = document.getElementById('appBreadcrumb');
  if (!el) return;
  clear(el);
  const prefix = config.appName;
  el.append(h('span', { class: 'breadcrumb-item', text: prefix }));
  items.forEach((item) => {
    el.append(h('span', { class: 'breadcrumb-sep', text: '/' }));
    el.append(
      h('span', { class: 'breadcrumb-item current', text: item.label }),
    );
  });
}

/** 404 视图 */
function renderNotFound(container, home) {
  clear(container);
  const tpl = document.createElement('template');
  tpl.innerHTML = emptyState({
    icon: 'search',
    title: '页面不存在',
    desc: '地址可能已失效，或该页面不属于当前端。',
    action: '<button class="btn btn-primary mt-2" data-action="go-home">返回工作台</button>',
  }).trim();
  container.append(tpl.content);
  container.querySelector('[data-action="go-home"]')?.addEventListener('click', () => {
    window.location.hash = `#${home}`;
  });
}

/* ------------------------------------------------------------
   五、菜单与路由同步
   ------------------------------------------------------------ */

/**
 * 把菜单项注册为路由（若尚未注册）。
 *
 * 这样菜单与路由只需维护一处：菜单项提供 path / title / perm，
 * 页面提供 handler。避免两处定义不一致。
 */
function syncMenusWithRoutes(menus, routes) {
  const existing = new Set(routes.map((route) => route.pattern));

  menus.forEach((group) => {
    (group.items || []).forEach((item) => {
      if (!item.path || existing.has(item.path)) return;
      // 菜单项没有页面实现时，渲染「建设中」占位
      routes.push({
        pattern: item.path,
        name: item.key || item.path.replace(/\//g, '-'),
        title: item.label,
        perm: item.perm,
        handler: (container) => {
          clear(container);
          const tpl = document.createElement('template');
          tpl.innerHTML = emptyState({
            icon: 'layers',
            title: `${item.label}（建设中）`,
            desc: '该页面将在后续阶段交付。',
          }).trim();
          container.append(tpl.content);
        },
      });
    });
  });
}

/* ------------------------------------------------------------
   六、用户操作
   ------------------------------------------------------------ */

function roleLabel(roleCode) {
  const map = {
    PLATFORM_ADMIN: '平台超级管理员',
    PLATFORM_OPERATOR: '平台运营',
    MERCHANT_ADMIN: '商户管理员',
    MERCHANT_OPERATOR: '商户运营',
    FACTORY_ADMIN: '工厂管理员',
    FACTORY_OPERATOR: '工厂操作员',
  };
  return map[roleCode] || roleCode || '';
}

/** 个人信息弹窗 */
export function showProfile() {
  const user = auth.user || {};
  const rows = [
    ['账号', user.account],
    ['昵称', user.nickname],
    ['角色', roleLabel(user.role)],
    ['角色类型', user.roleType],
    ['所属租户', user.tenantName || (user.tenantId ?? '（平台/工厂账号，不属于任何租户）')],
    ['权限数量', `${(user.permissions || []).length} 项`],
  ];

  modal({
    title: '个人信息',
    size: 'sm',
    body: `<dl class="desc-list">${rows
      .map(
        ([k, v]) =>
          `<dt>${esc(k)}</dt><dd>${esc(v ?? '-')}</dd>`,
      )
      .join('')}</dl>`,
    footer: (dlg) => {
      const close = h('button', { class: 'btn', type: 'button', text: '关闭' });
      close.addEventListener('click', () => dlg.close());
      const pwd = h('button', { class: 'btn btn-primary', type: 'button', text: '修改密码' });
      pwd.addEventListener('click', () => {
        dlg.close();
        showChangePassword();
      });
      return [close, pwd];
    },
  });
}

/** 修改密码弹窗 */
export function showChangePassword({ forced = false } = {}) {
  const formId = uid('pwd');

  const dialog = modal({
    title: forced ? '首次登录需修改密码' : '修改密码',
    subtitle: forced ? '为保障账号安全，请先设置新密码后继续使用' : '',
    size: 'sm',
    // 强制改密时不允许通过遮罩 / ESC / 关闭按钮绕过
    closeOnMask: !forced,
    closeOnEsc: !forced,
    closable: !forced,
    body: `
      <form id="${formId}" class="form-rows" novalidate autocomplete="off">
        <div class="field">
          <label class="field-label field-required" for="${formId}-old">当前密码</label>
          <input class="input" type="password" id="${formId}-old" name="oldPassword"
                 autocomplete="current-password" data-autofocus />
        </div>
        <div class="field">
          <label class="field-label field-required" for="${formId}-new">新密码</label>
          <input class="input" type="password" id="${formId}-new" name="newPassword"
                 autocomplete="new-password" />
          <div class="field-hint">至少 8 位，不能为纯数字，不能使用常见弱口令</div>
        </div>
        <div class="field">
          <label class="field-label field-required" for="${formId}-confirm">确认新密码</label>
          <input class="input" type="password" id="${formId}-confirm" name="confirmPassword"
                 autocomplete="new-password" />
        </div>
        <div class="field-error" data-error></div>
      </form>
    `,
    footer: (dlg) => {
      const cancel = h('button', { class: 'btn', type: 'button', text: forced ? '退出登录' : '取消' });
      cancel.addEventListener('click', () => {
        dlg.close();
        if (forced) doLogout();
      });

      const submit = h('button', { class: 'btn btn-primary', type: 'button', text: '确认修改' });
      submit.addEventListener('click', async () => {
        const form = dialog.el.querySelector(`#${formId}`);
        const oldPassword = form.querySelector('[name="oldPassword"]').value;
        const newPassword = form.querySelector('[name="newPassword"]').value;
        const confirmPassword = form.querySelector('[name="confirmPassword"]').value;
        const errorEl = form.querySelector('[data-error]');

        errorEl.textContent = '';

        if (!oldPassword) {
          errorEl.textContent = '请输入当前密码';
          return;
        }
        if (newPassword.length < 8) {
          errorEl.textContent = '新密码长度不得少于 8 位';
          return;
        }
        if (newPassword !== confirmPassword) {
          errorEl.textContent = '两次输入的新密码不一致';
          return;
        }
        if (newPassword === oldPassword) {
          errorEl.textContent = '新密码不能与当前密码相同';
          return;
        }

        submit.disabled = true;
        submit.textContent = '提交中…';
        try {
          // 注意：此处的 api 是 HTTP 客户端（模块级导入），
          // 与回调参数 dlg（弹窗控制对象）刻意区分命名，避免遮蔽。
          await api.post('/me/password', { oldPassword, newPassword });
          toast.success('密码修改成功，请重新登录');
          dlg.close();
          setTimeout(() => {
            auth.clear();
            // 改密后不设回跳：原地址是「改密页」，回跳过去没有意义
            redirectToLogin({ reason: 'password-changed', redirect: '' });
          }, 800);
        } catch (error) {
          submit.disabled = false;
          submit.textContent = '确认修改';
          errorEl.textContent = error?.message || '修改失败，请稍后重试';
        }
      });

      // 回车快捷提交
      dlg.submit = () => submit.click();

      return [cancel, submit];
    },
  });

  return dialog.result;
}

/** 修改密码整页视图（守卫检测到 mustChangePassword 时跳转至此） */
function renderChangePasswordPage(container) {
  clear(container);
  const tpl = document.createElement('template');
  tpl.innerHTML = `
    <div class="card" style="max-width:420px;margin:48px auto;text-align:center">
      <div class="empty-icon" style="margin-bottom:12px">${icon('lock', { size: 36 })}</div>
      <div class="empty-title">首次登录需修改密码</div>
      <div class="empty-desc" style="text-align:left;margin-top:12px">
        为保障账号安全，系统要求首次登录后修改初始密码。
        修改成功后需要重新登录。
      </div>
      <button class="btn btn-primary btn-block mt-4" data-action="open-change-password">
        立即修改密码
      </button>
    </div>
  `.trim();
  container.append(tpl.content);

  container.querySelector('[data-action="open-change-password"]')?.addEventListener('click', () => {
    showChangePassword({ forced: true });
  });

  // 直接弹出，减少一次点击
  showChangePassword({ forced: true });
}

/** 退出登录 */
export async function doLogout() {
  const { confirmed } = await confirmDialog({
    title: '退出登录',
    description: '确定要退出当前账号吗？',
    tone: 'warning',
    confirmText: '退出',
  });
  if (!confirmed) return;

  try {
    // 通知后端吊销刷新令牌；失败也不阻塞本地退出
    await api.post('/auth/logout', { refreshToken: auth.refreshToken }, { silent: true });
  } catch {
    /* 忽略 */
  }
  auth.clear();
  redirectToLogin();
}

/* ------------------------------------------------------------
   七、菜单徽标
   ------------------------------------------------------------ */

/** 更新导航徽标（供工作台等页面调用） */
export function refreshBadges(badges = {}) {
  document.querySelectorAll('[data-badge-key]').forEach((el) => {
    const key = el.dataset.badgeKey;
    const value = badges[key];
    if (value === undefined || value === null || value === 0 || value === '') {
      el.remove();
      return;
    }
    el.textContent = String(value);
  });
}

function isMac() {
  return typeof navigator !== 'undefined' && /mac/i.test(navigator.platform || '');
}

export default { createShell, renderNav, showProfile, showChangePassword, doLogout, refreshBadges };
