/* ============================================================
   表单：渲染、校验、取值
   ------------------------------------------------------------
   替代原型的做法（手写 innerHTML + 提交时逐个读 DOM）。
   本模块提供声明式字段定义，自动完成渲染、校验与取值。

   字段定义示例：
     { key: 'name', label: '租户名称', type: 'text', required: true,
       maxLength: 128, placeholder: '请输入', span: 2 }
     { key: 'status', label: '状态', type: 'select',
       options: [{ value: 'ACTIVE', label: '启用' }] }
   ============================================================ */

import { clear, esc, html, orDash, raw } from './dom.js';

/* ------------------------------------------------------------
   一、字段渲染
   ------------------------------------------------------------ */

/** 选项标准化：支持 ['a','b'] 与 [{value,label}] 两种写法 */
function normalizeOptions(options = []) {
  return options.map((item) =>
    typeof item === 'object' && item !== null
      ? { value: String(item.value ?? ''), label: String(item.label ?? item.value ?? '') }
      : { value: String(item), label: String(item) },
  );
}

/**
 * 渲染单个字段。
 * @param {object} field 字段定义
 * @param {any} value 当前值
 */
export function field(field = {}, value = undefined) {
  const {
    key,
    label = '',
    type = 'text',
    required = false,
    placeholder = '',
    hint = '',
    options = [],
    rows = 3,
    min,
    max,
    maxLength,
    minLength,
    disabled = false,
    span = 1,
    value: defaultValue,
  } = field;

  const current = value !== undefined ? value : (defaultValue ?? '');
  const spanCls = span === 2 ? 'form-grid-full' : '';
  const labelCls = ['field-label', required ? 'field-required' : ''].filter(Boolean).join(' ');
  const disabledAttr = disabled ? 'disabled' : '';
  const commonAttrs = [
    `id="field-${esc(key)}"`,
    `name="${esc(key)}"`,
    `data-field="${esc(key)}"`,
    required ? 'data-required="true"' : '',
    disabledAttr,
    maxLength ? `maxlength="${esc(maxLength)}"` : '',
    min !== undefined ? `min="${esc(min)}"` : '',
    max !== undefined ? `max="${esc(max)}"` : '',
  ]
    .filter(Boolean)
    .join(' ');

  let control = '';

  switch (type) {
    case 'textarea':
      control = html`<textarea class="textarea" rows="${rows}" placeholder="${placeholder}" ${raw(commonAttrs)}>${orDash(current) === '-' ? '' : current}</textarea>`;
      break;

    case 'select': {
      const opts = normalizeOptions(options);
      const optionHtml = opts
        .map(
          (opt) =>
            `<option value="${esc(opt.value)}" ${String(opt.value) === String(current) ? 'selected' : ''}>${esc(opt.label)}</option>`,
        )
        .join('');
      control = html`<select class="select" ${raw(commonAttrs)}>
        ${raw(current === '' || current === null || current === undefined ? '<option value="" selected disabled>请选择</option>' : '')}
        ${raw(optionHtml)}
      </select>`;
      break;
    }

    case 'number':
      control = html`<input class="input" type="number" value="${current}" placeholder="${placeholder}" ${raw(commonAttrs)} />`;
      break;

    case 'password':
      control = html`<input class="input" type="password" value="${current}" placeholder="${placeholder}" autocomplete="new-password" ${raw(commonAttrs)} />`;
      break;

    case 'checkbox': {
      const checked = Array.isArray(current) ? current : [];
      const opts = normalizeOptions(options);
      const items = opts
        .map(
          (opt) => html`<label class="check">
            <input type="checkbox" name="${key}" value="${opt.value}" ${raw(checked.map(String).includes(opt.value) ? 'checked' : '')} ${raw(disabledAttr)} />
            <span>${opt.label}</span>
          </label>`,
        )
        .join('');
      control = html`<div class="check-group" data-field="${key}">${raw(items)}</div>`;
      break;
    }

    case 'radio': {
      const opts = normalizeOptions(options);
      const items = opts
        .map(
          (opt) => html`<label class="check">
            <input type="radio" name="${key}" value="${opt.value}" ${raw(String(opt.value) === String(current) ? 'checked' : '')} ${raw(disabledAttr)} />
            <span>${opt.label}</span>
          </label>`,
        )
        .join('');
      control = html`<div class="check-group" data-field="${key}">${raw(items)}</div>`;
      break;
    }

    case 'switch':
      control = html`<label class="switch">
        <input type="checkbox" name="${key}" ${raw(current ? 'checked' : '')} ${raw(disabledAttr)} />
        <span class="switch-track"></span>
        <span class="switch-label">${field.switchLabel || ''}</span>
      </label>`;
      break;

    case 'static':
      return html`<div class="field ${spanCls}">
        <div class="field-label">${label}</div>
        <div class="text-body">${orDash(current)}</div>
        ${raw(hint ? html`<div class="field-hint">${hint}</div>` : '')}
      </div>`;

    case 'custom':
      return html`<div class="field ${spanCls}">
        ${raw(label ? html`<label class="${labelCls}">${label}</label>` : '')}
        ${raw(String(field.control || ''))}
        ${raw(hint ? html`<div class="field-hint">${hint}</div>` : '')}
      </div>`;

    default:
      control = html`<input class="input" type="${type}" value="${current}" placeholder="${placeholder}" ${raw(commonAttrs)} />`;
  }

  return html`<div class="field ${spanCls}" data-field-wrap="${key}">
    <label class="${labelCls}" for="field-${key}">${label}</label>
    ${raw(control)}
    ${raw(hint ? html`<div class="field-hint">${hint}</div>` : '')}
    <div class="field-error" data-error-for="${esc(key)}" role="alert"></div>
  </div>`;
}

