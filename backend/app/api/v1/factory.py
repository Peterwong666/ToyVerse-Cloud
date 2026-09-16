"""工厂端 API（**P6 归属，本阶段不实现**）。

为什么现在就建这个文件
----------------------
``factory_orders`` / ``burn_reports`` / ``inspections`` 三张表已随
迁移 ``0010_factory_production`` 建好（P4 负责建表，P6 负责端点）。
把模块占位提前放在这里，是为了让 P6 的实现者一眼看到「文件归谁、
挂载点在哪」，避免又往 ``platform.py`` 里堆。

P6 需要补充的内容
-----------------
* ``GET /factory/orders`` / ``GET /factory/orders/{id}`` /
  ``POST /factory/orders/{id}/burn`` / ``POST /factory/orders/{id}/inspect``
* **字段白名单脱敏**（``FactoryOrderSerializer``）：
  客户名脱敏为「中**动」，金额与联系方式一律不下发。
  工厂是**跨租户**角色（``tenant_id = None``），因此这里不适用租户过滤，
  保护数据靠的是「只序列化白名单字段」，而不是「少查几个字段」。

在 P6 完成前，本模块不注册到 ``app/api/v1/router.py``——
一个没有任何端点的路由挂上去只会在 OpenAPI 里多出一个空分组。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/factory", tags=["工厂端 · 生产"])

__all__ = ["router"]
