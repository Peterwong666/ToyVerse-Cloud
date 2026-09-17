#!/usr/bin/env python3
"""文档一致性门禁（P11）。

文档最大的风险是**悄悄过期**：代码改了、文档没改，读者照着文档做会失败。
本脚本把这类「不一致」变成红灯，与 `make fe-check`（前端导入契约）、
`make openapi-check`（接口契约）是同一套思路。

七类校验
--------
1. **端点**：`docs/06` 的端点表格与 `tests/contract/openapi_snapshot.json` 逐条比对
   （方法 + 路径），并要求文档声明的总数与快照一致。
2. **数据表**：`docs/05` 的字段字典小节标题里列出的表名集合 == 迁移/模型里的表集合。
3. **错误码**：`docs/06` 的错误码表与 `ErrorCode` 枚举逐条比对，
   **并校验文档写的 HTTP 状态与 `_STATUS_MAP` 一致**。
4. **权限码**：`docs/07` 里出现的权限码值全部在 `permissions.py` 注册。
5. **枚举值**：`docs/05` 状态枚举小节里的每个枚举值都是 `enums.py` 里真实存在的成员。
6. **引用可解析**：文档里引用的 `make <target>` / `scripts/<file>` / 相对文档链接真实存在。
7. **文档规范**：每份 `docs/NN-*.md` 都含「已知局限 / 取证边界」小节；
   且任何文档都不含 `learning/` 路径。

用法
----
    python3 scripts/check_docs.py          # 全量校验，失败退出码 1
    python3 scripts/check_docs.py -v       # 额外打印通过项明细
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
BACKEND = ROOT / "backend"
SNAPSHOT = ROOT / "tests/contract/openapi_snapshot.json"
MAKEFILE = ROOT / "Makefile"

METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")

#: 只检查这些编号的文档（01–14）
DOC_PATTERN = re.compile(r"^(\d{2})-.*\.md$")

#: 「已知局限 / 取证边界」的合法小节标题形态
BOUNDARY_MARKERS = ("已知局限", "取证边界", "已知的安全边界")
BOUNDARY_HEADING = re.compile(r"^#{2,4}\s+.*(已知局限|取证边界)", re.M)

failures: list[str] = []
notes: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)


def ok(msg: str) -> None:
    notes.append(msg)


# ---------------------------------------------------------------------------
# 读取「事实来源」
# ---------------------------------------------------------------------------
def load_snapshot_endpoints() -> set[tuple[str, str]]:
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    out: set[tuple[str, str]] = set()
    for path, ops in data["paths"].items():
        for method in ops:
            if method.upper() in METHODS:
                out.add((method.upper(), path))
    return out


def load_tables() -> set[str]:
    """迁移 create_table 与模型 __tablename__ 的并集（并在脚本内交叉校验二者一致）。"""
    mig: set[str] = set()
    for f in (BACKEND / "alembic/versions").glob("*.py"):
        mig |= set(re.findall(r'op\.create_table\(\s*["\']([a-z_]+)["\']', f.read_text(encoding="utf-8")))
    model: set[str] = set()
    for f in (BACKEND / "app/models").glob("*.py"):
        model |= set(re.findall(r'__tablename__\s*=\s*["\']([a-z_]+)["\']', f.read_text(encoding="utf-8")))
    if mig != model:
        fail(
            f"迁移与模型表集合不一致：迁移独有 {sorted(mig - model)} / 模型独有 {sorted(model - mig)}"
        )
    return mig


def load_error_codes() -> tuple[set[str], dict[str, int]]:
    text = (BACKEND / "app/core/errors.py").read_text(encoding="utf-8")
    block = re.search(r"class ErrorCode\(StrEnum\):(.*?)(?=\nclass |\Z)", text, re.S)
    if not block:
        fail("找不到 ErrorCode 枚举")
        return set(), {}
    codes = set(re.findall(r"^\s{4}([A-Z][A-Z0-9_]*)\s*=", block.group(1), re.M))

    status_block = re.search(r"_STATUS_MAP[^=]*=\s*\{(.*?)\n\}", text, re.S)
    statuses: dict[str, int] = {}
    if status_block:
        for name, code in re.findall(
            r"ErrorCode\.([A-Z][A-Z0-9_]*):\s*(\d{3})", status_block.group(1)
        ):
            statuses[name] = int(code)
    return codes, statuses


def load_permissions() -> set[str]:
    text = (BACKEND / "app/core/permissions.py").read_text(encoding="utf-8")
    return set(re.findall(r'=\s*"([a-z]+:[a-z-]+:(?:read|write))"', text))


def load_enum_members() -> set[str]:
    """enums.py 里所有枚举成员名（用于校验文档里写的枚举值是否真实存在）。"""
    text = (BACKEND / "app/models/enums.py").read_text(encoding="utf-8")
    members: set[str] = set()
    for body in re.findall(r"class \w+\((?:str,\s*)?\w*Enum\):(.*?)(?=\nclass |\n[A-Z_]+\s*=|\Z)", text, re.S):
        for name in re.findall(r"^\s{4}([A-Z][A-Z0-9_]*)\s*=", body, re.M):
            members.add(name)
    # 常量里也散落枚举值（如 TRANSITIONS 的键、LABELS 的键）
    members |= set(re.findall(r"\b([A-Z][A-Z0-9_]{2,})\b", text))
    return members


def load_make_targets() -> set[str]:
    text = MAKEFILE.read_text(encoding="utf-8")
    return set(re.findall(r"^([a-zA-Z0-9_-]+):", text, re.M))


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def check_endpoints(snapshot: set[tuple[str, str]]) -> None:
    doc = DOCS / "06-API接口文档.md"
    if not doc.exists():
        fail("缺少 docs/06-API接口文档.md")
        return
    text = doc.read_text(encoding="utf-8")
    pairs = set(re.findall(r"^\|\s*`(GET|POST|PUT|DELETE|PATCH)`\s*\|\s*`([^`]+)`\s*\|", text, re.M))
    if not pairs:
        fail("docs/06 未找到端点表格（期望形如 | `GET` | `/api/v1/...` |）")
        return
    missing = pairs - snapshot
    extra = snapshot - pairs
    if missing:
        fail(f"docs/06 声明了快照里不存在的端点（{len(missing)}）：{sorted(missing)[:5]}")
    if extra:
        fail(f"docs/06 缺少实现里存在的端点（{len(extra)}）：{sorted(extra)[:5]}")
    declared = re.findall(r"合计\s*\*{0,2}(\d+)\*{0,2}\s*个\s*HTTP\s*端点", text)
    if not declared:
        fail("docs/06 未声明端点总数（期望「合计 N 个 HTTP 端点」）")
    else:
        for n in declared:
            if int(n) != len(snapshot):
                fail(f"docs/06 声明的端点总数 {n} 与实现 {len(snapshot)} 不一致")
    claim = re.search(r"OpenAPI `paths`\s*\|\s*\*{0,2}(\d+)", text)
    paths_count = len(json.loads(SNAPSHOT.read_text(encoding="utf-8"))["paths"])
    if claim and int(claim.group(1)) != paths_count:
        fail(f"docs/06 声明的 paths 数 {claim.group(1)} 与快照 {paths_count} 不一致")
    ok(f"端点：docs/06 与快照一致（{len(snapshot)} 个，paths {paths_count}）")


def check_tables(tables: set[str]) -> None:
    doc = DOCS / "05-数据模型与ER图.md"
    if not doc.exists():
        fail("缺少 docs/05-数据模型与ER图.md")
        return
    text = doc.read_text(encoding="utf-8")
    declared = set(re.findall(r"^####\s+[\d.]+\s+`([a-z_]+)`（\d+ 列）", text, re.M))
    if not declared:
        fail("docs/05 未找到字段字典小节（期望形如 #### 4.1.1 `table`（N 列））")
        return
    missing = tables - declared
    extra = declared - tables
    if missing:
        fail(f"docs/05 字段字典缺少表（{len(missing)}）：{sorted(missing)}")
    if extra:
        fail(f"docs/05 字段字典列出了不存在的表（{len(extra)}）：{sorted(extra)}")
    for n in re.findall(r"表总数\s*\|\s*\*{0,2}(\d+)", text):
        if int(n) != len(tables):
            fail(f"docs/05 声明的表总数 {n} 与实现 {len(tables)} 不一致")
    ok(f"数据表：docs/05 字段字典覆盖 {len(tables)} 张表且与迁移/模型一致")


def check_error_codes(codes: set[str], statuses: dict[str, int]) -> None:
    doc = DOCS / "06-API接口文档.md"
    if not doc.exists():
        return
    text = doc.read_text(encoding="utf-8")
    rows = re.findall(r"^\|\s*`([A-Z][A-Z0-9_]+)`\s*\|\s*(\d{3})\s*\|", text, re.M)
    if not rows:
        fail("docs/06 未找到错误码表（期望形如 | `CODE` | 409 | ...）")
        return
    declared = {name: int(status) for name, status in rows}
    unknown = set(declared) - codes
    if unknown:
        fail(f"docs/06 引用了不存在的错误码（{len(unknown)}）：{sorted(unknown)}")
    absent = codes - set(declared)
    if absent:
        fail(f"docs/06 遗漏了错误码（{len(absent)}）：{sorted(absent)}")
    wrong = {
        name: (declared[name], statuses[name])
        for name in set(declared) & codes
        if statuses.get(name) and declared[name] != statuses[name]
    }
    if wrong:
        detail = ", ".join(f"{k}: 文档 {v[0]} vs 实现 {v[1]}" for k, v in sorted(wrong.items())[:5])
        fail(f"docs/06 的 HTTP 状态与 _STATUS_MAP 不一致（{len(wrong)}）：{detail}")
    ok(f"错误码：docs/06 覆盖 {len(codes)} 个错误码，HTTP 状态全部与 _STATUS_MAP 一致")


def check_permissions(perms: set[str]) -> None:
    doc = DOCS / "07-多租户与权限设计.md"
    if not doc.exists():
        fail("缺少 docs/07-多租户与权限设计.md")
        return
    text = doc.read_text(encoding="utf-8")
    declared = set(re.findall(r"`([a-z]+:[a-z-]+:(?:read|write))`", text))
    if not declared:
        fail("docs/07 未找到权限码（期望形如 `platform:tenant:read`）")
        return
    unknown = declared - perms
    if unknown:
        fail(f"docs/07 引用了未注册的权限码（{len(unknown)}）：{sorted(unknown)}")
    absent = perms - declared
    if absent:
        fail(f"docs/07 遗漏了权限码（{len(absent)}）：{sorted(absent)}")
    ok(f"权限码：docs/07 覆盖 {len(perms)} 个权限码且全部已注册")


def check_enums(members: set[str]) -> None:
    doc = DOCS / "05-数据模型与ER图.md"
    if not doc.exists():
        return
    text = doc.read_text(encoding="utf-8")
    section = re.search(r"^## 5\. 状态枚举(.*?)^## 6\.", text, re.S | re.M)
    if not section:
        fail("docs/05 未找到「## 5. 状态枚举」小节")
        return
    tokens = set(re.findall(r"`([A-Z][A-Z0-9_]{2,})`", section.group(1)))
    unknown = {t for t in tokens if t not in members}
    if unknown:
        fail(f"docs/05 状态枚举小节引用了不存在的枚举值（{len(unknown)}）：{sorted(unknown)}")
    ok(f"枚举值：docs/05 状态枚举小节的 {len(tokens)} 个取值均真实存在")


def check_references(targets: set[str]) -> None:
    """文档里引用的 make 目标 / scripts 文件 / 相对文档链接必须存在。"""
    bad_make: set[str] = set()
    bad_script: set[str] = set()
    bad_link: set[str] = set()
    script_dir = ROOT / "scripts"

    for doc in sorted(DOCS.glob("*.md")):
        text = doc.read_text(encoding="utf-8")
        for target in re.findall(r"`make\s+([a-zA-Z0-9_-]+)", text):
            if target not in targets:
                bad_make.add(f"{doc.name} → make {target}")
        for s in re.findall(r"scripts/([A-Za-z0-9_.-]+\.(?:py|sh))", text):
            if not (script_dir / s).exists():
                bad_script.add(f"{doc.name} → scripts/{s}")
        for link in re.findall(r"\]\(\./([0-9]{2}-[^)#]+\.md)\)", text):
            if not (DOCS / link).exists():
                bad_link.add(f"{doc.name} → ./{link}")
        for link in re.findall(r"\]\(\.\./([A-Za-z0-9_.-]+\.md)\)", text):
            if not (ROOT / link).exists():
                bad_link.add(f"{doc.name} → ../{link}")

    if bad_make:
        fail(f"文档引用了不存在的 make 目标（{len(bad_make)}）：{sorted(bad_make)[:5]}")
    if bad_script:
        fail(f"文档引用了不存在的脚本（{len(bad_script)}）：{sorted(bad_script)[:5]}")
    if bad_link:
        fail(f"文档里的相对链接无法解析（{len(bad_link)}）：{sorted(bad_link)[:5]}")
    if not (bad_make or bad_script or bad_link):
        ok("引用：make 目标 / scripts 文件 / 相对文档链接全部可解析")


def load_test_counts() -> dict[str, int]:
    """每个测试文件里 `def test_` 的个数（与 docs/11 的表格口径一致）。"""
    counts: dict[str, int] = {}
    for f in (BACKEND / "tests").rglob("test_*.py"):
        n = len(re.findall(r"^\s*(?:async )?def test_", f.read_text(encoding="utf-8"), re.M))
        counts[f.name] = n
    return counts


def check_test_counts() -> None:
    """docs/11 的逐文件用例数必须与源码一致，且分组合计等于总函数数。"""
    doc = DOCS / "11-测试与质量保障.md"
    if not doc.exists():
        fail("缺少 docs/11-测试与质量保障.md")
        return
    text = doc.read_text(encoding="utf-8")
    actual = load_test_counts()

    declared: dict[str, int] = {}
    for name, num in re.findall(r"^\|\s*`(test_[a-z_]+\.py)`\s*\|\s*\*{0,2}(\d+)\*{0,2}\s*\|", text, re.M):
        declared[name] = int(num)
    if not declared:
        fail("docs/11 未找到逐文件用例数表格（期望形如 | `test_x.py` | **12** |）")
        return

    wrong = {
        name: (num, actual.get(name, -1))
        for name, num in declared.items()
        if actual.get(name) != num
    }
    if wrong:
        detail = ", ".join(f"{k}: 文档 {v[0]} vs 实际 {v[1]}" for k, v in sorted(wrong.items())[:5])
        fail(f"docs/11 的逐文件用例数与源码不一致（{len(wrong)}）：{detail}")

    # 三条口径的总数
    for label in ("unit", "integration", "e2e"):
        m = re.search(rf"\|\s*\*\*{label}\*\*\s*\|\s*\*{{0,2}}(\d+)\*{{0,2}}\s*\|\s*\*{{0,2}}(\d+)\*{{0,2}}\s*\|", text)
        if not m:
            fail(f"docs/11 未找到 {label} 的文件数/函数数行")
            continue
        files_n, funcs_n = int(m.group(1)), int(m.group(2))
        real_files = [n for n in actual if n.startswith("test_") and _group_of(n) == label]
        real_funcs = sum(actual[n] for n in real_files)
        if (files_n, funcs_n) != (len(real_files), real_funcs):
            fail(
                f"docs/11 的 {label} 行与源码不一致：文档 {files_n} 文件/{funcs_n} 函数，"
                f"实际 {len(real_files)} 文件/{real_funcs} 函数"
            )
    ok(
        f"测试计数：docs/11 的 {len(declared)} 个逐文件用例数与源码一致"
        f"（合计 {sum(actual.values())} 个测试函数）"
    )


def _group_of(filename: str) -> str:
    for group in ("unit", "integration", "e2e"):
        if (BACKEND / "tests" / group / filename).exists():
            return group
    return "?"


def check_doc_conventions() -> None:
    numbered = sorted(p for p in DOCS.glob("*.md") if DOC_PATTERN.match(p.name))
    if not numbered:
        fail("docs/ 下没有找到 NN-*.md 形式的文档")
        return
    no_boundary: list[str] = []
    leaks: list[str] = []
    for doc in numbered:
        text = doc.read_text(encoding="utf-8")
        if not BOUNDARY_HEADING.search(text):
            no_boundary.append(doc.name)
        # 只禁止**指向** learning/ 内部的路径或链接（那会让读者点开 404），
        # 允许在正文里提到这个目录名本身（例如描述红线 6 或本项检查规则）。
        if re.search(r"\]\([^)]*learning/", text) or re.search(r"`learning/[^`]+`", text):
            leaks.append(doc.name)
    if no_boundary:
        fail(f"以下文档缺少「已知局限 / 取证边界」小节：{no_boundary}")
    if leaks:
        fail(f"以下文档引用了不上传的 learning/ 内部路径（会导致链接 404）：{leaks}")
    if not no_boundary and not leaks:
        ok(f"文档规范：{len(numbered)} 份文档均含边界声明，且无 learning/ 内部路径引用")


def main() -> int:
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    if not SNAPSHOT.exists():
        fail(f"缺少契约快照：{SNAPSHOT.relative_to(ROOT)}")
        return 1

    snapshot = load_snapshot_endpoints()
    tables = load_tables()
    codes, statuses = load_error_codes()
    perms = load_permissions()
    members = load_enum_members()
    targets = load_make_targets()

    check_endpoints(snapshot)
    check_tables(tables)
    check_error_codes(codes, statuses)
    check_permissions(perms)
    check_enums(members)
    check_references(targets)
    check_test_counts()
    check_doc_conventions()

    if failures:
        print(f"\n✘ 文档一致性校验未通过（{len(failures)} 项）：\n")
        for f in failures:
            print(f"  • {f}")
        return 1
    print(f"✔ 文档一致性校验通过（{len(notes)} 类检查）")
    if not verbose:
        for n in notes:
            print(f"  · {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
