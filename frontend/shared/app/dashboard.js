/* ============================================================
   工作台构建器（三端共用）
   ------------------------------------------------------------
   三端工作台的骨架一致：页头 + 统计卡片 + 左栏业务说明 + 右栏账号与动态。
   差异只在「统计项、流程步骤、侧栏内容」，因此抽成配置驱动。

   容错设计：
   各统计项依赖的接口在不同阶段交付。这里对每个数据源独立容错，
   接口未就绪时显示「—」而非整页报错——后续阶段接口上线后自动亮起。
   ============================================================ */

import api from '../core/api.js';
import auth from '../core/auth.js';
import { clear, esc, formatRelative, fromHtml, h } from '../ui/dom.js';
import {
  alert,
  card,
  emptyState,
  loadingState,
  statGrid,
  steps,
  timeline,
} from '../ui/components.js';
import { renderPageHead } from './page.js';

/** 角色编码 → 中文名 */
const ROLE_LABELS = {
  PLATFORM_ADMIN: '平台超级管理员',
  PLATFORM_OPERATOR: '平台运营',
  MERCHANT_ADMIN: '商户管理员',
  MERCHANT_OPERATOR: '商户运营',
  FACTORY_ADMIN: '工厂管理员',
  FACTORY_OPERATOR: '工厂操作员',
};

export function roleLabel(roleCode) {
  return ROLE_LABELS[roleCode] || roleCode || '';
}

/**
 * 安全地取列表总数。
 * @param {string} path
 * @param {object} [params]
 * @param {string|number} [fallback='—'] 接口未就绪时的占位值
 */
export async function countSafe(path, params = {}, fallback = '—') {
  try {
    const result = await api.get(path, { params: { pageSize: 1, ...params }, silent: true });
    return result?.total ?? 0;
  } catch {
    return fallback;
  }
}

/** 安全地取列表记录 */
export async function listSafe(path, params = {}) {
  try {
    const result = await api.get(path, { params, silent: true });
    return result?.records || [];
  } catch {
    return [];
  }
}

/**
 * 渲染工作台。
 *
 * @param {HTMLElement} container
 * @param {object} o
 * @param {string} o.end
 * @param {string} o.desc                     页头描述
 * @param {Array}  o.stats                    statCard 配置数组
 * @param {Array}  [o.flowSteps]              流程步骤
 * @param {string} [o.flowTitle]
 * @param {string} [o.flowNote]               流程下方的说明 HTML
 * @param {object} [o.audit]                  { path, params, title } 最近操作来源
 * @param {Array}  [o.extraCards]             额外卡片（已生成的 HTML 字符串数组）
 * @param {string} [o.scopeLabel]             数据范围说明（如「全部租户」）
 * @param {Array}  [o.fallbacks]              接口未就绪时展示的提示条
 */
export async function renderDashboard(container, o) {
  const {
    desc = '',
    stats = [],
    flowSteps = [],
    flowTitle = '核心业务流程',
    flowSubtitle = '',
    flowNote = '',
    audit = null,
    extraCards = [],
    scopeLabel = '',
    fallbacks = [],
  } = o;

  clear(container);
  container.append(fromHtml(loadingState('正在加载工作台…')));

  const user = auth.user || {};
  const [statValues, auditRecords] = await Promise.all([
    Promise.all(stats.map((item) => item.load())),
    audit ? listSafe(audit.path, audit.params || { pageSize: 8 }) : Promise.resolve([]),
  ]);

  clear(container);

  /* ---- 页头 ---- */
  renderPageHead(container, {
    title: `欢迎回来，${user.nickname || user.account || ''}`,
    desc,
  });

  /* ---- 接口未就绪提示 ---- */
  if (fallbacks.length) {
    const host = h('div', { class: 'mb-4' });
    host.innerHTML = fallbacks.map((text) => alert({ tone: 'info', text })).join('');
    container.append(host);
  }

  /* ---- 统计卡片 ---- */
  const statsHost = h('div');
  statsHost.innerHTML = statGrid(
    stats.map((item, index) => ({
      label: item.label,
      value: statValues[index],
      unit: item.unit,
      icon: item.icon,
      tone: item.tone,
      foot: typeof item.foot === 'function' ? item.foot(statValues[index]) : item.foot,
    })),
  ).trim();
  container.append(statsHost.firstElementChild);

  /* ---- 主体两栏 ---- */
  const grid = h('div', {
    style: {
      display: 'grid',
      gridTemplateColumns: 'minmax(0, 2fr) minmax(0, 1fr)',
      gap: 'var(--space-4)',
      alignItems: 'start',
    },
  });
  container.append(grid);

  /* ---- 左栏 ---- */
  const leftHost = h('div');
  const flowBody = [
    flowNote ? `<div class="mb-4">${alert({ tone: 'info', text: flowNote })}</div>` : '',
    flowSteps.length ? steps(flowSteps, 1) : '',
  ].join('');

  const leftCards = [
    card({
      title: flowTitle,
      subtitle: flowSubtitle,
      body: flowBody || emptyState({ title: '暂无流程说明' }),
    }),
    ...extraCards,
  ];
  leftHost.innerHTML = leftCards.join('');
  grid.append(leftHost);

  /* ---- 右栏 ---- */
  const rightHost = h('div');

  const auditHtml = auditRecords.length
    ? timeline(
        auditRecords.map((item) => ({
          title: item.summary || item.action,
          time: formatRelative(item.createdAt || item.created_at),
          body: item.actorAccount ? `操作者：${item.actorAccount}` : '',
          tone: item.success === false ? 'danger' : 'brand',
        })),
      )
    : emptyState({
        icon: 'activity',
        title: '暂无操作记录',
        desc: '随着后续阶段接口交付，这里会显示真实审计数据',
      });

  rightHost.innerHTML =
    card({
      title: '我的账号',
      body: `
        <div class="flex items-center gap-3 mb-4">
          <span class="avatar avatar-lg">${esc((user.nickname || user.account || 'U').slice(0, 1).toUpperCase())}</span>
          <div>
            <div class="font-semibold text-primary">${esc(user.nickname || user.account || '')}</div>
            <div class="text-xs text-secondary">${esc(roleLabel(user.role))}</div>
          </div>
        </div>
        <div class="kv-list">
          <div class="kv-row"><span class="kv-key">账号</span><span class="kv-value mono">${esc(user.account || '')}</span></div>
          <div class="kv-row"><span class="kv-key">角色类型</span><span class="kv-value">${esc(user.roleType || '')}</span></div>
          <div class="kv-row"><span class="kv-key">权限项</span><span class="kv-value num">${(user.permissions || []).length}</span></div>
          ${
            scopeLabel
              ? `<div class="kv-row"><span class="kv-key">数据范围</span><span class="kv-value">${esc(scopeLabel)}</span></div>`
              : ''
          }
        </div>
      `,
    }) +
    card({
      title: '最近操作',
      actions: '<span class="text-xs text-secondary">审计日志</span>',
      body: auditHtml,
    });

  grid.append(rightHost);

  /* ---- 响应式 ---- */
  const mq = window.matchMedia('(max-width: 1100px)');
  const applyLayout = (matches) => {
    grid.style.gridTemplateColumns = matches ? '1fr' : 'minmax(0, 2fr) minmax(0, 1fr)';
  };
  applyLayout(mq.matches);
  mq.addEventListener('change', (event) => applyLayout(event.matches));
}

export default { renderDashboard, countSafe, listSafe, roleLabel };