/**
 * 渲染表单。
 * @param {object} o
 * @param {Array} o.fields
 * @param {object} [o.values] 初始值
 * @param {number} [o.columns=2]
 * @param {string} [o.id]
 */
export function form(o = {}) {
  const { fields = [], values = {}, columns = 2, id = 'appForm' } = o;

  const fieldHtml = fields
    .map((item) => field(item, values[item.key]))
    .join('');

  return html`<form
    id="${id}"
    class="form-grid"
    style="grid-template-columns:repeat(${columns},minmax(0,1fr))"
    novalidate
    autocomplete="off"
  >${raw(fieldHtml)}</form>`;
}

/* ------------------------------------------------------------
   二、取值
   ------------------------------------------------------------ */

/**
 * 从表单收集值。
 *
 * 规则：
   * 普通控件 → 字符串（空串转为 null）
   * number → 数值
   * checkbox 组 → 数组
   * switch → 布尔
 *
 * @param {HTMLFormElement|HTMLElement} root
 * @param {Array} fields 字段定义（用于类型转换）
 */
export function collect(root, fields = []) {
  const typeMap = new Map(fields.map((item) => [item.key, item.type || 'text']));
  const result = {};

  fields.forEach((item) => {
    const type = typeMap.get(item.key);
    const name = item.key;

    if (type === 'switch') {
      const input = root.querySelector(`input[name="${name}"]`);
      result[name] = Boolean(input?.checked);
      return;
    }

    if (type === 'checkbox') {
      const checked = Array.from(root.querySelectorAll(`input[name="${name}"]:checked`));
      result[name] = checked.map((el) => el.value);
      return;
    }

    if (type === 'radio') {
      const selected = root.querySelector(`input[name="${name}"]:checked`);
      result[name] = selected ? selected.value : null;
      return;
    }

    if (type === 'custom' || type === 'static') return;

    const el = root.querySelector(`[name="${name}"]`);
    if (!el) return;

    let value = el.value;

    if (type === 'number') {
      result[name] = value === '' ? null : Number(value);
      return;
    }

    value = typeof value === 'string' ? value.trim() : value;
    result[name] = value === '' ? null : value;
  });

  return result;
}

/* ------------------------------------------------------------
   三、校验
   ------------------------------------------------------------ */

/**
 * 校验表单值。
 * @param {object} values collect() 的结果
 * @param {Array} fields 字段定义
 * @returns {{ok:boolean, errors:Record<string,string>}}
 */
