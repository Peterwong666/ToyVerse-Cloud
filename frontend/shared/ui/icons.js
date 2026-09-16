/* ============================================================
   图标集（内联 SVG）
   ------------------------------------------------------------
   为什么不用 emoji：
     原型的菜单与按钮大量使用 emoji，在不同操作系统上字形差异大、
     也无法继承文字颜色。这里统一为 24x24 线性 SVG 图标，
     使用 currentColor 继承颜色，视觉一致。
   ============================================================ */

/** 图标路径定义：viewBox 统一 24x24，stroke 线性风格 */
const PATHS = {
  /* ---- 导航与布局 ---- */
  dashboard: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
  collapse: '<path d="M4 6h16M4 12h10M4 18h16"/>',
  expand: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  chevronRight: '<path d="M9 6l6 6-6 6"/>',
  chevronLeft: '<path d="M15 6l-6 6 6 6"/>',
  chevronDown: '<path d="M6 9l6 6 6-6"/>',
  chevronUp: '<path d="M6 15l6-6 6 6"/>',
  arrowLeft: '<path d="M19 12H5M12 19l-7-7 7-7"/>',
  arrowRight: '<path d="M5 12h14M12 5l7 7-7 7"/>',

  /* ---- 业务域 ---- */
  building: '<path d="M3 21h18M5 21V5a2 2 0 012-2h6a2 2 0 012 2v16M15 21V11a2 2 0 012-2h2a2 2 0 012 2v10"/><path d="M9 7h2M9 11h2M9 15h2"/>',
  box: '<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>',
  cloud: '<path d="M7 18a4 4 0 010-8 5.5 5.5 0 0110.5-1.5A3.5 3.5 0 1118 18H7z"/>',
  clipboard: '<rect x="8" y="3" width="8" height="4" rx="1"/><path d="M16 5h1a2 2 0 012 2v12a2 2 0 01-2 2H7a2 2 0 01-2-2V7a2 2 0 012-2h1"/><path d="M9 12h6M9 16h4"/>',
  device: '<rect x="6" y="2" width="12" height="20" rx="2.5"/><path d="M11 18h2"/>',
  factory: '<path d="M3 21h18V10l-5 3V10l-5 3V7l-8 4v10z"/><path d="M7 21v-4M12 21v-4M17 21v-4"/>',
  qrcode: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3h-3zM20 14v3M14 20h3M20 20h1"/>',
  robot: '<rect x="4" y="8" width="16" height="12" rx="2.5"/><path d="M12 4v4M9 14h.01M15 14h.01M9 17.5h6"/><circle cx="12" cy="3" r="1"/>',
  fire: '<path d="M12 3s4 4 4 8a4 4 0 11-8 0c0-1.5.5-2.5 1-3.5"/>',
  check: '<path d="M20 6L9 17l-5-5"/>',
  checkCircle: '<circle cx="12" cy="12" r="9"/><path d="M8.5 12.5l2.5 2.5 4.5-5"/>',
  xCircle: '<circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6M9 9l6 6"/>',
  alertTriangle: '<path d="M12 3l9 16H3l9-16z"/><path d="M12 9v4M12 16h.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  disk: '<path d="M12 3v10M7 8l5-5 5 5"/><path d="M4 15v3a3 3 0 003 3h10a3 3 0 003-3v-3"/>',

  /* ---- 组织与权限 ---- */
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0113 0"/><path d="M16 5.5a3 3 0 010 5.5M17.5 20a6.5 6.5 0 00-2-4.7"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M5 20a7 7 0 0114 0"/>',
  shield: '<path d="M12 3l8 3v6c0 4.5-3 7.8-8 9-5-1.2-8-4.5-8-9V6l8-3z"/><path d="M9 12l2 2 4-4"/>',
  sitemap: '<rect x="9" y="2" width="6" height="5" rx="1"/><rect x="2" y="17" width="6" height="5" rx="1"/><rect x="16" y="17" width="6" height="5" rx="1"/><path d="M12 7v5M5 17v-3h14v3"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l7-7 3 3-2 2 2 2-3 3-2-2-2 2"/>',

  /* ---- 数据与运营 ---- */
  chartBar: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  chartLine: '<path d="M3 17l5-5 4 3 6-7"/><path d="M3 21h18"/>',
  chartPie: '<path d="M12 3v9h9a9 9 0 10-9-9z"/><path d="M12 12v9a9 9 0 009-9h-9z"/>',
  trendUp: '<path d="M3 17l6-6 4 4 8-8"/><path d="M15 7h6v6"/>',
  trendDown: '<path d="M3 7l6 6 4-4 8 8"/><path d="M15 17h6v-6"/>',
  activity: '<path d="M3 12h4l3 8 4-16 3 8h4"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
  database: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
  layers: '<path d="M12 2l9 5-9 5-9-5 9-5z"/><path d="M3 12l9 5 9-5"/><path d="M3 17l9 5 9-5"/>',

  /* ---- AI 与语音 ---- */
  sparkles: '<path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8L12 3z"/><path d="M18 15l.9 2.1 2.1.9-2.1.9-.9 2.1-.9-2.1-2.1-.9 2.1-.9.9-2.1z"/>',
  mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0014 0M12 18v3M8 21h8"/>',
  volume: '<path d="M11 5L6 9H3v6h3l5 4V5z"/><path d="M15.5 8.5a5 5 0 010 7M18.5 5.5a9 9 0 010 13"/>',
  chat: '<path d="M21 12a8 8 0 01-8 8H8l-5 3 1.5-5A8 8 0 1121 12z"/>',
  book: '<path d="M4 4h7a3 3 0 013 3v13a3 3 0 00-3-3H4V4z"/><path d="M20 4h-3a3 3 0 00-3 3v13a3 3 0 013-3h3V4z"/>',

  /* ---- 操作 ---- */
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  edit: '<path d="M4 20h4L20 8l-4-4L4 16v4z"/><path d="M14 6l4 4"/>',
  trash: '<path d="M4 7h16M9 7V5a1 1 0 011-1h4a1 1 0 011 1v2"/><path d="M6 7l1 13a1 1 0 001 1h8a1 1 0 001-1l1-13"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
  filter: '<path d="M3 5h18l-7 8v6l-4 2v-8L3 5z"/>',
  refresh: '<path d="M20 11a8 8 0 10-2 6"/><path d="M20 5v6h-6"/>',
  download: '<path d="M12 3v12M7 10l5 5 5-5"/><path d="M4 19h16"/>',
  upload: '<path d="M12 21V9M7 14l5-5 5 5"/><path d="M4 5h16"/>',
  copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 012-2h8"/>',
  external: '<path d="M14 4h6v6"/><path d="M20 4l-8 8"/><path d="M18 14v4a2 2 0 01-2 2H6a2 2 0 01-2-2V8a2 2 0 012-2h4"/>',
  more: '<circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
  logout: '<path d="M15 4h3a2 2 0 012 2v12a2 2 0 01-2 2h-3"/><path d="M10 17l-5-5 5-5"/><path d="M5 12h10"/>',
  lock: '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 018 0v3"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/>',
  eyeOff: '<path d="M4 4l16 16"/><path d="M9.9 5.2A9.8 9.8 0 0112 5c6.5 0 10 7 10 7a17 17 0 01-3.2 4.2M6.3 7.5A17 17 0 002 12s3.5 7 10 7a9.9 9.9 0 004.2-.9"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="1.8"/><path d="M4 18l5-5 4 4 3-3 4 4"/>',
  file: '<path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8l-5-5z"/><path d="M14 3v5h5"/>',
  folder: '<path d="M3 7a2 2 0 012-2h3.5l2 2.5H19a2 2 0 012 2V17a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"/>',
  link: '<path d="M10 13a5 5 0 007 0l2-2a5 5 0 00-7-7l-1 1"/><path d="M14 11a5 5 0 00-7 0l-2 2a5 5 0 007 7l1-1"/>',
  filter2: '<path d="M4 6h16M7 12h10M10 18h4"/>',

  /* ---- 状态 ---- */
  play: '<path d="M7 4l12 8-12 8V4z"/>',
  pause: '<path d="M8 4v16M16 4v16"/>',
  power: '<path d="M12 3v9"/><path d="M6.5 7a8 8 0 1011 0"/>',
  wifi: '<path d="M2.5 9a15 15 0 0119 0"/><path d="M6 12.5a10 10 0 0112 0"/><path d="M9.5 16a5 5 0 015 0"/><circle cx="12" cy="19.5" r="1"/>',
  signal: '<path d="M4 20V10M9 20V6M14 20V13M19 20V4"/>',
  battery: '<rect x="2" y="8" width="17" height="9" rx="2"/><path d="M22 11v3"/>',
  snow: '<path d="M12 3v18M4 7.5l16 9M20 7.5l-16 9"/>',
  archive: '<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v10a2 2 0 002 2h10a2 2 0 002-2V9"/><path d="M10 13h4"/>',
  ban: '<circle cx="12" cy="12" r="9"/><path d="M5.5 5.5l13 13"/>',
  package: '<path d="M16 4l4 4-8 3-8-3 4-4h8z"/><path d="M4 8v9l8 3 8-3V8"/><path d="M12 11v9"/>',
};

