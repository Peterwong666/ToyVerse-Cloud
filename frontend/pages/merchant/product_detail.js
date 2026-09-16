/* ============================================================
   商户端 · 产品详情
   ------------------------------------------------------------
   路由：`#/products/:id`（参数化路由，刷新与分享都不丢状态）

   三个 Tab：
     概览         设备数 / 激活数 / 累计交互 + 产品基本信息
     AI 配置摘要  契约里的 `aiConfigSummary`（缺字段时引导去 AI 配置页）
     运营摘要     最近 7 天的 `metrics/overview` 摘要

   为什么「运营摘要」可以直接看数字
   --------------------------------
   本页在**商户端**，令牌带 merchant 权限，因此可以直接调
   `/merchant/metrics/**`；这与平台端「客户产品详情」的运营 Tab 不同——
   平台令牌调 `/merchant/**` 会 403，那边只能给产品维度的基础摘要。

   为什么不逐行回查列表缺的统计字段
   --------------------------------
   本页是单条记录，详情接口按契约返回 `deviceCount` 等统计字段，
   因此这里**不需要**再调别的接口补字段（列表页才有那个问题）。

   Tab 事件为什么绑在 `.tabs` 元素上而不是 container
   -------------------------------------------------
   container 由应用壳长期持有（切页只清空子节点、不换元素），
   把监听器绑在它上面会随每次进入本页叠加一层；而 `.tabs` 是
   createDetailPage 每次新建的元素，随页面一起被回收。
   ============================================================ */

import api from '/shared/core/api.js';
import { formatNumber, fromHtml, h, html } from '/shared/ui/dom.js';
import {
  alert,
  descList,
  emptyState,
  loadingState,
  statGrid,
} from '/shared/ui/components.js';
import { createDetailPage } from '/shared/app/page.js';
import { NETWORK_MAP, notifyError } from '/pages/platform/common.js';

const TABS = [
  { key: 'overview', label: '概览' },
  { key: 'ai', label: 'AI 配置摘要' },
  { key: 'ops', label: '运营摘要' },
];

/** AI 配置启用状态（取值同后端 EnableStatus） */
const AI_ENABLE_MAP = {
  ENABLED: { text: '已启用', tone: 'success' },
  DISABLED: { text: '未启用', tone: 'default' },
};

const DAY_MS = 24 * 60 * 60 * 1000;

/** 相对今天偏移 N 天的 YYYY-MM-DD（本地时区） */
function dayOffset(days) {
  return new Date(Date.now() + days * DAY_MS).toISOString().slice(0, 10);
}

/* ------------------------------------------------------------
   一、概览
   ------------------------------------------------------------ */

function renderOverview(host, data, ctx) {
  host.append(
    fromHtml(
      statGrid([
        {
          label: '设备总数',
          value: formatNumber(data.deviceCount),
          unit: '台',
          icon: 'device',
          tone: 'brand',
          foot: '归属本产品的设备',
        },
        {
          label: '已激活',
          value: formatNumber(data.activatedDeviceCount),
          unit: '台',
          icon: 'checkCircle',
          tone: 'teal',
          foot: '终端用户已完成激活',
        },
        {
          label: '累计交互',
          value: formatNumber(data.totalInteractions),
          unit: '次',
          icon: 'chat',
          tone: 'accent',
          foot: '设备与 AI 的对话次数',
        },
        {
          label: 'AI 配置',
          value: data.aiConfigSummary?.status === 'ENABLED' ? '已启用' : data.aiEnabled ? '已配置' : '未配置',
          icon: 'sparkles',
          tone: data.aiConfigSummary?.status === 'ENABLED' || data.aiEnabled ? 'success' : 'warning',
          foot: data.aiConfigSummary?.providerName || data.aiConfigSummary?.providerCode || '可在 AI 配置页设置',
        },
      ]),
    ),
  );

  const card = h('div', { class: 'card mt-4' });
  card.append(fromHtml(`<div class="card-head"><div class="card-title">产品信息</div></div>`));
  card.append(
    fromHtml(
      descList([
        ['产品编码', data.code],
        ['联网方式', NETWORK_MAP[data.networkType]?.text || data.networkType],
        ['来源模板', data.templateName || data.templateId],
        ['固件版本', data.firmwareVersion],
        ['状态', data.status === 'ENABLED' ? '启用' : data.status === 'DISABLED' ? '停用' : data.status],
      ]),
    ),
  );
  host.append(card);

  const bar = h('div', { class: 'btn-group mt-4' });
  const aiBtn = h('button', { class: 'btn btn-primary', type: 'button', text: '进入 AI 配置' });
  const metricsBtn = h('button', { class: 'btn', type: 'button', text: '进入运营看板' });
  aiBtn.addEventListener('click', () => ctx.router.go('/ai-config', { productId: data.id }));
  metricsBtn.addEventListener('click', () => ctx.router.go('/metrics', { productId: data.id }));
  bar.append(aiBtn, metricsBtn);
  host.append(bar);
}

