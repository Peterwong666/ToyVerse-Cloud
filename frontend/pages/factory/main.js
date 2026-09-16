/* ============================================================
   工厂端启动入口
   ------------------------------------------------------------
   P6 起工厂端不再只有工作台：生产订单 / 工单详情 / 烧录上报 /
   抽检确认 / 固件版本 全部落地。

   与菜单的关系（见 shared/app/menus.js 的 FACTORY_MENUS）：
   菜单项与路由**同源**——菜单提供 path / title / perm，本文件提供
   handler。`/orders/:id` 是参数化路由，把工单标识写进 URL，
   刷新与分享都不丢状态（P-01）。

   未在此登记的菜单（如「批次查询」/batches）由 syncMenusWithRoutes
   自动补为「建设中」占位，因此菜单不会点进去空白。
   ============================================================ */

import { bootEnd } from '/shared/app/boot.js';
import { PERM } from '/shared/core/auth.js';
import { renderDashboard } from './dashboard.js';
import { renderFactoryOrders } from './orders.js';
import { renderFactoryOrderDetail } from './order_detail.js';
import { renderBurn } from './burn.js';
import { renderInspect } from './inspect.js';
import { renderFirmware } from './firmware.js';

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

    /* ---- P6：生产任务 ---- */
    {
      pattern: '/orders',
      name: 'orders',
      title: '生产订单',
      perm: PERM.factory.orderRead,
      handler: renderFactoryOrders,
    },
    {
      // 参数化路由：工单标识写在 URL 里，刷新与分享都不丢状态
      pattern: '/orders/:id',
      name: 'factoryOrderDetail',
      title: '工单详情',
      perm: PERM.factory.orderRead,
      handler: renderFactoryOrderDetail,
    },
    {
      pattern: '/burn',
      name: 'burn',
      title: '烧录上报',
      perm: PERM.factory.burnWrite,
      handler: renderBurn,
    },
    {
      pattern: '/inspect',
      name: 'inspect',
      title: '抽检确认',
      perm: PERM.factory.inspectWrite,
      handler: renderInspect,
    },

    /* ---- P6：资源 ---- */
    {
      pattern: '/firmware',
      name: 'firmware',
      title: '固件版本',
      perm: PERM.factory.firmwareRead,
      handler: renderFirmware,
    },
  ],
});
