/* ============================================================
   基础组件库
   ------------------------------------------------------------
   设计约定：
     * 组件返回 **HTML 字符串**，通过 html`` 标签自动转义插值
     * 传入的已生成 HTML 片段用 raw() 显式标记
     * 交互通过事件委托绑定（见 delegate()），不写内联 onclick
       —— 原型大量使用内联 onclick + 全局函数，难以维护且有注入风险
   ============================================================ */

import { esc, formatNumber, html, orDash, raw } from './dom.js';
import { icon } from './icons.js';

/* ------------------------------------------------------------
   一、按钮
   ------------------------------------------------------------ */

/**
 * 按钮。
 * @param {object} o
 * @param {string} o.label
 * @param {'default'|'primary'|'secondary'|'danger'|'ghost'|'link'} [o.variant]
 * @param {'sm'|'md'|'lg'} [o.size]
 * @param {string} [o.icon] 图标名
 * @param {string} [o.action] data-action 值（供事件委托识别）
 * @param {object} [o.data] 附加 data-* 属性
 * @param {boolean}[o.disabled]
 * @param {boolean}[o.block]
 * @param {string} [o.id]
 * @param {string} [o.title] 悬停说明
 * @param {string} [o.type='button']
 */
export function button(o = {}) {
  const {
    label = '',
    variant = 'default',
    size = 'md',
    icon: iconName = '',
    action = '',
    data = {},
    disabled = false,
    block = false,
    id = '',
    title = '',
    type = 'button',
    loading = false,
  } = o;

  const classes = [
    'btn',
    variant !== 'default' ? `btn-${variant}` : '',
    size !== 'md' ? `btn-${size}` : '',
    block ? 'btn-block' : '',
  ]
    .filter(Boolean)
    .join(' ');

  const dataAttrs = Object.entries({ action, ...data })
    .filter(([, v]) => v !== '' && v !== null && v !== undefined)
    .map(([k, v]) => `data-${esc(k)}="${esc(v)}"`)
    .join(' ');

  const iconHtml = loading
    ? '<span class="spinner" style="width:14px;height:14px;border-width:2px"></span>'
    : iconName
      ? icon(iconName, { size: size === 'sm' ? 13 : 15 })
      : '';

  return html`<button
    type="${type}"
    class="${classes}"
    ${raw(dataAttrs)}
    ${raw(id ? `id="${esc(id)}"` : '')}
    ${raw(disabled || loading ? 'disabled aria-disabled="true"' : '')}
    ${raw(title ? `title="${esc(title)}"` : '')}
  >${raw(iconHtml)}${label ? raw(html`<span>${esc(label)}</span>`) : ''}</button>`;
}

/** 图标按钮（无文字） */
export function iconButton(o = {}) {
  const { icon: iconName, label = '', size = 'md', variant = 'ghost', ...rest } = o;
  const classes = ['btn', 'btn-icon', variant !== 'default' ? `btn-${variant}` : '', size !== 'md' ? `btn-${size}` : '']
    .filter(Boolean)
    .join(' ');
  const dataAttrs = Object.entries({ action: o.action, ...(o.data || {}) })
    .filter(([, v]) => v !== '' && v !== null && v !== undefined)
    .map(([k, v]) => `data-${esc(k)}="${esc(v)}"`)
    .join(' ');

  return html`<button
    type="button"
    class="${classes}"
    ${raw(dataAttrs)}
    ${raw(o.disabled ? 'disabled' : '')}
    title="${label}"
    aria-label="${label}"
  >${raw(icon(iconName, { size: size === 'sm' ? 14 : 16 }))}</button>`;
}

/* ------------------------------------------------------------
   二、标签
   ------------------------------------------------------------ */

/**
 * 标签。
 * @param {string} text
 * @param {string} [tone='default'] default|brand|success|warning|danger|info|teal|coral
 * @param {object} [o] size / dot
 */
export function tag(text, tone = 'default', o = {}) {
  const { size = 'md', dot = false } = o;
  const classes = ['tag', `tag-${tone}`, size === 'lg' ? 'tag-lg' : ''].filter(Boolean).join(' ');
  return html`<span class="${classes}"
    >${raw(dot ? '<span class="tag-dot"></span>' : '')}${text}</span
  >`;
}

