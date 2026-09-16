/* ============================================================
   商户端启动入口
   ============================================================ */

import { bootEnd } from '/shared/app/boot.js';
import { PERM } from '/shared/core/auth.js';
import { renderDashboard } from './dashboard.js';
import { renderMerchantOrders } from './orders.js';

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
  ],
});
