/* ============================================================
   平台端 · 客户产品详情（含小程序配置）
   ------------------------------------------------------------
   路由：`#/client-products/:id`（参数化路由，刷新不丢状态）

   两块内容：
     基本信息     产品归属 / 来源模板 / 快照字段 / 编辑
     小程序配置   1:1 的小程序配置（upsert），AppSecret 只回掩码

   为什么把小程序配置做成**页内表单**而不是弹窗
   ------------------------------------------
   小程序配置有 12 个字段，属于「经常需要来回比对、改几个再保存」的场景；
   弹窗会遮挡产品信息（尤其是 AppID 与 AppSecret 的对应关系），
   页内表单能同时看到「快照字段」与「可编辑字段」。
   ============================================================ */

import api from '/shared/core/api.js';
import { PERM } from '/shared/core/auth.js';
import { formatDay, formatRelative, fromHtml, h } from '/shared/ui/dom.js';
import { descList, emptyState, loadingState, statGrid } from '/shared/ui/components.js';
import { createDetailPage } from '/shared/app/page.js';
import { form, readAndValidate } from '/shared/ui/form.js';
import { confirmDialog } from '/shared/ui/modal.js';
import toast from '/shared/ui/toast.js';
import { ENABLE_OPTIONS, notifyError } from './common.js';

/** 读取客户产品详情 */
function loadProduct(id) {
  return api.get(`/platform/client-products/${id}`);
}

/** 读取小程序配置（未配置时后端返回 null，不是错误） */
async function loadMiniapp(id) {
  return api.get(`/platform/client-products/${id}/miniapp-config`, { silent: true });
}

/** 小程序配置表单字段 */
const MINIAPP_FIELDS = [
  { key: 'app_name', label: '小程序名称', required: true, maxLength: 128 },
  { key: 'app_id', label: 'AppID', maxLength: 64, placeholder: '微信小程序 AppID' },
  {
    key: 'app_secret',
    label: 'AppSecret',
    maxLength: 512,
    placeholder: '留空表示保持原值',
    hint: '明文录入、加密存储；任何接口都只返回前 4 位掩码',
  },
  { key: 'original_id', label: '原始 ID', maxLength: 64, placeholder: 'gh_ 开头' },
  { key: 'theme_color', label: '主题色', maxLength: 16, placeholder: '如 #4F46E5' },
  { key: 'logo_url', label: 'Logo 地址', maxLength: 512 },
  { key: 'share_title', label: '分享标题', maxLength: 128 },
  { key: 'share_desc', label: '分享描述', maxLength: 256 },
  { key: 'service_phone', label: '客服电话', maxLength: 32 },
  { key: 'service_qr_url', label: '客服二维码地址', maxLength: 512 },
  { key: 'version', label: '版本号', maxLength: 32, placeholder: '如 1.0.0' },
  { key: 'status', label: '发布状态', type: 'select', options: ENABLE_OPTIONS, value: 'DISABLED' },
  { key: 'remark', label: '备注', type: 'textarea', maxLength: 2000, span: 2 },
];

/* ------------------------------------------------------------
   基本信息
   ------------------------------------------------------------ */

