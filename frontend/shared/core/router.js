/* ============================================================
   哈希路由
   ------------------------------------------------------------
   解决的问题（对应遗留缺陷）：
     P-01 子页面刷新状态丢失
          → 路由**参数化**，把子页面所需标识写进 URL
            （#/tenant/t-001 而不是靠内存里的 _view* 变量），
            刷新后可从 URL 重建页面。
     P-02 返回路径错误
          → back() 基于「路由历史 + 各端首页」决定目标，
            不再硬编码某一端的返回地址。

   URL 形态：
     #/tenants              列表页
     #/tenants/t-001        详情页（参数化）
     #/devices/d-1?tab=events  详情页 + 查询参数

   与旧原型（key 直连函数）的差异：
     旧：#orders  → routes['orders']；参数只能塞内存
     新：#/orders/detail/ord-1 → 参数在 URL 中，可分享、可刷新
   ============================================================ */

/**
 * 把路由模式编译为正则与参数名列表。
 * @param {string} pattern 例如 '/devices/:id'
 */
function compile(pattern) {
  const names = [];
  const source = pattern
    .replace(/\/+$/, '')
    .split('/')
    .map((segment) => {
      if (segment.startsWith(':')) {
        names.push(segment.slice(1));
        return '([^/]+)';
      }
      if (segment === '*') {
        names.push('rest');
        return '(.*)';
      }
      return segment.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    })
    .join('/');

  return { regex: new RegExp(`^${source}/?$`), names };
}

/**
 * 匹配路径。
 * @returns {{params: object}|null}
 */
export function matchPath(pattern, path) {
  const { regex, names } = compile(pattern);
  const matched = regex.exec(path);
  if (!matched) return null;

  const params = {};
  names.forEach((name, index) => {
    params[name] = decodeURIComponent(matched[index + 1] ?? '');
  });
  return { params };
}

/** 把参数填回模式，生成路径 */
export function buildPath(pattern, params = {}) {
  return pattern
    .split('/')
    .map((segment) => {
      if (segment.startsWith(':')) {
        const key = segment.slice(1);
        const value = params[key];
        if (value === undefined || value === null) {
          throw new Error(`路由参数缺失：${key}（模式 ${pattern}）`);
        }
        return encodeURIComponent(String(value));
      }
      return segment;
    })
    .join('/');
}

/* ------------------------------------------------------------
   解析 / 构造 hash
   ------------------------------------------------------------ */

