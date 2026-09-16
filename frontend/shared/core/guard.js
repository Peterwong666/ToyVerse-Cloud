/* ============================================================
   路由守卫
   ------------------------------------------------------------
   解决的问题（对应遗留缺陷 P-05）：
     旧原型没有守卫，直接访问子页面 URL 会得到空白或错误页面
     （因为角色/租户信息不存在）。本模块在每次导航前逐项校验。

   校验顺序：
     1. 末登录 → 跳登录页（带回跳地址）
     2. 令牌过期且无法刷新 → 跳登录页
     3. 端不匹配（商户账号打开了平台端）→ 跳到该账号对应的端
     4. 权限不足 → 渲染 403 视图，不跳转（保留上下文，避免用户困惑）
     5. PasswordChangeRequired → 强制跳改密页
   ============================================================ */

import auth, { redirectToEnd, redirectToLogin } from './auth.js';
import { clear } from '../ui/dom.js';
import { config } from './config.js';

/** 守卫结果类型 */
export const GUARD_RESULT = {
  ALLOW: 'allow',
  REDIRECTED: 'redirected',
  FORBIDDEN: 'forbidden',
  PASSWORD_CHANGE: 'password-change',
};

/** 角色类型 → 所属端 */
const ROLE_TYPE_TO_END = {
  PLATFORM: 'platform',
  MERCHANT: 'merchant',
  FACTORY: 'factory',
};

/** 各端的首页路径 */
export const HOME_PATHS = {
  platform: '/dashboard',
  merchant: '/dashboard',
  factory: '/dashboard',
  miniapp: '/home',
};

/** 改密页路径（各端共用同一个路径，渲染由应用壳决定） */
export const PASSWORD_CHANGE_PATH = '/change-password';

/**
 * 校验一次导航是否放行。
 *
 * @param {object} context
 * @param {string} context.end            当前端
 * @param {object} context.route          命中的路由定义（可为 null）
 * @param {object} context.params
 * @param {object} context.query
 * @returns {{result:string, redirect?:string, reason?:string, required?:string}}
 */
export function guard({ end, route, params, query }) {
  /* ---- 1. 登录校验 ---- */
  if (!auth.accessToken) {
    redirectToLogin({ reason: 'unauthenticated' });
    return { result: GUARD_RESULT.REDIRECTED, reason: 'unauthenticated' };
  }

  if (!auth.isAuthenticated) {
    // 有令牌但已过期：清掉后跳登录页
    // （api.js 会在有 refreshToken 时先尝试自动刷新；
    //   走到这里说明刷新也失败了）
    auth.clear();
    redirectToLogin({ reason: 'expired' });
    return { result: GUARD_RESULT.REDIRECTED, reason: 'expired' };
  }

  /* ---- 2. 首次登录强制改密 ---- */
  const user = auth.user;
  if (user?.mustChangePassword && route?.name !== 'changePassword') {
    return { result: GUARD_RESULT.PASSWORD_CHANGE };
  }

  /* ---- 3. 端与角色匹配 ---- */
  const expectedEnd = ROLE_TYPE_TO_END[auth.roleType];
  if (expectedEnd && expectedEnd !== end) {
    // 商户账号打开了平台端 → 送回它自己的端，而不是报错
    redirectToEnd(expectedEnd);
    return {
      result: GUARD_RESULT.REDIRECTED,
      reason: 'wrong-end',
      redirect: config.basePaths[expectedEnd],
    };
  }

  /* ---- 4. 权限校验 ---- */
  if (route?.perm && !auth.hasPerm(route.perm)) {
    // 不跳转，交由页面渲染 403 视图：保留用户当前位置，
    // 便于其理解「我没有这个权限」而不是「页面打不开」
    return {
      result: GUARD_RESULT.FORBIDDEN,
      reason: 'no-permission',
      required: route.perm,
    };
  }

  return { result: GUARD_RESULT.ALLOW };
}

/**
 * 渲染 403 视图。
 *
 * 刻意不跳转：保留用户当前位置，让其理解「我没有这个权限」，
 * 而不是感到「页面打不开」。
 *
 * @param {HTMLElement} container
 * @param {{required?:string, onBack?:Function}} options
 */
export function renderForbidden(container, { required, onBack } = {}) {
  clear(container);
  container.innerHTML = `
    <div class="empty">
      <div class="empty-icon">🔒</div>
      <div class="empty-title">无访问权限</div>
      <div class="empty-desc">
        当前账号没有访问该页面所需的权限。
        ${required ? `<br><span class="mono text-xs">缺少权限：${required}</span>` : ''}
        <br>如确需访问，请联系管理员为你的角色授予对应权限。
      </div>
      <button class="btn btn-primary mt-3" id="forbiddenBack">返回工作台</button>
    </div>
  `;
  container.querySelector('#forbiddenBack')?.addEventListener('click', () => {
    if (typeof onBack === 'function') onBack();
  });
}

/**
 * 校验当前端是否与账号匹配（用于页面首屏，守卫之外的兜底保护）。
 * @param {string} end
 * @returns {boolean}
 */
export function assertEndMatches(end) {
  const expected = ROLE_TYPE_TO_END[auth.roleType];
  if (!expected) return true;
  if (expected === end) return true;
  redirectToEnd(expected);
  return false;
}

export default guard;
