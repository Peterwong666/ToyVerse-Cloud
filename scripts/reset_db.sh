#!/usr/bin/env bash
#
# ToyVerse Cloud — 安全地重置开发库（P10 运维脚本）
#
# 为什么要有这个脚本（而不是直接用 make reset-db）
# ================================================
# 现有 `make reset-db` 是「`rm -f data/*.db` + 迁移 + 种子」三步直达，
# 对日常开发够用，但有两个在演示前夜很致命的性质：
#
#   1. 删库前**不做备份**。演示数据一旦被误删，只能从头再拼一遍；
#   2. 唯一的保护是 `sleep 5`。它只防「手快」，不防「手滑」——
#      五秒过后不看屏幕也会照删，而且它无法在 CI 里被显式跳过。
#
# 因此这个脚本的三条原则是：
#   * **先备份再问**，备份不成则中止（绝不出现「删了但没留底」的中间态）；
#   * **交互式二次确认**，必须逐字输入 `yes`；自动化场景用 `--yes` 显式表态；
#   * **复用既有入口**（alembic / seed_demo.py），不在脚本里重写迁移或种子逻辑，
#     否则脚本与线上初始化行为会各自漂移。
#
# 用法::
#
#     bash scripts/reset_db.sh          # 交互式，需输入 yes
#     bash scripts/reset_db.sh --yes    # 跳过确认（供 CI / 自动化）
#     bash scripts/reset_db.sh --help
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKEND_DIR="${REPO_ROOT}/backend"
VENV_DIR="${BACKEND_DIR}/.venv"
PY="${VENV_DIR}/bin/python"
DATA_DIR="${REPO_ROOT}/data"
BACKUP_DIR="${DATA_DIR}/backups"

ASSUME_YES=0

usage() {
    cat <<'EOF'
安全地重置开发库：先备份 → 二次确认 → 删库 → 迁移 → 写入种子数据

用法：
  bash scripts/reset_db.sh [--yes|-y]

选项：
  -y, --yes   跳过交互式确认（供 CI / 自动化使用）
  -h, --help  显示本帮助

说明：
  * 现有 data/*.db 与 data/*.sqlite3 会先复制到 data/backups/<YYYYmmdd-HHMMSS>/
  * 备份失败时脚本中止，不会删除任何数据库文件
  * 重置后请用 .env 中的管理员账号登录（首次登录需改密）
EOF
}

while (( $# > 0 )); do
    case "$1" in
        -y | --yes)
            ASSUME_YES=1
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

echo "===================================================================="
echo "ToyVerse Cloud — 重置开发库"
echo "===================================================================="
echo "仓库根目录: ${REPO_ROOT}"
echo "数据目录  : ${DATA_DIR}"
echo

# 虚拟环境是迁移与种子的执行环境。缺了它说明还没 make setup，
# 此时继续只会得到一堆 ModuleNotFoundError，不如提前说清楚该做什么。
if [[ ! -x "${PY}" ]]; then
    echo "✗ 未找到虚拟环境：${VENV_DIR}"
    echo "  请先执行 \`make setup\` 完成初始化。"
    exit 1
fi

# ---------------------------------------------------------------------------
# 第 1 步：备份
# ---------------------------------------------------------------------------
# 用 nullglob 而不是 `ls *.db`：没有匹配文件时 glob 展开为空数组，
# 不会把字面量 "data/*.db" 当成一个真实文件名传下去。
shopt -s nullglob
DB_FILES=("${DATA_DIR}"/*.db "${DATA_DIR}"/*.sqlite3)
shopt -u nullglob

if (( ${#DB_FILES[@]} == 0 )); then
    echo "→ 未发现数据库文件，跳过备份。"
else
    # 时间戳精确到秒：同一次操作的所有 DB 文件必须落在同一个目录里，
    # 否则「备份」就变成了散落在多个目录里的一堆文件，无法整体回滚。
    STAMP="$(date +%Y%m%d-%H%M%S)"
    TARGET_DIR="${BACKUP_DIR}/${STAMP}"
    mkdir -p "${TARGET_DIR}"

    echo "→ 备份 ${#DB_FILES[@]} 个数据库文件 ..."
    for db in "${DB_FILES[@]}"; do
        name="$(basename "${db}")"
        # 备份失败必须中止：带着「以为有备份」的错觉去删库是最坏的结局
        if ! cp -p "${db}" "${TARGET_DIR}/${name}"; then
            echo "✗ 备份失败：${db}"
            echo "  已中止，未删除任何数据库文件。"
            exit 1
        fi
        echo "    ✔ ${name}"
    done
    echo "  备份路径：${TARGET_DIR}"
fi
echo

# ---------------------------------------------------------------------------
# 第 2 步：二次确认
# ---------------------------------------------------------------------------
if (( ASSUME_YES == 0 )); then
    echo "⚠️  接下来会删除上述数据库并重建（数据不可恢复，只能从备份还原）。"
    printf "   请输入 yes 继续（其他任意输入取消）："
    # read 失败（如无 TTY / EOF）时不视为确认
    if ! read -r answer; then
        echo
        echo "✗ 无法读取输入，已取消。自动化场景请使用 --yes。"
        exit 1
    fi
    if [[ "${answer}" != "yes" ]]; then
        # 以退出码 1 结束：让 `reset_db.sh && ...` 这类串联命令停下来，
        # 避免下游步骤把「取消」误当成「重置完成」。
        echo "✗ 已取消，未做任何修改。"
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# 第 3 步：删库
# ---------------------------------------------------------------------------
# 只删数据库文件本身：data/ 下还有 storage/（对象存储）与 backups/（刚写的备份），
# 一旦用 `rm -rf data` 会把备份连同业务文件一起抹掉。
shopt -s nullglob
DB_FILES=("${DATA_DIR}"/*.db "${DATA_DIR}"/*.sqlite3)
shopt -u nullglob

if (( ${#DB_FILES[@]} == 0 )); then
    echo "→ 未发现数据库文件，跳过删除。"
else
    for db in "${DB_FILES[@]}"; do
        rm -f -- "${db}"
        echo "→ 已删除 $(basename "${db}")"
    done
fi
echo

# ---------------------------------------------------------------------------
# 第 4 步：迁移 + 种子数据（复用既有入口）
# ---------------------------------------------------------------------------
# alembic.ini 在 backend/ 下，必须在那个目录执行；-m alembic 走的是项目自己的
# 迁移环境与配置，与 `make migrate` 完全同一条路径。
echo "→ 执行数据库迁移（alembic upgrade head）..."
( cd "${BACKEND_DIR}" && "${PY}" -m alembic upgrade head )
echo

echo "→ 写入种子数据（scripts/seed_demo.py）..."
( cd "${REPO_ROOT}" && "${PY}" scripts/seed_demo.py )
echo

echo "===================================================================="
echo "✔ 数据库已重置为纯净演示态。"
echo "  演示账号与密码见 .env（首次登录需修改密码）。"
echo "===================================================================="
