/* ============================================================
   商户端启动入口
   ============================================================ */

import { bootEnd } from '/shared/app/boot.js';
import { PERM } from '/shared/core/auth.js';
import { renderDashboard } from './dashboard.js';
import { renderMerchantOrders } from './orders.js';
import { renderMerchantDevices } from './devices.js';
import { renderBindings } from './bindings.js';

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
  ],
});
