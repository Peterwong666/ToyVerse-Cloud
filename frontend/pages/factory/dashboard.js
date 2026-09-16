/* ============================================================
   工厂端 · 工作台
   ============================================================ */

import { alert, card } from '/shared/ui/components.js';
import { countSafe, renderDashboard as render } from '/shared/app/dashboard.js';

/** 工厂端视角的五步流程 */
const FACTORY_STEPS = [
  { title: '接收订单' },
  { title: '烧录固件' },
  { title: '贴二维码' },
  { title: '扫码抽检' },
  { title: '出货登记' },
];

export async function renderDashboard(container) {
  return render(container, {
    desc: '工厂端 · 跨租户承接生产任务，客户信息已脱敏',
    scopeLabel: '跨租户（字段脱敏）',

    stats: [
      {
        label: '待生产订单',
        unit: '单',
        icon: 'clipboard',
        tone: 'accent',
        foot: '等待烧录',
        load: () => countSafe('/factory/orders', { status: 'PENDING' }),
      },
      {
        label: '生产中订单',
        unit: '单',
        icon: 'fire',
        tone: 'brand',
        foot: '烧录进行中',
        load: () => countSafe('/factory/orders', { status: 'PRODUCING' }),
      },
      {
        label: '已完成订单',
        unit: '单',
        icon: 'checkCircle',
        tone: 'teal',
        foot: '烧录完成',
        load: () => countSafe('/factory/orders', { status: 'COMPLETED' }),
      },
      {
        label: '固件版本',
        unit: '个',
        icon: 'disk',
        tone: 'coral',
        foot: '可用于烧录',
        load: () => countSafe('/factory/firmwares'),
      },
    ],

    flowSteps: FACTORY_STEPS,
    flowTitle: '生产流程',
    flowSubtitle: '从接单到出货',
    flowNote:
      '工厂端只看到生产必需的信息：产品型号、固件版本、数量与二维码清单。客户名称、金额与联系方式**在服务端脱敏**，工厂端拿不到真实信息。',

    extraCards: [
      card({
        title: '字段脱敏规则',
        body: `
          <div class="mb-3">
            ${alert({
              tone: 'warning',
              text: '以下字段在服务端即被处理，工厂端接口**不会返回**真实值。',
            })}
          </div>
          <div class="kv-list">
            <div class="kv-row"><span class="kv-key">客户名称</span><span class="kv-value">首字符 + 星号 + 尾字符（如 <span class="mono">中**动</span>）</span></div>
            <div class="kv-row"><span class="kv-key">订单金额</span><span class="kv-value text-disabled">不下发</span></div>
            <div class="kv-row"><span class="kv-key">联系方式</span><span class="kv-value text-disabled">不下发</span></div>
            <div class="kv-row"><span class="kv-key">邮箱</span><span class="kv-value text-disabled">不下发</span></div>
          </div>
        `,
      }),
    ],

    audit: {
      path: '/factory/audits',
      params: { pageSize: 8, sortBy: 'createdAt', order: 'desc' },
    },

    fallbacks: [
      '当前处于 P2 前端地基阶段：工厂端接口将在 P6 交付。统计项显示「—」表示接口尚未就绪。',
    ],
  });
}

export default { renderDashboard };
