/* ============================================================
   DOM 工具与安全转义
   ------------------------------------------------------------
   安全设计（重要）：
   原型的做法是把后端数据直接插入 innerHTML，存在 XSS 风险。
   本模块提供统一的转义入口，**所有**插入 HTML 的动态数据都必须经过
   esc()，或改用 h() 通过 DOM API 设置文本。

   约定：
     esc(v)      → 转义后用于字符串模板
     h(tag, ...) → 用 DOM API 构造元素，文本自动安全
     html``      → 模板标签，自动转义插值（推荐）
   ============================================================ */

/* ------------------------------------------------------------
   一、转义
   ------------------------------------------------------------ */

const ESCAPE_MAP = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
  '`': '&#96;',
};

/**
 * HTML 转义。用于字符串模板中插入动态数据。
 * @param {unknown} value
 * @returns {string}
 */
export function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value).replace(/[&<>"'`]/g, (ch) => ESCAPE_MAP[ch]);
}

/**
 * 属性值转义（比 esc 更严格，额外处理换行）
 * @param {unknown} value
 */
export function escAttr(value) {
  return esc(value).replace(/\n/g, '&#10;').replace(/\r/g, '&#13;');
}

/**
 * 模板标签：自动转义插值，返回 HTML 字符串。
 *
 * 用法：
 *   const row = html`<td>${user.name}</td>`;   // name 会被自动转义
 *
 * 需要插入已知安全的 HTML 片段时，用 raw() 显式标记：
 *   html`<div>${raw(trustedHtml)}</div>`
 */
export function html(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i += 1) {
    out += renderValue(values[i]) + strings[i + 1];
  }
  return out;
}

/** 标记一段「已知安全」的 HTML，跳过转义 */
export function raw(trustedHtml) {
  return { __raw: true, value: String(trustedHtml ?? '') };
}

function renderValue(value) {
  if (value === null || value === undefined || value === false) return '';
  if (Array.isArray(value)) return value.map(renderValue).join('');
  if (typeof value === 'object' && value.__raw) return value.value;
  return esc(value);
}

/* ------------------------------------------------------------
   二、查询与创建
   ------------------------------------------------------------ */

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

/**
 * 用 DOM API 构造元素，文本内容自动安全。
 * @param {string} tag
 * @param {object} [attrs] 属性；class/text/html/dataset/on* 有特殊处理
 * @param {...(Node|string)} children
 */
export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);

  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;

    if (key === 'class' || key === 'className') {
      node.className = value;
    } else if (key === 'text') {
      node.textContent = value;
    } else if (key === 'html') {
      // 显式要求插入 HTML；调用方需自行保证安全（建议改用 text）
      node.innerHTML = value;
    } else if (key === 'dataset') {
      Object.assign(node.dataset, value);
    } else if (key === 'style' && typeof value === 'object') {
      Object.assign(node.style, value);
    } else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) {
      node.setAttribute(key, '');
    } else {
      node.setAttribute(key, value);
    }
  }

  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }

  return node;
}

/** 从 HTML 字符串创建元素（仅用于本模块内部或静态模板） */
export function fromHtml(markup) {
  const template = document.createElement('template');
  template.innerHTML = String(markup).trim();
  return template.content.firstElementChild;
}

/** 从 HTML 字符串创建多个元素 */
export function fromHtmlAll(markup) {
  const template = document.createElement('template');
  template.innerHTML = String(markup).trim();
  return Array.from(template.content.children);
}

/** 清空元素 */
export function clear(node) {
  if (!node) return;
  while (node.firstChild) node.removeChild(node.firstChild);
}

/** 设置内容（接受字符串或节点） */
export function setContent(node, content) {
  clear(node);
  if (content === null || content === undefined) return;
  if (content instanceof Node) {
    node.append(content);
  } else if (Array.isArray(content)) {
    content.forEach((item) => setContent(node, item));
  } else {
    node.append(document.createTextNode(String(content)));
  }
}

/* ------------------------------------------------------------
   三、事件
   ------------------------------------------------------------ */

/** 绑定事件，返回解绑函数 */
export function on(target, event, handler, options) {
  target.addEventListener(event, handler, options);
  return () => target.removeEventListener(event, handler, options);
}