/**
 * 状态标签：根据状态映射表着色。
 *
 * 映射表由各业务模块提供，格式：
 *   { 'IN_STOCK': { text: '已入库待生产', tone: 'info' }, ... }
 *
 * @param {string} status
 * @param {Record<string,{text:string,tone?:string}>} map
 * @param {object} [o]
 */
export function statusTag(status, map = {}, o = {}) {
  const entry = map[status] || {};
  const text = entry.text || orDash(status);
  const tone = entry.tone || 'default';
  const withDot = o.dot ?? ['success', 'warning', 'danger', 'info', 'teal'].includes(tone);
  return tag(text, tone, { ...o, dot: withDot });
}

/** 数字徽标 */
export function badge(value, tone = 'brand') {
  if (!value) return '';
  const cls = tone === 'brand' ? 'badge' : `badge badge-${tone}`;
  return html`<span class="${cls}">${formatNumber(value)}</span>`;
}

/* ------------------------------------------------------------
   三、卡片
   ------------------------------------------------------------ */

/**
 * 卡片容器。
 * @param {object} o
 * @param {string} [o.title]
 * @param {string} [o.subtitle]
 * @param {string} [o.body] 已生成的 HTML（需 raw 包裹）
 * @param {string} [o.actions] 右上角操作区 HTML
 * @param {boolean}[o.flush] 去掉内边距（表格卡片使用）
 * @param {boolean}[o.hoverable]
 * @param {string} [o.class]
 * @param {string} [o.id]
 */
export function card(o = {}) {
  const { title = '', subtitle = '', body = '', actions = '', flush = false, hoverable = false, class: extra = '', id = '' } = o;

  const classes = ['card', flush ? 'card-flush' : '', hoverable ? 'card-hoverable' : '', extra]
    .filter(Boolean)
    .join(' ');

  const head =
    title || actions
      ? html`<div class="card-head">
          <div class="card-title">${title}${subtitle ? raw(html`<span class="card-title-sub">${subtitle}</span>`) : ''}</div>
          ${raw(actions ? html`<div class="card-actions">${raw(actions)}</div>` : '')}
        </div>`
      : '';

  return html`<div class="${classes}" ${raw(id ? `id="${esc(id)}"` : '')}>${raw(head)}${raw(body)}</div>`;
}

/**
 * 统计卡片。
 * @param {object} o
 * @param {string} o.label
 * @param {string|number} o.value
 * @param {string} [o.unit]
 * @param {string} [o.icon]
 * @param {string} [o.tone]
 * @param {string} [o.foot]
 * @param {'up'|'down'} [o.trend]
 */
export function statCard(o = {}) {
  const { label = '', value = '-', unit = '', icon: iconName = 'chartBar', tone = 'brand', foot = '', trend = '' } = o;

  const trendHtml = trend
    ? html`<span class="${trend === 'up' ? 'stat-trend-up' : 'stat-trend-down'}">
        ${raw(icon(trend === 'up' ? 'trendUp' : 'trendDown', { size: 12 }))}
      </span>`
    : '';

  return html`<div class="stat-card">
    <div class="stat-icon" data-tone="${tone}">${raw(icon(iconName, { size: 20 }))}</div>
    <div class="stat-body">
      <div class="stat-label">${label}</div>
      <div class="stat-value">${value}${unit ? raw(html`<span class="stat-unit">${unit}</span>`) : ''}</div>
      ${raw(foot ? html`<div class="stat-foot">${raw(trendHtml)}${foot}</div>` : '')}
    </div>
  </div>`;
}

/** 统计卡片分组 */
export function statGrid(items = []) {
  return html`<div class="stat-grid">${raw(items.map((item) => statCard(item)).join(''))}</div>`;
}

/* ------------------------------------------------------------
   四、描述列表 / 键值列表
   ------------------------------------------------------------ */

/**
 * 描述列表（表格化的字段展示）。
 * @param {Array<[string, any]>} items
 * @param {object} [o] cols=1|2
 */
