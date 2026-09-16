#!/usr/bin/env bash
# ============================================================
# ToyVerse Cloud — 全端点冒烟测试
#
#   make smoke                                  （默认 http://localhost:8000）
#   SMOKE_BASE_URL=http://localhost:8080 bash scripts/smoke_test.sh   （经 Nginx）
#
# 这个脚本要回答的问题
# --------------------
# 「服务起来了，但它**真的能用**吗？」——健康检查返回 200 只说明进程活着，
# 不代表路由挂上了、鉴权链通了、种子数据在库里。因此这里按**真实角色**
# 登录，再用**真实资源**去请求每一个读端点。
#
# 与其它检查的分工
# ----------------
# * `make openapi-check`  —— 契约快照与代码一致（**路由是否被注册**）
# * `make test`           —— 全量自动化测试（含写路径与边界）
# * 本脚本（`make smoke`）—— **部署产物**的功能冒烟：镜像里的迁移是否执行、
#   `.env` 是否被读到、四端静态资源是否可访问、真实服务上的读写是否通。
#   它是「部署完之后」的那道闸门，因此刻意**不依赖测试夹具**，只依赖演示数据。
#
# 为什么只做**只读**请求（+ 极少数幂等写）
# ---------------------------------------
# 冒烟会在演示库上反复执行，写操作会留下痕迹（订单、设备、分配单），
# 让「重置一次演示环境」变成必需步骤。因此这里只读，例外只有两个明确幂等的动作：
#   * `POST /merchant/metrics/rebuild` —— 按唯一键 upsert，重复执行结果相同
#   * `POST /miniapp/auth/code`        —— 只刷新验证码字段
# 需要写路径的验证请走 `make test`（隔离的测试库）。
#
# 退出码：0 = 全部通过；1 = 有失败（可直接用于 CI 与 `make smoke`）
# ============================================================

set -uo pipefail

# 刻意**不**用 `set -e`：单个端点失败时还要继续把剩下的跑完，
# 最后统一汇总——否则排查一次冒烟要重跑十几遍。

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ------------------------------------------------------------
# 配置
# ------------------------------------------------------------

APP_PORT_DEFAULT="$(grep -E '^APP_PORT=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)"
BASE_URL="${SMOKE_BASE_URL:-http://localhost:${APP_PORT_DEFAULT:-8000}}"
API="${BASE_URL}/api/v1"

# 请求体临时文件（避免命令替换里嵌套引号）
BODY_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE"' EXIT

PASS=0
FAIL=0
SKIP=0
FAILED_LABELS=()

C_GREEN=$'\033[32m'; C_RED=$'\033[31m'; C_YELLOW=$'\033[33m'; C_DIM=$'\033[2m'; C_RESET=$'\033[0m'

# ------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------

# 从 .env 取一个变量的值。
#
# 为什么用 cut 而不是 source：口令里常含 `$`、`!`、反引号，source 会做词展开
# 与命令替换（`$x` 变空、`$(...)` 被执行）。cut 是纯文本切分，不解释内容。
env_value() {
  local key="$1"
  [ -f .env ] || return 0
  grep -E "^${key}=" .env | head -1 | cut -d= -f2-
}

# 发一个请求，把响应体写进 $BODY_FILE，返回 HTTP 状态码。
#
# 参数：method path [token] [json_body]
req() {
  local method="$1" path="$2" token="${3:-}" data="${4:-}"
  local -a args=(-s -o "$BODY_FILE" -w '%{http_code}' -X "$method" "${API}${path}")
  [ -n "$token" ] && args+=(-H "Authorization: Bearer ${token}")
  if [ -n "$data" ]; then
    args+=(-H 'Content-Type: application/json' -d "$data")
  fi
  curl --max-time 20 "${args[@]}" 2>/dev/null || echo "000"
}