function renderBasic(host, data, ctx) {
  const { reload } = ctx;

  host.append(
    fromHtml(
      statGrid([
        {
          label: '归属租户',
          value: data.tenantName || '-',
          icon: 'building',
          tone: 'brand',
          foot: data.tenantCode || data.tenantId,
        },
        {
          label: '来源模板',
          value: data.templateName || '-',
          icon: 'box',
          tone: 'teal',
          foot: data.templateCode || data.templateId,
        },
        {
          label: '联网方式',
          value: data.networkType === '4G' ? '4G' : 'Wi-Fi',
          icon: 'wifi',
          tone: 'accent',
          foot: '决定激活路径与二维码格式',
        },
        {
          label: '小程序',
          value: data.hasMiniappConfig ? '已配置' : '未配置',
          icon: 'robot',
          tone: data.hasMiniappConfig ? 'brand' : 'coral',
          foot: data.miniappConfig?.appName || '可在「小程序配置」标签页设置',
        },
      ]),
    ),
  );

  const card = h('div', { class: 'card mt-4' });
  card.append(fromHtml(`<div class="card-head"><div class="card-title">产品信息</div></div>`));
  card.append(
    fromHtml(
      descList([
        ['产品名称', data.name],
        ['产品编码', data.code],
        ['状态', data.status === 'ENABLED' ? '启用' : '停用'],
        ['固件版本', data.firmwareVersion],
        ['云服务商', data.cloudProviderName],
        ['AI 已配置', data.aiEnabled ? '是' : '否'],
        ['创建时间', formatDay(data.createdAt)],
        ['最后更新', formatRelative(data.updatedAt)],
        ['备注', data.remark],
      ]),
    ),
  );
  host.append(card);

  /* ---- 快照说明（避免误以为「改了模板产品没变」是 bug） ---- */
  const note = h('div', { class: 'alert alert-neutral mt-4' });
  note.append(
    h('div', {
      class: 'alert-body',
      text:
        '联网方式、云服务商、固件版本是创建时从模板「快照」的副本。' +
        '之后修改模板不会追溯影响本产品 —— 这是有意设计：历史数据必须可解释，' +
        '不能让一个模板改动把已交付产品的配置一起改掉。',
    }),
  );
  host.append(note);

  /* ---- 编辑按钮 ---- */
  const bar = h('div', { class: 'mt-4' });
  const editBtn = h('button', {
    class: 'btn btn-primary',
    type: 'button',
    text: '编辑产品信息',
    'data-action': 'edit-product',
  });
  bar.append(editBtn);
  host.append(bar);

  editBtn.addEventListener('click', async () => {
    const { formModal } = await import('/shared/ui/form.js');
    const updated = await formModal({
      title: `编辑客户产品「${data.name}」`,
      subtitle: '归属租户与来源模板不可修改（它们决定产品归属与授权依据）',
      size: 'md',
      submitText: '保存',
      fields: [
        { key: 'name', label: '产品名称', required: true, maxLength: 128 },
        { key: 'firmware_version', label: '固件版本', maxLength: 64 },
        { key: 'status', label: '状态', type: 'select', options: ENABLE_OPTIONS },
        {
          key: 'ai_enabled',
          label: 'AI 已配置',
          type: 'switch',
          switchLabel: '该产品是否已完成 AI 配置',
          span: 2,
        },
        { key: 'remark', label: '备注', type: 'textarea', maxLength: 2000, span: 2 },
      ],
      values: {
        name: data.name,
        firmware_version: data.firmwareVersion,
        status: data.status,
        ai_enabled: data.aiEnabled,
        remark: data.remark,
      },
      onSubmit: (values) =>
        api.put(`/platform/client-products/${data.id}`, values, {
          idempotencyKey: api.newIdempotencyKey(),
        }),
    });
    if (updated) {
      toast.success('产品信息已更新');
      reload();
    }
  });
}

/* ------------------------------------------------------------
   小程序配置
   ------------------------------------------------------------ */

async function renderMiniapp(host, data, ctx) {
  const { reload } = ctx;

  host.append(fromHtml(loadingState('正在读取小程序配置…')));
  let config = null;
  try {
    config = await loadMiniapp(data.id);
  } catch (error) {
    notifyError(error);
  }
  host.replaceChildren();

  /* ---- 当前状态摘要 ---- */
  const statusCard = h('div', { class: 'card mb-4' });
  statusCard.append(fromHtml(`<div class="card-head"><div class="card-title">当前状态</div></div>`));
  const rows = config
    ? [
        ['小程序名称', config.appName],
        ['AppID', config.appId],
        ['AppSecret', config.appSecretHint || '未配置'],
        ['发布状态', config.status === 'ENABLED' ? '已启用' : '未启用'],
        ['版本号', config.version],
        ['最后更新', formatRelative(config.updatedAt)],
      ]
    : [];
  statusCard.append(
    fromHtml(
      config
        ? descList(rows)
        : emptyState({
            icon: 'robot',
            title: '尚未配置小程序',
            desc: '在下方表单填写并保存即可创建；AppSecret 为明文录入、加密存储，保存后只显示前 4 位掩码。',
          }),
    ),
  );
  host.append(statusCard);

  /* ---- 编辑表单（upsert：新建与更新用同一个 PUT） ---- */
  const formCard = h('div', { class: 'card' });
  formCard.append(
    fromHtml(
      `<div class="card-head"><div class="card-title">${config ? '编辑配置' : '新建配置'}</div></div>`,
    ),
  );
  const formHost = h('div', { class: 'p-4' });
  formHost.append(
    fromHtml(
      form({
        fields: MINIAPP_FIELDS,
        id: 'miniappForm',
        values: config
          ? {
              app_name: config.appName,
              app_id: config.appId,
              // AppSecret 刻意不回填：后端不回明文，前端也无从回填
              app_secret: '',
              original_id: config.originalId,
              theme_color: config.themeColor,
              logo_url: config.logoUrl,
              share_title: config.shareTitle,
              share_desc: config.shareDesc,
              service_phone: config.servicePhone,
              service_qr_url: config.serviceQrUrl,
              version: config.version,
              status: config.status,
              remark: config.remark,
            }
          : { app_name: data.name, status: 'DISABLED', theme_color: '#4F46E5' },
      }),
    ),
  );

  const actions = h('div', { class: 'btn-group mt-4' });
  const saveBtn = h('button', {
    class: 'btn btn-primary',
    type: 'button',
    text: config ? '保存配置' : '创建配置',
  });
  const resetBtn = h('button', { class: 'btn', type: 'button', text: '重置' });
  actions.append(saveBtn, resetBtn);
  formHost.append(actions);
  formCard.append(formHost);
  host.append(formCard);

  const root = formHost.querySelector('#miniappForm');

  resetBtn.addEventListener('click', () => renderMiniapp(host, data, ctx));

  saveBtn.addEventListener('click', async () => {
    const { ok, values } = readAndValidate(root, MINIAPP_FIELDS);
    if (!ok) {
      toast.warning('请检查表单填写');
      return;
    }

    saveBtn.disabled = true;
    saveBtn.textContent = '保存中…';
    try {
      const saved = await api.put(`/platform/client-products/${data.id}/miniapp-config`, values, {
        idempotencyKey: api.newIdempotencyKey(),
      });
      toast.success(
        values.app_secret
          ? '小程序配置已保存，AppSecret 已加密存储'
          : `小程序配置已保存${saved.appSecretHint ? '' : '（AppSecret 仍为空）'}`,
      );
      await reload();
    } catch (error) {
      notifyError(error);
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = config ? '保存配置' : '创建配置';
    }
  });
}