/* ------------------------------------------------------------
   二、AI 配置摘要
   ------------------------------------------------------------ */

function renderAiSummary(host, data, ctx) {
  const summary = data.aiConfigSummary;

  if (!summary) {
    host.append(
      fromHtml(
        emptyState({
          icon: 'sparkles',
          title: '暂未返回 AI 配置摘要',
          desc: '产品详情接口没有返回 aiConfigSummary 字段。请到「AI 配置」页查看并设置该产品的供应商、提示词、角色与内容安全。',
        }),
      ),
    );
    const bar = h('div', { class: 'mt-3' });
    const go = h('button', { class: 'btn btn-primary', type: 'button', text: '去 AI 配置' });
    go.addEventListener('click', () => ctx.router.go('/ai-config', { productId: data.id }));
    bar.append(go);
    host.append(bar);
    return;
  }

  host.append(
    fromHtml(
      descList([
        ['供应商', summary.providerName || summary.providerCode],
        ['供应商密钥', summary.providerConfigured === false ? '未配置（对话会安全失败）' : '已配置'],
        ['角色预设', summary.rolePresetCode],
        ['知识库', summary.knowledgeBaseName],
        ['内容安全', summary.safetyEnabled === false ? '已关闭' : '已开启'],
        ['配置状态', AI_ENABLE_MAP[summary.status]?.text || summary.status],
      ]),
    ),
  );

  if (summary.providerConfigured === false) {
    host.append(
      fromHtml(
        alert({
          tone: 'danger',
          title: '供应商未配置密钥',
          text: '选定的供应商在平台上还没有录入密钥，对话会按「安全失败」处理（返回明确错误，不伪造回复）。请先到平台端完成配置。',
        }),
      ),
    );
  }

  const bar = h('div', { class: 'mt-4' });
  const go = h('button', { class: 'btn btn-primary', type: 'button', text: '编辑 AI 配置' });
  go.addEventListener('click', () => ctx.router.go('/ai-config', { productId: data.id }));
  bar.append(go);
  host.append(bar);
}

/* ------------------------------------------------------------
   三、运营摘要
   ------------------------------------------------------------ */

