/* ============================================================
   端启动引导
   ------------------------------------------------------------
   三个后台端（平台 / 商户 / 工厂）的启动流程完全一致，
   统一在此实现，各端的 main.js 只需提供路由表。

   流程：
     1. 校验登录态（否则跳登录页）
     2. 按权限过滤菜单
     3. 装配外壳（布局 + 路由 + 守卫 + 命令面板）
     4. 渲染侧边导航
     5. 更新菜单徽标（待办数量等）
   ============================================================ */

import auth from '../core/auth.js';
import api from '../core/api.js';
import { MENUS_BY_END, filterMenusByPermission } from './menus.js';
import { createShell, renderNav, refreshBadges } from './shell.js';

/**
 * 启动一个后台端。
 *
 * @param {object} o
 * @param {'platform'|'merchant'|'factory'} o.end
 * @param {Array} o.routes 该端的路由定义
 * @param {string} [o.home] 首页路径
 * @param {string} [o.title] 端标题
 * @returns {Promise<{router:object, palette:object} | null>}
 *          已登录时返回外壳实例；未登录已跳转时返回 null
 */
export async function bootEnd({ end, routes = [], home = '/dashboard', title = '' }) {
  /* ---- 0. 刷新用户信息 ---- */
  // 本地缓存的用户信息可能已过期（权限变更、租户被禁用、需强制改密等），
  // 因此启动时用 /me 拉取一次最新状态。失败不阻塞启动——
  // 若令牌已失效，api 层会触发 401 处理并跳转登录页。
  await refreshCurrentUser();

  /* ---- 1. 按权限过滤菜单（无权限的入口直接不显示） ---- */
  const menus = filterMenusByPermission(MENUS_BY_END[end] || [], (perm) => auth.hasPerm(perm));

  /* ---- 2 & 3. 装配外壳 ---- */
  const shell = await createShell({ end, title, menus, routes, home });
  if (!shell) return null;

  /* ---- 4. 渲染导航 ---- */
  renderNav(shell.shell.nav, menus, (path) => shell.router.go(path));

  /* ---- 5. 标记导航高亮（首屏） ---- */
  syncActiveNav(shell.shell.nav, shell.router.path);

  /* ---- 6. 异步拉取菜单徽标，不阻塞首屏 ---- */
  loadMenuBadges(end).then((badges) => {
    Object.entries(badges).forEach(([key, value]) => {
      updateMenuBadge(shell.shell.nav, key, value);
    });
  });

  return shell;
}

/**
 * 用 ``/me`` 刷新本地缓存的用户信息。
 *
 * ``/me`` 返回的字段与登录响应中的 ``user`` 结构一致（另含 lastLoginAt
 * 与 mustChangePassword），因此可直接覆盖缓存。
 * 失败时静默忽略，交由 api 层的 401 处理负责跳转。
 */
async function refreshCurrentUser() {
  try {
    const me = await api.get('/me', { silent: true });
    if (me && me.userId) auth.setUser(me);
  } catch {
    /* 令牌失效时 api 层会跳登录页；其它错误不阻塞启动 */
  }
}

/** 同步导航高亮 */
export function syncActiveNav(navEl, path) {
  navEl.querySelectorAll('.nav-item').forEach((el) => {
    const itemPath = el.dataset.path;
    const active = itemPath === path || (itemPath !== '/' && path.startsWith(`${itemPath}/`));
    el.classList.toggle('active', active);
  });
}

/** 更新某个菜单徽标 */
export function updateMenuBadge(navEl, key, value) {
  const el = navEl.querySelector(`[data-badge-key="${key}"]`);
  if (!el) return;
  if (!value) {
    el.remove();
    return;
  }
  el.textContent = String(value);
}

/**
 * 拉取各端的菜单徽标（待办数量）。
 * 失败时静默忽略——徽标只是辅助信息，不应影响主流程。
 */
async function loadMenuBadges(end) {
  const badges = {};

  try {
    if (end === 'platform') {
      // 待审核订单数
      const result = await api.get('/platform/orders', {
        params: { pageSize: 1, status: 'PENDING_AUDIT' },
        silent: true,
      });
      badges.pendingOrders = result?.total || 0;
    } else if (end === 'merchant') {
      const result = await api.get('/merchant/orders', {
        params: { pageSize: 1, status: 'PENDING_AUDIT' },
        silent: true,
      });
      badges.pendingOrders = result?.total || 0;
    } else if (end === 'factory') {
      const result = await api.get('/factory/orders', {
        params: { pageSize: 1, status: 'PENDING' },
        silent: true,
      });
      badges.pendingOrders = result?.total || 0;
    }
  } catch {
    // 相关接口尚未交付时忽略（后续阶段会补上）
  }

  return badges;
}

export default { bootEnd, syncActiveNav, updateMenuBadge, refreshBadges };
