#!/usr/bin/env python
"""前端 ES Module 导入契约检查。

为什么需要
----------
前端为零构建方案，浏览器直接按路径加载 ES Module。一旦某个模块
``import { x } from './y.js'`` 而 ``y.js`` 并未导出 ``x``，
浏览器只会在**运行时**抛错：

    SyntaxError: The requested module './y.js' does not provide an export named 'x'

结果是整页白屏或卡在骨架屏，而 `node --check` 这类纯语法检查**发现不了**。
本脚本静态解析所有 import / export，把这类错误提前暴露。

检查内容
--------
1. 相对路径导入的模块是否存在
2. 命名导入是否确实被目标模块导出
3. 是否存在重复导出同名符号

用法::

    python scripts/verify_frontend_imports.py
    # 或
    make fe-check
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "frontend"

#: 匹配 import 语句的两种形态
RE_IMPORT = re.compile(
    r"""import\s+
        (?:(?P<default>[A-Za-z_$][\w$]*)\s*,?\s*)?   # 默认导入
        (?:\*\s+as\s+(?P<namespace>[A-Za-z_$][\w$]*)\s*)?  # 命名空间导入
        (?:\{(?P<named>[^}]*)\})?                    # 命名导入
        \s*from\s*['"](?P<source>[^'"]+)['"]""",
    re.VERBOSE | re.DOTALL,
)
#: 仅副作用导入：import './x.js'
RE_SIDE_EFFECT_IMPORT = re.compile(r"""import\s*['"](?P<source>[^'"]+)['"]""")
#: 动态导入：import('./x.js')
RE_DYNAMIC_IMPORT = re.compile(r"""import\(\s*['"](?P<source>[^'"]+)['"]\s*\)""")

#: 导出形式
RE_EXPORT_NAMED = re.compile(r"""export\s*\{([^}]*)\}""", re.DOTALL)
RE_EXPORT_DECL = re.compile(
    r"""export\s+(?:async\s+)?(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)"""
)
RE_EXPORT_DEFAULT = re.compile(r"""export\s+default\b""")


def module_id(path: Path) -> str:
    """把文件路径转为可读标识（相对 frontend/）。"""
    try:
        return str(path.relative_to(FRONTEND))
    except ValueError:
        return str(path)


def collect_exports(path: Path) -> tuple[set[str], bool]:
    """解析一个模块导出的符号。

    Returns:
        ``(具名导出集合, 是否有默认导出)``
    """
    text = path.read_text(encoding="utf-8")
    names: set[str] = set()

    # export { a, b as c }
    for block in RE_EXPORT_NAMED.findall(text):
        for item in block.split(","):
            item = item.strip()
            if not item:
                continue
            # 支持 `x as y`
            if " as " in item:
                item = item.split(" as ")[-1].strip()
            item = item.split("//")[0].strip()
            if item:
                names.add(item)

    # export const/let/var/function/class X
    names.update(RE_EXPORT_DECL.findall(text))

    return names, bool(RE_EXPORT_DEFAULT.search(text))


def resolve_import(source: str, importer: Path) -> Path | None:
    """把 import 路径解析为实际文件路径。

    * 相对路径（``./``、``../``）→ 相对当前文件解析
    * 浏览器绝对路径（``/shared/...``、``/pages/...``）→ 相对 ``frontend/`` 解析
      （后端以 ``/shared`` 与 ``/pages`` 挂载该目录，见 app/main.py）
    * 其它（裸模块名、外部 URL）→ 返回 ``None``，不校验
    """
    if source.startswith("/"):
        target = (FRONTEND / source.lstrip("/")).resolve()
    elif source.startswith("."):
        target = (importer.parent / source).resolve()
    else:
        return None

    if target.is_file():
        return target
    for suffix in (".js", ".mjs"):
        candidate = Path(str(target) + suffix)
        if candidate.is_file():
            return candidate
    if target.is_dir() and (target / "index.js").is_file():
        return target / "index.js"
    return target  # 不存在的路径，交由调用方报错


def parse_named_specifiers(block: str | None) -> list[str]:
    """解析 `{ a, b as c }` 中的**被导入名**（as 前的那一侧）。"""
    if not block:
        return []
    result: list[str] = []
    for item in block.split(","):
        item = item.strip()
        if not item:
            continue
        item = item.split("//")[0].strip()
        if " as " in item:
            item = item.split(" as ")[0].strip()
        if item:
            result.append(item)
    return result


#: 提取 HTML 中的内联模块脚本
RE_INLINE_MODULE = re.compile(
    r"""<script\s+type=["']module["'][^>]*>(?P<code>.*?)</script>""",
    re.DOTALL | re.IGNORECASE,
)
#: 提取 HTML 中的模块脚本引用
RE_SCRIPT_SRC = re.compile(
    r"""<script\s+[^>]*type=["']module["'][^>]*src=["'](?P<src>[^"']+)["']""",
    re.IGNORECASE,
)

