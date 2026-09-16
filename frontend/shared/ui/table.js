/* ============================================================
   数据表格
   ------------------------------------------------------------
   替代原型的静态表格 + 假分页（原型的 pagination 只渲染按钮，
   点击提示「原型仅展示前10条」）。本实现为**真实的服务端分页**。

   两种用法：
     1. table()  —— 渲染静态表格（数据已在手）
     2. DataTable —— 有状态表格，接管排序 / 分页 / 加载态 / 空态
   ============================================================ */

import { clear, esc, formatNumber, html, raw } from './dom.js';
import { icon } from './icons.js';
import { emptyState, loadingState } from './components.js';

/* ------------------------------------------------------------
   一、静态表格
   ------------------------------------------------------------ */

/**
 * 渲染表格。
 *
 * @param {object} o
 * @param {Array<{key:string,title:string,width?:string,align?:string,className?:string,nowrap?:boolean,sortable?:boolean,render?:(row:any)=>string}>} o.columns
 * @param {Array<any>} o.rows
 * @param {string} [o.emptyText]
 * @param {object} [o.data] 供事件委托使用的 data-* 属性（放在 <table> 上）
 * @param {string} [o.rowAction] 行点击的 data-action 值
 * @param {(row:any)=>string} [o.rowDataAttrs] 生成行级 data-* 属性
 */
export function table(o = {}) {
  const {
    columns = [],
    rows = [],
    emptyText = '暂无数据',
    rowAction = '',
    rowKey = 'id',
    rowDataAttrs = null,
    sort = null,
    action = '',
  } = o;

  const head = columns
    .map((col) => {
      const isActive = sort && sort.by === col.key;
      const alignCls = col.align === 'right' ? 'cell-actions' : col.align === 'center' ? 'text-center' : '';
      const sortCls = col.sortable ? 'sortable' : '';
      const indicator = col.sortable
        ? html`<span class="sort-indicator">${isActive ? (sort.order === 'asc' ? '▲' : '▼') : '⇅'}</span>`
        : '';
      const sortAttrs = col.sortable
        ? `data-action="sort" data-key="${esc(col.key)}" data-sort-active="${isActive ? 'true' : 'false'}"`
        : '';
      return html`<th
        class="${alignCls} ${sortCls}"
        ${raw(col.width ? `style="width:${esc(col.width)}"` : '')}
        ${raw(sortAttrs)}
      >${col.title}${raw(indicator)}</th>`;
    })
    .join('');

  if (!rows.length) {
    return html`<div class="table-wrap">
      <table class="table">
        <thead><tr>${raw(head)}</tr></thead>
        <tbody>
          <tr class="empty-row">
            <td colspan="${columns.length}">${raw(emptyState({ title: emptyText }))}</td>
          </tr>
        </tbody>
      </table>
    </div>`;
  }

  const body = rows
    .map((row) => {
      const cells = columns
        .map((col) => {
          const content = typeof col.render === 'function' ? col.render(row) : (row[col.key] ?? '-');
          const classes = [
            col.className || '',
            col.align === 'right' ? 'cell-actions' : '',
            col.align === 'center' ? 'text-center' : '',
            col.align === 'num' ? 'cell-num' : '',
            col.nowrap ? 'cell-nowrap' : '',
          ]
            .filter(Boolean)
            .join(' ');
          const style = col.width ? `style="width:${esc(col.width)}"` : '';
          // 列渲染函数返回的已是可信 HTML（调用方负责用 html`` 转义内部数据）
          return html`<td class="${classes}" ${raw(style)}>${raw(String(content ?? '-'))}</td>`;
        })
        .join('');

      const attrs = rowDataAttrs ? rowDataAttrs(row) : '';
      const clickable = rowAction ? 'true' : 'false';
      const actionAttr = rowAction ? `data-action="${esc(rowAction)}" data-key="${esc(row[rowKey] ?? '')}"` : '';

      return html`<tr data-clickable="${clickable}" ${raw(actionAttr)} ${raw(attrs)}>${raw(cells)}</tr>`;
    })
    .join('');

  const tableAttrs = Object.entries(o.data || {})
    .map(([k, v]) => `data-${esc(k)}="${esc(v)}"`)
    .join(' ');

  return html`<div class="table-wrap" ${raw(tableAttrs)} ${raw(action ? `data-table="${esc(action)}"` : '')}>
    <table class="table">
      <thead><tr>${raw(head)}</tr></thead>
      <tbody>${raw(body)}</tbody>
    </table>
  </div>`;
}

/* ------------------------------------------------------------
   二、工具条
   ------------------------------------------------------------ */

/**
 * 表格工具条。
 * @param {object} o
 * @param {string} [o.left] 左侧内容（筛选器）
 * @param {string} [o.right] 右侧内容（操作按钮）
 */