# 从响应体里按点号路径取字段（如 data.accessToken）。
#
# ⚠️ 这里**必须**用 `python3 -c` 而不是 heredoc：
# `python3 - <<'PY'` 让解释器从 stdin 读程序，而响应体也要从 stdin 读——
# 两者争用同一个文件描述符，`sys.stdin.read()` 只会拿到空字符串，
# 于是所有取值都返回空、"登录失败"却查不出原因（本脚本踩过这个坑：
# 同一口令用 curl 直连成功，跑脚本却报「未取到 accessToken」）。
json_get() {
  local path="$1"
  python3 -c '
import json, sys
try:
    cur = json.load(sys.stdin)
except Exception:
    print("")
    raise SystemExit
for part in sys.argv[1].split("."):
    if not part:
        break
    if isinstance(cur, list):
        try:
            cur = cur[int(part)]
        except Exception:
            print("")
            raise SystemExit
    elif isinstance(cur, dict):
        cur = cur.get(part)
    else:
        print("")
        raise SystemExit
print("" if cur is None else cur)
' "$path" <"$BODY_FILE" 2>/dev/null || true
}


# ------------------------------------------------------------
# 断言与计数
#
# 三个计数器是这套冒烟脚本的「成绩单」。注意退出条件还额外要求
# **断言总数下限**（见文件末尾）：否则脚本自身坏掉（函数名写错、
# 计数器不增）时会打印「0 通过 / 全部通过」——一个会静默通过的检查
# 比没有检查更危险。（本脚本确实踩过一次：误删计数函数后 0 项断言仍报成功。）
# ------------------------------------------------------------

ok()   { PASS=$((PASS + 1)); printf '  %s✔%s %-58s %s\n' "$C_GREEN" "$C_RESET" "$1" "${2:-}"; }
bad()  { FAIL=$((FAIL + 1)); FAILED_LABELS+=("$1"); printf '  %s✘%s %-58s %s\n' "$C_RED" "$C_RESET" "$1" "${2:-}"; }
skip() { SKIP=$((SKIP + 1)); printf '  %s○%s %-58s %s\n' "$C_YELLOW" "$C_RESET" "$1" "${2:-}"; }


# 断言状态码等于期望值
expect() {
  local label="$1" want="$2" got="$3"
  if [ "$got" = "$want" ]; then
    ok "$label" "$got"
  else
    bad "$label" "期望 $want，实际 $got $(head -c 160 "$BODY_FILE" 2>/dev/null)"
  fi
}

# 断言状态码属于给定集合（用于「200 或 404 都算通过」这类端点）
expect_in() {
  local label="$1" want_list="$2" got="$3"
  local want
  for want in $want_list; do
    if [ "$got" = "$want" ]; then ok "$label" "$got"; return; fi
  done
  bad "$label" "期望 {$want_list}，实际 $got $(head -c 160 "$BODY_FILE" 2>/dev/null)"
}

section() { printf '\n%s── %s %s\n' "$C_DIM" "$1" "$C_RESET"; }

# ------------------------------------------------------------
# 0) 前置：服务可达性与 .env
# ------------------------------------------------------------

printf '\n%sToyVerse Cloud 冒烟测试%s  →  %s\n' "$C_DIM" "$C_RESET" "$BASE_URL"

section "健康检查与静态资源"

CODE="$(curl -s -o "$BODY_FILE" -w '%{http_code}' --max-time 10 "${API}/health" || echo 000)"
if [ "$CODE" = "000" ]; then
  printf '\n%s服务不可达：%s%s\n' "$C_RED" "$BASE_URL" "$C_RESET"
  printf '请先启动服务：\n  make dev            （本地开发）\n  make up             （Docker）\n' 
  exit 1
fi
expect "GET /health" 200 "$CODE"

CODE="$(curl -s -o "$BODY_FILE" -w '%{http_code}' --max-time 10 "${API}/health/ready" || echo 000)"
expect "GET /health/ready（含数据库连通）" 200 "$CODE"