export function descList(items = [], o = {}) {
  const { cols = 2 } = o;
  const rows = items
    .map(([key, value]) => html`<dt>${key}</dt><dd>${orDash(value)}</dd>`)
    .join('');
  return html`<dl class="desc-list ${cols === 2 ? 'desc-list-2col' : ''}">${raw(rows)}</dl>`;
}

/**
 * 键值列表（左键右值，用于侧栏摘要）。
 * @param {Array<[string, any]>} items
 * @param {object} [o] raw=true 表示值已是 HTML
 */
export function kvList(items = [], o = {}) {
  const { htmlValues = false } = o;
  const rows = items
    .map(
      ([key, value]) =>
        html`<div class="kv-row">
          <span class="kv-key">${key}</span>
          <span class="kv-value">${raw(htmlValues ? String(value ?? '-') : esc(orDash(value)))}</span>
        </div>`,
    )
    .join('');
  return html`<div class="kv-list">${raw(rows)}</div>`;
}

/* ------------------------------------------------------------
   五、提示条
   ------------------------------------------------------------ */

/**
 * 提示条。
 * @param {object} o
 * @param {'info'|'success'|'warning'|'danger'|'neutral'} [o.tone]
 * @param {string} [o.title]
 * @param {string} [o.text]
 * @param {boolean}[o.dismissible] 是否可关闭
 */
export function alert(o = {}) {
  const { tone = 'info', title = '', text = '', dismissible = false } = o;
  const icons = { info: 'info', success: 'checkCircle', warning: 'alertTriangle', danger: 'xCircle', neutral: 'info' };

  return html`<div class="alert alert-${tone}" role="alert">
    <span class="alert-icon">${raw(icon(icons[tone] || 'info', { size: 16 }))}</span>
    <div class="alert-body">
      ${raw(title ? html`<div class="alert-title">${title}</div>` : '')}
      ${raw(text ? html`<div>${text}</div>` : '')}
    </div>
    ${raw(
      dismissible
        ? html`<button class="btn-icon btn-sm" type="button" data-action="dismiss-alert" aria-label="关闭">×</button>`
        : '',
    )}
  </div>`;
}

/* ------------------------------------------------------------
   六、空状态 / 骨架屏
   ------------------------------------------------------------ */

/**
 * 空状态。
 * @param {object} o
 * @param {string} [o.icon] 图标名（默认 inbox 风格）
 * @param {string} [o.title]
 * @param {string} [o.desc]
 * @param {string} [o.action] 按钮 HTML
 */
export function emptyState(o = {}) {
  const { icon: iconName = 'folder', title = '暂无数据', desc = '', action = '' } = o;
  return html`<div class="empty">
    <div class="empty-icon">${raw(icon(iconName, { size: 40, class: 'empty-svg' }))}</div>
    <div class="empty-title">${title}</div>
    ${raw(desc ? html`<div class="empty-desc">${desc}</div>` : '')}
    ${raw(action)}
  </div>`;
}

/**
 * 骨架屏。
 * @param {object} o
 * @param {number} [o.rows=5]
 * @param {boolean}[o.title=true]
 */
export function skeleton(o = {}) {
  const { rows = 5, title = true } = o;
  const body = Array.from({ length: rows }, () => '<div class="skeleton skeleton-row"></div>').join('');
  return html`<div class="p-5">
    ${raw(title ? '<div class="skeleton skeleton-title"></div>' : '')}
    ${raw(body)}
  </div>`;
}

/** 加载中占位 */
export function loadingState(text = '加载中…') {
  return html`<div class="page-loading">
    <span class="spinner spinner-lg"></span>
    <span>${text}</span>
  </div>`;
}

/* ------------------------------------------------------------
   七、Tabs
   ------------------------------------------------------------ */

/**
 * 标签页。
 * @param {Array<{key:string,label:string,badge?:number}>} items
 * @param {string} active
 * @param {object} [o] pill=true 药丸样式 / action 事件委托标识
 */
export function tabs(items = [], active = '', o = {}) {
  const { pill = false, action = 'switch-tab' } = o;
  const links = items
    .map(
      (item) => html`<div
        class="tab ${item.key === active ? 'active' : ''}"
        data-action="${action}"
        data-key="${item.key}"
        role="tab"
        aria-selected="${item.key === active ? 'true' : 'false'}"
      >${item.label}${item.badge ? raw(html`<span class="tab-badge">${formatNumber(item.badge)}</span>`) : ''}</div>`,
    )
    .join('');
  return html`<div class="tabs ${pill ? 'tabs-pill' : ''}" role="tablist">${raw(links)}</div>`;
}

