/* ============================================================
   平台端 · 工作台
   ============================================================ */

import { countSafe, renderDashboard as render } from '/shared/app/dashboard.js';

/** 业务主干七步（与 docs/02-系统流程图.md 对应） */
const BUSINESS_STEPS = [
  { title: '开通租户' },
  { title: '配置产品' },
  { title: '客户下单' },
  { title: '平台审核' },
  { title: '生成设备' },
  { title: '工厂烧录' },
  { title: '终端激活' },
];

export async function renderDashboard(container) {
  return render(container, {
    desc: '平台端 · 全局视角：管理租户、产品、订单、设备与运营',
    scopeLabel: '全部租户',

    /* ---- 统计项：每项独立容错，接口未就绪时显示「—」 ---- */
    stats: [
      {
        label: '租户总数',
        unit: '个',
        icon: 'building',
        tone: 'brand',
        foot: '含演示租户',
        load: () => countSafe('/platform/tenants'),
      },
      {
        label: '产品模板',
        unit: '个',
        icon: 'box',
        tone: 'teal',
        foot: '平台级模板',
        load: () => countSafe('/platform/templates'),
      },
      {
        label: '订单总数',
        unit: '单',
        icon: 'clipboard',
        tone: 'accent',
        foot: (value) => (typeof value === 'number' ? '含全部状态' : '待后续阶段交付'),
        load: () => countSafe('/platform/orders'),
      },
      {
        label: '设备总量',
        unit: '台',
        icon: 'device',
        tone: 'coral',
        foot: '全部租户合计',
        load: () => countSafe('/platform/devices'),
      },
    ],

    flowSteps: BUSINESS_STEPS,
    flowTitle: '核心业务流程',
    flowSubtitle: '七步闭环',
    flowNote:
      '平台端负责租户开通、产品配置、订单审核与设备生成；工厂烧录与终端激活由对应端承接。',

    audit: {
      path: '/platform/audits',
      params: { pageSize: 8, sortBy: 'createdAt', order: 'desc' },
    },

    fallbacks: [
      '当前处于 P2 前端地基阶段：租户、产品、订单、设备等接口将在后续阶段交付。统计项显示「—」表示接口尚未就绪，上线后本页会自动展示真实数据。',
    ],
  });
}

export default { renderDashboard };