/** 事件委托：在容器上监听，匹配选择器后触发 */
export function delegate(root, event, selector, handler) {
  return on(root, event, (e) => {
    const matched = e.target.closest(selector);
    if (matched && root.contains(matched)) handler(e, matched);
  });
}

/** 阻止默认行为与冒泡 */
export function stop(e) {
  e.preventDefault();
  e.stopPropagation();
}

/* ------------------------------------------------------------
   四、函数工具
   ------------------------------------------------------------ */

/** 防抖 */
export function debounce(fn, wait = 300) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

/** 节流 */
export function throttle(fn, wait = 200) {
  let last = 0;
  let timer;
  return (...args) => {
    const now = Date.now();
    const remaining = wait - (now - last);
    if (remaining <= 0) {
      last = now;
      fn(...args);
    } else if (!timer) {
      timer = setTimeout(() => {
        last = Date.now();
        timer = null;
        fn(...args);
      }, remaining);
    }
  };
}

/** 生成短随机 ID */
export function uid(prefix = 'id') {
  return `${prefix}-${Math.random().toString(36).slice(2, 10)}`;
}

/** 简易深拷贝（仅处理 JSON 可序列化数据） */
export function clone(value) {
  if (value === null || typeof value !== 'object') return value;
  if (typeof structuredClone === 'function') {
    try {
      return structuredClone(value);
    } catch {
      /* 含函数等不可克隆值时回退 */
    }
  }
  return JSON.parse(JSON.stringify(value));
}

/** 空值判定（null / undefined / 空串 / 空数组） */
export function isEmpty(value) {
  if (value === null || value === undefined) return true;
  if (typeof value === 'string') return value.trim() === '';
  if (Array.isArray(value)) return value.length === 0;
  return false;
}

/** 占位显示：空值统一显示为 '-' */
export function orDash(value) {
  return isEmpty(value) ? '-' : value;
}

/* ------------------------------------------------------------
   五、格式化
   ------------------------------------------------------------ */

/** 数字千分位 */
export function formatNumber(value, fallback = '-') {
  const num = Number(value);
  if (!Number.isFinite(num)) return fallback;
  return num.toLocaleString('zh-CN');
}

