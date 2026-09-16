/* ============================================================
   通知提示（Toast）
   ------------------------------------------------------------
   替代原型的 alert() 与单例 toast。
   支持堆叠多条、自动消失、手动关闭、错误详情展开。
   ============================================================ */

import { h } from './dom.js';

const CONTAINER_ID = 'toastStack';

/** 类型 → 图标与样式 */
const TONES = {
  success: { icon: '✓', cls: 'toast-success' },
  error: { icon: '!', cls: 'toast-error' },
  warning: { icon: '!', cls: 'toast-warning' },
  info: { icon: 'i', cls: 'toast-info' },
  loading: { icon: null, cls: '' },
};

const DEFAULT_DURATION = {
  success: 2600,
  info: 3000,
  warning: 4000,
  error: 6000,
  loading: 0, // 不自动关闭
};

function ensureContainer() {
  let container = document.getElementById(CONTAINER_ID);
  if (!container) {
    container = h('div', { id: CONTAINER_ID, class: 'toast-stack', role: 'status', 'aria-live': 'polite' });
    document.body.append(container);
  }
  return container;
}

/**
 * 显示一条通知。
 *
 * @param {string} message
 * @param {object} [options]
 * @param {'success'|'error'|'warning'|'info'|'loading'} [options.type]
 * @param {number} [options.duration] 毫秒；0 表示不自动关闭
 * @param {string} [options.description] 次要说明
 * @param {string} [options.traceId] 错误追踪 ID（便于用户报障）
 * @param {boolean}[options.closable]
 * @returns {{close:Function, el:HTMLElement}}
 */
export function toast(message, options = {}) {
  const {
    type = 'info',
    duration,
    description = '',
    traceId = '',
    closable = true,
  } = options;

  const tone = TONES[type] || TONES.info;
  const container = ensureContainer();

  const iconNode = tone.icon
    ? h('span', { class: 'toast-icon', text: tone.icon })
    : h('span', { class: 'spinner' });

  const body = h('div', { class: 'toast-message' }, h('span', { text: message }));

  if (description) {
    body.append(h('div', { class: 'text-xs mt-1', style: { opacity: '0.85' }, text: description }));
  }

  if (traceId) {
    body.append(
      h('div', {
        class: 'text-xs mt-1 mono',
        style: { opacity: '0.7' },
        text: `traceId: ${traceId}`,
      }),
    );
  }

  const node = h('div', { class: `toast ${tone.cls}`, role: 'alert' }, iconNode, body);

  let closeBtn = null;
  if (closable) {
    closeBtn = h('button', {
      class: 'btn-icon btn-sm',
      type: 'button',
      'aria-label': '关闭提示',
      text: '×',
      style: { fontSize: '16px', lineHeight: '1' },
    });
    node.append(closeBtn);
  }

  const close = () => {
    if (node.dataset.closing) return;
    node.dataset.closing = 'true';
    node.classList.add('leaving');
    setTimeout(() => node.remove(), 150);
  };

  closeBtn?.addEventListener('click', close);
  container.append(node);

  const wait = duration ?? DEFAULT_DURATION[type] ?? 3000;
  if (wait > 0) setTimeout(close, wait);

  return { close, el: node };
}

/* ------------------------------------------------------------
   便捷方法
   ------------------------------------------------------------ */

toast.success = (message, options) => toast(message, { ...options, type: 'success' });
toast.error = (message, options) => toast(message, { ...options, type: 'error' });
toast.warning = (message, options) => toast(message, { ...options, type: 'warning' });
toast.info = (message, options) => toast(message, { ...options, type: 'info' });

/**
 * 展示加载中提示，返回可手动关闭的句柄。
 * 用于「操作进行中」的反馈，完成后调用返回值的 close()。
 */
toast.loading = (message = '处理中…') => toast(message, { type: 'loading', closable: false });

/**
 * 包装一个异步操作：自动展示 loading，成功/失败时替换为结果提示。
 *
 * @param {Function} task 异步任务
 * @param {object} [messages]
 * @param {string} [messages.loading]
 * @param {string} [messages.success]
 * @param {string} [messages.error] 失败时的兜底提示（优先使用后端 message）
 * @returns {Promise<any>}
 */
export async function withToast(task, messages = {}) {
  const { loading = '处理中…', success = '操作成功', error = '操作失败' } = messages;
  const handle = loading ? toast.loading(loading) : null;

  try {
    const result = await task();
    handle?.close();
    if (success) toast.success(success);
    return result;
  } catch (err) {
    handle?.close();
    const detail = err?.message || error;
    const traceId = err?.traceId || '';
    toast.error(detail, { traceId });
    throw err;
  }
}

/**
 * 把 ApiError 渲染为可读提示（供 api.setErrorHandler 使用）。
 */
export function toastApiError(err) {
  if (!err) return;
  // 认证类错误由守卫统一处理，避免重复提示
  if (err.isAuthError) return;
  toast.error(err.message || '请求失败', { traceId: err.traceId || '' });
}

/** 清空全部通知 */
export function clearToasts() {
  const container = document.getElementById(CONTAINER_ID);
  if (container) container.innerHTML = '';
}

export default toast;