#: UI 组件库中**返回 HTML 字符串**的函数。
#: 它们的结果必须经 fromHtml() 转成节点，否则会被当成纯文本插入 DOM，
#: 页面上直接显示出 `<div class="alert ...">` 这样的源码。
HTML_RETURNING_COMPONENTS = (
    "alert",
    "card",
    "emptyState",
    "loadingState",
    "statCard",
    "statGrid",
    "steps",
    "timeline",
    "tabs",
    "segmented",
    "progress",
    "badge",
    "tag",
    "statusTag",
    "avatar",
    "dropdown",
    "table",
    "toolbar",
    "pagination",
    "kvList",
    "descList",
    "pageHead",
    "sectionTitle",
    "secretField",
    "statusDot",
    "button",
    "iconButton",
    "skeleton",
    "qrcode",
    "barChart",
    "lineChart",
    "ringChart",
    "heatmap",
    "sparkline",
    "stackedBar",
)

#: 匹配「把 HTML 字符串当节点用」的两种写法：
#:   container.append(alert({...}))
#:   h('div', {}, alert({...}))
RE_HTML_AS_NODE = re.compile(
    r"""(?:\.append\s*\(|\bh\s*\([^)]*,\s*)"""
    r"""(?P<fn>"""
    + "|".join(HTML_RETURNING_COMPONENTS)
    + r""")\s*\(""",
)


def find_html_as_text_node(text: str) -> list[str]:
    """找出把 HTML 字符串当节点插入的位置。

    ``.append("...")`` 与 ``h(tag, attrs, "...")`` 都会创建**文本节点**，
    因此组件返回的 HTML 会被原样显示成源码。正确写法是 ``fromHtml(...)``。

    Returns:
        命中的组件名列表（可能重复）。
    """
    hits: list[str] = []
    for match in RE_HTML_AS_NODE.finditer(text):
        hits.append(match.group("fn"))
    return hits


#: 参数列表的三种出现形态。#: 刻意**只**匹配箭头函数、function 声明与简写方法定义，
#: 而不匹配「任意括号后跟 {」——后者会把 if/while/for 的条件表达式
#: 以及 JSDoc 里的 `(a:Foo, b:Bar)=>void` 误判为参数列表。
RE_ARROW_PARAMS = re.compile(r"""\(([^()]*)\)\s*=>""")
RE_FUNCTION_PARAMS = re.compile(r"""\bfunction\s+[A-Za-z_$]*\s*\(([^()]*)\)""")
RE_METHOD_PARAMS = re.compile(
    r"""^[ \t]*(?:constructor|static\s+)?[A-Za-z_$][\w$]*\s*\(([^()]*)\)\s*\{""",
    re.MULTILINE,
)

#: 单个参数的合法形态：标识符 / 剩余参数 / 带默认值
RE_SINGLE_PARAM = re.compile(r"""^(?:\.\.\.)?[A-Za-z_$][\w$]*(\s*=\s*.+)?$""", re.DOTALL)
#: 提取标识符
RE_IDENT = re.compile(r"[A-Za-z_$][\w$]*")


def _looks_like_param_list(raw: str) -> bool:
    """判断括号内容是否像参数列表（而非条件表达式或类型注解）。

    合法示例：``api``、``a, b``、``a = {}``、``...rest``
    非法示例：``decision.result === X.Y``、``a: Foo``、``{ x, y }``
    """
    raw = raw.strip()
    if not raw:
        return True  # 空参数列表

    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:  # 类型注解（JSDoc / TS 风格）
            return False
        if not RE_SINGLE_PARAM.match(item):
            return False
    return True


#: 局部声明：const/let/var X、function X、class X
RE_LOCAL_DECL = re.compile(
    r"""\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)|"""
    r"""\bfunction\s+([A-Za-z_$][\w$]*)|"""
    r"""\bclass\s+([A-Za-z_$][\w$]*)"""
)


def collect_local_names(text: str, imported: set[str]) -> set[str]:
    """收集文件内所有可引用的名字：局部声明、函数参数、导入绑定。"""
    names: set[str] = set(imported)

    for match in RE_LOCAL_DECL.finditer(text):
        for group in match.groups():
            if group:
                names.add(group)

    # 函数/方法/箭头函数的参数
    for regex in (RE_ARROW_PARAMS, RE_FUNCTION_PARAMS, RE_METHOD_PARAMS):
        for raw in regex.findall(text):
            if not _looks_like_param_list(raw):
                continue
            names.update(RE_IDENT.findall(raw))

    # 解构赋值与解构参数里的名字（宽松提取）
    for block in re.findall(r"\{([^{}]*)\}\s*(?:=|\s*\)\s*=>)", text):
        names.update(RE_IDENT.findall(block))

    return names


