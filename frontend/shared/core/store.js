/* ============================================================
   轻量状态容器（发布订阅）
   ------------------------------------------------------------
   不引入任何状态管理库：四端共享同一份实现，避免各端各自造轮子。

   能力：
     * 具名 state 槽位，支持点路径读写（'user.name'）
     * 订阅变更（支持按 key 精确订阅）
     * 批量合并更新，避免多次重渲染
     * 页面卸载时批量取消订阅，防止内存泄漏
   ============================================================ */

/** 按点路径取值 */
function getByPath(target, path) {
  return String(path)
    .split('.')
    .reduce((acc, key) => (acc === null || acc === undefined ? undefined : acc[key]), target);
}

/** 按点路径赋值（自动创建中间对象） */
function setByPath(target, path, value) {
  const keys = String(path).split('.');
  const last = keys.pop();
  let cursor = target;
  for (const key of keys) {
    if (cursor[key] === null || typeof cursor[key] !== 'object') cursor[key] = {};
    cursor = cursor[key];
  }
  cursor[last] = value;
  return target;
}

export class Store {
  constructor(initial = {}) {
    this.state = { ...initial };
    /** @type {Map<string, Set<Function>>} */
    this.listeners = new Map();
    this._batchDepth = 0;
    /** @type {Set<string>} */
    this._pendingKeys = new Set();
    this._flushScheduled = false;
  }

  /* ----------------------------------------------------------
     读取
     ---------------------------------------------------------- */

  /** 读取整个状态（浅拷贝，避免外部直接改内部对象） */
  get all() {
    return { ...this.state };
  }

  /**
   * 读取某个 key（支持点路径）。
   * @param {string} [key] 不传则返回整个状态
   */
  get(key) {
    if (key === undefined) return this.all;
    return getByPath(this.state, key);
  }

  /* ----------------------------------------------------------
     写入
     ---------------------------------------------------------- */

  /**
   * 设置单个 key。
   * @param {string} key 支持点路径
   * @param {any} value
   */
  set(key, value) {
    const previous = getByPath(this.state, key);
    if (Object.is(previous, value)) return; // 值未变则不通知

    setByPath(this.state, key, value);
    this._notify(key);
  }

  /**
   * 批量合并更新。
   * @param {object} patch 顶层键的浅合并
   */
  merge(patch = {}) {
    let changed = false;
    for (const [key, value] of Object.entries(patch)) {
      if (!Object.is(this.state[key], value)) {
        this.state[key] = value;
        changed = true;
      }
    }
    if (changed) this._notify(Object.keys(patch).join(','));
  }

  /** 重置为初始值 */
  reset(initial = {}) {
    this.state = { ...initial };
    this._notify('*');
  }

  /* ----------------------------------------------------------
     订阅
     ---------------------------------------------------------- */

  /**
   * 订阅变更。
   * @param {string|string[]} keys 关注的 key（'*' 表示全部）
   * @param {Function} handler (state, changedKeys) => void
   * @returns {Function} 取消订阅函数
   */
  subscribe(keys, handler) {
    const list = Array.isArray(keys) ? keys : [keys];
    list.forEach((key) => {
      if (!this.listeners.has(key)) this.listeners.set(key, new Set());
      this.listeners.get(key).add(handler);
    });
    return () => list.forEach((key) => this.listeners.get(key)?.delete(handler));
  }

  /** 只订阅一次 */
  once(key, handler) {
    const off = this.subscribe(key, (...args) => {
      off();
      handler(...args);
    });
    return off;
  }

  /** 清空全部订阅（页面卸载时调用） */
  clearListeners() {
    this.listeners.clear();
  }

  /* ----------------------------------------------------------
     批量更新
     ---------------------------------------------------------- */

  /**
   * 在回调内多次 set 只触发一次通知。
   * @param {Function} fn
   */
  batch(fn) {
    this._batchDepth += 1;
    try {
      fn();
    } finally {
      this._batchDepth -= 1;
      if (this._batchDepth === 0) this._flush();
    }
  }

  /* ----------------------------------------------------------
     内部
     ---------------------------------------------------------- */

  _notify(key) {
    this._pendingKeys.add(key);
    if (this._batchDepth > 0) return;

    // 同一帧内多次写入合并为一次通知
    if (this._flushScheduled) return;
    this._flushScheduled = true;
    const run = () => {
      this._flushScheduled = false;
      this._flush();
    };
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(run);
    else setTimeout(run, 0);
  }

  _flush() {
    const keys = Array.from(this._pendingKeys);
    this._pendingKeys.clear();
    if (!keys.length) return;

    const notified = new Set();
    const state = this.all;

    const dispatch = (key) => {
      this.listeners.get(key)?.forEach((handler) => {
        if (notified.has(handler)) return;
        notified.add(handler);
        try {
          handler(state, keys);
        } catch (error) {
          console.error('[store] 订阅回调执行失败', error);
        }
      });
    };

    dispatch('*');
    keys.forEach(dispatch);

    // 支持 'user.*' 形式的父级订阅
    keys.forEach((key) => {
      const segments = key.split('.');
      while (segments.length > 1) {
        segments.pop();
        dispatch(`${segments.join('.')}.*`);
      }
    });
  }
}

/* ------------------------------------------------------------
   四端共享的应用状态
   ------------------------------------------------------------ */

export const store = new Store({
  /** 当前端：platform / merchant / factory / miniapp */
  end: 'platform',

  /** 侧栏折叠状态 */
  sidebarCollapsed: false,

  /** 侧栏抽屉是否展开（移动端） */
  sidebarOpen: false,

  /** 当前页面标题，供顶部面包屑使用 */
  pageTitle: '',

  /** 当前页面的加载态 */
  loading: false,

  /** 全局待办计数（用于菜单徽标） */
  badges: {},

  /** 缓存的下拉选项 */
  options: {
    tenants: [],
    cloudProviders: [],
    templates: [],
  },
});

export default store;
