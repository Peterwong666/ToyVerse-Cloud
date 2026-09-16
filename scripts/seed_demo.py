#!/usr/bin/env python
"""写入演示数据。

调用生产种子逻辑（``app.db.seed``），保证脚本与线上初始化行为一致。
**幂等**：可重复执行，已存在的记录不会重复插入。

用法::

    make seed
    # 或
    python scripts/seed_demo.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

sys.path.insert(0, str(BACKEND_ROOT))


async def main() -> int:
    from app.core.config import settings
    from app.core.logging import setup_logging
    from app.db.seed import seed_demo_data, seed_summary
    from app.db.session import database_backend_name, dispose_engine

    setup_logging(level=settings.LOG_LEVEL, as_json=settings.LOG_JSON)

    print("=" * 68)
    print("ToyVerse Cloud — 写入演示数据")
    print("=" * 68)
    print(f"运行环境  : {settings.APP_ENV}")
    print(f"数据库    : {database_backend_name()}")
    print(f"演示数据  : {'启用' if settings.SEED_DEMO_DATA else '已关闭（SEED_DEMO_DATA=false）'}")
    print()

    if not settings.SEED_DEMO_DATA:
        print("⚠️  SEED_DEMO_DATA=false，仅写入基础数据（角色与权限），跳过演示数据。")
        print()

    try:
        await seed_demo_data()
    finally:
        await dispose_engine()

    summary = seed_summary()
    print()
    print("✔ 完成。当前种子数据配置：")
    print(f"  租户      : {', '.join(summary['tenants']) or '（未创建）'}")
    print(f"  角色      : {len(summary['roles'])} 个")
    for account in summary["adminAccounts"]:
        print(f"  管理员账号: {account}")
    print()
    print("提示：账号密码取自 .env 中的 *_ADMIN_PASSWORD，首次登录需修改密码。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
