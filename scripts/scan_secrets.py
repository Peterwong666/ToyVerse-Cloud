"""敏感信息与弱口令扫描（P10 安全验收）。

    python3 scripts/scan_secrets.py            # 扫描并报告
    python3 scripts/scan_secrets.py --strict   # 命中即非零退出（CI 用）

要解决什么问题
--------------
本项目早期曾把弱口令写进 `.env` 并进入仓库，也出现过 SecretKey 明文下发到前端的情况。
这些问题的共同点是：**它们在功能上完全正常工作**，因此靠「跑一遍看看」永远发现不了。

本脚本把「不该出现的东西」变成可自动化的断言，覆盖四类：

1. **弱口令/占位口令**：`admin@2024` 这类历史弱口令、`changeme` / `your-password`
   之类的占位符出现在**被 git 跟踪**的文件里（`.env.example` 的空值占位不算问题）
2. **硬编码密钥**：`JWT_SECRET_KEY = "..."` / `secret_key = "..."` 等被赋了**真实值**
   （而不是从配置读取或留空）
3. **敏感文件入库**：`.env`、`data/`、`*.jar`、`learning/`（用户明确要求不上传）
   是否被 git 跟踪
4. **明文密钥下发**：源码里出现把 `secretKey` / `apiKey` 直接放进响应模型的写法
   （这类回归一旦发生，等于把密钥交给任何能登录的账号）

为什么用「行级白名单 + 命中即失败」而不是一个正则一把梭
----------------------------------------------------
扫描器的价值取决于**误报率**：一个天天误报的检查会被迅速无视。
因此每个模式都配了「允许的上下文」（如 `.env.example` 里的空赋值、
测试夹具里的假口令、文档里的反例说明），命中未获允许时才算失败。

退出码：0 = 干净（可能有 allowlist 说明）；1 = 有命中或敏感文件被跟踪。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 扫描的文件后缀（只扫源码与配置，不扫二进制/静态资源）
SCAN_SUFFIXES = {
    ".py", ".sh", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf",
    ".md", ".json", ".example", ".env", ".txt", ".css", ".js", ".html",
}

#: 永不扫描的目录（第三方依赖、构建产物、本地数据）
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "htmlcov", ".pytest_cache",
             ".ruff_cache", ".mypy_cache", "data", ".codebuddy"}


@dataclass(slots=True)
class Finding:
    """一条命中。"""

    path: str
    line_no: int
    rule: str
    detail: str
    line: str


# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------

#: 历史弱口令与常见占位口令。
#:
#: ⚠️ 这份表是**调过误报**的，别随手加条目：
#:   * **不加** `12345678`：它是手机号（`13812345678`）、IMEI、appId 的常见子串，
#:     一加进来就会在演示账号与测试数据里刷出几十条假警报。
#:   * **不加** `placeholder`：前端到处是 HTML 的 `placeholder=` 属性。
#:   * 只保留「作为口令出现就一定是问题」的串（历史事故里的 `admin@2024`、
#:     以及 `changeme` 这类明确的占位口令）。
WEAK_PASSWORDS = (
    "admin@2024",
    "changeme",
    "your-password",
    "your_password",
    "toyverse-demo-salt",
)

#: 只有在**被赋值为字面量**时才算命中。
#:
#: 为什么必须限定「赋值」：`13812345678` 里的 `12345678`、日志文案里的
#: 「不要使用 changeme」、黑名单表本身——这些都不是「有人把弱口令写进了配置」。
#: 只在 `= "..."` / `: "..."` 这种右侧是引号字面量的位置匹配，才能把
#: 「真实使用」与「文字提及」区分开。
WEAK_ASSIGN_RE = re.compile(
    r"""[=:]\s*(?:str\s*=\s*)?['"](?P<value>[^'"]{4,})['"]"""
)

#: 允许出现上述词的文件（写的是**反例**、**规则表**或**测试夹具**）
WEAK_PASSWORD_ALLOWLIST = {
    "scripts/scan_secrets.py",           # 本文件自身（规则来源）
    "todolist.md",                       # 附录 B 记录历史弱口令问题
    "CHANGELOG.md",                      # 修复记录里点名
    "SECURITY.md",                       # 安全说明里列反例
    "backend/app/core/config.py",        # 弱口令校验的**规则表**本身
    "backend/app/core/security.py",      # 弱口令黑名单本身（`_WEAK_PASSWORDS`）
    "deploy/.env.example",               # 部署清单里的「反面示例」注释
}

#: 整个目录放行：测试夹具里的口令**按定义**就是合成的假口令，
#: 它们出现在断言里是预期行为（`Merch4nt-It#2026` 之类）。
#: 放行整目录而不是逐个文件，是为了让新增测试不需要维护这份清单。
ALLOWED_PATH_PREFIXES = (
    "backend/tests/",
)

#: 「密钥被赋了真实值」的可疑写法。
#:
#: 判据：变量名像密钥，且右侧是**非空的字面量字符串**（长度 ≥ 8）。
#: 留空、从 settings 读取、或写在 .env.example 里都放行。
SECRET_ASSIGN_RE = re.compile(
    r"""(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:secret|password|passwd|token|api_?key)[A-Za-z0-9_]*)"""
    r"""\s*[:=]\s*(?:str\s*=\s*)?"""
    r"""(?P<quote>['"])(?P<value>[^'"]{8,})(?P=quote)""",
    re.IGNORECASE,
)

#: 允许「密钥变量 = 字面量」的文件（测试夹具、示例配置、文档反例）
SECRET_ALLOWLIST = {
    "scripts/scan_secrets.py",
    "backend/tests/conftest.py",
    "backend/tests/integration/test_auth.py",
    "backend/tests/unit/test_security.py",
    "backend/tests/integration/test_binding.py",
    ".env.example",
    "deploy/.env.example",
    "README.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "todolist.md",
    "项目进度.md",
    "SUMMARY.md",
    "docs/13-火山引擎硬件对话智能体配置说明.md",
    "docs/14-ESP32-S3刷机与联调步骤清单.md",
}

#: 描述了「从配置读取」的写法，命中即放行（避免把 `self.key = settings.X` 之类当成硬编码）。
LOOKS_CONFIGURED = re.compile(r"(settings\.|os\.environ|getenv|self\.[a-z_]+|env_value|Field\(|default)", re.IGNORECASE)

#: 明文密钥下发的可疑写法：把 *key/secret 直接声明成字段。
#:
#: 只查「字段名」而不是宽泛的 `secret`：本项目大量字段叫 `secret_hint` /
#: `api_key_hint`（**掩码**，正是正确的做法），所以规则必须精确到
#: 「不带 hint/masked/enc 后缀的密钥字段」。
#:
#: ★ 而且**只查出参模型**（类名以 `Response` / `Result` / `Brief` 结尾）：
#: `CreateRequest` / `UpdateRequest` 里的 `access_key` 是**明文入参**——
#: 客户端把密钥发进来、服务层加密后落库，这个字段名必须存在且必须是明文。
#: 不区分出入参，规则就会把「加密存储的正确实现」报成安全问题。
LEAK_FIELD_RE = re.compile(
    r"""^\s*(?P<name>(?:api_?key|secret_?key|access_?key|app_?secret|private_?key))\s*[:=]""",
    re.IGNORECASE,
)

#: 出参模型的类名后缀（以此判定「这是要发给客户端的结构」）
RESPONSE_CLASS_SUFFIXES = ("Response", "Result", "Brief", "Detail")

#: 类声明（用于判断当前字段属于哪个类）
CLASS_DECL_RE = re.compile(r"^class\s+(?P<name>\w+)")

#: 允许出现明文密钥字段的文件与理由：
#: 加密存储层与掩码工具**本来就**要接触明文（它们的职责就是加密/脱敏）。
LEAK_FIELD_ALLOWED_PATHS = {
    "backend/app/core/crypto.py",         # 加解密实现，必然出现明文变量
    "backend/app/core/security.py",       # 密码哈希与令牌
    "backend/app/core/config.py",         # 从环境变量读取密钥
    "backend/app/core/storage.py",        # 不涉及密钥（保守放行以便将来扩展）
    "backend/app/services/catalog_service.py",
    "backend/app/services/ai_config_service.py",
    "backend/app/ai/base.py",
    "scripts/scan_secrets.py",
}

#: 必须**不被 git 跟踪**的路径前缀（项目硬性要求）
FORBIDDEN_TRACKED_PREFIXES = (
    "data/",
    "learning/",
    "backend/.venv/",
)

#: 必须精确等于这些文件名（注意：`.env.example` **必须入库**，
#: 它是部署者填写口令的模板）。早期版本用 `rel.startswith(".env")` 判断，
#: 于是把 `.env.example` 也报成泄漏——一条永远为真的告警等于没有告警。
FORBIDDEN_TRACKED_EXACT = (
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
)


def _git_tracked_files() -> list[str]:
    """列出被 git 跟踪的文件（相对仓库根的 POSIX 路径）。"""
    try:
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _iter_source_files() -> list[Path]:
    """遍历要扫描的源文件。

    以 `git ls-files` 为准而不是自己 walk：**只有进了仓库的文件才需要担心泄漏**，
    同时天然排除 `.venv` / `data` / 本地脚本。未在 git 仓库时回退到 walk。
    """
    tracked = _git_tracked_files()
    if not tracked:
        files: list[Path] = []
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix in SCAN_SUFFIXES:
                files.append(path)
        return files

    result: list[Path] = []
    for rel in tracked:
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        if path.suffix.lower() not in SCAN_SUFFIXES and path.name != ".env.example":
            continue
        result.append(path)
    return result


def _is_allowed_path(rel: str) -> bool:
    """整个目录放行（测试夹具的合成口令按定义就是假的）。"""
    return any(rel.startswith(prefix) for prefix in ALLOWED_PATH_PREFIXES)


def _rel(path: Path) -> str:
    """相对仓库根的 POSIX 路径（仓库外的文件返回绝对路径）。

    容错是刻意的：模块的规则函数要能被**单独调用**做自测
    （用一个 /tmp 下的临时文件验证「规则确实会命中」），
    此时路径不在仓库内，`relative_to` 会抛 ValueError。
    """
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def scan_weak_passwords(files: list[Path]) -> list[Finding]:
    """扫描历史弱口令与占位口令。"""
    findings: list[Finding] = []
    for path in files:
        rel = _rel(path)
        if rel in WEAK_PASSWORD_ALLOWLIST or _is_allowed_path(rel):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith(("#", "//", "*", ">")):
                continue
            # 只认「被赋值为字面量」的弱口令（见 WEAK_ASSIGN_RE 的说明）
            match = WEAK_ASSIGN_RE.search(line)
            if not match:
                continue
            value = match.group("value").lower()
            for weak in WEAK_PASSWORDS:
                if weak in value:
                    findings.append(
                        Finding(
                            rel,
                            idx,
                            "weak-password",
                            f"口令被设为弱口令/占位串「{weak}」",
                            stripped[:120],
                        )
                    )
    return findings


#: 值里带这些标记的一律视为**开发期占位符**，不算硬编码密钥。
#:
#: 这些值本身都在启动期被 `app.core.config.validate_security` 强制替换
#: （生产环境会拒绝启动），因此把它们报成「硬编码密钥」是误报——
#: 真正要抓的是「有人在生产代码里写了一个像真密钥的串」。
PLACEHOLDER_MARKERS = ("dev-only", "devonly", "change-me", "changeme", "insecure",
                       "placeholder", "example", "dummy", "fake", "test-only", "xxx",
                       # CI 里的一次性占位值（如 `Ci-Only-Str0ng#Pass1`）——它们只存在于
                       # GitHub Actions 的 runner 内、不指向任何真实环境，且自我声明为 ci-only。
                       # 与其把它们塞进按文件的 allowlist（那会**整个文件**不再扫描），
                       # 不如按「值自带占位标记」排除——这样 ci.yml 的其它内容仍受扫描。
                       "ci-only")


def _looks_like_a_real_secret(value: str) -> bool:
    """判断一个字符串字面量「是否像真密钥」。

    四条排除（都是实测出来的误报类）：
      1. ``SCREAMING_SNAKE`` 值（如 ``DEVICE_SECRET = "DEVICE_SECRET"``）
         —— 那是**枚举常量名**，不是密钥；
      2. 以 ``/`` 或 ``http`` 开头 —— URL 路径常量（如 ``PATH_TOKEN = "/oauth/2.0/token"``）；
      3. 含占位符标记（``dev-only`` / ``change-me`` / …）—— 开发期默认值，
         且启动期校验会强制替换；
      4. 纯字母数字且无大小写混排、无符号的长串，且长度 < 20 —— 更像是
         标识符/常量名而不是随机密钥（保守起见只在明显不像时排除）。
    """
    stripped = value.strip()
    if not stripped:
        return False
    # ★ 命令替换 / 变量展开不是字面量：shell 里 `TOKEN="$(login ...)"`、`"${VAR}"`
    #   都只是把命令输出或环境变量赋给变量，与「硬编码密钥」无关。
    #   （此前把 4 处 `TOKEN="$(...)"` 误报为硬编码密钥，属规则过宽。）
    if stripped.startswith("$(") or stripped.startswith("`") or "$(" in stripped:
        return False
    if stripped.isupper() and re.fullmatch(r"[A-Z][A-Z0-9_]*", stripped):
        return False
    if stripped.startswith(("/", "http://", "https://", "${")):
        return False
    lowered = stripped.lower()
    return not any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def scan_hardcoded_secrets(files: list[Path]) -> list[Finding]:
    """扫描「密钥变量被赋了真实字面量」。"""
    findings: list[Finding] = []
    for path in files:
        rel = _rel(path)
        if rel in SECRET_ALLOWLIST or _is_allowed_path(rel):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith(("#", "//", "*")):
                continue
            match = SECRET_ASSIGN_RE.search(line)
            if not match:
                continue
            value = match.group("value")
            if not _looks_like_a_real_secret(value):
                continue
            if LOOKS_CONFIGURED.search(line):
                continue
            findings.append(
                Finding(
                    rel,
                    idx,
                    "hardcoded-secret",
                    f"变量 {match.group('name')} 被赋了字面量值（长度 {len(value)}）",
                    stripped[:120],
                )
            )
    return findings


def scan_plaintext_key_fields(files: list[Path]) -> list[Finding]:
    """扫描「明文密钥字段」的可疑声明（应为 *_hint / *_enc / *_exposed 之类）。"""
    findings: list[Finding] = []
    for path in files:
        rel = _rel(path)
        if rel in LEAK_FIELD_ALLOWED_PATHS:
            continue
        if not rel.endswith(".py"):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        current_class = ""
        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            class_match = CLASS_DECL_RE.match(line)
            if class_match:
                current_class = class_match.group("name")
                continue
            if not current_class.endswith(RESPONSE_CLASS_SUFFIXES):
                # 请求模型 / 普通类里的密钥字段是**明文入参**，属正常设计
                continue
            if LEAK_FIELD_RE.match(line):
                findings.append(
                    Finding(
                        rel,
                        idx,
                        "plaintext-key-field",
                        f"出参模型 {current_class} 里出现未加密的密钥字段"
                        "（应为 *_hint / *_enc 掩码或密文）",
                        stripped[:120],
                    )
                )
    return findings


def scan_forbidden_tracked() -> list[Finding]:
    """检查敏感文件是否被 git 跟踪。"""
    findings: list[Finding] = []
    for rel in _git_tracked_files():
        if rel in FORBIDDEN_TRACKED_EXACT:
            findings.append(
                Finding(rel, 0, "forbidden-tracked", "敏感文件不应被 git 跟踪", "")
            )
        for prefix in FORBIDDEN_TRACKED_PREFIXES:
            if rel.startswith(prefix):
                findings.append(
                    Finding(rel, 0, "forbidden-tracked", f"该路径不应被 git 跟踪（规则：{prefix}）", "")
                )
    return findings


def main() -> int:
    """入口：扫描、报告、按需以非零码退出。"""
    parser = argparse.ArgumentParser(description="敏感信息与弱口令扫描")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="有任何命中即以退出码 1 结束（CI 用；默认也会在命中时报错，此开关保留给将来的分级使用）",
    )
    parser.add_argument("--quiet", action="store_true", help="只输出结论")
    args = parser.parse_args()

    files = _iter_source_files()
    if not files:
        print("✘ 没有可扫描的文件（是否在仓库根目录执行？）")
        return 1

    findings: list[Finding] = []
    findings += scan_forbidden_tracked()
    findings += scan_weak_passwords(files)
    findings += scan_hardcoded_secrets(files)
    findings += scan_plaintext_key_fields(files)

    if not args.quiet:
        print("敏感信息扫描")
        print(f"  扫描文件：{len(files)} 个（以 git ls-files 为准）")
        print(f"  规则：弱口令 {len(WEAK_PASSWORDS)} 条 / 密钥赋值 / 明文密钥字段 / 敏感文件入库")
        print()

    if not findings:
        print(f"✔ 未发现敏感信息问题（扫描 {len(files)} 个文件）")

    for item in findings:
        location = f"{item.path}:{item.line_no}" if item.line_no else item.path
        print(f"✘ [{item.rule}] {location}")
        print(f"    {item.detail}")
        if item.line:
            print(f"    > {item.line}")

    if findings:
        print()
        print(f"共 {len(findings)} 处命中。修复建议：")
        print("  1) 口令/密钥一律从环境变量读取（.env 不入库，见 .gitignore）")
        print("  2) 确属反例/占位/测试夹具时，把它加入 scripts/scan_secrets.py 的 allowlist 并写明理由")
        print("  3) 敏感文件（.env / data/ / learning/）确认已被 .gitignore 覆盖：git check-ignore -v <路径>")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
