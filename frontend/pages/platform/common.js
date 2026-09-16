/* ============================================================
   平台端页面共用工具
   ------------------------------------------------------------
   六个目录域页面（租户 / 租户详情 / 模板 / 云服务商 / 客户产品 /
   客户产品详情）共用的小工具，收敛三件容易写错的重复劳动：

     1. 下拉选项加载（要静默容错：选项接口挂了不该让整页空白）
     2. **一次性密码展示**（服务端只在响应里返回一次，必须给用户
        明确的复制入口，否则这个功能等于白做）
     3. 把后端错误码翻成用户能理解的动作建议
        （尤其 CASCADE_CONFLICT —— 只显示「冲突」没有意义，
         必须告诉用户「还被什么挡着」）

   注意：本模块不含任何密钥处理 —— 后端从不下发明文密钥，
   前端只需展示 `*Hint` 掩码，见 renderSecretHint()。
   ============================================================ */

import api from '/shared/core/api.js';
import { clear, esc, formatDate, fromHtml, h, html, raw } from '/shared/ui/dom.js';
import { button, descList, tag } from '/shared/ui/components.js';
import { copyText } from '/shared/ui/dom.js';
import toast from '/shared/ui/toast.js';
import { modal } from '/shared/ui/modal.js';

/* ------------------------------------------------------------
   一、取值与选项
   ------------------------------------------------------------ */

/** 状态映射：租户 / 账号 */
export const TENANT_STATUS_MAP = {
  ACTIVE: { text: '启用', tone: 'success' },
  DISABLED: { text: '已禁用', tone: 'danger' },
};

/** 状态映射：启用 / 停用（模板、客户产品、授权） */
export const ENABLE_STATUS_MAP = {
  ENABLED: { text: '启用', tone: 'success' },
  DISABLED: { text: '已停用', tone: 'default' },
};

/** 联网方式映射 */
export const NETWORK_MAP = {
  '4G': { text: '4G（集贤方案）', tone: 'info' },
  WIFI: { text: 'Wi-Fi（JoyInside / 火山方案）', tone: 'teal' },
};

/** 云服务商接入状态映射 */
export const CLOUD_STATUS_MAP = {
  CONNECTED: { text: '已接入', tone: 'success' },
  NOT_CONNECTED: { text: '未接入', tone: 'warning' },
};

/** 云服务商厂商映射（与后端 app/models/enums.py 的 CLOUD_VENDOR_LABELS 对齐） */
export const VENDOR_LABELS = {
  JIXIAN: '集贤（4G 设备云）',
  JOYINSIDE: '京东云 JoyInside（Wi-Fi）',
  VOLCANO: '火山引擎智能云（硬件对话智能体）',
  OTHER: '其它厂商',
};

/** 厂商下拉选项 */
export const VENDOR_OPTIONS = Object.entries(VENDOR_LABELS).map(([value, label]) => ({
  value,
  label,
}));

/** 联网方式下拉选项 */
export const NETWORK_OPTIONS = [
  { value: 'WIFI', label: 'Wi-Fi' },
  { value: '4G', label: '4G' },
];

/** 启用状态下拉选项 */
export const ENABLE_OPTIONS = [
  { value: 'ENABLED', label: '启用' },
  { value: 'DISABLED', label: '停用' },
];

/**
 * 静默取一个列表接口的 records（选项、下拉数据用）。
 *
 * 为什么静默：选项类请求失败不应该把整页变成报错页；
 * 失败时返回空数组，页面用「暂无选项」降级即可。
 *
 * @param {string} path
 * @param {object} [params]
 * @returns {Promise<Array>}
 */
export async function fetchRecords(path, params = {}) {
  try {
    const result = await api.get(path, { params: { pageSize: 200, ...params }, silent: true });
    return Array.isArray(result?.records) ? result.records : [];
  } catch {
    return [];
  }
}

/** 把 records 映射为下拉选项 */
export function toOptions(records, labelOf) {
  return records.map((item) => ({ value: String(item.id), label: labelOf(item) }));
}

/* ------------------------------------------------------------
   二、密钥展示（只展示掩码）
   ------------------------------------------------------------ */

/**
 * 渲染密钥提示单元格。
 *
 * 后端只回 `*Hint`（前 4 位 + ****），**永远不会**返回明文或密文；
 * 未配置时明确显示「未配置」，避免用户误以为已接入。
 *
 * @param {string|null|undefined} hint
 */
export function renderSecretHint(hint) {
  if (!hint) return tag('未配置', 'default');
  return html`<span class="mono text-xs">${hint}</span>`;
}

/* ------------------------------------------------------------
   三、一次性密码展示
   ------------------------------------------------------------ */

/**
 * 展示服务端生成的一次性密码。
 *
 * 文案刻意强调「只显示一次」——这是安全设计（服务端不存明文，
 * 账号被置为下次登录必须改密），用户若关掉弹窗就再也拿不到了。
 *
 * @param {object} o
 * @param {string} o.account 账号
 * @param {string} o.password 一次性密码
 * @param {string} [o.title]
 */
