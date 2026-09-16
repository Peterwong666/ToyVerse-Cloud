/* ============================================================
   商户端启动入口
   ============================================================ */

import { bootEnd } from '/shared/app/boot.js';
import { PERM } from '/shared/core/auth.js';
import { renderDashboard } from './dashboard.js';

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
  ],
});
