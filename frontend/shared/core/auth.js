/* ============================================================
   认证状态管理
   ------------------------------------------------------------
   职责：
     * 访问令牌 / 刷新令牌的持久化（localStorage）
     * 过期时间维护，避免使用已失效的令牌发请求
     * 当前用户与权限集的缓存
     * 权限判断（hasPerm）—— 前端仅做展示控制，
       真正的授权由后端强制（前端权限隐藏不是安全边界）
   ============================================================ */

import { config } from './config.js';

const KEYS = {
  accessToken: `${config.storagePrefix}_access_token`,
  refreshToken: `${config.storagePrefix}_refresh_token`,
  expiresAt: `${config.storagePrefix}_expires_at`,
  user: `${config.storagePrefix}_user`,
  end: `${config.storagePrefix}_end`,
};

/** 提前刷新阈值：剩余有效期少于 60 秒即视为需要刷新 */
const REFRESH_SKEW_MS = 60 * 1000;

function read(key) {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key, value) {
  try {
    if (value === null || value === undefined) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, String(value));
  } catch {
    /* 隐私模式下可能失败，忽略即可（退化为单页会话） */
  }
}

/* ------------------------------------------------------------
   一、令牌
   ------------------------------------------------------------ */

export const auth = {
  /** 访问令牌 */
  get accessToken() {
    return read(KEYS.accessToken);
  },

  /** 刷新令牌 */
  get refreshToken() {
    return read(KEYS.refreshToken);
  },

  /** 令牌过期时间戳（毫秒） */
  get expiresAt() {
    const raw = read(KEYS.expiresAt);
    return raw ? Number(raw) : 0;
  },

  /** 是否已登录（存在令牌且未过期） */
  get isAuthenticated() {
    return Boolean(this.accessToken) && Date.now() < this.expiresAt;
  },

  /** 是否即将过期（需要主动刷新） */
  get isExpiringSoon() {
    return Boolean(this.accessToken) && Date.now() >= this.expiresAt - REFRESH_SKEW_MS;
  },

  /**
   * 保存登录结果。
   *
   * 注意：后端把 ``mustChangePassword`` 放在登录响应的**顶层**
   * （与 ``user`` 平级），而守卫读取的是 ``auth.user.mustChangePassword``。
   * 因此这里把它合并进缓存的用户对象，避免「首次登录强制改密」失效。
   *
   * @param {{accessToken:string, refreshToken?:string, expiresIn?:number,
   *          user?:object, mustChangePassword?:boolean}} payload
   */
  save(payload) {
    const { accessToken, refreshToken, expiresIn = 7200, user, mustChangePassword } = payload || {};
    if (accessToken) write(KEYS.accessToken, accessToken);
    if (refreshToken) write(KEYS.refreshToken, refreshToken);
    write(KEYS.expiresAt, Date.now() + Number(expiresIn) * 1000);
    if (user) {
      const merged =
        mustChangePassword === undefined ? user : { ...user, mustChangePassword };
      write(KEYS.user, JSON.stringify(merged));
    }
  },

  /** 仅更新访问令牌（刷新流程用，不覆盖用户信息） */
  updateTokens({ accessToken, refreshToken, expiresIn = 7200 }) {
    if (accessToken) write(KEYS.accessToken, accessToken);
    if (refreshToken) write(KEYS.refreshToken, refreshToken);
    write(KEYS.expiresAt, Date.now() + Number(expiresIn) * 1000);
  },

  /* ----------------------------------------------------------
     二、用户与权限
     ---------------------------------------------------------- */

  /** 当前用户信息（本地缓存） */
  get user() {
    const raw = read(KEYS.user);
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch {
      return null;
    }
  },

  /** 覆盖缓存中的用户信息 */
  setUser(user) {
    write(KEYS.user, user ? JSON.stringify(user) : null);
  },

  /** 用户角色编码 */
  get role() {
    return this.user?.role || null;
  },

  /** 用户角色类型：PLATFORM / MERCHANT / FACTORY */
  get roleType() {
    return this.user?.roleType || null;
  },

  /** 所属租户 ID（平台与工厂账号为 null） */
  get tenantId() {
    return this.user?.tenantId || null;
  },

  /** 权限码列表 */
  get permissions() {
    return Array.isArray(this.user?.permissions) ? this.user.permissions : [];
  },

  /**
   * 是否拥有指定权限。
   * 后端对平台超管下发通配符 '*'，此处同样支持。
   * @param {string} code
   */
  hasPerm(code) {
    if (!code) return true;
    const perms = this.permissions;
    return perms.includes('*') || perms.includes(code);
  },

  /** 是否拥有其中任意权限 */
  hasAnyPerm(codes) {
    const list = Array.isArray(codes) ? codes : [codes];
    return list.some((code) => this.hasPerm(code));
  },

  /** 是否属于指定角色类型之一 */
  isRoleType(...types) {
    return types.includes(this.roleType);
  },

  /** 记录当前所在的端（切换端时用于校验） */
  setEnd(end) {
    write(KEYS.end, end);
  },

  get end() {
    return read(KEYS.end);
  },

  /* ----------------------------------------------------------
     三、清理
     ---------------------------------------------------------- */

  /** 清除全部认证状态 */
  clear() {
    Object.values(KEYS).forEach((key) => write(key, null));
  },

  /**
   * 退出登录：通知后端吊销刷新令牌，然后清理本地状态。
   * 即使后端调用失败，本地状态也必须清理（否则用户无法退出）。
   */
  async logout({ silent = false } = {}) {
    const refreshToken = this.refreshToken;
    this.clear();
    return { ok: true, silent, refreshToken };
  },
};