/** 需要填充而非描边的图标 */
const FILLED = new Set(['play', 'pause']);

/**
 * 生成图标 SVG 字符串。
 * @param {string} name 图标名
 * @param {object} [options]
 * @param {number} [options.size=16]
 * @param {string} [options.class]
 * @param {string} [options.title] 无障碍标题
 */
export function icon(name, { size = 16, class: className = '', title = '' } = {}) {
  const path = PATHS[name];
  if (!path) {
    // 未知图标：返回一个中性占位，避免整页渲染失败
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" class="${className}" aria-hidden="true"><circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="3 3"/></svg>`;
  }

  const filled = FILLED.has(name);
  const fill = filled ? 'currentColor' : 'none';
  const ariaAttrs = title
    ? `role="img" aria-label="${title}"`
    : 'aria-hidden="true"';

  return (
    `<svg width="${size}" height="${size}" viewBox="0 0 24 24" ` +
    `fill="${fill}" stroke="currentColor" stroke-width="1.6" ` +
    `stroke-linecap="round" stroke-linejoin="round" ` +
    `class="${className}" ${ariaAttrs}>${path}</svg>`
  );
}

/**
 * 生成图标 DOM 节点。
 */
export function iconNode(name, options) {
  const template = document.createElement('template');
  template.innerHTML = icon(name, options).trim();
  return template.content.firstElementChild;
}

/** 判断图标是否存在 */
export function hasIcon(name) {
  return Object.prototype.hasOwnProperty.call(PATHS, name);
}

/** 全部图标名 */
export function iconNames() {
  return Object.keys(PATHS);
}

export default { icon, iconNode, hasIcon, iconNames };
