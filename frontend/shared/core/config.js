/* ============================================================
   运行时配置
   ------------------------------------------------------------
   前端为「零构建」ES Module，没有打包期变量注入，
   因此配置通过以下优先级解析：
     1. window.__TOYVERSE_CONFIG__（部署时可注入）
     2. <html> 上的 data-api-base 属性
     3. 默认同源 /api/v1
   ============================================================ */

const injected = (typeof window !== 'undefined' && window.__TOYVERSE_CONFIG__) || {};

/** 从 <html> 属性读取配置（便于单文件部署时改地址） */
function fromDocument(attr, fallback) {
  if (typeof document === 'undefined') return fallback;
  const value = document.documentElement?.getAttribute(attr);
  return value || fallback;
}

export const config = {
  /** 应用名 */
  appName: injected.appName || 'ToyVerse Cloud',

  /** API 基址（同源部署时保持相对路径，避免 CORS） */
  apiBase: injected.apiBase || fromDocument('data-api-base', '/api/v1'),

  /** 请求超时（毫秒） */
  requestTimeout: injected.requestTimeout || 20000,

  /** 访问令牌在 localStorage 中的键名前缀 */
  storagePrefix: injected.storagePrefix || 'toyverse',

  /** 各端的路由基路径 */
  basePaths: {
    platform: '/platform/',
    merchant: '/merchant/',
    factory: '/factory/',
    miniapp: '/miniapp/',
    login: '/login',
  },

  /** 主题色（与 tokens.css 保持一致，供图表与二维码使用） */
  colors: {
    brand: '#4f46e5',
    brandLight: '#eef2ff',
    accent: '#f59e0b',
    coral: '#fb7185',
    success: '#10b981',
    warning: '#f59e0b',
    danger: '#ef4444',
    info: '#0ea5e9',
    ink: '#0f172a',
    muted: '#64748b',
    line: '#e2e8f0',
    /** 图表调色板 */
    chart: ['#4f46e5', '#14b8a6', '#f59e0b', '#fb7185', '#8b5cf6', '#0ea5e9'],
  },

  /** 分页默认值（与后端 MAX_PAGE_SIZE 对齐） */
  pagination: {
    defaultPageSize: 20,
    pageSizeOptions: [10, 20, 50, 100],
  },

  /** 各端的显示配置 */
  ends: {
    platform: { key: 'platform', label: '平台端', title: '平台管理后台', accent: '#4f46e5', iconKey: 'platform' },
    merchant: { key: 'merchant', label: '商户端', title: '商户管理后台', accent: '#0d9488', iconKey: 'merchant' },
    factory: { key: 'factory', label: '工厂端', title: '烧录工厂工作台', accent: '#d97706', iconKey: 'factory' },
    miniapp: { key: 'miniapp', label: '终端用户端', title: '智能玩具', accent: '#fb7185', iconKey: 'miniapp' },
  },
};

/** 拼接完整 API 地址 */
export function apiUrl(path) {
  const base = config.apiBase.replace(/\/+$/, '');
  const suffix = String(path || '').replace(/^\/+/, '');
  return suffix ? `${base}/${suffix}` : base;
}

/** 读取当前页面对应的端标识（由 body[data-end] 或路径推断） */
export function currentEnd() {
  if (typeof document === 'undefined') return 'platform';
  const declared = document.body?.dataset?.end;
  if (declared) return declared;

  const path = window.location.pathname;
  for (const end of Object.keys(config.basePaths)) {
    if (path.includes(`/${end}/`)) return end;
  }
  return 'platform';
}

/** 判断是否开发环境（用于显示调试信息） */
export const isDev = (() => {
  if (typeof window === 'undefined') return false;
  const host = window.location.hostname;
  return host === 'localhost' || host === '127.0.0.1' || host.endsWith('.local');
})();

export default config;
