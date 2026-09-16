/* ============================================================
   各端菜单定义
   ------------------------------------------------------------
   菜单与路由**同源**：菜单项提供 path / title / perm，
   页面模块提供 handler（见 shell.js 的 syncMenusWithRoutes）。
   因此不会出现「菜单有入口但页面不存在」或反之的不一致。

   perm 字段为权限码（与后端 app/core/permissions.py 一致）。
   注意：前端隐藏菜单只是体验优化，**真正的授权由后端强制**。
   ============================================================ */

import { PERM } from '../core/auth.js';

/* ------------------------------------------------------------
   平台端
   ------------------------------------------------------------ */

export const PLATFORM_MENUS = [
  {
    groupTitle: '总览',
    items: [
      { key: 'dashboard', label: '工作台', icon: 'dashboard', path: '/dashboard', perm: PERM.platform.dashboardRead },
    ],
  },
  {
    groupTitle: '客户与产品',
    items: [
      { key: 'tenants', label: '租户管理', icon: 'building', path: '/tenants', perm: PERM.platform.tenantRead },
      { key: 'templates', label: '产品模板', icon: 'box', path: '/templates', perm: PERM.platform.templateRead },
      { key: 'clientProducts', label: '客户产品', icon: 'package', path: '/client-products', perm: PERM.platform.productRead },
      { key: 'clouds', label: '云服务商配置', icon: 'cloud', path: '/clouds', perm: PERM.platform.cloudRead },
    ],
  },
  {
    groupTitle: '订单与设备',
    items: [
      {
        key: 'orders',
        label: '订单管理',
        icon: 'clipboard',
        path: '/orders',
        perm: PERM.platform.orderRead,
        badgeKey: 'pendingOrders',
        badgeTone: 'brand',
      },
      { key: 'devices', label: '设备库存', icon: 'device', path: '/devices', perm: PERM.platform.deviceRead },
      { key: 'batches', label: '设备批次', icon: 'layers', path: '/batches', perm: PERM.platform.batchRead },
      { key: 'allocations', label: '分配单', icon: 'sitemap', path: '/allocations', perm: PERM.platform.allocationRead },
      { key: 'qrcodes', label: '二维码 / SN', icon: 'qrcode', path: '/qrcodes', perm: PERM.platform.orderRead },
    ],
  },
  {
    groupTitle: '生产',
    items: [
      { key: 'factoryOrders', label: '工厂订单', icon: 'factory', path: '/factory-orders', perm: PERM.platform.factoryOrderRead },
    ],
  },
  {
    groupTitle: '应用',
    items: [
      { key: 'miniapps', label: '小程序配置', icon: 'robot', path: '/miniapps', perm: PERM.platform.productRead },
      { key: 'ota', label: 'OTA 管理', icon: 'disk', path: '/ota', perm: PERM.platform.otaRead },
    ],
  },
  {
    groupTitle: '组织与权限',
    items: [
      { key: 'org', label: '组织架构', icon: 'sitemap', path: '/organizations', perm: PERM.platform.orgRead },
      { key: 'positions', label: '职位管理', icon: 'clipboard', path: '/positions', perm: PERM.platform.orgRead },
      { key: 'members', label: '成员管理', icon: 'users', path: '/members', perm: PERM.platform.memberRead },
      { key: 'roles', label: '角色权限', icon: 'shield', path: '/roles', perm: PERM.platform.roleRead },
    ],
  },
  {
    groupTitle: '系统',
    items: [
      { key: 'audits', label: '操作日志', icon: 'activity', path: '/audits', perm: PERM.platform.auditRead },
    ],
  },
];

/* ------------------------------------------------------------
   商户端
   ------------------------------------------------------------ */

