/* ============================================================
   模态框 / 抽屉 / 确认对话框
   ------------------------------------------------------------
   替代原型的 App.modal()（其缺陷：不支持嵌套、无法返回结果、
   关闭后不清理事件）。本实现：

     * 返回 Promise，便于 await 结果
     * 支持 ESC 关闭、点击遮罩关闭（可配置）
     * 自动聚焦 / 焦点陷阱
     * 关闭时清理 DOM 与事件，避免泄漏

   ⚠️ 实现注意（曾经踩过的坑）
   ---------------------------
   body / footer 允许传**函数**，函数会收到 api 对象用于关闭弹窗。
   因此 `api` 必须在渲染 body/footer **之前**定义好，否则
   `renderInto(el, footerFn)` 里调用 `footerFn(api)` 会命中
   `const api` 的暂时性死区（TDZ），抛出：
     ReferenceError: Cannot access 'api' before initialization
   表现为弹窗出现但内容为空。
   本文件按「先建元素 → 再定义 close/api → 最后渲染内容」的顺序组织。
   ============================================================ */

import { clear, h, uid } from './dom.js';

/** 当前打开的浮层栈（支持嵌套） */
const stack = [];

/* ------------------------------------------------------------
   一、基础工具
   ------------------------------------------------------------ */

function createMask(className) {
  const mask = h('div', { class: className });
  document.body.append(mask);
  // 打开浮层时禁止背景滚动
  document.body.style.overflow = 'hidden';
  return mask;
}

function releaseBodyScroll() {
  if (stack.length === 0) document.body.style.overflow = '';
}