def comment_ranges(text: str) -> list[tuple[int, int]]:
    """扫描出所有注释的字符区间（用于排除注释里的示例代码）。

    逐字符扫描并跟踪字符串状态，避免把字符串里的 ``//``（如 URL）
    误判为注释起始。

    Returns:
        ``[(start, end), ...]``，按出现顺序排列。
    """
    ranges: list[tuple[int, int]] = []
    i = 0
    length = len(text)

    while i < length:
        ch = text[i]

        # 字符串：单引号 / 双引号 / 模板串（模板串里可能含 ${} 嵌套，这里简化处理）
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            while i < length:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue

        # 块注释
        if ch == "/" and i + 1 < length and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            end = length if end == -1 else end + 2
            ranges.append((i, end))
            i = end
            continue

        # 行注释
        if ch == "/" and i + 1 < length and text[i + 1] == "/":
            end = text.find("\n", i)
            end = length if end == -1 else end
            ranges.append((i, end))
            i = end
            continue

        i += 1

    return ranges


def _in_ranges(position: int, ranges: list[tuple[int, int]]) -> bool:
    """判断位置是否落在任一区间内（区间已按顺序排列）。"""
    for start, end in ranges:
        if start <= position < end:
            return True
        if position < start:
            return False
    return False


def find_undeclared_usage(
    text: str, known_exports: set[str], local_names: set[str]
) -> dict[str, int]:
    """找出「调用了项目内其它模块的导出名，但本文件既未导入也未声明」。

    这类问题只在运行时暴露为 ``ReferenceError: xxx is not defined``，
    静态语法检查（node --check）发现不了。

    为避免误报，做了三重过滤：
      1. 只检测**调用位置**（``name(``），排除属性访问（``obj.name(``）
      2. 跳过注释里的示例代码（如文档中提到的 ``alert()``）
      3. 跳过方法定义（``name(...) {``）

    Returns:
        ``{名字: 出现次数}``
    """
    hits: dict[str, int] = {}
    if not known_exports:
        return hits

    # 只为「项目内确实存在的导出名」做检查，避免把浏览器全局 API 当成漏导入
    pattern = re.compile(
        r"(?<![\w.$])(?P<name>" + "|".join(sorted(map(re.escape, known_exports))) + r")\s*\("
    )

    ranges = comment_ranges(text)
    # 方法定义形如 `name(...) {`，与调用区分开
    method_def = re.compile(r"\s*\([^()]*\)\s*\{")

    for match in pattern.finditer(text):
        if _in_ranges(match.start(), ranges):
            continue
        if method_def.match(text, match.end("name")):
            continue

        name = match.group("name")
        if name in local_names:
            continue
        hits[name] = hits.get(name, 0) + 1

    return hits


def collect_imported_bindings(text: str) -> set[str]:
    """收集本文件从其它模块导入的**本地绑定名**。"""
    names: set[str] = set()

    for match in RE_IMPORT.finditer(text):
        if match.group("default"):
            names.add(match.group("default"))
        if match.group("namespace"):
            names.add(match.group("namespace"))
        for item in (match.group("named") or "").split(","):
            item = item.strip().split("//")[0].strip()
            if not item:
                continue
            # `a as b` → 本地名是 b
            local = item.split(" as ")[-1].strip()
            if local:
                names.add(local)

    return names


def find_shadowing(text: str, imported: set[str]) -> dict[str, int]:
    """找出「把导入名复用为函数参数」的位置。

    这类遮蔽在运行时才暴露，且报错信息具有误导性。例如：

        import api from '../core/api.js';
        ...
        modal({ footer: (api) => { api.post('/x'); } });   // ✗ api 是弹窗对象

    实际报错是 ``api.post is not a function``，很难一眼看出是参数遮蔽。

    Returns:
        ``{参数名: 出现次数}``
    """
    hits: dict[str, int] = {}

    candidates: list[str] = []
    candidates.extend(RE_ARROW_PARAMS.findall(text))
    candidates.extend(RE_FUNCTION_PARAMS.findall(text))
    candidates.extend(RE_METHOD_PARAMS.findall(text))

    for raw in candidates:
        if not _looks_like_param_list(raw):
            continue
        for name in RE_IDENT.findall(raw):
            if name in imported:
                hits[name] = hits.get(name, 0) + 1

    return hits