/** 金额（元） */
export function formatMoney(value, { symbol = '¥', digits = 2, fallback = '-' } = {}) {
  const num = Number(value);
  if (!Number.isFinite(num)) return fallback;
  return `${symbol}${num.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

/** 百分比 */
export function formatPercent(value, { digits = 1, fallback = '-' } = {}) {
  const num = Number(value);
  if (!Number.isFinite(num)) return fallback;
  return `${(num * 100).toFixed(digits)}%`;
}

/** 文件大小 */
export function formatBytes(bytes, fallback = '-') {
  const num = Number(bytes);
  if (!Number.isFinite(num) || num < 0) return fallback;
  if (num < 1024) return `${num} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let size = num / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(1)} ${units[index]}`;
}

/** 日期时间格式化 */
export function formatDate(value, pattern = 'YYYY-MM-DD HH:mm') {
  if (!value) return '-';
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return '-';

  const pad = (n) => String(n).padStart(2, '0');
  const map = {
    YYYY: date.getFullYear(),
    MM: pad(date.getMonth() + 1),
    DD: pad(date.getDate()),
    HH: pad(date.getHours()),
    mm: pad(date.getMinutes()),
    ss: pad(date.getSeconds()),
  };
  return pattern.replace(/YYYY|MM|DD|HH|mm|ss/g, (token) => map[token]);
}

/** 仅日期 */
export function formatDay(value) {
  return formatDate(value, 'YYYY-MM-DD');
}

/** 相对时间（刚刚 / N分钟前 / N小时前 / N天前） */
export function formatRelative(value, fallback = '-') {
  if (!value) return fallback;
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return fallback;

  const diff = Date.now() - date.getTime();
  const minute = 60 * 1000;
  const hour = 60 * minute;
  const day = 24 * hour;

  if (diff < 0) return formatDate(date, 'MM-DD HH:mm');
  if (diff < minute) return '刚刚';
  if (diff < hour) return `${Math.floor(diff / minute)} 分钟前`;
  if (diff < day) return `${Math.floor(diff / hour)} 小时前`;
  if (diff < 30 * day) return `${Math.floor(diff / day)} 天前`;
  return formatDate(date, 'YYYY-MM-DD');
}

/** 时长（秒 → 1分23秒） */
export function formatDuration(seconds, fallback = '-') {
  const num = Number(seconds);
  if (!Number.isFinite(num) || num < 0) return fallback;
  if (num < 60) return `${Math.round(num)} 秒`;
  const min = Math.floor(num / 60);
  const sec = Math.round(num % 60);
  if (min < 60) return sec ? `${min}分${sec}秒` : `${min}分钟`;
  const hr = Math.floor(min / 60);
  return `${hr}小时${min % 60}分`;
}

/** 姓名脱敏：中国移动 → 中**动；张 → 张* */
export function maskName(name) {
  const text = String(name ?? '').trim();
  if (!text) return '-';
  if (text.length === 1) return text;
  if (text.length === 2) return `${text[0]}*`;
  return `${text[0]}${'*'.repeat(Math.min(text.length - 2, 4))}${text[text.length - 1]}`;
}

/** 手机号脱敏：138****8888 */
export function maskPhone(phone) {
  const text = String(phone ?? '').trim();
  if (text.length < 7) return text || '-';
  return `${text.slice(0, 3)}****${text.slice(-4)}`;
}

/** 密钥脱敏：只保留前 4 位 */
export function maskSecret(secret) {
  const text = String(secret ?? '');
  if (!text) return '';
  if (text.length <= 4) return '*'.repeat(text.length);
  return `${text.slice(0, 4)}****`;
}

/* ------------------------------------------------------------
   六、剪贴板与下载
   ------------------------------------------------------------ */

/** 复制到剪贴板（带降级方案） */
export async function copyText(text) {
  const value = String(text ?? '');
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    /* 继续走降级方案 */
  }

  try {
    const textarea = h('textarea', {
      value,
      style: { position: 'fixed', top: '-9999px', opacity: '0' },
    });
    document.body.append(textarea);
    textarea.select();
    const ok = document.execCommand('copy');
    textarea.remove();
    return ok;
  } catch {
    return false;
  }
}

/** 触发浏览器下载 */
export function downloadFile(filename, content, mime = 'text/plain;charset=utf-8') {
  const blob = content instanceof Blob ? content : new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const link = h('a', { href: url, download: filename });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** 导出 CSV（带 BOM，Excel 打开不乱码） */
export function downloadCsv(filename, rows) {
  const escapeCell = (cell) => {
    const text = cell === null || cell === undefined ? '' : String(cell);
    return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  const csv = rows.map((row) => row.map(escapeCell).join(',')).join('\r\n');
  downloadFile(filename, `\uFEFF${csv}`, 'text/csv;charset=utf-8');
}

/* ------------------------------------------------------------
   七、滚动与定位
   ------------------------------------------------------------ */

/** 滚动到元素 */
export function scrollToEl(node, options = { behavior: 'smooth', block: 'start' }) {
  node?.scrollIntoView?.(options);
}

/** 在参照元素附近定位浮层（自动避开视口边缘） */
export function positionFloating(floating, anchor, { gap = 6, align = 'end' } = {}) {
  const rect = anchor.getBoundingClientRect();
  const width = floating.offsetWidth;
  const height = floating.offsetHeight;

  let left = align === 'end' ? rect.right - width : rect.left;
  let top = rect.bottom + gap;

  left = Math.max(8, Math.min(left, window.innerWidth - width - 8));
  if (top + height > window.innerHeight - 8) {
    top = rect.top - height - gap;
  }
  top = Math.max(8, top);

  floating.style.position = 'fixed';
  floating.style.left = `${left}px`;
  floating.style.top = `${top}px`;
}

export default {
  esc,
  escAttr,
  html,
  raw,
  $,
  $$,
  h,
  fromHtml,
  fromHtmlAll,
  clear,
  setContent,
  on,
  delegate,
  stop,
  debounce,
  throttle,
  uid,
  clone,
  isEmpty,
  orDash,
  formatNumber,
  formatMoney,
  formatPercent,
  formatBytes,
  formatDate,
  formatDay,
  formatRelative,
  formatDuration,
  maskName,
  maskPhone,
  maskSecret,
  copyText,
  downloadFile,
  downloadCsv,
  scrollToEl,
  positionFloating,
};
