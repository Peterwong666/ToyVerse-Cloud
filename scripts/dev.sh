#!/usr/bin/env bash
#
# ToyVerse Cloud — 本地开发启动脚本（P10 运维脚本）
#
# 为什么要有这个脚本（而不是只用 make dev）
# ========================================
# `make dev` 把「装环境 → 起服务」串成了一条固定流水线，适合第一次跑通项目。
# 日常开发却常需要「换个端口起一份」「关掉热重载复现生产行为」——
# make 目标的参数要从命令行透传进去很别扭（make dev PORT=8080 也改不了
# 写死在目标里的 --port 8000）。本脚本是同一个启动动作的**直接入口**：
#
#   * 端口 / 监听地址可用参数覆盖，默认值取自 .env（与 `make dev` 同源）；
#   * `--no-reload` 用于本机复现「无热重载」的生产启动行为；
#   * 启动前把四端入口与 API 文档地址打出来——本项目的四端都挂在同一个
#     后端上（静态托管），记不住路径时不用再去翻 README。
#
# 与前缀检查的分工：`make dev` 面向「能一把跑起来」，
# 本脚本面向「已经跑通过、现在要带参数再起来一次」。
#
# 用法::
#
#     bash scripts/dev.sh                       # 读取 .env 的 HOST / PORT
#     bash scripts/dev.sh --port 8080           # 临时换端口
#     bash scripts/dev.sh --no-reload           # 模拟生产启动（无热重载）
#     bash scripts/dev.sh --help
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKEND_DIR="${REPO_ROOT}/backend"
VENV_DIR="${BACKEND_DIR}/.venv"
PY="${VENV_DIR}/bin/python"
ENV_FILE="${REPO_ROOT}/.env"

HOST_OVERRIDE=""
PORT_OVERRIDE=""
RELOAD=1

usage() {
    cat <<'EOF'
启动本地开发服务器（uvicorn + 前端静态托管）

用法：
  bash scripts/dev.sh [--host HOST] [--port PORT] [--no-reload]

选项：
  --host HOST    监听地址（默认取 .env 的 HOST，回退 0.0.0.0）
  --port PORT    监听端口（默认取 .env 的 PORT，回退 8000）
  --no-reload    不启用热重载（模拟生产启动）
  -h, --help     显示本帮助
EOF
}

while (( $# > 0 )); do
    case "$1" in
        --host)
            # ${2:?} 在缺少取值时直接报错退出，避免把空值一路带到 uvicorn
            HOST_OVERRIDE="${2:?--host 需要一个取值}"
            shift 2
            ;;
        --host=*)
            HOST_OVERRIDE="${1#*=}"
            shift
            ;;
        --port)
            PORT_OVERRIDE="${2:?--port 需要一个取值}"
            shift 2
            ;;
        --port=*)
            PORT_OVERRIDE="${1#*=}"
            shift
            ;;
        --no-reload)
            RELOAD=0
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "✗ 未知参数：$1"
            echo
            usage
            exit 2
            ;;
    esac
done

# ---------------------------------------------------------------------------
# 前置检查
# ---------------------------------------------------------------------------
# 不替用户创建 .env：.env 里是密钥与管理员口令，必须由人显式填写。
# 自动复制一份占位符 .env 只会把「启动失败」推迟到安全校验那一层，
# 而且会让人误以为「已经配好了」。
if [[ ! -f "${ENV_FILE}" ]]; then
    echo "✗ 未找到 .env：${ENV_FILE}"
    echo "  请先执行 \`make env\` 并填写强口令（JWT_SECRET_KEY / QR_SIGN_SECRET / 各管理员密码）。"
    exit 1
fi

if [[ ! -x "${PY}" ]]; then
    echo "✗ 未找到虚拟环境：${VENV_DIR}"
    echo "  请先执行 \`make setup\` 完成初始化。"
    exit 1
fi

# ---------------------------------------------------------------------------
# 读取 .env 中的监听配置
# ---------------------------------------------------------------------------
# 只按 key 取需要的两项，**不 source 整个 .env**：
# source 会把所有密钥导入本进程环境，任何一条 `env` / 报错堆栈都可能把它们带出来。
# 末尾的 `|| true` 是给「key 不存在」留后路——grep 无匹配返回 1，
# 在 set -e 下会直接中断脚本。
env_value() {
    local key="$1" fallback="$2" raw
    raw="$(grep -E "^[[:space:]]*${key}[[:space:]]*=" "${ENV_FILE}" | tail -n 1 | cut -d= -f2- || true)"
    raw="${raw%%$'\r'}"          # 容忍 CRLF 换行的 .env
    raw="${raw%%#*}"             # 去掉行尾注释
    raw="${raw%\"}"; raw="${raw#\"}"   # 去掉包裹的引号
    raw="${raw%\'}"; raw="${raw#\'}"
    raw="$(printf '%s' "${raw}" | xargs)"   # 去首尾空白（此处只有 HOST/PORT，无特殊字符）
    printf '%s' "${raw:-${fallback}}"
}

HOST="${HOST_OVERRIDE:-$(env_value HOST 0.0.0.0)}"
PORT="${PORT_OVERRIDE:-$(env_value PORT 8000)}"

# ---------------------------------------------------------------------------
# 入口提示
# ---------------------------------------------------------------------------
# 监听 0.0.0.0 表示「所有网卡」，它不是浏览器能访问的地址；
# 展示时换成 localhost，否则四端链接点不开。
BROWSER_HOST="${HOST}"
case "${HOST}" in
    0.0.0.0 | "::" | "*" | "")
        BROWSER_HOST="localhost"
        ;;
esac
BASE_URL="http://${BROWSER_HOST}:${PORT}"

MODE="开发（热重载）"
(( RELOAD == 0 )) && MODE="生产模拟（无热重载）"

echo "===================================================================="
echo "ToyVerse Cloud — 启动本地服务"
echo "===================================================================="
echo "运行模式: ${MODE}"
echo "监听地址: ${HOST}:${PORT}"
echo
echo "入口地址："
echo "  平台端  : ${BASE_URL}/platform/"
echo "  商户端  : ${BASE_URL}/merchant/"
echo "  工厂端  : ${BASE_URL}/factory/"
echo "  小程序  : ${BASE_URL}/miniapp/"
echo "  API 文档: ${BASE_URL}/docs"
echo
echo "按 Ctrl+C 停止服务。"
echo "===================================================================="

# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
# app.main:app 的相对导入依赖 backend/ 作为工作目录，必须 cd 过去。
# 这里刻意不加 --workers：多进程会让本地日志交错、断点调试失效，
# 而本脚本的定位是「本地开发」；多 worker 场景请用 `make serve` 或 Docker。
UVICORN_ARGS=(app.main:app --host "${HOST}" --port "${PORT}")
if (( RELOAD == 1 )); then
    UVICORN_ARGS+=(--reload)
fi

# exec 让 uvicorn 顶替当前 shell：Ctrl+C / SIGTERM 直达进程本身，
# 不会留下一个「收不到信号」的孤儿服务。
cd "${BACKEND_DIR}"
exec "${PY}" -m uvicorn "${UVICORN_ARGS[@]}"
