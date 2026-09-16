/* ============================================================
   商户端启动入口
   ============================================================ */

import { bootEnd } from '/shared/app/boot.js';
import { PERM } from '/shared/core/auth.js';
import { renderDashboard } from './dashboard.js';
import { renderMerchantOrders } from './orders.js';
import { renderMerchantDevices } from './devices.js';
import { renderBindings } from './bindings.js';
import { renderProducts } from './products.js';
import { renderMerchantProductDetail } from './product_detail.js';
import { renderAiConfig } from './ai_config.js';
import { renderKnowledge } from './knowledge.js';
import { renderMetrics } from './metrics.js';

bootEnd({
  end: 'merchant',
  home: '/dashboard',
  routes: [
    {
      pattern: '/dashboard',
      name: 'dashboard',
      title: '工作台',
      perm: PERM.merchant.dashboardRead,
      handler: renderDashboard,
    },
    {
      // P4：商户端「我的订单」（列表 + 下单）
      pattern: '/orders',
      name: 'orders',
      title: '我的订单',
      perm: PERM.merchant.orderRead,
      handler: renderMerchantOrders,
    },
    /* ---- P5：设备与绑定 ---- */
    {
      pattern: '/devices',
      name: 'devices',
      title: '我的设备',
      perm: PERM.merchant.deviceRead,
      handler: renderMerchantDevices,
    },
    {
      pattern: '/bindings',
      name: 'bindings',
      title: '绑定管理',
      perm: PERM.merchant.deviceRead,
      handler: renderBindings,
    },

    /* ---- P9：AI 配置与运营看板 ---- */
    {
      // 「我的产品」：P4 已有列表端点（下单选择用），P9 才把它做成页面
      pattern: '/products',
      name: 'products',
      title: '我的产品',
      perm: PERM.merchant.productRead,
      handler: renderProducts,
    },
    {
      // 参数化路由：详情所需的产品 ID 写在 URL 里，刷新与分享都不丢状态
      pattern: '/products/:id',
      name: 'merchantProductDetail',
      title: '产品详情',
      perm: PERM.merchant.productRead,
      handler: renderMerchantProductDetail,
    },
    {
      pattern: '/ai-config',
      name: 'aiConfig',
      title: 'AI 配置',
      perm: PERM.merchant.aiConfigRead,
      handler: renderAiConfig,
    },
    {
      // 路径与菜单一致：menus.js 里「知识库」的 path 是 /knowledge-bases
      pattern: '/knowledge-bases',
      name: 'knowledgeBases',
      title: '知识库',
      perm: PERM.merchant.kbRead,
      handler: renderKnowledge,
    },
    {
      pattern: '/metrics',
      name: 'metrics',
      title: '运营看板',
      perm: PERM.merchant.metricsRead,
      handler: renderMetrics,
    },
  ],
});
