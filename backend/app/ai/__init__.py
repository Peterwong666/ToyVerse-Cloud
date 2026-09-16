"""AI 抽象层（P7）。

模块导航
--------
* :mod:`app.ai.base` —— ``AIProvider`` 抽象基类与全部 DTO（能力域分层说明见模块 docstring）
* :mod:`app.ai.registry` —— 供应商注册表与解析顺序（无数据库也可工作）
* :mod:`app.ai.mock` —— 离线模拟引擎（零密钥可跑通全流程）
* :mod:`app.ai.jixian` / :mod:`app.ai.joyinside` / :mod:`app.ai.volcano` / :mod:`app.ai.baidu`
  —— 真实厂商适配器骨架；未配置密钥时一律安全失败（ADR-07）

本包**不导入任何 ORM 模型**，因此可以在没有数据库的单元测试里直接使用。
需要数据库的解析逻辑集中在 :func:`app.ai.registry.resolve_for_product`。
"""

from __future__ import annotations

__all__: list[str] = []
