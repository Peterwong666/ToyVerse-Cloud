"""API v1 路由汇总。

按端分组挂载。后续阶段新增的路由模块在此登记。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    ai,
    auth,
    device,
    health,
    merchant,
    platform,
    platform_allocations,
    platform_orders,
)

api_router = APIRouter()

# ---- 健康检查（无需认证） ----
api_router.include_router(health.router)

# ---- 认证与当前用户 ----
api_router.include_router(auth.router, tags=["认证"])

# ---- 平台端（租户 / 云服务商 / 模板 / 授权 / 客户产品 / 小程序配置） ----
api_router.include_router(platform.router)

# ---- 平台端 · 业务主干（P4：订单 / 设备 / 批次导入） ----
# 单独一个模块而不是并入上面的 platform.py：P3 与 P4 并行开发时，
# 追加到同一文件必然冲突，拆开后只在下面这几行相遇。
api_router.include_router(platform_orders.router)

# ---- 平台端 · 分配（P5：分配单 创建 / 执行 / 明细） ----
# 与 P4 分模块同理：P5 与其它阶段并行开发，追加到同一文件必然冲突。
api_router.include_router(platform_allocations.router)

# ---- 商户端（P4：我的订单；P5 起继续追加设备 / 绑定 / 小程序 / 运营数据） ----
api_router.include_router(merchant.router)

# ---- 设备侧（P5：心跳上报，密钥鉴权、**不挂 JWT**） ----
# 设备（玩具本体）没有账号可登录，鉴权靠平台签发的 DEVICE_SECRET 摘要比对，
# 因此独占一个模块，避免被误加到需要认证的依赖分组里。
api_router.include_router(device.router)

# ---- AI 供应商与调试台（P7：供应商列表 / 健康探测 / 对话 / ASR / TTS / 离线素材） ----
api_router.include_router(ai.router)

# 后续阶段在此追加：
#   factory    工厂端（生产订单 / 烧录 / 抽检 / 固件）——P6，模块已占位
#   miniapp    终端用户端（扫码激活 / 对话 / 充值 / 设置）
