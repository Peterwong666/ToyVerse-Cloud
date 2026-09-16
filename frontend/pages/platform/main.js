/* ============================================================
   平台端启动入口
   ------------------------------------------------------------
   P3 起本端不再只有工作台：目录域（租户 / 模板 / 云服务商 /
   客户产品）全部落地，并**首次真正使用参数化路由**
   （`/tenants/:id`、`/client-products/:id`）——
   这同时回验了 P2 的遗留观察「参数化路由已就绪但尚无页面使用」：
   现在刷新详情页不会再回落首页（P-01）。

   未在此登记的路由（订单 / 设备 / 批次 / 分配 / 工厂订单 / 组织 /
   权限 / 审计 / OTA 等）由 syncMenusWithRoutes 自动补为「建设中」占位，
   因此菜单不会出现点进去空白的情况。
   ============================================================ */

import { bootEnd } from '/shared/app/boot.js';
import { PERM } from '/shared/core/auth.js';
import { renderDashboard } from './dashboard.js';
import { renderTenants } from './tenants.js';
import { renderTenantDetail } from './tenant_detail.js';
import { renderTemplates } from './templates.js';
import { renderClouds } from './clouds.js';
import { renderClientProducts } from './client_products.js';
import { renderClientProductDetail } from './client_product_detail.js';

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

    /* ---- 目录域 ---- */
    {
      pattern: '/tenants',
      name: 'tenants',
      title: '租户管理',
      perm: PERM.platform.tenantRead,
      handler: renderTenants,
    },
    {
      // 参数化路由：详情页所需标识写在 URL 里，刷新与分享都不丢状态（P-01）
      pattern: '/tenants/:id',
      name: 'tenantDetail',
      title: '租户详情',
      perm: PERM.platform.tenantRead,
      handler: renderTenantDetail,
    },
    {
      pattern: '/templates',
      name: 'templates',
      title: '产品模板',
      perm: PERM.platform.templateRead,
      handler: renderTemplates,
    },
    {
      pattern: '/clouds',
      name: 'clouds',
      title: '云服务商配置',
      perm: PERM.platform.cloudRead,
      handler: renderClouds,
    },
    {
      pattern: '/client-products',
      name: 'clientProducts',
      title: '客户产品',
      perm: PERM.platform.productRead,
      handler: renderClientProducts,
    },
    {
      pattern: '/client-products/:id',
      name: 'clientProductDetail',
      title: '客户产品详情',
      perm: PERM.platform.productRead,
      handler: renderClientProductDetail,
    },
  ],
});