async function renderOpsSummary(host, data, ctx) {
  host.append(fromHtml(loadingState('正在读取运营指标…')));

  const to = dayOffset(0);
  const from = dayOffset(-6);

  let overview = null;
  try {
    overview = await api.get('/merchant/metrics/overview', {
      params: { productId: data.id, from, to },
    });
  } catch (error) {
    notifyError(error, '运营指标读取失败');
  }
  host.replaceChildren();

  if (!overview) {
    host.append(
      fromHtml(
        emptyState({
          icon: 'alertTriangle',
          title: '运营指标暂时不可用',
          desc: '接口未返回数据（可能尚未生成快照或该产品还没有对话数据）。可到运营看板点「刷新快照」后重试。',
        }),
      ),
    );
    return;
  }

  host.append(
    fromHtml(
      html`<div class="alert alert-neutral">
        <div class="alert-body">
          统计区间 ${overview.range?.from || from} ~ ${overview.range?.to || to}，
          数据来源：${overview.source === 'live' ? '实时聚合' : overview.source || '未知'}。
          完整趋势 / 留存 / 地域分布请进入运营看板。
        </div>
      </div>`,
    ),
  );

  host.append(
    fromHtml(
      statGrid([
        {
          label: '活跃设备',
          value: formatNumber(overview.activeDevices),
          unit: '台',
          icon: 'activity',
          tone: 'brand',
          foot: `设备总数 ${formatNumber(overview.totalDevices)} 台`,
        },
        {
          label: '总交互',
          value: formatNumber(overview.totalInteractions),
          unit: '次',
          icon: 'chat',
          tone: 'teal',
          foot: `助手消息 ${formatNumber(overview.assistantMessages)} 条`,
        },
        {
          label: '会话数',
          value: formatNumber(overview.sessionCount),
          unit: '次',
          icon: 'clock',
          tone: 'accent',
          foot: `独立终端用户 ${formatNumber(overview.uniqueEndUsers)}`,
        },
        {
          label: '平均端到端延迟',
          value: formatNumber(overview.avgLatencyMs),
          unit: 'ms',
          icon: 'signal',
          tone: 'coral',
          foot: '首字到播报完成的平均耗时',
        },
        {
          label: '安全拦截',
          value: formatNumber(overview.safetyBlocked),
          unit: '次',
          icon: 'shield',
          tone: 'warning',
          foot: '命中内容安全的回复次数',
        },
        {
          label: '内容命中',
          value: formatNumber(overview.contentHits),
          unit: '次',
          icon: 'book',
          tone: 'info',
          foot: '故事 / 儿歌被点播的次数',
        },
      ]),
    ),
  );

  const bar = h('div', { class: 'mt-4' });
  const go = h('button', { class: 'btn btn-primary', type: 'button', text: '进入运营看板' });
  go.addEventListener('click', () => ctx.router.go('/metrics', { productId: data.id }));
  bar.append(go);
  host.append(bar);
}

/* ------------------------------------------------------------
   页面入口
   ------------------------------------------------------------ */

/**
 * 渲染商户端产品详情。
 *
 * @param {HTMLElement} container
 * @param {{router: object, params: {id: string}}} ctx
 */
export async function renderMerchantProductDetail(container, ctx) {
  const { router, params } = ctx;
  const productId = params?.id;
  if (!productId) {
    router.go('/products');
    return;
  }

  let data;
  try {
    data = await api.get(`/merchant/products/${productId}`);
  } catch (error) {
    notifyError(error, '产品不存在或已被删除');
    router.go('/products');
    return;
  }

  const detail = createDetailPage(container, {
    title: data.name || '产品详情',
    desc: `${data.code || productId}　·　${NETWORK_MAP[data.networkType]?.text || data.networkType || ''}　·　设备 ${formatNumber(data.deviceCount)} 台`,
    backPath: '/products',
    router,
    actions: [
      { label: '刷新', icon: 'refresh', variant: 'ghost', action: 'reload-detail' },
      { label: 'AI 配置', icon: 'sparkles', action: 'goto-ai-config' },
      { label: '运营看板', icon: 'chartLine', action: 'goto-metrics' },
    ],
    tabs: TABS,
    activeTab: 'overview',
  });

  const reload = async () => {
    data = await api.get(`/merchant/products/${productId}`);
    renderTab(detail.activeTab);
  };

  const renderTab = (key) => {
    const host = h('div');
    detail.body.replaceChildren(host);
    // 每个 Tab 自己负责异步取数；失败时只影响本 Tab，不拖垮整页
    const task =
      key === 'ai'
        ? Promise.resolve(renderAiSummary(host, data, { router }))
        : key === 'ops'
          ? renderOpsSummary(host, data, { router })
          : Promise.resolve(renderOverview(host, data, { router }));
    task.catch((error) => notifyError(error));
  };

  const tabsEl = container.querySelector('.tabs');
  tabsEl?.addEventListener('click', (event) => {
    const tab = event.target.closest('[data-tab]');
    if (tab) renderTab(tab.dataset.tab);
  });

  renderTab('overview');

  detail.head.addEventListener('click', async (event) => {
    const target = event.target.closest('[data-action]');
    if (!target) return;

    if (target.dataset.action === 'reload-detail') {
      await reload();
      return;
    }
    if (target.dataset.action === 'goto-ai-config') {
      router.go('/ai-config', { productId });
      return;
    }
    if (target.dataset.action === 'goto-metrics') {
      router.go('/metrics', { productId });
    }
  });
}

export default { renderMerchantProductDetail };