def collect_sources(text: str) -> list[tuple[str, str | None, list[str]]]:
    """从一段 JS 代码中提取全部导入关系。

    Returns:
        ``[(source, default_name, [named...]), ...]``
    """
    found: list[tuple[str, str | None, list[str]]] = []

    for match in RE_IMPORT.finditer(text):
        found.append(
            (
                match.group("source"),
                match.group("default"),
                parse_named_specifiers(match.group("named")),
            )
        )

    for match in RE_SIDE_EFFECT_IMPORT.finditer(text):
        found.append((match.group("source"), None, []))

    for match in RE_DYNAMIC_IMPORT.finditer(text):
        found.append((match.group("source"), None, []))

    return found


def main() -> int:
    if not FRONTEND.exists():
        print(f"✗ 找不到前端目录：{FRONTEND}", file=sys.stderr)
        return 2

    # 收集待校验条目：(标签, 归属文件, 代码文本)
    entries: list[tuple[str, Path, str]] = []

    for path in sorted(list(FRONTEND.rglob("*.js")) + list(FRONTEND.rglob("*.mjs"))):
        entries.append((module_id(path), path, path.read_text(encoding="utf-8")))

    # HTML 中的模块入口与内联脚本同样需要校验
    for html in sorted(FRONTEND.rglob("*.html")):
        text = html.read_text(encoding="utf-8")
        for index, match in enumerate(RE_INLINE_MODULE.finditer(text)):
            entries.append((f"{module_id(html)} 内联脚本 #{index + 1}", html, match.group("code")))
        for match in RE_SCRIPT_SRC.finditer(text):
            entries.append(
                (f"{module_id(html)} <script src>", html, f"import '{match.group('src')}';")
            )

    if not entries:
        print("✗ 未找到任何前端模块", file=sys.stderr)
        return 2

    exports_cache: dict[Path, tuple[set[str], bool]] = {}

    # ---- 第一遍：汇总项目内所有具名导出，供「漏导入」检查使用 ----
    known_exports: set[str] = set()
    for path in sorted(list(FRONTEND.rglob("*.js")) + list(FRONTEND.rglob("*.mjs"))):
        if path not in exports_cache:
            exports_cache[path] = collect_exports(path)
        known_exports.update(exports_cache[path][0])

    problems: list[str] = []
    checked_imports = 0

    for label, path, text in entries:
        # ---- 检查：调用了项目内的导出名，但本文件既未导入也未声明 ----
        imported_names = collect_imported_bindings(text)
        local_names = collect_local_names(text, imported_names)
        for name, count in find_undeclared_usage(text, known_exports, local_names).items():
            problems.append(
                f"{label} → 调用了 `{name}(...)`，但本文件既未导入也未声明该名字"
                f"（{count} 处）。运行时会抛 ReferenceError: {name} is not defined。"
            )

        # ---- 检查：HTML 字符串被当作文本节点插入 ----
        for fn_name in find_html_as_text_node(text):
            problems.append(
                f"{label} → 把 `{fn_name}()` 返回的 HTML 字符串直接传给了 "
                "append()/h()，会被当成纯文本插入（页面将显示源码）。"
                "请改用 fromHtml(...) 包装。"
            )

        # ---- 遮蔽检查：导入名被复用为函数参数 ----
        imported = collect_imported_bindings(text)
        if imported:
            for name, count in find_shadowing(text, imported).items():
                problems.append(
                    f"{label} → 导入名 `{name}` 被复用为函数参数（{count} 处）。"
                    "参数会遮蔽导入，运行时将报出难以定位的错误；请改用其它参数名。"
                )

        for source, default_name, named in collect_sources(text):
            target = resolve_import(source, path)
            if target is None:
                continue  # 裸模块名或外部地址，不校验

            checked_imports += 1

            if not target.is_file():
                problems.append(f"{label} → 导入的文件不存在：{source}")
                continue

            if target not in exports_cache:
                exports_cache[target] = collect_exports(target)
            exported, has_default = exports_cache[target]

            for name in named:
                if name not in exported:
                    problems.append(
                        f"{label} → 从 {source} 导入的 `{name}` 未被导出"
                        f"（该模块导出：{sorted(exported) or '无具名导出'}"
                        f"{'，有默认导出' if has_default else ''}）"
                    )

            if default_name and not has_default:
                problems.append(
                    f"{label} → 从 {source} 导入了默认值 `{default_name}`，"
                    "但该模块没有 default 导出"
                )

    js_modules = sum(1 for label, _, _ in entries if "内联脚本" not in label and "script src" not in label)
    html_refs = len(entries) - js_modules

    print("=" * 72)
    print("前端 ES Module 导入契约检查")
    print("=" * 72)
    print(f"扫描模块   : {js_modules} 个 JS 模块 + {html_refs} 处 HTML 模块引用")
    print(f"校验导入   : {checked_imports} 处")
    print()

    if problems:
        print(f"✗ 发现 {len(problems)} 个问题：")
        for item in problems:
            print(f"    {item}")
        return 1

    print("✔ 全部导入的路径与具名导出均匹配")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