export function toolbar(o = {}) {
  return html`<div class="toolbar">
    <div class="toolbar-filters">${raw(o.left || '')}</div>
    <div class="toolbar-spacer"></div>
    <div class="btn-group">${raw(o.right || '')}</div>
  </div>`;
}

/* ------------------------------------------------------------
   三、分页
   ------------------------------------------------------------ */

/**
 * 渲染分页控件。
 * @param {object} o
 * @param {number} o.page
 * @param {number} o.pageSize
 * @param {number} o.total
 * @param {number[]} [o.pageSizeOptions]
 */
export function pagination(o = {}) {
  const { page = 1, pageSize = 20, total = 0, pageSizeOptions = [10, 20, 50, 100] } = o;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  /* ---- 页码按钮（当前页附近 ±2，首尾 + 省略号） ---- */
  const pages = [];
  const push = (value) => {
    if (!pages.includes(value)) pages.push(value);
  };
  push(1);
  for (let i = page - 2; i <= page + 2; i += 1) {
    if (i > 1 && i < totalPages) push(i);
  }
  if (totalPages > 1) push(totalPages);
  pages.sort((a, b) => a - b);

  const buttons = [];
  let previous = 0;
  pages.forEach((value) => {
    if (previous && value - previous > 1) buttons.push('<span class="pg-ellipsis">…</span>');
    buttons.push(
      `<button type="button" class="pg-btn ${value === page ? 'active' : ''}" data-action="goto-page" data-page="${value}">${value}</button>`,
    );
    previous = value;
  });

  const sizeOptions = pageSizeOptions
    .map((size) => `<option value="${size}" ${size === pageSize ? 'selected' : ''}>${size} 条/页</option>`)
    .join('');

  return html`<div class="table-foot">
    <div class="table-foot-info">共 <span class="num">${formatNumber(total)}</span> 条 · 第 ${page}/${totalPages} 页</div>
    <div class="flex items-center gap-4">
      <div class="pg-size">
        <select class="select" data-action="change-page-size" aria-label="每页条数">${raw(sizeOptions)}</select>
      </div>
      <div class="pagination">
        <button type="button" class="pg-btn" data-action="goto-page" data-page="${page - 1}" ${raw(page <= 1 ? 'disabled' : '')} aria-label="上一页">
          ${raw(icon('chevronLeft', { size: 14 }))}
        </button>
        ${raw(buttons.join(''))}
        <button type="button" class="pg-btn" data-action="goto-page" data-page="${page + 1}" ${raw(page >= totalPages ? 'disabled' : '')} aria-label="下一页">
          ${raw(icon('chevronRight', { size: 14 }))}
        </button>
      </div>
    </div>
  </div>`;
}

/* ------------------------------------------------------------
   四、有状态表格
   ------------------------------------------------------------ */

/**
 * 有状态数据表格：接管加载、排序、分页与空态。
 *
 * 用法：
 *   const dt = new DataTable({
 *     container: el,
 *     columns: [...],
 *     fetcher: (params) => api.get('/platform/tenants', { params }),
 *     rowAction: 'open-tenant',
 *   });
 *   await dt.load();
 *
 * fetcher 接收 { page, pageSize, sortBy, order, ...filters }，
 * 需返回 { records, total, page, pageSize }。
 */
export class DataTable {
  constructor(options = {}) {
    const {
      container,
      columns = [],
      fetcher,
      filters = {},
      pageSize = 20,
      rowAction = '',
      emptyText = '暂无数据',
      toolbarLeft = '',
      toolbarRight = '',
      rowDataAttrs = null,
      /** 是否在无数据时隐藏工具条 */
      hideToolbarWhenEmpty = false,
    } = options;

    if (!container) throw new Error('DataTable 需要 container');
    if (typeof fetcher !== 'function') throw new Error('DataTable 需要 fetcher');

    this.container = container;
    this.columns = columns;
    this.fetcher = fetcher;
    this.filters = { ...filters };
    this.rowAction = rowAction;
    this.emptyText = emptyText;
    this.toolbarLeft = toolbarLeft;
    this.toolbarRight = toolbarRight;
    this.rowDataAttrs = rowDataAttrs;
    this.hideToolbarWhenEmpty = hideToolbarWhenEmpty;

    this.state = {
      page: 1,
      pageSize,
      sortBy: null,
      order: 'desc',
      total: 0,
      rows: [],
      loading: false,
      error: null,
    };

    /** @type {Set<Function>} */
    this._listeners = new Set();
    this._requestSeq = 0;
  }

  /* ---- 订阅渲染完成事件（供页面绑定行内按钮） ---- */
  onRendered(handler) {
    this._listeners.add(handler);
    return () => this._listeners.delete(handler);
  }

