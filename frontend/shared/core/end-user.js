/* ============================================================
   终端用户会话（与后台管理端**完全分离**）
   ------------------------------------------------------------
   为什么必须分离
   --------------
   本平台有两套彼此独立的身份体系：

   ==================  ==========================================
   后台管理端          终端用户端（小程序）
   ==================  ==========================================
   平台/商户/工厂账号   消费者手机号
   user_accounts 表    end_users 表
   JWT（含 tenantId）  设备绑定令牌
   管理后台用          智能玩具用
   ==================  ==========================================

   若共用同一份 localStorage 登录态，会出现「已登录管理后台的人打开
   小程序直接跳过扫码激活」这类错误行为，也会让两端的令牌互相覆盖。

   因此终端用户会话使用**独立的键名前缀**，并提供独立令牌，
   供后续 `/miniapp/*` 接口鉴权使用。
   ============================================================ */

import { config } from './config.js';

const PREFIX = `${config.storagePrefix}_enduser`;

const KEYS = {
  token: `${PREFIX}_token`,
  expiresAt: `${PREFIX}_expires_at`,
  profile: `${PREFIX}_profile`,
  deviceId: `${PREFIX}_device_id`,
};

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
    /* 隐私模式下可能失败，退化为单页会话 */
  }
}

export const endUser = {
  /** 终端用户令牌 */
  get token() {
    return read(KEYS.token);
  },

  /** 是否已登录且未过期 */
  get isLoggedIn() {
    const expires = Number(read(KEYS.expiresAt) || 0);
    return Boolean(this.token) && Date.now() < expires;
  },

  /** 用户资料（手机号、昵称等） */
  get profile() {
    const raw = read(KEYS.profile);
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch {
      return null;
    }
  },

  /** 当前绑定设备 ID */
  get deviceId() {
    return read(KEYS.deviceId);
  },

  setDeviceId(deviceId) {
    write(KEYS.deviceId, deviceId);
  },

  /**
   * 保存登录结果。
   * @param {{token:string, expiresIn?:number, profile?:object}} payload
   */
  save({ token, expiresIn = 7 * 24 * 3600, profile }) {
    if (token) write(KEYS.token, token);
    write(KEYS.expiresAt, Date.now() + Number(expiresIn) * 1000);
    if (profile) write(KEYS.profile, JSON.stringify(profile));
  },

  /** 清除终端用户登录态（保留设备绑定信息，便于重新登录后继续使用） */
  clear() {
    write(KEYS.token, null);
    write(KEYS.expiresAt, null);
    write(KEYS.profile, null);
  },

  /** 完全重置（含设备绑定） */
  reset() {
    Object.values(KEYS).forEach((key) => write(key, null));
  },
};

export default endUser;