# 四端入口与共享模块：只验证**可访问 + 是 HTML/JS**，不验证渲染（那是浏览器验收的事）
for entry in platform merchant factory miniapp; do
  CODE="$(curl -s -o "$BODY_FILE" -w '%{http_code}' --max-time 10 "${BASE_URL}/${entry}/" || echo 000)"
  expect "GET /${entry}/（前端入口）" 200 "$CODE"
done
CODE="$(curl -s -o "$BODY_FILE" -w '%{http_code}' --max-time 10 "${BASE_URL}/shared/core/api.js" || echo 000)"
expect "GET /shared/core/api.js（ES Module 原样提供）" 200 "$CODE"
CODE="$(curl -s -o "$BODY_FILE" -w '%{http_code}' --max-time 10 "${BASE_URL}/login" || echo 000)"
expect "GET /login（统一登录页）" 200 "$CODE"

# ------------------------------------------------------------
# 1) 三端登录
# ------------------------------------------------------------

section "鉴权：三端登录"

PLATFORM_ACCOUNT="$(env_value PLATFORM_ADMIN_ACCOUNT)"
PLATFORM_PASSWORD="$(env_value PLATFORM_ADMIN_PASSWORD)"
MERCHANT_ACCOUNT="$(env_value MERCHANT_ADMIN_ACCOUNT)"
MERCHANT_PASSWORD="$(env_value MERCHANT_ADMIN_PASSWORD)"
FACTORY_ACCOUNT="$(env_value FACTORY_ADMIN_ACCOUNT)"
FACTORY_PASSWORD="$(env_value FACTORY_ADMIN_PASSWORD)"

login() {  # account password -> 打印 accessToken
  local account="$1" password="$2"
  req POST /auth/login "" "$(python3 -c 'import json,sys;print(json.dumps({"account":sys.argv[1],"password":sys.argv[2]}))' "$account" "$password")" >/dev/null
  json_get accessToken
}

PLATFORM_TOKEN="$(login "$PLATFORM_ACCOUNT" "$PLATFORM_PASSWORD")"
if [ -n "$PLATFORM_TOKEN" ]; then ok "平台超管登录（${PLATFORM_ACCOUNT}）"; else bad "平台超管登录" "未取到 accessToken（检查 .env 口令是否为强口令）"; fi

MERCHANT_TOKEN="$(login "$MERCHANT_ACCOUNT" "$MERCHANT_PASSWORD")"
if [ -n "$MERCHANT_TOKEN" ]; then ok "商户管理员登录（${MERCHANT_ACCOUNT}）"; else bad "商户管理员登录" "未取到 accessToken"; fi

FACTORY_TOKEN="$(login "$FACTORY_ACCOUNT" "$FACTORY_PASSWORD")"
if [ -n "$FACTORY_TOKEN" ]; then ok "工厂管理员登录（${FACTORY_ACCOUNT}）"; else bad "工厂管理员登录" "未取到 accessToken"; fi

if [ -z "$PLATFORM_TOKEN" ]; then
  printf '\n%s平台登录失败，后续依赖平台 token 的检查无法进行，提前结束。%s\n' "$C_RED" "$C_RESET"
  printf '提示：首次部署请确认 .env 中的管理员口令已设置为强口令（启动日志会逐项列出问题）。\n'
  exit 1
fi

# 无令牌访问必须被拒（防止「忘记挂鉴权依赖」这类回归）
CODE="$(req GET /platform/devices "" )"
expect "未携带令牌访问受保护端点 → 401" 401 "$CODE"

# ------------------------------------------------------------
# 2) 平台端（只读）
# ------------------------------------------------------------

section "平台端：只读端点"

for path in /platform/tenants /platform/clouds /platform/templates /platform/client-products \
            /platform/orders /platform/devices /platform/batches /platform/allocations \
            /platform/factory-orders /platform/factories /platform/content-items; do
  CODE="$(req GET "$path?pageSize=5" "$PLATFORM_TOKEN")"
  expect "GET ${path}" 200 "$CODE"
done