/** 分段控件 */
export function segmented(items = [], active = '', o = {}) {
  const { action = 'switch-segment' } = o;
  const links = items
    .map(
      (item) => html`<div
        class="segmented-item ${item.key === active ? 'active' : ''}"
        data-action="${action}"
        data-key="${item.key}"
      >${item.label}</div>`,
    )
    .join('');
  return html`<div class="segmented">${raw(links)}</div>`;
}

/* ------------------------------------------------------------
   八、步骤条
   ------------------------------------------------------------ */

/**
 * 步骤条。
 * @param {Array<{title:string}>} steps
 * @param {number} current 从 1 开始
 */
export function steps(steps = [], current = 1) {
  const parts = steps.map((step, index) => {
    const num = index + 1;
    const state = num < current ? 'done' : num === current ? 'current' : '';
    const line = index < steps.length - 1 ? '<div class="step-line"></div>' : '';
    return html`<div class="step ${state}">
        <span class="step-num">${num < current ? raw('✓') : num}</span>
        <span class="step-title">${step.title}</span>
      </div>
      ${raw(line)}`;
  });
  return html`<div class="steps">${raw(parts.join(''))}</div>`;
}

/* ------------------------------------------------------------
   九、时间线
   ------------------------------------------------------------ */

/**
 * 时间线。
 * @param {Array<{title:string,time?:string,body?:string,tone?:string}>} items
 */
export function timeline(items = []) {
  const rows = items
    .map(
      (item) => html`<div class="timeline-item">
        <span class="timeline-dot" data-tone="${item.tone || 'brand'}"></span>
        <div class="timeline-head">
          <span class="timeline-title">${item.title}</span>
          ${raw(item.time ? html`<span class="timeline-time">${item.time}</span>` : '')}
        </div>
        ${raw(item.body ? html`<div class="timeline-body">${item.body}</div>` : '')}
      </div>`,
    )
    .join('');
  return html`<div class="timeline">${raw(rows)}</div>`;
}

/* ------------------------------------------------------------
   十、树形
   ------------------------------------------------------------ */

/**
 * 渲染树（递归）。
 * @param {Array<{id:string,label:string,children?:Array,meta?:string}>} nodes
 * @param {object} [o]
 * @param {string} [o.activeId]
 * @param {Set<string>} [o.expanded] 展开的节点 ID 集合
 * @param {string} [o.action='select-tree']
 */
export function tree(nodes = [], o = {}) {
  const { activeId = '', expanded = new Set(), action = 'select-tree', level = 0 } = o;

  const rows = nodes
    .map((node) => {
      const children = Array.isArray(node.children) ? node.children : [];
      const hasChildren = children.length > 0;
      const isOpen = expanded.has(node.id);

      return html`<div class="tree-node" data-open="${isOpen ? 'true' : 'false'}" data-node-id="${node.id}">
        <div
          class="tree-row ${node.id === activeId ? 'active' : ''}"
          data-action="${action}"
          data-id="${node.id}"
          style="padding-left:${level * 12 + 8}px"
        >
          ${raw(hasChildren ? html`<span class="tree-toggle" data-action="toggle-tree" data-id="${node.id}">▸</span>` : '<span class="tree-toggle"></span>')}
          <span class="tree-label">${node.label}</span>
          ${raw(node.meta ? html`<span class="text-xs text-secondary">${node.meta}</span>` : '')}
        </div>
        ${raw(hasChildren ? html`<div class="tree-children">${raw(tree(children, { ...o, level: level + 1 }))}</div>` : '')}
      </div>`;
    })
    .join('');

  return level === 0 ? html`<div class="tree">${raw(rows)}</div>` : rows;
}

/* ------------------------------------------------------------
   十一、其他
   ------------------------------------------------------------ */