export function showOneTimePassword({ account, password, title = '初始密码（仅显示一次）' }) {
  const body = h('div');
  const rows = [
    ['登录账号', account],
    ['初始密码', password],
  ];
  body.append(fromHtml(descList(rows, { cols: 1 })));
  body.append(
    fromHtml(
      html`<div class="field-hint mt-2">
        该密码只会显示这一次，关闭后无法再次查看。请立即复制并转交客户；
        客户首次登录时系统会强制其修改密码。
      </div>`,
    ),
  );

  const dialog = modal({
    title,
    size: 'sm',
    body,
    footer: (dlg) => {
      const copy = h('button', { class: 'btn btn-primary', type: 'button', text: '复制密码' });
      copy.addEventListener('click', async () => {
        const ok = await copyText(password);
        if (ok) toast.success('密码已复制到剪贴板');
        else toast.warning('复制失败，请手动选中后复制');
      });
      const close = h('button', { class: 'btn', type: 'button', text: '我已保存，关闭' });
      close.addEventListener('click', () => dlg.close());
      return [close, copy];
    },
  });

  return dialog.result;
}

/* ------------------------------------------------------------
   四、错误提示：把错误码翻成「下一步该做什么」
   ------------------------------------------------------------ */

/** 错误码 → 处理建议（补充后端 message 之外的动作指引） */
const ERROR_HINTS = {
  CASCADE_CONFLICT: '该数据仍被其它数据引用。请先按提示解除关联，或改为「停用」而不是删除。',
  PRODUCT_NOT_AUTHORIZED: '请先在「产品模板」页把该模板授权给这个租户，再创建客户产品。',
  PRODUCT_CODE_EXISTS: '产品编码需要全局唯一，请更换一个。',
  CLOUD_CODE_EXISTS: '云服务商编码需要全局唯一，请更换一个。',
  TEMPLATE_CODE_EXISTS: '产品模板编码需要全局唯一，请更换一个。',
  TENANT_CODE_EXISTS: '租户编码需要全局唯一，请更换一个。',
  PERMISSION_DENIED: '当前账号没有该操作的权限（平台运营只能查看配置类数据）。',
  RESOURCE_NOT_FOUND: '目标数据不存在，可能已被其他管理员删除。请刷新后重试。',
};

/**
 * 统一处理接口异常。
 *
 * 设计取舍：**只弹 toast，不改页面状态**。
 * 列表页/详情页的重载由调用方决定（有些错误不该重载，比如表单校验失败）。
 *
 * @param {Error & {code?:string, details?:object, traceId?:string}} error
 * @param {string} [fallback] 兜底文案
 */
export function notifyError(error, fallback = '操作失败，请稍后重试') {
  const code = error?.code || '';
  const hint = ERROR_HINTS[code] || '';
  const message = error?.message || fallback;
  toast.error(hint ? `${message}　${hint}` : message, { traceId: error?.traceId || '' });
}

/* ------------------------------------------------------------
   五、其它小工具
   ------------------------------------------------------------ */

/** 时间列渲染（空值显示 '-'） */
export function timeCell(value) {
  return value ? esc(formatDate(value)) : '-';
}

/** 布尔列渲染 */
export function boolTag(value, trueText = '是', falseText = '否') {
  return value ? tag(trueText, 'success') : tag(falseText, 'default');
}

/** 行内操作按钮组 */
export function rowActions(items) {
  return html`<div class="btn-group">${raw(items.filter(Boolean).map((item) => button(item)).join(''))}</div>`;
}

/**
 * 确认框 + 执行动作的通用封装。
 * @param {object} o
 * @param {string} o.title
 * @param {string} [o.description]
 * @param {string} [o.detail]
 * @param {'danger'|'warning'} [o.tone]
 * @param {string} [o.confirmText]
 * @param {() => Promise<any>} o.run 确认后执行的动作
 * @param {string} [o.successMessage]
 * @param {() => void} [o.onDone]
 */
export async function confirmThenRun(o) {
  const { confirmDialog } = await import('/shared/ui/modal.js');
  const { confirmed } = await confirmDialog({
    title: o.title,
    description: o.description || '',
    detail: o.detail || '',
    tone: o.tone || 'warning',
    confirmText: o.confirmText || '确定',
  });
  if (!confirmed) return false;

  try {
    await o.run();
    if (o.successMessage) toast.success(o.successMessage);
    o.onDone?.();
    return true;
  } catch (error) {
    notifyError(error);
    return false;
  }
}

/** 清空容器并写入单个 HTML 片段（自动用 fromHtml，避免把 HTML 当文本插入） */
export function setHtml(container, markup) {
  clear(container);
  const node = fromHtml(markup);
  if (node) container.append(node);
}

export default {
  TENANT_STATUS_MAP,
  ENABLE_STATUS_MAP,
  NETWORK_MAP,
  CLOUD_STATUS_MAP,
  VENDOR_LABELS,
  VENDOR_OPTIONS,
  NETWORK_OPTIONS,
  ENABLE_OPTIONS,
  fetchRecords,
  toOptions,
  renderSecretHint,
  showOneTimePassword,
  notifyError,
  timeCell,
  boolTag,
  rowActions,
  confirmThenRun,
  setHtml,
};