/** 焦点陷阱：Tab 在浮层内循环 */
function trapFocus(container, event) {
  const focusable = container.querySelectorAll(
    'a[href], button:not([disabled]), textarea:not([disabled]), ' +
      'input:not([disabled]):not([type="hidden"]), select:not([disabled]), ' +
      '[tabindex]:not([tabindex="-1"])',
  );
  if (!focusable.length) return;

  const first = focusable[0];
  const last = focusable[focusable.length - 1];

  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

/**
 * 渲染内容到容器。
 * @param {HTMLElement} target
 * @param {string|Node|Function} content
 * @param {object} api 传给函数形式内容的接口对象
 */
function renderInto(target, content, api) {
  clear(target);

  if (typeof content === 'function') {
    const produced = content(api);
    if (produced instanceof Node) target.append(produced);
    else if (Array.isArray(produced)) {
      produced.forEach((item) => {
        if (item instanceof Node) target.append(item);
        else if (item !== null && item !== undefined) {
          target.append(document.createTextNode(String(item)));
        }
      });
    } else if (produced !== null && produced !== undefined) {
      target.append(document.createTextNode(String(produced)));
    }
    return;
  }

  if (content instanceof Node) {
    target.append(content);
    return;
  }

  if (content !== null && content !== undefined && content !== '') {
    // 调用方传入的 HTML 字符串；动态数据须由调用方自行转义（用 dom.html``）
    target.innerHTML = String(content);
  }
}

/** 自动聚焦 */
function autoFocus(root) {
  requestAnimationFrame(() => {
    const target =
      root.querySelector('[data-autofocus]') ||
      root.querySelector('input:not([type="hidden"]), textarea, select') ||
      root.querySelector('button');
    target?.focus();
  });
}

/* ------------------------------------------------------------
   二、Modal
   ------------------------------------------------------------ */

/**
 * 打开模态框。
 *
 * @param {object} options
 * @param {string} options.title
 * @param {string} [options.subtitle]
 * @param {string|Node|Function} options.body
 * @param {string|Node|Function} [options.footer]
 * @param {'sm'|'md'|'lg'|'xl'} [options.size='md']
 * @param {boolean} [options.closeOnMask=true]
 * @param {boolean} [options.closeOnEsc=true]
 * @param {boolean} [options.closable=true] 是否显示右上角关闭按钮（强制流程设为 false）
 * @param {Function} [options.onMount] (bodyEl, api) => void
 * @returns {{close:Function, result:Promise<any>, el:HTMLElement, body:HTMLElement,
 *           setBody:Function, setFooter:Function, showError:Function, submit?:Function}}
 */
export function modal(options = {}) {
  const {
    title = '',
    subtitle = '',
    body = '',
    footer = null,
    size = 'md',
    closeOnMask = true,
    closeOnEsc = true,
    closable = true,
    onMount = null,
  } = options;

  /* ---- 1. 结果 Promise ---- */
  let resolveResult;
  const result = new Promise((resolve) => {
    resolveResult = resolve;
  });

  /* ---- 2. 骨架元素 ---- */
  const mask = createMask('modal-mask show');
  const titleId = uid('modal-title');

  const modalEl = h('div', {
    class: `modal ${size === 'md' ? '' : `modal-${size}`}`.trim(),
    role: 'dialog',
    'aria-modal': 'true',
    'aria-labelledby': title ? titleId : null,
  });

  const closeBtn = h('button', {
    class: 'modal-close',
    type: 'button',
    'aria-label': '关闭',
    text: '×',
  });

  const modalTitle = h('div', { class: 'modal-title', id: titleId, text: title });
  const head = h(
    'div',
    { class: 'modal-header' },
    h('div', {}, modalTitle, subtitle ? h('div', { class: 'modal-subtitle', text: subtitle }) : null),
    // 强制流程（如首次登录改密）不提供关闭入口，避免用户绕过
    closable ? closeBtn : null,
  );

  const bodyEl = h('div', { class: 'modal-body' });
  let footerEl = null;

  /* ---- 3. 关闭逻辑（必须先于 api 定义） ---- */
  let entry = null;

  const close = (value) => {
    const index = stack.indexOf(entry);
    if (index >= 0) stack.splice(index, 1);

    document.removeEventListener('keydown', onKeyDown, true);
    mask.classList.remove('show');
    mask.remove();
    releaseBodyScroll();

    resolveResult(value);
  };

  const onKeyDown = (event) => {
    if (stack[stack.length - 1] !== entry) return;

    if (event.key === 'Escape' && closeOnEsc) {
      event.stopPropagation();
      close(undefined);
      return;
    }
    if (event.key === 'Tab') {
      trapFocus(modalEl, event);
    }
  };

  /* ---- 4. api 对象（body/footer 的函数形式会用到） ---- */
  const api = {
    close,
    el: modalEl,
    body: bodyEl,
    get footer() {
      return footerEl;
    },
    /** 替换正文内容 */
    setBody: (content) => renderInto(bodyEl, content, api),
    /** 替换或创建底部内容 */
    setFooter: (content) => {
      if (!footerEl) {
        footerEl = h('div', { class: 'modal-footer' });
        modalEl.append(footerEl);
      }
      renderInto(footerEl, content, api);
    },
    /** 在正文位置展示一个内联错误提示 */
    showError: (message) => {
      clear(bodyEl);
      bodyEl.append(
        h(
          'div',
          { class: 'alert alert-danger' },
          h('span', { class: 'alert-icon', text: '!' }),
          h('div', { class: 'alert-body', text: message }),
        ),
      );
    },
    /** 提交入口占位；footer 中可赋值，便于回车触发 */
    submit: null,
  };

  /* ---- 5. 渲染内容（此时 api 已就绪） ---- */
  renderInto(bodyEl, body, api);
  modalEl.append(head, bodyEl);

  if (footer) {
    footerEl = h('div', { class: 'modal-footer' });
    renderInto(footerEl, footer, api);
    modalEl.append(footerEl);
  }

  mask.append(modalEl);

  /* ---- 6. 事件 ---- */
  closeBtn.addEventListener('click', () => close(undefined));

  if (closeOnMask) {
    mask.addEventListener('mousedown', (event) => {
      if (event.target === mask) close(undefined);
    });
  }

  document.addEventListener('keydown', onKeyDown, true);

  entry = { close, el: modalEl, mask };
  stack.push(entry);

  autoFocus(modalEl);
  onMount?.(bodyEl, api);

  return { ...api, result, resolve: resolveResult };
}

/* ------------------------------------------------------------
   三、确认对话框
   ------------------------------------------------------------ */

const CONFIRM_TONES = {
  danger: { icon: '⚠', tone: 'danger', confirmClass: 'btn-danger' },
  warning: { icon: '⚠', tone: 'warning', confirmClass: 'btn-primary' },
  info: { icon: 'i', tone: 'info', confirmClass: 'btn-primary' },
};

/**
 * 打开确认对话框。
 *
 * @param {object} options
 * @param {string} options.title
 * @param {string} [options.description]
 * @param {string} [options.detail] 补充说明（如影响范围）
 * @param {'danger'|'warning'|'info'} [options.tone='warning']
 * @param {string} [options.confirmText='确定']
 * @param {string} [options.cancelText='取消']
 * @param {boolean}[options.requireReason] 需要填写原因
 * @param {string} [options.reasonLabel]
 * @returns {Promise<{confirmed:boolean, reason?:string}>}
 */
export function confirmDialog(options = {}) {
  const {
    title = '确认操作',
    description = '',
    detail = '',
    tone = 'warning',
    confirmText = '确定',
    cancelText = '取消',
    requireReason = false,
    reasonLabel = '操作原因',
  } = options;

  const preset = CONFIRM_TONES[tone] || CONFIRM_TONES.warning;

  return new Promise((resolve) => {
    let reasonInput = null;

    /* 注意：这里用函数形式，因此必须是「先建元素、再渲染」的形态 */
    const buildBody = () => {
      const node = h(
        'div',
        { class: 'confirm-body' },
        h('div', { class: 'confirm-icon', 'data-tone': preset.tone, text: preset.icon }),
        h(
          'div',
          { class: 'confirm-text' },
          h('div', { class: 'confirm-title', text: title }),
          description ? h('div', { class: 'confirm-desc', text: description }) : null,
          detail ? h('div', { class: 'confirm-desc text-secondary', text: detail }) : null,
          requireReason
            ? h(
                'div',
                { class: 'field mt-3' },
                h('label', { class: 'field-label', text: reasonLabel }),
                h('textarea', {
                  class: 'textarea',
                  rows: '3',
                  placeholder: '请填写原因（必填）',
                  'data-autofocus': '',
                }),
              )
            : null,
        ),
      );

      if (requireReason) reasonInput = node.querySelector('textarea');
      return node;
    };

    const dialog = modal({
      title: '',
      body: buildBody,
      size: 'sm',
      footer: (api) => {
        const cancel = h('button', { class: 'btn', type: 'button', text: cancelText });
        const ok = h('button', {
          class: `btn ${preset.confirmClass}`,
          type: 'button',
          text: confirmText,
          'data-autofocus': requireReason ? null : '',
        });

        cancel.addEventListener('click', () => api.close({ confirmed: false }));

        ok.addEventListener('click', () => {
          if (requireReason) {
            const reason = reasonInput?.value.trim() || '';
            if (!reason) {
              reasonInput?.setAttribute('aria-invalid', 'true');
              reasonInput?.focus();
              return;
            }
            api.close({ confirmed: true, reason });
            return;
          }
          api.close({ confirmed: true });
        });

        api.submit = () => ok.click();
        return [cancel, ok];
      },
    });

    dialog.result.then((value) => resolve(value || { confirmed: false }));
  });
}

/** 简化的确认（只关心是否确认） */
export async function confirm(title, description, options = {}) {
  const { confirmed } = await confirmDialog({ title, description, ...options });
  return confirmed;
}

/** 危险操作确认（删除等） */
export function confirmDanger(title, description, detail) {
  return confirmDialog({ title, description, detail, tone: 'danger', confirmText: '确认删除' });
}

/* ------------------------------------------------------------
   四、抽屉
   ------------------------------------------------------------ */

/**
 * 打开右侧抽屉。
 *
 * @param {object} options
 * @param {string} options.title
 * @param {string|Node|Function} options.body
 * @param {string|Node|Function} [options.footer]
 * @param {boolean} [options.wide]
 * @param {Function} [options.onMount]
 */
export function drawer(options = {}) {
  const { title = '', body = '', footer = null, wide = false, onMount = null } = options;

  let resolveResult;
  const result = new Promise((resolve) => {
    resolveResult = resolve;
  });

  /* ---- 1. 骨架 ---- */
  const mask = createMask('drawer-mask show');
  const titleId = uid('drawer-title');

  const panel = h('div', {
    class: `drawer ${wide ? 'drawer-wide' : ''}`.trim(),
    role: 'dialog',
    'aria-modal': 'true',
    'aria-labelledby': titleId,
  });

  const closeBtn = h('button', {
    class: 'modal-close',
    type: 'button',
    'aria-label': '关闭',
    text: '×',
  });

  const bodyEl = h('div', { class: 'modal-body' });
  let footerEl = null;

  /* ---- 2. 关闭逻辑 ---- */
  let entry = null;

  const close = (value) => {
    const index = stack.indexOf(entry);
    if (index >= 0) stack.splice(index, 1);

    document.removeEventListener('keydown', onKeyDown, true);
    mask.classList.remove('show');
    mask.remove();
    releaseBodyScroll();
    resolveResult(value);
  };

  const onKeyDown = (event) => {
    if (event.key === 'Escape' && stack[stack.length - 1] === entry) {
      event.stopPropagation();
      close(undefined);
    }
  };

  /* ---- 3. api（先于内容渲染） ---- */
  const api = {
    close,
    el: panel,
    body: bodyEl,
    get footer() {
      return footerEl;
    },
    setBody: (content) => renderInto(bodyEl, content, api),
    setFooter: (content) => {
      if (!footerEl) {
        footerEl = h('div', { class: 'modal-footer' });
        panel.append(footerEl);
      }
      renderInto(footerEl, content, api);
    },
    submit: null,
  };

  /* ---- 4. 渲染 ---- */
  panel.append(
    h('div', { class: 'modal-header' }, h('div', { class: 'modal-title', id: titleId, text: title }), closeBtn),
    bodyEl,
  );

  renderInto(bodyEl, body, api);

  if (footer) {
    footerEl = h('div', { class: 'modal-footer' });
    renderInto(footerEl, footer, api);
    panel.append(footerEl);
  }

  mask.append(panel);

  /* ---- 5. 事件 ---- */
  closeBtn.addEventListener('click', () => close(undefined));
  mask.addEventListener('mousedown', (event) => {
    if (event.target === mask) close(undefined);
  });
  document.addEventListener('keydown', onKeyDown, true);

  entry = { close, el: panel, mask };
  stack.push(entry);

  autoFocus(panel);
  onMount?.(bodyEl, api);

  return { ...api, result, el: panel };
}

/* ------------------------------------------------------------
   五、栈操作
   ------------------------------------------------------------ */

/** 关闭最顶层浮层 */
export function closeTop() {
  stack[stack.length - 1]?.close(undefined);
}

/** 关闭全部浮层 */
export function closeAll() {
  while (stack.length) stack[stack.length - 1].close(undefined);
}

/** 当前是否有浮层打开 */
export function hasOpenLayer() {
  return stack.length > 0;
}

export default {
  modal,
  confirm,
  confirmDialog,
  confirmDanger,
  drawer,
  closeTop,
  closeAll,
  hasOpenLayer,
};