/** 进度条 */
export function progress(value, o = {}) {
  const { tone = 'brand', max = 100, showLabel = false } = o;
  const percent = Math.max(0, Math.min(100, (Number(value) / max) * 100));
  return html`<div>
    ${raw(
      showLabel
        ? html`<div class="flex-between mb-1 text-xs text-secondary"><span>进度</span><span class="num">${percent.toFixed(0)}%</span></div>`
        : '',
    )}
    <div class="progress">
      <div class="progress-bar" data-tone="${tone}" style="width:${percent}%"></div>
    </div>
  </div>`;
}

/** 头像 */
export function avatar(name, o = {}) {
  const { size = 'md', color = '' } = o;
  const text = String(name || '?').trim().slice(0, 1) || '?';
  const cls = ['avatar', size !== 'md' ? `avatar-${size}` : ''].filter(Boolean).join(' ');
  return html`<span class="${cls}" ${raw(color ? `style="background:${esc(color)}"` : '')}>${text.toUpperCase()}</span>`;
}

/**
 * 下拉菜单。
 * 由调用方负责定位（见 dom.positionFloating）。
 * @param {Array<{key:string,label:string,icon?:string,danger?:boolean,divider?:boolean}>} items
 * @param {object} [o] action
 */
export function dropdown(items = [], o = {}) {
  const { action = 'menu-click' } = o;
  const rows = items
    .map((item) => {
      if (item.divider) return '<div class="dropdown-divider"></div>';
      return html`<div class="dropdown-item" data-action="${action}" data-key="${item.key}" ${raw(item.danger ? 'data-danger="true"' : '')}>
        ${raw(item.icon ? icon(item.icon, { size: 14 }) : '')}<span>${item.label}</span>
      </div>`;
    })
    .join('');
  return html`<div class="dropdown" role="menu">${raw(rows)}</div>`;
}

/**
 * 页面头部（标题 + 描述 + 操作按钮）。
 * @param {object} o
 * @param {string} o.title
 * @param {string} [o.desc]
 * @param {string} [o.actions] 按钮 HTML
 * @param {string} [o.breadcrumb] 面包屑 HTML
 */
export function pageHead(o = {}) {
  const { title = '', desc = '', actions = '' } = o;
  return html`<div class="page-head">
    <div class="page-head-title">
      <h2>${title}</h2>
      ${raw(desc ? html`<div class="page-head-desc">${desc}</div>` : '')}
    </div>
    ${raw(actions ? html`<div class="page-head-actions">${raw(actions)}</div>` : '')}
  </div>`;
}

/** 区块标题（卡片之间的小标题） */
export function sectionTitle(title, o = {}) {
  const { desc = '', actions = '' } = o;
  return html`<div class="flex-between mb-3">
    <div>
      <div class="font-semibold text-primary">${title}</div>
      ${raw(desc ? html`<div class="text-xs text-secondary mt-1">${desc}</div>` : '')}
    </div>
    ${raw(actions ? html`<div class="btn-group">${raw(actions)}</div>` : '')}
  </div>`;
}

/**
 * 密钥展示（脱敏 + 复制）。
 * 注意：不接收也不渲染完整 SecretKey —— 后端不回传，前端也无从展示。
 */
export function secretField(maskedValue, o = {}) {
  const { caption = '' } = o;
  return html`<div>
    <div class="secret-field">
      <span class="secret-value">${orDash(maskedValue)}</span>
    </div>
    ${raw(caption ? html`<div class="field-hint mt-1">${caption}</div>` : '')}
  </div>`;
}

/** 状态点（在线/离线） */
export function statusDot(online, label = '') {
  return html`<span class="inline-flex items-center gap-1 text-xs">
    <span class="mp-dot ${online ? 'pulse' : 'offline'}"></span>
    ${raw(label ? html`<span>${label}</span>` : '')}
  </span>`;
}

export default {
  button,
  iconButton,
  tag,
  statusTag,
  badge,
  card,
  statCard,
  statGrid,
  descList,
  kvList,
  alert,
  emptyState,
  skeleton,
  loadingState,
  tabs,
  segmented,
  steps,
  timeline,
  tree,
  progress,
  avatar,
  dropdown,
  pageHead,
  sectionTitle,
  secretField,
  statusDot,
};