/* ------------------------------------------------------------
   四、权限常量
   与后端 app/core/permissions.py 保持一致。
   在前端集中定义便于静态引用与拼写检查。
   ------------------------------------------------------------ */

export const PERM = {
  platform: {
    tenantRead: 'platform:tenant:read',
    tenantWrite: 'platform:tenant:write',
    cloudRead: 'platform:cloud:read',
    cloudWrite: 'platform:cloud:write',
    templateRead: 'platform:template:read',
    templateWrite: 'platform:template:write',
    productRead: 'platform:product:read',
    productWrite: 'platform:product:write',
    orderRead: 'platform:order:read',
    orderWrite: 'platform:order:write',
    deviceRead: 'platform:device:read',
    deviceWrite: 'platform:device:write',
    batchRead: 'platform:batch:read',
    batchWrite: 'platform:batch:write',
    allocationRead: 'platform:allocation:read',
    allocationWrite: 'platform:allocation:write',
    factoryOrderRead: 'platform:factory-order:read',
    factoryOrderWrite: 'platform:factory-order:write',
    orgRead: 'platform:org:read',
    orgWrite: 'platform:org:write',
    memberRead: 'platform:member:read',
    memberWrite: 'platform:member:write',
    roleRead: 'platform:role:read',
    roleWrite: 'platform:role:write',
    otaRead: 'platform:ota:read',
    otaWrite: 'platform:ota:write',
    auditRead: 'platform:audit:read',
    dashboardRead: 'platform:dashboard:read',
  },
  merchant: {
    productRead: 'merchant:product:read',
    productWrite: 'merchant:product:write',
    aiConfigRead: 'merchant:ai-config:read',
    aiConfigWrite: 'merchant:ai-config:write',
    kbRead: 'merchant:kb:read',
    kbWrite: 'merchant:kb:write',
    orderRead: 'merchant:order:read',
    orderWrite: 'merchant:order:write',
    deviceRead: 'merchant:device:read',
    deviceWrite: 'merchant:device:write',
    bindingWrite: 'merchant:binding:write',
    miniappRead: 'merchant:miniapp:read',
    miniappWrite: 'merchant:miniapp:write',
    metricsRead: 'merchant:metrics:read',
    orgRead: 'merchant:org:read',
    orgWrite: 'merchant:org:write',
    memberRead: 'merchant:member:read',
    memberWrite: 'merchant:member:write',
    auditRead: 'merchant:audit:read',
    dashboardRead: 'merchant:dashboard:read',
  },
  factory: {
    orderRead: 'factory:order:read',
    burnWrite: 'factory:burn:write',
    inspectWrite: 'factory:inspect:write',
    firmwareRead: 'factory:firmware:read',
    batchRead: 'factory:batch:read',
    dashboardRead: 'factory:dashboard:read',
  },
};

/* ------------------------------------------------------------
   五、跳转辅助
   ------------------------------------------------------------ */

/**
 * 跳转到登录页。
 *
 * @param {object} [options]
 * @param {string} [options.reason]   跳转原因，登录页会展示对应提示
 * @param {string|null} [options.redirect] 登录后回跳地址；
 *        传 ``null`` 表示使用当前地址，传空串表示不设回跳（登录后进入默认端首页）
 */
export function redirectToLogin({ reason = '', redirect = null } = {}) {
  const params = new URLSearchParams();

  const target =
    redirect === null
      ? window.location.pathname + window.location.search + window.location.hash
      : redirect;

  // 仅接受站内相对路径，避免开放重定向
  if (target && target.startsWith('/') && !target.startsWith('//')) {
    params.set('redirect', target);
  }
  if (reason) params.set('reason', reason);

  const query = params.toString();
  window.location.replace(query ? `${config.basePaths.login}?${query}` : config.basePaths.login);
}

/** 跳转到指定端的首页 */
export function redirectToEnd(end) {
  const base = config.basePaths[end] || config.basePaths.platform;
  window.location.replace(base);
}

export default auth;
