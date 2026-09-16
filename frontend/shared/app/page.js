/* ============================================================
   页面构建辅助
   ------------------------------------------------------------
   把「页头 + 筛选条 + 表格 + 分页」的重复装配收敛到一处，
   各业务页面只需声明列定义与取数函数。

   同时提供权限感知的按钮渲染：无权限时按钮不渲染，
   避免用户点击后才收到 403。
   ============================================================ */

import auth from '../core/auth.js';
import { clear, h } from '../ui/dom.js';
import { button, pageHead } from '../ui/components.js';
import { DataTable, bindTableEvents } from '../ui/table.js';
import { filterBar, readFilters, resetFilters } from '../ui/form.js';

/**
 * 渲染页头。
 * @param {HTMLElement} container
 * @param {object} o
 * @param {string} o.title
 * @param {string} [o.desc]
 * @param {Array<{label:string,icon?:string,variant?:string,action:string,perm?:string,disabled?:boolean}>} [o.actions]
 */
export function renderPageHead(container, o = {}) {
  const { title = '', desc = '', actions = [] } = o;

  const actionsHtml = actions
    .filter((item) => !item.perm || auth.hasPerm(item.perm))
    .map((item) => button(item))
    .join('');

  const head = h('div');
  head.innerHTML = pageHead({ title, desc, actions: actionsHtml }).trim();
  container.append(head.firstElementChild);
}

/**
 * 按权限渲染按钮；无权限返回空字符串。
 * @param {object} o 同 components.button
 */
export function permButton(o = {}) {
  if (o.perm && !auth.hasPerm(o.perm)) return '';
  return button(o);
}

/**
 * 创建列表页：页头 + 筛选条 + 数据表格，并自动接线事件。
 *
 * @param {object} o
 * @param {HTMLElement} o.container  挂载容器
 * @param {string} o.title
 * @param {string} [o.desc]
 * @param {Array} [o.actions]        页头按钮
 * @param {Array} o.columns          列定义
 * @param {Function} o.fetcher       取数函数 ({page,pageSize,sortBy,order,...filters}) => {records,total}
 * @param {Array} [o.filters]        筛选字段定义（form.filterField 的配置）
 * @param {string} [o.rowAction]     行点击的 data-action
 * @param {object} [o.tableOptions]  透传给 DataTable 的其他选项
 * @param {(container:HTMLElement, table:DataTable)=>void} [o.onReady]
 *        表格首次渲染后回调，用于绑定行内按钮
 * @returns {{table:DataTable, reload:Function, setFilters:Function}}
 */
export function createListPage(o = {}) {
  const {
    container,
    title = '',
    desc = '',
    actions = [],
    columns = [],
    fetcher,
    filters = [],
    rowAction = '',
    tableOptions = {},
    onReady = null,
    onAction = null,
  } = o;

  clear(container);

  /* ---- 页头 ---- */
  if (title) renderPageHead(container, { title, desc, actions });

  /* ---- 表格容器 ---- */
  const tableHost = h('div');
  container.append(tableHost);

  /* ---- 数据表 ---- */
  const table = new DataTable({
    container: tableHost,
    columns,
    fetcher,
    filters: {},
    rowAction,
    hideToolbarWhenEmpty: filters.length === 0,
    ...tableOptions,
  });

  /* ---- 筛选条（作为工具条左侧内容） ---- */
  if (filters.length) {
    table.toolbarLeft = filterBar(filters);
  }

  /* ---- 事件接线 ---- */
  const unbindTable = bindTableEvents(tableHost, table);

  // 筛选：查询 / 重置
  const filterHandler = (event) => {
    const target = event.target.closest('[data-action]');
    if (!target) return;

    if (target.dataset.action === 'apply-filter') {
      table.setFilters(readFilters(tableHost));
      return;
    }
    if (target.dataset.action === 'reset-filter') {
      resetFilters(tableHost);
      table.setFilters(readFilters(tableHost));
    }
  };
  tableHost.addEventListener('click', filterHandler);

  // 行内 / 页头操作：交给页面自定义处理
  const actionHandler = (event) => {
    const target = event.target.closest('[data-action]');
    if (!target || !container.contains(target)) return;
    const action = target.dataset.action;
    // 已由表格内部处理的动作不重复分发
    if (['goto-page', 'sort', 'retry-load', 'apply-filter', 'reset-filter'].includes(action)) return;
    onAction?.(action, target, { table, reload: () => table.load() });
  };
  container.addEventListener('click', actionHandler);

  table.onRendered((host, instance) => onReady?.(host, instance));

  const api = {
    table,
    reload: () => table.load(),
    setFilters: (patch) => table.setFilters(patch),
    destroy: () => {
      unbindTable();
      tableHost.removeEventListener('click', filterHandler);
      container.removeEventListener('click', actionHandler);
    },
  };

  api.reload();

  return api;
}

