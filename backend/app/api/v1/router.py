"""API v1 路由汇总。

按端分组挂载。后续阶段新增的路由模块在此登记。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import auth, health

api_router = APIRouter()

# ---- 健康检查（无需认证） ----
api_router.include_router(health.router)

# ---- 认证与当前用户 ----
api_router.include_router(auth.router, tags=["认证"])

# 后续阶段在此追加：
#   platform   平台端（租户 / 云服务商 / 模板 / 订单 / 设备 / 批次 / 分配 / 工厂订单 / 组织 / 权限）
#   merchant   商户端（我的产品 / AI 配置 / 订单 / 设备 / 绑定 / 小程序 / 运营数据）
#   factory    工厂端（生产订单 / 烧录 / 抽检 / 固件）
#   miniapp    终端用户端（扫码激活 / 对话 / 充值 / 设置）
#   ai         AI 供应商与调试台