/** 解析当前 hash → { path, query } */
export function parseHash(hash = window.location.hash) {
  const raw = String(hash || '').replace(/^#/, '');
  const [pathPart, queryPart = ''] = raw.split('?');

  const query = {};
  if (queryPart) {
    new URLSearchParams(queryPart).forEach((value, key) => {
      query[key] = value;
    });
  }

  // 归一化：确保以 / 开头，去掉末尾斜杠
  let path = pathPart || '/';
  if (!path.startsWith('/')) path = `/${path}`;
  if (path.length > 1) path = path.replace(/\/+$/, '');

  return { path, query };
}

/** 构造 hash 字符串 */
export function toHash(path, query) {
  const normalized = path.startsWith('/') ? path : `/${path}`;
  const search = query && Object.keys(query).length ? `?${new URLSearchParams(query)}` : '';
  return `#${normalized}${search}`;
}

/* ------------------------------------------------------------
   路由器
   ------------------------------------------------------------ */

export class Router {
  constructor({ fallback = '/dashboard', onNavigate = null } = {}) {
    /** @type {Array<{pattern:string,name:string,handler:Function,title?:string,perm?:string,hidden?:boolean}>} */
    this.routes = [];
    this.fallback = fallback;
    this.onNavigate = onNavigate;
    this.current = { path: '/', query: {}, route: null, params: {} };
    this.history = [];
    this._listening = false;
  }

  /**
   * 注册路由。
   * @param {object|Array} route 或路由数组
   */
  register(route) {
    const list = Array.isArray(route) ? route : [route];
    list.forEach((item) => {
      if (!item || !item.pattern || typeof item.handler !== 'function') {
        throw new Error(`路由定义不合法：${JSON.stringify(item)}`);
      }
      this.routes.push({ hidden: false, ...item });
    });
    return this;
  }

  /** 解析路径到路由 */
  resolve(path) {
    for (const route of this.routes) {
      const matched = matchPath(route.pattern, path);
      if (matched) return { route, params: matched.params };
    }
    return null;
  }

  /** 启动监听 */
  start() {
    if (this._listening) return;
    this._listening = true;

    window.addEventListener('hashchange', () => this.dispatch());

    // 首次进入：无 hash 时落到默认路由
    if (!window.location.hash) {
      window.location.replace(toHash(this.fallback));
      // replace 也会触发 hashchange，无需手动 dispatch
      return;
    }
    this.dispatch();
  }

  /** 执行当前 hash 对应的渲染 */
  async dispatch() {
    const { path, query } = parseHash();
    const resolved = this.resolve(path);

    if (!resolved) {
      const fallbackResolved = this.resolve(this.fallback);
      if (fallbackResolved && path !== this.fallback) {
        // 未知路由 → 回到默认页（不保留历史，避免用户困在 404）
        window.location.replace(toHash(this.fallback));
        return;
      }
      this.current = { path, query, route: null, params: {} };
      await this.onNavigate?.({ path, query, route: null, params: {}, notFound: true });
      return;
    }

    const previous = this.current.path;
    this.current = { path, query, route: resolved.route, params: resolved.params };

    if (previous && previous !== path) {
      this.history.push(previous);
      if (this.history.length > 50) this.history.shift();
    }

    await this.onNavigate?.(this.current);
  }

  /**
   * 导航到指定路径。
   * @param {string} path
   * @param {object} [query]
   * @param {{replace?:boolean}} [options]
   */
  go(path, query = null, { replace = false } = {}) {
    const target = toHash(path, query);
    if (window.location.hash === target) {
      // 同一地址重复导航：手动触发一次，保证「点击当前菜单刷新数据」的行为一致
      this.dispatch();
      return;
    }
    if (replace) window.location.replace(target);
    else window.location.hash = target;
  }

  /** 按路由名导航 */
  goByName(name, params = {}, query = null) {
    const route = this.routes.find((item) => item.name === name);
    if (!route) throw new Error(`未找到路由：${name}`);
    this.go(buildPath(route.pattern, params), query);
  }

  /**
   * 返回上一页。
   *
   * 修复 P-02：不再硬编码某一端的返回地址。
   * 仅当本会话内确实发生过站内导航时才走浏览器历史，
   * 否则直接回本端首页 —— 避免「用户直接打开详情页，
   * 点返回却离开了本系统」的困惑。
   *
   * @param {string} homePath 本端首页路径
   */
  back(homePath = this.fallback) {
    if (this.history.length > 0) {
      this.history.pop();
      window.history.back();
      return;
    }
    this.go(homePath, null, { replace: true });
  }

  /** 当前路径 */
  get path() {
    return this.current.path;
  }

  /** 当前查询参数 */
  get query() {
    return this.current.query;
  }

  /** 当前路由参数 */
  get params() {
    return this.current.params;
  }

  /** 当前路由定义 */
  get route() {
    return this.current.route;
  }

  /** 判断某路径是否为当前页（用于菜单高亮） */
  isActive(path) {
    if (path === this.current.path) return true;
    // 详情页高亮其列表菜单：/tenants/t-001 命中 /tenants
    return path !== '/' && this.current.path.startsWith(`${path}/`);
  }

  /** 判断某路由名是否为当前页 */
  isActiveName(name) {
    return this.current.route?.name === name;
  }
}

export default Router;