export function validate(values, fields = []) {
  const errors = {};

  fields.forEach((item) => {
    const { key, label = key, required, minLength, maxLength, min, max, pattern, message, type } = item;
    const value = values[key];

    const isEmptyValue =
      value === null ||
      value === undefined ||
      (typeof value === 'string' && value.trim() === '') ||
      (Array.isArray(value) && value.length === 0);

    if (required && isEmptyValue) {
      errors[key] = message || `请填写${label}`;
      return;
    }

    if (isEmptyValue) return; // 非必填且为空 → 跳过后续规则

    if (typeof value === 'string') {
      if (minLength && value.length < minLength) {
        errors[key] = `${label}不得少于 ${minLength} 个字符`;
        return;
      }
      if (maxLength && value.length > maxLength) {
        errors[key] = `${label}不得超过 ${maxLength} 个字符`;
        return;
      }
      if (pattern && !new RegExp(pattern).test(value)) {
        errors[key] = message || `${label}格式不正确`;
        return;
      }
    }

    if (type === 'number' && typeof value === 'number') {
      if (min !== undefined && value < min) {
        errors[key] = `${label}不得小于 ${min}`;
        return;
      }
      if (max !== undefined && value > max) {
        errors[key] = `${label}不得大于 ${max}`;
      }
    }
  });

  return { ok: Object.keys(errors).length === 0, errors };
}

/**
 * 把校验结果显示到表单上。
 * @param {HTMLElement} root
 * @param {Record<string,string>} errors
 * @returns {boolean} 是否全部通过
 */
export function showErrors(root, errors = {}) {
  // 先清除旧错误
  root.querySelectorAll('[data-error-for]').forEach((el) => {
    el.textContent = '';
  });
  root.querySelectorAll('[aria-invalid="true"]').forEach((el) => {
    el.removeAttribute('aria-invalid');
  });

  const keys = Object.keys(errors);
  if (!keys.length) return true;

  let firstInvalid = null;
  keys.forEach((key) => {
    const errorEl = root.querySelector(`[data-error-for="${key}"]`);
    if (errorEl) errorEl.textContent = errors[key];

    const input = root.querySelector(`[name="${key}"]`);
    if (input) {
      input.setAttribute('aria-invalid', 'true');
      if (!firstInvalid) firstInvalid = input;
    }
  });

  firstInvalid?.focus();
  return false;
}

/**
 * 一步完成「取值 → 校验 → 显示错误」。
 * @returns {{ok:boolean, values:object}}
 */
export function readAndValidate(root, fields = []) {
  const values = collect(root, fields);
  const { ok, errors } = validate(values, fields);
  showErrors(root, errors);
  return { ok, values };
}

/* ------------------------------------------------------------
   四、筛选器渲染（工具条用）
   ------------------------------------------------------------ */

/**
 * 渲染一个内联筛选控件。
 * @param {object} o
 * @param {string} o.key
 * @param {string} [o.type='text'] text|select|date
 * @param {string} [o.placeholder]
 * @param {Array} [o.options]
 * @param {string} [o.value]
 */
export function filterField(o = {}) {
  const { key, type = 'text', placeholder = '', options = [], value = '', width = '180px' } = o;
  const style = `width:${esc(width)}`;

  if (type === 'select') {
    const opts = normalizeOptions(options)
      .map(
        (opt) =>
          `<option value="${esc(opt.value)}" ${String(opt.value) === String(value) ? 'selected' : ''}>${esc(opt.label)}</option>`,
      )
      .join('');
    return html`<select class="select" data-filter="${esc(key)}" style="${raw(style)}" aria-label="${placeholder || key}">${raw(opts)}</select>`;
  }

  if (type === 'date') {
    return html`<input class="input" type="date" data-filter="${esc(key)}" value="${value}" style="${raw(style)}" aria-label="${placeholder || key}" />`;
  }

  return html`<input
    class="input"
    type="search"
    data-filter="${esc(key)}"
    value="${value}"
    placeholder="${placeholder}"
    style="${raw(style)}"
    aria-label="${placeholder || key}"
  />`;
}

/**
 * 渲染一整行筛选器。
 * @param {Array} fields
 * @param {object} [o] action='apply-filter' 搜索按钮的 data-action
 */