CODE="$(req GET "/platform/devices/stats" "$PLATFORM_TOKEN")"
expect "GET /platform/devices/stats（四维统计）" 200 "$CODE"

# 取一个真实租户 ID，用于带路径参数的端点
TENANT_ID=""
req GET "/platform/tenants?pageSize=1" "$PLATFORM_TOKEN" >/dev/null && TENANT_ID="$(json_get records.0.id)"
if [ -n "$TENANT_ID" ]; then
  CODE="$(req GET "/platform/tenants/${TENANT_ID}" "$PLATFORM_TOKEN")"
  expect "GET /platform/tenants/{id}" 200 "$CODE"
else
  skip "GET /platform/tenants/{id}" "库里没有租户（先 make seed）"
fi

DEVICE_ID=""
req GET "/platform/devices?pageSize=1" "$PLATFORM_TOKEN" >/dev/null && DEVICE_ID="$(json_get records.0.id)"
if [ -n "$DEVICE_ID" ]; then
  CODE="$(req GET "/platform/devices/${DEVICE_ID}" "$PLATFORM_TOKEN")"
  expect "GET /platform/devices/{id}" 200 "$CODE"
  CODE="$(req GET "/platform/devices/${DEVICE_ID}/events" "$PLATFORM_TOKEN")"
  expect "GET /platform/devices/{id}/events（时间线）" 200 "$CODE"
else
  skip "GET /platform/devices/{id}" "库里没有设备（先 make seed）"
fi

ORDER_ID=""
req GET "/platform/orders?pageSize=1" "$PLATFORM_TOKEN" >/dev/null && ORDER_ID="$(json_get records.0.id)"
if [ -n "$ORDER_ID" ]; then
  CODE="$(req GET "/platform/orders/${ORDER_ID}" "$PLATFORM_TOKEN")"
  expect "GET /platform/orders/{id}" 200 "$CODE"
  CODE="$(req GET "/platform/orders/${ORDER_ID}/qrcodes" "$PLATFORM_TOKEN")"
  expect "GET /platform/orders/{id}/qrcodes" 200 "$CODE"
else
  skip "GET /platform/orders/{id}" "库里没有订单（先 make seed）"
fi

PACKAGE_ID=""
req GET "/platform/ota/packages?pageSize=1" "$PLATFORM_TOKEN" >/dev/null && PACKAGE_ID="$(json_get records.0.id)"
CODE="$(req GET "/platform/ota/records?pageSize=5" "$PLATFORM_TOKEN")"
expect "GET /platform/ota/records（推送记录）" 200 "$CODE"

# ------------------------------------------------------------
# 3) 商户端（只读 + 一个幂等写）
# ------------------------------------------------------------

section "商户端：只读端点与运营快照"