/**
 * 渲染一个详情页的通用骨架（返回按钮 + 页头 + 内容区）。
 *
 * 修复 P-02：返回按钮的行为由调用方按**当前端**给出，
 * 不再硬编码某一端的返回地址。
 *
 * @param {HTMLElement} container
 * @param {object} o
 * @param {string} o.title
 * @param {string} [o.desc]
 * @param {string} o.backPath   本端的列表页路径
 * @param {import('../core/router.js').Router} o.router
 * @param {Array} [o.actions]
 * @param {Array<{key:string,label:string,badge?:number}>} [o.tabs]
 * @param {string} [o.activeTab]
 * @returns {{body:HTMLElement, setTab:Function}}
 */
export function createDetailPage(container, o = {}) {
  const { title = '', desc = '', backPath, router, actions = [], tabs = [], activeTab = '' } = o;

  clear(container);

  /* ---- 返回 + 页头 ---- */
  const head = h('div', { class: 'mb-4' });

  const backRow = h('div', { class: 'mb-2' });
  const backWrapper = document.createElement('template');
  backWrapper.innerHTML = button({
    label: '返回',
    icon: 'arrowLeft',
    variant: 'ghost',
    size: 'sm',
  }).trim();
  backRow.append(backWrapper.content);
  backRow.firstElementChild?.addEventListener('click', () => router.back(backPath));
  head.append(backRow);

  const headWrapper = document.createElement('template');
  headWrapper.innerHTML = pageHead({
    title,
    desc,
    actions: actions
      .filter((item) => !item.perm || auth.hasPerm(item.perm))
      .map((item) => button(item))
      .join(''),
  }).trim();
  head.append(headWrapper.content);
  container.append(head);

  /* ---- 标签页 ---- */
  const body = h('div');
  let active = activeTab;

  if (tabs.length) {
    const tabsEl = h('div', { class: 'tabs mb-4', role: 'tablist' });
    tabs.forEach((tab) => {
      const node = h('div', {
        class: `tab ${tab.key === active ? 'active' : ''}`,
        'data-tab': tab.key,
        role: 'tab',
        text: tab.label,
      });
      tabsEl.append(node);
    });
    container.append(tabsEl);

    const onTabClick = (event) => {
      const target = event.target.closest('[data-tab]');
      if (!target) return;
      active = target.dataset.tab;
      tabsEl.querySelectorAll('[data-tab]').forEach((el) => {
        el.classList.toggle('active', el.dataset.tab === active);
      });
      container.dispatchEvent(new CustomEvent('tabchange', { detail: { tab: active } }));
    };
    tabsEl.addEventListener('click', onTabClick);
  }

  container.append(body);

  return {
    body,
    get activeTab() {
      return active;
    },
    setTab: (key) => {
      active = key;
      container.querySelectorAll('[data-tab]').forEach((el) => {
        el.classList.toggle('active', el.dataset.tab === key);
      });
    },
  };
}

export default { renderPageHead, permButton, createListPage, createDetailPage };
