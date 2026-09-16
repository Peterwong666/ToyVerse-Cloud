/* ============================================================
   商户端 · 工作台
   ============================================================ */

import { countSafe, renderDashboard as render } from '/shared/app/dashboard.js';

/** 商户端视角的六步流程 */
const MERCHANT_STEPS = [
  { title: '选择产品' },
  { title: '配置 AI' },
  { title: '提交订单' },
  { title: '等待审核' },
  { title: '设备到手' },
  { title: '扫码绑定' },
];

export async function renderDashboard(container) {
  return render(container, {
    desc: '商户端 · 仅可见本租户数据（服务端强制按租户隔离）',
    scopeLabel: '仅本租户',

    stats: [
      {
        label: '我的产品',
        unit: '个',
        icon: 'package',
        tone: 'brand',
        foot: '已配置的产品实例',
        load: () => countSafe('/merchant/products'),
      },
      {
        label: '我的订单',
        unit: '单',
        icon: 'clipboard',
        tone: 'accent',
        foot: '全部状态',
        load: () => countSafe('/merchant/orders'),
      },
      {
        label: '我的设备',
        unit: '台',
        icon: 'device',
        tone: 'teal',
        foot: '已分配至本租户',
        load: () => countSafe('/merchant/devices'),
      },
      {
        label: '已绑定设备',
        unit: '台',
        icon: 'link',
        tone: 'coral',
        foot: '终端用户已完成激活',
        load: () => countSafe('/merchant/devices', { bindStatus: 'BOUND' }),
      },
    ],

    flowSteps: MERCHANT_STEPS,
    flowTitle: '我的业务流程',
    flowSubtitle: '从选品到绑定',
    flowNote:
      '商户端可以定义自己的产品与 AI 角色，提交订单后由平台审核并生成设备；设备到手后由终端用户扫码绑定。',

    audit: {
      path: '/merchant/audits',
      params: { pageSize: 8, sortBy: 'createdAt', order: 'desc' },
    },

    fallbacks: [
      'P4/P5 已交付：我的订单（下单与查单）、我的设备（扫码绑定 / 解绑）、绑定管理均已接入真实数据。统计卡显示「—」表示对应接口尚未就绪。',
      '商户端的所有查询都会在服务端强制附加租户条件，因此你只能看到自己租户的数据。',
    ],
  });
}

export default { renderDashboard };