/* ------------------------------------------------------------
   页面入口
   ------------------------------------------------------------ */

const TABS = [
  { key: 'basic', label: '基本信息' },
  { key: 'miniapp', label: '小程序配置' },
];

/**
 * 渲染客户产品详情。
 *
 * @param {HTMLElement} container
 * @param {{router: object, params: {id: string}}} ctx
 */
export async function renderClientProductDetail(container, ctx) {
  const { router, params } = ctx;
  const productId = params?.id;
  if (!productId) {
    router.go('/client-products');
    return;
  }

  let data;
  try {
    data = await loadProduct(productId);
  } catch (error) {
    notifyError(error, '客户产品不存在或已被删除');
    router.go('/client-products');
    return;
  }

  const detail = createDetailPage(container, {
    title: data.name,
    desc: `${data.code}　·　归属 ${data.tenantName || data.tenantId}　·　来源模板 ${data.templateName || data.templateId}`,
    backPath: '/client-products',
    router,
    actions: [
      { label: '刷新', icon: 'refresh', variant: 'ghost', action: 'reload-detail' },
      {
        label: '删除产品',
        variant: 'ghost',
        action: 'delete-product',
        perm: PERM.platform.productWrite,
      },
    ],
    tabs: TABS,
    activeTab: 'basic',
  });

  const reload = async () => {
    data = await loadProduct(productId);
    renderTab(detail.activeTab);
  };

  const renderTab = (key) => {
    const host = h('div');
    detail.body.replaceChildren(host);
    if (key === 'miniapp') {
      renderMiniapp(host, data, { reload }).catch((error) => notifyError(error));
    } else {
      renderBasic(host, data, { router, reload });
    }
  };

  renderTab('basic');
  container.addEventListener('tabchange', (event) => renderTab(event.detail.tab));

  /* 页头按钮绑在本次渲染新建的 head 上（见 page.js 的说明） */
  detail.head.addEventListener('click', async (event) => {
    const target = event.target.closest('[data-action]');
    if (!target) return;

    if (target.dataset.action === 'reload-detail') {
      await reload();
      toast.info('已刷新');
      return;
    }

    if (target.dataset.action === 'delete-product') {
      const { confirmed } = await confirmDialog({
        title: `删除客户产品「${data.name}」`,
        description: '产品的小程序配置会一并删除。',
        detail: 'P4 设备表落地后，此处会追加「产品下已有设备则拒绝删除」的校验。',
        tone: 'danger',
        confirmText: '确认删除',
      });
      if (!confirmed) return;
      try {
        await api.del(`/platform/client-products/${productId}`, undefined, {
          idempotencyKey: api.newIdempotencyKey(),
        });
        toast.success('客户产品已删除');
        router.go('/client-products');
      } catch (error) {
        notifyError(error);
      }
    }
  });
}

export default { renderClientProductDetail };