if [ -n "$MERCHANT_TOKEN" ]; then
  for path in /merchant/products /merchant/orders /merchant/devices /merchant/bindings \
              /merchant/knowledge-bases /merchant/content-items /merchant/ai/providers \
              /merchant/role-presets /merchant/voice-profiles; do
    CODE="$(req GET "$path?pageSize=5" "$MERCHANT_TOKEN")"
    expect "GET ${path}" 200 "$CODE"
  done

  PRODUCT_ID=""
  req GET "/merchant/products?pageSize=1" "$MERCHANT_TOKEN" >/dev/null && PRODUCT_ID="$(json_get records.0.id)"
  if [ -n "$PRODUCT_ID" ]; then
    for path in /products/${PRODUCT_ID} /products/${PRODUCT_ID}/ai-config; do
      CODE="$(req GET "/merchant${path}" "$MERCHANT_TOKEN")"
      expect "GET /merchant${path}" 200 "$CODE"
    done

    # 运营指标：概览是实时聚合、趋势读快照，两者都要能取到
    CODE="$(req GET "/merchant/metrics/overview?productId=${PRODUCT_ID}" "$MERCHANT_TOKEN")"
    expect "GET /merchant/metrics/overview（实时聚合）" 200 "$CODE"
    CODE="$(req GET "/merchant/metrics/trend?productId=${PRODUCT_ID}" "$MERCHANT_TOKEN")"
    expect "GET /merchant/metrics/trend（读快照）" 200 "$CODE"
    CODE="$(req GET "/merchant/metrics/retention?productId=${PRODUCT_ID}" "$MERCHANT_TOKEN")"
    expect "GET /merchant/metrics/retention" 200 "$CODE"

    # 幂等写：重建快照（按唯一键 upsert，重复执行结果相同）
    CODE="$(req POST "/merchant/metrics/rebuild" "$MERCHANT_TOKEN" \
      "$(python3 -c 'import json,sys;print(json.dumps({"productId":sys.argv[1],"dateFrom":sys.argv[2],"dateTo":sys.argv[2]}))' \
        "$PRODUCT_ID" "$(date -u +%F)")")"
    expect "POST /merchant/metrics/rebuild（幂等 upsert）" 200 "$CODE"

    # 越权红线：平台口令**不能**读商户端指标（角色守卫先于作用域）
    CODE="$(req GET "/merchant/metrics/overview?productId=${PRODUCT_ID}" "$PLATFORM_TOKEN")"
    expect "平台 token 访问商户端端点 → 403" 403 "$CODE"
  else
    skip "商户端产品相关端点" "库里没有客户产品（先 make seed）"
  fi
else
  skip "商户端全部检查" "商户登录失败"
fi

# ------------------------------------------------------------
# 4) 工厂端（只读）
# ------------------------------------------------------------

section "工厂端：只读端点"

if [ -n "$FACTORY_TOKEN" ]; then
  for path in /factory/orders /factory/inspections /factory/firmwares /factory/stats; do
    CODE="$(req GET "$path?pageSize=5" "$FACTORY_TOKEN")"
    expect "GET ${path}" 200 "$CODE"
  done

  WORK_ORDER_ID=""
  req GET "/factory/orders?pageSize=1" "$FACTORY_TOKEN" >/dev/null && WORK_ORDER_ID="$(json_get records.0.id)"
  if [ -n "$WORK_ORDER_ID" ]; then
    CODE="$(req GET "/factory/orders/${WORK_ORDER_ID}" "$FACTORY_TOKEN")"
    expect "GET /factory/orders/{id}" 200 "$CODE"
    CODE="$(req GET "/factory/orders/${WORK_ORDER_ID}/qrcodes" "$FACTORY_TOKEN")"
    expect "GET /factory/orders/{id}/qrcodes（贴码清单）" 200 "$CODE"
  else
    skip "GET /factory/orders/{id}" "本厂暂无工单"
  fi

  # 权限边界：工厂 token 不得访问平台端点
  CODE="$(req GET "/platform/factories" "$FACTORY_TOKEN")"
  expect "工厂 token 访问平台端端点 → 403" 403 "$CODE"
else
  skip "工厂端全部检查" "工厂登录失败"
fi

# ------------------------------------------------------------
# 5) 小程序端（终端用户）
# ------------------------------------------------------------

section "小程序端：终端用户链路"

SMS_PROVIDER="$(env_value MINIAPP_SMS_PROVIDER)"
SMS_PROVIDER="${SMS_PROVIDER:-mock}"

