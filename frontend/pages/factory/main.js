/* ============================================================
   工厂端启动入口
   ============================================================ */

import { bootEnd } from '/shared/app/boot.js';
import { PERM } from '/shared/core/auth.js';
import { renderDashboard } from './dashboard.js';

bootEnd({
  end: 'factory',
  home: '/dashboard',
  routes: [
    {
      pattern: '/dashboard',
      name: 'dashboard',
      title: '工作台',
      perm: PERM.factory.dashboardRead,
      handler: renderDashboard,
    },
  ],
});