export function filterBar(fields = [], o = {}) {
  const { action = 'apply-filter', resetAction = 'reset-filter', showReset = true } = o;
  const controls = fields.map((item) => filterField(item)).join('');

  return html`<div class="flex items-center gap-2 flex-wrap">
    ${raw(controls)}
    <button type="button" class="btn btn-sm" data-action="${action}">查询</button>
    ${raw(showReset ? html`<button type="button" class="btn btn-sm btn-ghost" data-action="${resetAction}">重置</button>` : '')}
  </div>`;
}

/**
 * 从工具条读取筛选值。
 * @param {HTMLElement} root
 */
export function readFilters(root) {
  const result = {};
  root.querySelectorAll('[data-filter]').forEach((el) => {
    const key = el.dataset.filter;
    const value = typeof el.value === 'string' ? el.value.trim() : el.value;
    result[key] = value === '' ? null : value;
  });
  return result;
}

/** 重置工具条内的筛选控件 */
export function resetFilters(root) {
  root.querySelectorAll('[data-filter]').forEach((el) => {
    el.value = '';
  });
}

/* ------------------------------------------------------------
   五、表单弹窗
   ------------------------------------------------------------ */

/**
 * 打开表单弹窗：内部完成渲染、校验、提交与错误展示。
 *
 * @param {object} o
 * @param {string} o.title
 * @param {string} [o.subtitle]
 * @param {Array} o.fields
 * @param {object} [o.values]
 * @param {number} [o.columns=2]
 * @param {'sm'|'md'|'lg'|'xl'} [o.size='lg']
 * @param {string} [o.submitText='确定']
 * @param {Function} o.onSubmit (values) => Promise<any>
 * @returns {Promise<any|null>} 提交成功返回结果，取消返回 null
 */
export async function formModal(o = {}) {
  const {
    title,
    subtitle = '',
    fields = [],
    values = {},
    columns = 2,
    size = 'lg',
    submitText = '确定',
    cancelText = '取消',
    onSubmit,
    footerExtra = '',
  } = o;

  const { modal } = await import('./modal.js');
  const { toast } = await import('./toast.js');
  const formId = `form-${Date.now()}`;

  const dialog = modal({
    title,
    subtitle,
    size,
    body: (api) => {
      const wrapper = document.createElement('div');
      wrapper.innerHTML = form({ fields, values, columns, id: formId }).trim();
      wrapper.dataset.formWrapper = 'true';

      // 提交时回车触发
      wrapper.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && event.target.tagName !== 'TEXTAREA') {
          event.preventDefault();
          api.submit?.();
        }
      });

      return wrapper;
    },
    footer: (api) => {
      const cancel = document.createElement('button');
      cancel.type = 'button';
      cancel.className = 'btn';
      cancel.textContent = cancelText;
      cancel.addEventListener('click', () => api.close(null));

      const extra = document.createElement('div');
      if (footerExtra) extra.innerHTML = footerExtra;

      const submit = document.createElement('button');
      submit.type = 'button';
      submit.className = 'btn btn-primary';
      submit.textContent = submitText;
      submit.dataset.formSubmit = 'true';

      const run = async () => {
        const wrapper = dialog.el.querySelector('[data-form-wrapper]');
        const root = wrapper?.firstElementChild;
        if (!root) return;

        const { ok, values: collected } = readAndValidate(root, fields);
        if (!ok) {
          toast.warning('请检查表单填写');
          return;
        }

        submit.disabled = true;
        submit.classList.add('btn-loading');
        const originalText = submit.textContent;
        submit.textContent = '提交中…';

        try {
          const result = await onSubmit?.(collected);
          dialog.close(result ?? true);
        } catch (error) {
          submit.disabled = false;
          submit.textContent = originalText;

          const message = error?.message || '提交失败，请稍后重试';
          // 字段级错误（后端 VALIDATION_ERROR 会带 details）
          if (Array.isArray(error?.details)) {
            const fieldErrors = {};
            error.details.forEach((item) => {
              if (item.field) fieldErrors[item.field] = item.message;
            });
            showErrors(root, fieldErrors);
          }
          toast.error(message, { traceId: error?.traceId || '' });
        }
      };

      submit.addEventListener('click', run);
      api.submit = run;

      return [extra, cancel, submit];
    },
  });

  return await dialog.result;
}

export default {
  field,
  form,
  collect,
  validate,
  showErrors,
  readAndValidate,
  filterField,
  filterBar,
  readFilters,
  resetFilters,
  formModal,
};
