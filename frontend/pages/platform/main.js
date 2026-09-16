/* ============================================================
   平台端启动入口
   ============================================================ */

import { bootEnd } from '/shared/app/boot.js';
import { PERM } from '/shared/core/auth.js';
import { renderDashboard } from './dashboard.js';

bootEnd({
  end: 'platform',
  home: '/dashboard',
  routes: [
    {
      pattern: '/dashboard',
      name: 'dashboard',
      title: '工作台',
      perm: PERM.platform.dashboardRead,
      handler: renderDashboard,
    },
  ],
});