if [ "$SMS_PROVIDER" = "mock" ]; then
  SMOKE_PHONE="13900000009"
  CODE="$(req POST /miniapp/auth/code "" "$(python3 -c 'import json,sys;print(json.dumps({"phone":sys.argv[1]}))' "$SMOKE_PHONE")")"
  expect "POST /miniapp/auth/code（mock 通道）" 200 "$CODE"
  MOCK_CODE="$(json_get mockCode)"

  if [ -n "$MOCK_CODE" ]; then
    CODE="$(req POST /miniapp/auth/login "" \
      "$(python3 -c 'import json,sys;print(json.dumps({"phone":sys.argv[1],"code":sys.argv[2]}))' "$SMOKE_PHONE" "$MOCK_CODE")")"
    expect "POST /miniapp/auth/login" 200 "$CODE"
    END_USER_TOKEN="$(json_get accessToken)"

    if [ -n "$END_USER_TOKEN" ]; then
      for path in /miniapp/profile /miniapp/devices; do
        CODE="$(req GET "$path" "$END_USER_TOKEN")"
        expect "GET ${path}" 200 "$CODE"
      done

      # 令牌类型隔离：终端用户令牌不得访问管理端
      CODE="$(req GET "/platform/devices" "$END_USER_TOKEN")"
      expect "终端用户 token 访问管理端 → 401" 401 "$CODE"

      # 4G 演示设备的 JX 载荷由服务端生成（避免脚本里复制一份格式规则）；
      # 设备可能不存在（未 seed 或已被解绑），因此允许 404
      CODE="$(req POST /miniapp/scan/resolve "$END_USER_TOKEN" \
        "$(python3 -c 'import json;print(json.dumps({"payload":"JX|SN-DEMO-4G-001|866000000000001|8986000000000000001|jx-demo-device-001"}))')")"
      expect_in "POST /miniapp/scan/resolve（扫码解析）" "200 404" "$CODE"
    else
      skip "小程序端后续检查" "登录未返回 accessToken"
    fi
  else
    skip "小程序端后续检查" "验证码未回显"
  fi
else
  # 生产环境禁用 mock 短信通道（启动期已强制），此时发码接口应当**安全失败**
  CODE="$(req POST /miniapp/auth/code "" '{"phone":"13900000009"}')"
  expect_in "POST /miniapp/auth/code（非 mock 通道 → 安全失败 503）" "503" "$CODE"
  skip "小程序端登录链路" "MINIAPP_SMS_PROVIDER=${SMS_PROVIDER}，未配置真实短信通道"
fi

# ------------------------------------------------------------
# 汇总
# ------------------------------------------------------------

TOTAL=$((PASS + FAIL))

# ★ 最低断言数守卫：脚本自身故障（函数缺失、计数器不增）时会得到 0 项断言，
# 那种情况下打印「全部通过」是**假阳性**——而冒烟是部署后的最后一道闸门，
# 假阳性比漏报更危险。这里设一个远低于正常值（正常约 60+ 项）的下限。
MIN_EXPECTED_CHECKS=25
if [ "$TOTAL" -lt "$MIN_EXPECTED_CHECKS" ]; then
  printf '\n%s✘ 冒烟脚本异常：只执行了 %d 项断言（应 ≥ %d）——脚本自身可能出错，不能据此判定通过%s\n' \
    "$C_RED" "$TOTAL" "$MIN_EXPECTED_CHECKS" "$C_RESET"
  exit 1
fi

printf '\n%s──────────────────────────────────────────────────────────────%s\n' "$C_DIM" "$C_RESET"
printf '冒烟结果：%s%d 通过%s' "$C_GREEN" "$PASS" "$C_RESET"
[ "$FAIL" -gt 0 ] && printf ' / %s%d 失败%s' "$C_RED" "$FAIL" "$C_RESET"
[ "$SKIP" -gt 0 ] && printf ' / %s%d 跳过%s' "$C_YELLOW" "$SKIP" "$C_RESET"
printf '（共 %d 项断言）\n' "$TOTAL"

if [ "$FAIL" -gt 0 ]; then
  printf '\n失败清单：\n'
  for label in "${FAILED_LABELS[@]}"; do printf '  %s✘%s %s\n' "$C_RED" "$C_RESET" "$label"; done
  printf '\n排查建议：\n'
  printf '  1) 是否忘了迁移/种子：make migrate && make seed\n'
  printf '  2) 是否改了代码没重启：make dev（或 make up --build）\n'
  printf '  3) 服务端日志：/tmp/toyverse-uvicorn.log 或 make logs\n'
  exit 1
fi

printf '\n%s✔ 冒烟全部通过%s\n\n' "$C_GREEN" "$C_RESET"
exit 0