export const MERCHANT_MENUS = [
  {
    groupTitle: '总览',
    items: [
      { key: 'dashboard', label: '工作台', icon: 'dashboard', path: '/dashboard', perm: PERM.merchant.dashboardRead },
    ],
  },
  {
    groupTitle: '我的业务',
    items: [
      { key: 'products', label: '我的产品', icon: 'package', path: '/products', perm: PERM.merchant.productRead },
      { key: 'aiConfig', label: 'AI 配置', icon: 'sparkles', path: '/ai-config', perm: PERM.merchant.aiConfigRead },
      { key: 'knowledge', label: '知识库', icon: 'book', path: '/knowledge-bases', perm: PERM.merchant.kbRead },
      {
        key: 'orders',
        label: '我的订单',
        icon: 'clipboard',
        path: '/orders',
        perm: PERM.merchant.orderRead,
        badgeKey: 'pendingOrders',
        badgeTone: 'accent',
      },
      { key: 'devices', label: '我的设备', icon: 'device', path: '/devices', perm: PERM.merchant.deviceRead },
      { key: 'bindings', label: '扫码绑定', icon: 'qrcode', path: '/bindings', perm: PERM.merchant.bindingWrite },
      { key: 'miniapp', label: '小程序', icon: 'robot', path: '/miniapp', perm: PERM.merchant.miniappRead },
    ],
  },
  {
    groupTitle: '数据',
    items: [
      { key: 'metrics', label: '运营看板', icon: 'chartLine', path: '/metrics', perm: PERM.merchant.metricsRead },
    ],
  },
  {
    groupTitle: '组织',
    items: [
      { key: 'org', label: '组织架构', icon: 'sitemap', path: '/organizations', perm: PERM.merchant.orgRead },
      { key: 'positions', label: '职位管理', icon: 'clipboard', path: '/positions', perm: PERM.merchant.orgRead },
      { key: 'members', label: '成员管理', icon: 'users', path: '/members', perm: PERM.merchant.memberRead },
    ],
  },
  {
    groupTitle: '系统',
    items: [
      { key: 'audits', label: '操作日志', icon: 'activity', path: '/audits', perm: PERM.merchant.auditRead },
    ],
  },
];

/* ------------------------------------------------------------
   工厂端
   ------------------------------------------------------------ */

export const FACTORY_MENUS = [
  {
    groupTitle: '总览',
    items: [
      { key: 'dashboard', label: '工作台', icon: 'dashboard', path: '/dashboard', perm: PERM.factory.dashboardRead },
    ],
  },
  {
    groupTitle: '生产任务',
    items: [
      {
        key: 'orders',
        label: '生产订单',
        icon: 'clipboard',
        path: '/orders',
        perm: PERM.factory.orderRead,
        badgeKey: 'pendingOrders',
        badgeTone: 'accent',
      },
      { key: 'burn', label: '烧录上报', icon: 'fire', path: '/burn', perm: PERM.factory.burnWrite },
      { key: 'inspect', label: '抽检确认', icon: 'checkCircle', path: '/inspect', perm: PERM.factory.inspectWrite },
    ],
  },
  {
    groupTitle: '资源',
    items: [
      { key: 'firmware', label: '固件版本', icon: 'disk', path: '/firmware', perm: PERM.factory.firmwareRead },
      { key: 'batches', label: '批次查询', icon: 'layers', path: '/batches', perm: PERM.factory.batchRead },
    ],
  },
];

/* ------------------------------------------------------------
   按端取菜单
   ------------------------------------------------------------ */

export const MENUS_BY_END = {
  platform: PLATFORM_MENUS,
  merchant: MERCHANT_MENUS,
  factory: FACTORY_MENUS,
  miniapp: [],
};

/**
 * 过滤掉当前账号无权限的菜单项。
 * 分组内若全部无权限，则该分组整体隐藏。
 *
 * @param {Array} menus
 * @param {(perm:string)=>boolean} hasPerm
 */
export function filterMenusByPermission(menus, hasPerm) {
  return menus
    .map((group) => ({
      ...group,
      items: (group.items || []).filter((item) => !item.perm || hasPerm(item.perm)),
    }))
    .filter((group) => (group.items || []).length > 0);
}

export default { PLATFORM_MENUS, MERCHANT_MENUS, FACTORY_MENUS, MENUS_BY_END, filterMenusByPermission };
