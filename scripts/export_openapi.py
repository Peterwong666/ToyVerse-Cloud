#!/usr/bin/env python
"""导出 OpenAPI 契约快照。

用途
----
把当前 API 契约固化为 JSON 文件，提交进仓库。CI 中比对快照，
一旦接口发生**非预期变更**（如误删字段、改名、改类型）就会失败，
从而把「接口契约」变成可被审查的产物。

用法::

    make openapi
    # 或
    python scripts/export_openapi.py [--check]

参数:
    --check  仅比对，不写入。有差异时以非 0 退出（供 CI 使用）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SNAPSHOT_PATH = REPO_ROOT / "tests" / "contract" / "openapi_snapshot.json"

# 让脚本能 import app 包
sys.path.insert(0, str(BACKEND_ROOT))


def build_snapshot() -> dict:
    """生成 OpenAPI 契约快照。"""
    from app.main import create_app

    app = create_app()
    schema = app.openapi()
    # 版本号随代码变动，不属于契约语义差异，比对时归一化
    schema.setdefault("info", {})["version"] = "stable"
    return schema


def _stable_dump(schema: dict) -> str:
    """稳定序列化：键排序，保证同一契约产生同一文本。"""
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _summarize(schema: dict) -> str:
    """生成人类可读的契约摘要。"""
    paths: dict = schema.get("paths", {})
    lines = [f"共 {len(paths)} 个路径："]
    for path in sorted(paths):
        methods = sorted(m.upper() for m in paths[path] if m.islower())
        lines.append(f"  {'/'.join(methods):20s} {path}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 OpenAPI 契约快照")
    parser.add_argument(
        "--check",
        action="store_true",
        help="仅比对，不写入；有差异时以非 0 退出",
    )
    args = parser.parse_args()

    schema = build_snapshot()
    rendered = _stable_dump(schema)

    if args.check:
        if not SNAPSHOT_PATH.exists():
            print(f"✗ 契约快照不存在：{SNAPSHOT_PATH}")
            print("  请先执行 `make openapi` 生成并提交。")
            return 1

        current = SNAPSHOT_PATH.read_text(encoding="utf-8")
        if current == rendered:
            print("✔ OpenAPI 契约与快照一致")
            return 0

        print("✗ OpenAPI 契约与快照不一致（契约发生了变更）")
        print("\n当前契约：")
        print(_summarize(schema))
        print("\n若变更符合预期，请执行 `make openapi` 更新快照并提交。")
        return 1

    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(rendered, encoding="utf-8")

    print(f"✔ 已导出契约快照：{SNAPSHOT_PATH.relative_to(REPO_ROOT)}")
    print()
    print(_summarize(schema))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