  /* ---- 加载数据 ---- */
  async load({ resetPage = false } = {}) {
    if (resetPage) this.state.page = 1;

    const seq = ++this._requestSeq;
    this.state.loading = true;
    this.state.error = null;
    this.render();

    try {
      const payload = await this.fetcher({
        page: this.state.page,
        pageSize: this.state.pageSize,
        sortBy: this.state.sortBy,
        order: this.state.order,
        ...this.filters,
      });

      // 丢弃过期响应（快速连点分页时的竞态）
      if (seq !== this._requestSeq) return;

      this.state.rows = payload?.records ?? [];
      this.state.total = payload?.total ?? 0;
      this.state.loading = false;

      // 删除最后一页最后一条后，自动回退到有效页
      if (!this.state.rows.length && this.state.page > 1) {
        this.state.page = Math.max(1, Math.ceil(this.state.total / this.state.pageSize));
        await this.load();
        return;
      }
    } catch (error) {
      if (seq !== this._requestSeq) return;
      this.state.loading = false;
      this.state.error = error;
      this.state.rows = [];
      this.state.total = 0;
    }

    this.render();
  }

  /** 更新筛选条件并重新加载 */
  setFilters(patch, { resetPage = true } = {}) {
    Object.assign(this.filters, patch);
    return this.load({ resetPage });
  }

  /** 排序 */
  toggleSort(key) {
    if (!key) return;
    if (this.state.sortBy === key) {
      this.state.order = this.state.order === 'asc' ? 'desc' : 'asc';
    } else {
      this.state.sortBy = key;
      this.state.order = 'desc';
    }
    return this.load();
  }

  gotoPage(page) {
    const totalPages = Math.max(1, Math.ceil(this.state.total / this.state.pageSize));
    const target = Math.min(Math.max(1, Number(page) || 1), totalPages);
    if (target === this.state.page) return undefined;
    this.state.page = target;
    return this.load();
  }

  changePageSize(size) {
    this.state.pageSize = Number(size) || 20;
    this.state.page = 1;
    return this.load();
  }

  /** 重新加载当前页 */
  refresh() {
    return this.load();
  }

  /* ---- 渲染 ---- */
  render() {
    const { loading, error, rows, page, pageSize, total } = this.state;

    const showToolbar = !(this.hideToolbarWhenEmpty && !loading && !rows.length && !error);

    let bodyHtml;
    if (loading && !rows.length) {
      bodyHtml = loadingState('加载中…');
    } else if (error) {
      bodyHtml = emptyState({
        icon: 'alertTriangle',
        title: '加载失败',
        desc: error.message || '请稍后重试',
        action: '<button class="btn btn-primary mt-2" data-action="retry-load">重新加载</button>',
      });
    } else {
      bodyHtml =
        table({
          columns: this.columns,
          rows,
          emptyText: this.emptyText,
          rowAction: this.rowAction,
          rowDataAttrs: this.rowDataAttrs,
          sort: { by: this.state.sortBy, order: this.state.order },
        }) +
        (rows.length
          ? pagination({ page, pageSize, total })
          : '');
    }

    const markup = html`<div class="table-card">
      ${raw(showToolbar ? toolbar({ left: this.toolbarLeft, right: this.toolbarRight }) : '')}
      ${raw(bodyHtml)}
    </div>`;

    clear(this.container);
    const template = document.createElement('template');
    template.innerHTML = markup.trim();
    this.container.append(template.content);

    // 通知订阅者：可在此绑定行内按钮
    this._listeners.forEach((handler) => {
      try {
        handler(this.container, this);
      } catch (err) {
        console.error('[DataTable] 渲染回调失败', err);
      }
    });
  }

  /** 当前行数据（供行操作查找） */
  findRow(predicate) {
    return this.state.rows.find(predicate);
  }

  findRowById(id) {
    return this.state.rows.find((row) => String(row.id) === String(id));
  }

  get rows() {
    return this.state.rows;
  }
}

/**
 * 通用表格事件绑定：把工具条 / 分页 / 排序的点击接到 DataTable 上。
 * 页面只需调用一次。
 *
 * @param {HTMLElement} container
 * @param {DataTable} tableInstance
 * @returns {Function} 解绑函数
 */
export function bindTableEvents(container, tableInstance) {
  const handler = (event) => {
    const target = event.target.closest('[data-action]');
    if (!target || !container.contains(target)) return;

    const { action } = target.dataset;

    if (action === 'goto-page') {
      event.stopPropagation();
      tableInstance.gotoPage(target.dataset.page);
      return;
    }
    if (action === 'sort') {
      tableInstance.toggleSort(target.dataset.key);
      return;
    }
    if (action === 'retry-load') {
      tableInstance.refresh();
    }
  };

  const changeHandler = (event) => {
    const target = event.target.closest('[data-action="change-page-size"]');
    if (!target) return;
    tableInstance.changePageSize(target.value);
  };

  container.addEventListener('click', handler);
  container.addEventListener('change', changeHandler);

  return () => {
    container.removeEventListener('click', handler);
    container.removeEventListener('change', changeHandler);
  };
}

export default { table, toolbar, pagination, DataTable, bindTableEvents };
