# ToyVerse Cloud 项目任务清单（todolist）

> **项目**：多租户 AI 智能玩具 SaaS 平台
> **目录**：`/home/peter/py/saas_ai_toy_platform`
> **技术栈**：Python 3.12 + FastAPI + SQLAlchemy 2.0 + Pydantic v2 + Alembic + SQLite（默认）/ PostgreSQL（可选）；前端零构建 ES Module
> **创建日期**：2026-09-16
> **配套文件**：[`项目进度.md`](./项目进度.md)（实时进度追踪）

---

## 图例

| 标记 | 含义 |
|---|---|
| `[ ]` | 未开始 |
| `[~]` | 进行中 |
| `[x]` | 已完成 |
| `[!]` | 阻塞 |
| 🔑 | 关键任务，阻塞后续阶段 |
| ★ | 用户明确要求的交付物 |

---

## 背景：三个遗留项目的整合策略

三者本质不同，因此采用「各取所长」而非「代码合并」：

| 来源 | 本质 | 贡献内容 |
|---|---|---|
| 早期静态原型 | 纯静态可点击原型，**无后端无数据库** | UI/交互规范、领域模型、PRD、状态机、二维码规则、设计系统 |
| 早期实现的运行时快照 | 仅有运行时构建、无源码 | 数据库 Schema 结构（15 张表）、多租户 RBAC 设计、部署拓扑、菜单结构 |
| 早期可运行 MVP | Node + Python 单文件 | 业务闭环语义（幂等绑定、confirm-token、冻结校验、审计、180 秒在线窗口） |

**关键事实**：
1. 早期实现仅有运行时快照、源码已不可得，但其数据库迁移脚本可读，Schema 结构可完整复用。
2. **早期实现与原型中都没有任何 AI 实现**（`AI配置` 页是占位视图桩），因此 AI 部分是全新设计。
3. 早期可运行 MVP 存在静态文件路径穿越漏洞，**不移植**；只迁移其硬化后的语义。

---

## P0 脚手架与进度文件 ★

**目标**：建立可提交的仓库骨架，产出任务清单与进度文件。

- [x] 🔑 创建目录骨架（backend / frontend / deploy / scripts / docs / tests / learning）
- [x] 🔑 编写 `.gitignore`，**确认包含 `learning/`、`.env`、`data/`、`__pycache__/`、`*.jar`**
- [x] 🔑 编写 `.env.example`（十一大类变量，全含中文注释，无真实密钥）
- [x] 编写 `Makefile`（`help/setup/dev/test/migrate/seed/up/down` 等 30+ 命令）
- [x] 添加 `LICENSE`（MIT）
- [x] ★ 产出 `todolist.md`（本文件）
- [x] ★ 产出 `项目进度.md`
- [x] 编写 `README.md` 骨架（门面 + 快速开始 + 架构图占位）
- [x] 编写 `backend/requirements.txt` 与 `requirements-dev.txt`
- [x] 编写 `backend/pyproject.toml`（ruff / mypy / pytest 配置）
- [x] 编写 `docker-compose.yml` 草案
- [x] 编写 `CONTRIBUTING.md` / `SECURITY.md` / `CHANGELOG.md`
- [x] 🔑 `git init` 并首次提交
- [x] 验证：`git status` 干净；`git check-ignore learning/xxx` 返回命中；`make help` 正常输出

**验收**：仓库可提交，`learning/` 确认被忽略，`make help` 列出全部命令。

---

## P1 后端地基（配置 / 数据库 / 鉴权）★

**目标**：可登录、可鉴权、可测试的后端骨架。

- [x] 🔑 `app/core/config.py` — pydantic-settings 读取 `.env`，含启动期安全校验（弱密钥 / 空管理员密码则拒绝启动）
- [x] 🔑 `app/core/errors.py` — 25 个统一错误码枚举 + `AppException` 基类 + 便捷构造器 + FastAPI 异常处理器
- [x] 🔑 `app/core/logging.py` — 结构化日志 + **traceId 中间件**（纯 ASGI 实现，兼容 WebSocket 与流式响应）
- [x] 🔑 `app/core/permissions.py` — 权限码目录与 6 个内置角色的权限分配
- [x] 🔑 `app/db/base.py` — SQLAlchemy 2.0 `DeclarativeBase` + 命名约定 + `UTCDateTime` + 时间戳混入
- [x] 🔑 `app/db/session.py` — 异步引擎与会话工厂，SQLite 与 PostgreSQL 双适配 + 外键强制开启
- [x] 🔑 `app/db/scope.py` — **租户作用域收口**（架构红线 #1 的实现）
- [x] 🔑 `app/models/identity.py` — `tenants` `roles` `role_permissions` `user_accounts` `refresh_tokens`
- [x] 🔑 `app/models/org.py` — `organizations` `positions` `factories`
- [x] `app/models/audit.py` — `audit_logs` `outbox_events` `idempotency_keys`
- [x] `app/models/enums.py` — 领域枚举全集 + 订单/设备状态机迁移表 + 四维状态派生展示
- [x] 🔑 `app/core/security.py` — bcrypt 哈希、JWT 签发与校验（claims：`sub/role/tenantId/tenantCode/perms`）、refresh 轮换、密钥脱敏
- [x] 🔑 `app/core/deps.py` — `AuthContext` + `get_auth_context()` + `require_perm()` + `require_role()`
- [x] `app/core/idempotency.py` — `Idempotency-Key` 占位/完成/释放三段式
- [x] `app/core/pagination.py` — 统一分页参数与响应封装
- [x] `app/schemas/` — auth / common 的 Pydantic v2 模型
- [x] `app/services/auth_service.py` — 登录（失败计数 + 锁定）、刷新（含重放检测）、登出
- [x] `app/services/audit_service.py` — 审计写入（含 IP / UA / traceId 采集）
- [x] 🔑 `app/api/v1/{health,auth,router}.py` — `/health` `/health/ready` `/auth/login` `/auth/refresh` `/auth/logout` `/auth/tenants` `/me` `/me/password`
- [x] 🔑 `app/main.py` — 应用工厂、日志初始化、CORS、统一异常处理器、路由挂载、前端静态托管
- [x] 🔑 Alembic 0001（身份租户）/ 0002（组织工厂）/ 0003（用户账号）/ 0004（基础设施横切）
- [x] `app/db/seed.py` — 幂等种子数据（6 角色 / 2 演示租户 / 3 端管理员账号）
- [x] 🔑 `tests/conftest.py` — 测试库 fixture、鉴权 helper、数据工厂、用例间隔离
- [x] `tests/unit/test_security.py` — 密码哈希、密码强度、JWT 全场景、刷新令牌、密钥脱敏
- [x] `tests/unit/test_domain_rules.py` — 订单状态机、设备标签派生、错误码契约、分页契约
- [x] `tests/integration/test_auth.py` — 登录全场景 / 账号锁定 / 禁用租户(P-04) / 令牌轮换与重放 / 鉴权守卫 / 改密 / 登出 / 租户下拉不泄漏联系方式
- [x] `scripts/export_openapi.py` + `scripts/seed_demo.py`，并接入 `make openapi-check`

**验收**（2026-09-16 实测通过）：`make check` 全绿（ruff 0 问题 / mypy strict 36 文件无问题 / **118 个测试通过** / OpenAPI 契约一致）；`/auth/login` 返回 JWT；连续 5 次失败锁定 15 分钟；禁用租户登录被拒（**P-04 已修复**）；服务真实启动并完成种子数据写入。

---

## P2 前端地基（设计系统 / 路由 / 组件库）★

**目标**：四端可打开、可登录、路由守卫完备的全新视觉前端。

- [x] 🔑 `frontend/shared/design/tokens.css` — **全新视觉 token**（靛紫 `#4F46E5` 主色，替代早期实现的 Ant 蓝 `#1677ff`；浅色侧导轨替代深色 `#001529`）
- [x] `frontend/shared/design/base.css` — 重置、排版、字体（Inter + Noto Sans SC，数值 `tabular-nums`）
- [x] `frontend/shared/design/layout.css` — 顶部品牌条 64px + 可折叠侧导轨（72/240px）+ 内容区
- [x] `frontend/shared/design/components.css` — 全组件样式
- [x] `frontend/shared/design/miniapp.css` — 375px 手机壳
- [x] 🔑 `frontend/shared/core/config.js` — API 基址与运行时配置
- [x] 🔑 `frontend/shared/core/api.js` — **契约先行**：注入 `Authorization` 与 `x-trace-id`、统一解析 `{code,message,traceId}`、401 自动刷新、错误 toast
- [x] 🔑 `frontend/shared/core/auth.js` — token 存取（含过期时间）、`hasPerm()`、`requireRole()`
- [x] 🔑 `frontend/shared/core/router.js` — **参数化 hash 路由** `#/tenant/:id`（修复 P-01）；`back()` 按角色决定目标（修复 P-02）
- [x] 🔑 `frontend/shared/core/guard.js` — 路由守卫（修复 P-05：未登录跳登录页、角色不符跳本端首页）
- [x] `frontend/shared/core/store.js` — 轻量 pub/sub 状态管理
- [x] `frontend/shared/core/ws.js` — 对话 WebSocket 客户端（自动重连 + SSE 降级）
- [x] `frontend/shared/ui/dom.js` — DOM 工具与安全转义（**避免 XSS**，修复原 MVP 的 `innerHTML` 注入面）
- [x] `frontend/shared/ui/components.js` — Button/Input/Select/Switch/Tag/StatusTag/Card/StatCard/Tabs/Steps/Timeline/Tree/Upload/EmptyState/Skeleton/Badge
- [x] `frontend/shared/ui/table.js` — 表格（排序 + 分页 + 空态 + 骨架屏）
- [x] `frontend/shared/ui/form.js` — 表单校验与提交
- [x] `frontend/shared/ui/modal.js` — Modal / Drawer / ConfirmDialog
- [x] `frontend/shared/ui/toast.js` — 通知
- [x] `frontend/shared/ui/chart.js` — **纯 SVG 图表**（柱 / 线 / 热力 / 环形）最小可用版
- [x] `frontend/shared/ui/qrcode.js` — Canvas 自绘二维码（替代原型的假 CSS 方块）
- [x] `frontend/shared/ui/command.js` — ⌘K 命令面板
- [x] 🔑 `frontend/index.html` — 统一登录页 + 角色入口（对接真实 `/auth/login`，**不再有「任意密码可登录」**）
- [x] 🔑 `frontend/platform/index.html` + `frontend/pages/platform/main.js` — 平台端布局壳 + 空工作台
- [x] `frontend/{merchant,factory,miniapp}/index.html` — 其余三端壳与各自 `main.js`

**验收**（2026-09-16 实测通过）：四端页面均可打开（HTTP 200，JS 以 `text/javascript` 提供，ES Module 可直接加载）；登录后进入对应端工作台；**刷新子页面状态不丢失**（修复 P-01）；未登录直访子页被守卫拦截（修复 P-05，`localhost` 与 `127.0.0.1` 跨 Origin 实测）；⌘K 命令面板可用。

**配套校验脚本**：`make fe-check`（77 处 ES Module 导入的路径与具名导出全部匹配）、`make qr-verify`（手写二维码编码器与 `qrcode` 库逐模块比对，104 组矩阵一致）。

**遗留观察**：本轮尚无页面使用路径参数（`#/tenants/t-001` 会回落到 `#/dashboard`），参数化能力已在 `router.js` 落地，将在 P3/P5 的详情页启用；工作台首屏 5 个统计接口返回 404 属预期（对应域尚未开发，页面已降级为「建设中」）。

---

## P3 目录域（云服务商 / 模板 / 授权 / 客户产品）★

> **迁移编号说明**：本节原计划使用 `0003` / `0004`，但 P1 落地时实际占用了 `0001`–`0004`，故顺延为 `0005` / `0006`（P4 起顺延为 `0008`–`0010`、P5 `0011`、P6 `0012`+`0013`；P9 顺延为 `0014`；P7 使用 `0007`）。

- [x] 🔑 Alembic `0005_catalog.py` — `cloud_providers` `product_templates` `product_authorizations`
- [x] 🔑 Alembic `0006_client_product_miniapp.py` — `client_products` `miniapp_configs`
- [x] `app/models/catalog.py` + `app/schemas/catalog.py`
- [x] `app/services/catalog_service.py`、`app/services/tenant_service.py`
- [x] 🔑 密钥加密：`cloud_providers.access_key_enc` / `secret_key_enc` 加密存储，**任何响应不含 SK**（新增 `app/core/crypto.py`，另含 `*_hint` 掩码字段供列表展示，无需解密）
- [x] `app/api/v1/platform.py` — 租户 CRUD + `/status` + `/account` + `/reset-password`（实现为 `/tenants/{id}/accounts` 与 `/tenants/{id}/reset-password`）
- [x] 平台端 `clouds` CRUD + `/test`（连通性检测；未配置密钥时返回 `NOT_CONFIGURED` 且跳过真实调用）
- [x] 平台端 `templates` CRUD + `/authorize`（授权租户，幂等）
- [x] 平台端 `client-products` CRUD（+ `/miniapp-config` upsert）
- [x] 删除级联校验（修复 P-06：`CASCADE_CONFLICT`，并返回各项关联数量）
- [x] 🔑 `frontend/pages/platform/{tenants,tenant_detail,templates,clouds,client_products,client_product_detail}.js`（较原计划增加 `client_products.js` 列表页）；另新增共用工具 `frontend/pages/platform/common.js`
- [x] `tests/integration/test_catalog.py` + `test_tenant_scope.py`
- [x] **断言测试：SecretKey 绝不出现在任何响应中**（递归扫描创建/列表/详情/更新/连通性检测全响应，并校验库内为密文）

**验收**（2026-09-16 实测通过）：建租户 → 建云服务商 → 建模板 → 授权 → 建客户产品 闭环跑通；SK 泄漏测试通过（明文哨兵在响应中出现 0 次）；级联删除校验生效（409 + 数量明细）。浏览器另复验 12 项，含**参数化路由刷新不丢状态**（解除 P2 遗留观察①）、**P-03 客户详情可见产品**、**一次性密码弹窗**、**ADR-07 未配置安全失败**。

**配套统计**：测试 118 → **251**；平台端 17 个端点；`make fe-check` 145 处导入；`alembic check` 零漂移。

---

## P4 订单与设备生成 ★

- [x] 🔑 Alembic `0008_orders.py` — `orders`
- [x] 🔑 Alembic `0009_devices_batches.py` — `device_batches` `devices` `device_batch_lines` `device_credentials` `device_events`
- [x] 🔑 Alembic `0010_factory_production.py` — `factory_orders` `burn_reports` `inspections`

> **与原计划的编号差异**：原计划把 `device_batches` 放在 `0010`，但 `devices.batch_id` 指向 `device_batches`——若批次表晚于设备表创建，外键就指向尚不存在的表。因此 `0009` 内部按**依赖方向**排序（批次 → 设备 → 批次明细 → 凭证 → 事件），`0010` 只承载工厂生产三表。
- [x] `app/models/{order,device,factory}.py` + 对应 schemas
- [x] 🔑 `app/services/qrcode_service.py` — **双格式生成与解析**
  - 集贤 4G：`JX|{SN}|{IMEI}|{ICCID}|{deviceId}`（恒 5 段，空字段保留空段）
  - 京东 Wi-Fi：`JD|{tenant_id}|{product_id}|{sn}|{sign}`，`sign = HMAC-SHA256(f"{tenantId}|{productId}|{sn}", QR_SIGN_SECRET)` 小写 hex，校验用 `hmac.compare_digest`
  - **已修正原型 `data.js:595` 把 `clientId` 误传入 `tenant_id` 位置的缺陷**（并用单测钉死字段顺序）
- [x] 🔑 `app/services/order_service.py` — 订单状态机（`PENDING_AUDIT → APPROVED|REJECTED → GENERATING → GENERATED → IN_STOCK → PRODUCING → SHIPPED_TO_CLIENT → COMPLETED`，另有 `GENERATING → APPROVED` 回退边用于厂商失败；迁移一律经 `ORDER_TRANSITIONS` 校验）
- [x] 审核：`POST /platform/orders/{id}/audit`（仅 `PENDING_AUDIT` 可审核；驳回必须填原因）
- [x] 生成设备：`POST /platform/orders/{id}/generate`（JX 走 provider，**未配置密钥时 503 且绝不伪造成功**；JD 本地生成）
- [x] 设备入库、批次绑定
- [x] `GET /platform/orders/{id}/qrcodes` — 二维码/SN 导出（前端可导出 CSV）
- [x] 设备批次 CSV 导入（上传 → SHA-256 去重 → 预检 → 导入 → 进度 → 错误报告 → 幂等重跑）
- [x] 🔑 `frontend/pages/platform/{orders,order_detail,devices,batches}.js` + `frontend/pages/merchant/orders.js`（较原计划增加商户端下单页）
- [x] `tests/unit/test_order_state_machine.py` + `test_qrcode.py`（**格式断言**）+ `test_device_state_machine.py`（冻结语义与迁移表自洽，14 条）
- [x] `tests/integration/test_order_flow.py`（32 条）+ `test_batch_import.py`（24 条）
- [x] 商户端下单与查单：`POST/GET /merchant/orders`（带 `Idempotency-Key`）、`GET /merchant/products`（供下单选择产品）

**验收**（2026-09-16 实测通过）：`PENDING_AUDIT → APPROVED → GENERATED → IN_STOCK` 全通（浏览器实测：审核通过 → 生成设备 → 订单入库 + 3 台设备）；二维码两格式断言通过；CSV 导入产出错误行报告（`text/csv` + BOM + 行号原因）。`make check` 全绿（**397 passed**、mypy strict 69 文件、契约 51 路径一致）；`make fe-check` 195 处导入匹配；`alembic check` 零漂移。

**独立验证**：由AI Agent 产出 56 条集成用例（含租户隔离矩阵、幂等重放、批次安全限额、四维状态白名单），**发现 2 个真缺陷 + 4 条观察项并已全部修复**（见 `项目进度.md` 的 P4 验收记录）。

**已知局限**：① 工厂端端点属 P6（三张工厂表已建好）；② 设备详情页不在本阶段前端清单内，P5 补；③ `duplicatedRows` 是预检快照，口径修正不回溯历史批次；④ `COMPLETED` 订单再调生成接口为幂等回放（有心设计）。

---

## P5 设备 / 分配 / 绑定 ★

- [x] 🔑 Alembic `0011_allocations_bindings.py` — `allocation_orders` `allocation_items` `device_bindings`
  > **三表之外没有第 4 张表**：5 分钟 confirm-token 直接以 `device_bindings.status=PENDING` + `confirm_token_hash` 落在绑定行上，`bind` 成功时把摘要置空即「销毁」。这样「绑定意图」与「绑定事实」在同一行内完成生命周期，且 `UNIQUE(device_id)` 能把**单绑约束下沉到数据库层**（并发下也不依赖服务层的「先查后写」）。
> **以下三项已在 P4 提前交付**（设备表随 P4 落地，为不重复实现而一并完成）：四维状态模型与迁移服务、设备流转时间线（`device_events` + `GET /platform/devices/{id}/events`）、生命周期操作 `freeze` / `thaw` / `retire`。P5 只需在此基础上补「分配 → 绑定」链路。
> 注意：冻结范围已在 P4 收紧为**只允许 `IN_STOCK`**（与 `ASSET_TRANSITIONS[FROZEN]={IN_STOCK}` 对称，解冻才能原路恢复）；`activation_status` / `online_status` / `bind_status` 三条链路的**写入**仍待 P5 的激活与心跳实现。

- [x] 🔑 设备**四维状态**模型与状态迁移服务（P4 交付）
  - `asset_status`: `PENDING_GEN → GENERATED → IN_STOCK → PRODUCING → PRODUCED → SHIPPED → ALLOCATED → BOUND → RETIRED`，另有 `IN_STOCK ⇄ FROZEN`（`previous_asset_status` 记录）
  - `activation_status`: `NOT_ACTIVATED → ACTIVATING → ACTIVATED | BIND_FAILED`
  - `online_status`: `NEVER_ONLINE ⇄ ONLINE ⇄ OFFLINE`
  - `bind_status`: `UNBOUND ⇄ BOUND`
- [x] 设备流转时间线（`device_events`）与 `GET /platform/devices/{id}/events`（P4 交付）
- [x] 设备生命周期操作：`freeze` / `thaw` / `retire`（P4 交付，原因必填）
- [x] 分配单：创建 / 列表 / 详情 / 幂等执行（校验租户 ACTIVE、设备 IN_STOCK、产品已授权）
  - 逐行处理、**部分失败不整单回滚**（成功的行必须保留，否则重跑会重复分配）；`FAILED → EXECUTING` 允许重跑，`COMPLETED` 再次调用是**幂等回放**
- [x] 🔑 绑定流程：`precheck`（sha256 命中 + 5 分钟 confirm-token + 租户匹配）→ `bind`（幂等键、单绑约束、冻结拦截）
  - 「SHA-256 命中」= 用设备行**重算规范载荷**再比对摘要，因此无签名的集贤 `JX` 格式同样无法被篡改（`JX` 本身没有签名，只能靠这一层）
- [x] 解绑：`POST /merchant/devices/{id}/unbind`（原因必填，写审计与事件）
- [x] 心跳：`POST /device/heartbeat` + 180 秒在线窗口判定
  - 设备侧端点**不使用 JWT**：以 `sn` + 设备密钥（`SHA-256` 摘要比对）鉴权。为此新增 `POST /platform/devices/{id}/credentials` 签发 `DEVICE_SECRET`，明文仅回一次，续签覆盖同类型旧密钥——否则「任何人拿到 SN 就能把设备刷成在线」，而 SN 是印在机身与包装上的
  - 在线判定以 `last_heartbeat_at` + 180 秒窗口为**单一事实来源**；`online_status` 只是落库投影，列表/详情另给派生布尔 `online`，统计与筛选同样按窗口口径，避免「掉线了但枚举还写着 ONLINE」
- [x] 演示心跳：`POST /platform/devices/{id}/simulate-heartbeat`（响应明确 `simulated: true`，且 `online=false` 可模拟窗口超时）
- [x] 修复 P-04（登录校验租户状态）—— **P1 已修复，本阶段复核确认**（登录与刷新两条路径都校验 `tenant.status`，`tests/integration/test_auth.py` 已覆盖），未重复实现
- [x] `frontend/pages/platform/{devices,device_detail,allocations}.js`、`frontend/pages/merchant/{devices,bindings}.js`
  - `devices.js` 为改造：修掉 P4 两处遗留（`FREEZABLE` 过期集合含 `GENERATED`、「详情」按钮只给提示不跳转），新增「在线」列与「模拟心跳」
  - 商户端设备详情实现为弹窗（本阶段路由清单未含商户端设备详情页）
- [x] 🔑 `tests/integration/test_tenant_isolation.py` — **租户隔离参数化矩阵（每端点一条）**
- [x] `tests/integration/test_binding.py` — 幂等重放、confirm-token 过期、冻结拦截
- [x] 另补 `tests/integration/test_allocation.py`、`tests/integration/test_device_heartbeat.py`

**验收**：移植 `server.js` 全部硬化语义（confirm-token 过期与销毁、冻结校验、授权校验、幂等重放返回同一条）；租户隔离测试全绿。
👉 实测结论见 `项目进度.md` 的「P5 验收记录」。

### 已知局限

1. **「冻结已分配/已绑定设备」的能力缺失** —— **P6 已修复**（用户决策放宽
   `FREEZABLE_ASSET_STATUSES` 为 `{IN_STOCK, ALLOCATED, BOUND}`，并补齐
   `ALLOCATED`/`BOUND` → `FROZEN` 的入边；P4 钉死的单测已同步更新）。
2. **`GENERATED → IN_STOCK`（「入库」动作）仍无端点** —— **P6 已修复**
   （`POST /platform/devices/stock-in`，按设备列表、逐行判定、幂等重跑，
   平台端设备页提供行内「入库」与「批量入库」）。
3. `end_user_id` 只存不建外键（`end_users` 属 P9），与 P7 的
   `dialogue_sessions.device_id` 同一处理方式。
4. 元素截图通道在验收后半段超时（`evaluate` 正常），部分界面修复只有 DOM
   证据与一张修复前截图，未逐张留图。

---

## P6 烧录工厂端 ★

- [x] `app/api/v1/factory.py` — 生产订单 / 烧录上报 / 抽检 / 固件
- [x] 🔑 `FactoryOrderSerializer` — **服务端字段白名单脱敏**
  - 客户名脱敏：首个文字字符 + 星号 + 末个文字字符（`中国移动` → `中**动`；≤2 字则首字 + 星）
  - **金额、联系方式、邮箱绝不出现在任何工厂端响应**
  - 脱敏的是「首个/末个**文字字符**」而不是「第一个/最后一个字符」：`星辰玩具（演示租户）`
    若按字面首尾会脱敏成 `星********）`（括号占据可见位，看起来像坏数据，还漏出「后面跟着括号备注」）
- [x] 平台端派单：`POST /platform/orders/{id}/dispatch`（同一订单只能派一次；工单数量 = **实际派工台数**，合同台数另以 `orderQuantity` 下发）
- [x] 工厂端生产订单列表 / 详情（含烧录记录与抽检统计）
- [x] 烧录上报：`burned_count ≤ quantity` 校验（`BURN_COUNT_EXCEEDED`，`details` 带 `remaining`）
- [x] 抽检：不存在 SN 报 404；**不属于本工单**的 SN 报 400（判据是 `devices.factory_order_id`）；抽检记录落库
- [x] 出货登记：工单 `COMPLETED → SHIPPED`，设备 `PRODUCED → SHIPPED`
- [x] 固件版本列表（按本厂工单聚合）与工单二维码清单导出（**只含本工单认领的设备**）
- [x] 🔑 设备四维与订单状态联动：派单 → 烧录满额 → 出货，全部经 `transition_asset` / `transition_order` 唯一入口
- [x] 🔑 迁移 `0012_factory_account_link`（`user_accounts.factory_id` + JWT `factoryId`）与 `0013_device_factory_order`（`devices.factory_order_id`）
- [x] 🔑 **P5 遗留②**：`POST /platform/devices/stock-in` 入库端点（逐行判定 + 幂等）
- [x] 🔑 **P5 遗留①**：放宽冻结范围到「库存 / 已分配 / 已绑定」，前端集合同步
- [x] 修复历史测试笔误：`_transition_order` 重命名为公开 `transition_order`（工厂环节需在**同一处**校验订单状态机）
- [x] 🔑 `frontend/pages/factory/{dashboard,orders,order_detail,burn,inspect,firmware}.js`（6 页）+ 平台端 `factory_orders.js`
- [x] 平台端订单详情「派单给工厂」（仅 `IN_STOCK` 时出现）+ 设备页「入库 / 批量入库」
- [x] `tests/integration/test_factory_desensitize.py` — **断言响应中不含真实客户名 / 金额 / 电话 / 邮箱**
- [x] `tests/unit/test_mask.py` — 脱敏函数边界（1 字 / 2 字 / 多字 / 英文 / 空值 / None / 纯符号 / 括号与空格）+ 白名单一致性
- [x] `tests/integration/test_factory_flow.py` — 派单→烧录→抽检→出货全链路、跨厂隔离矩阵、归属与二维码范围
- [x] 补齐 P5 欠账测试：`test_allocation.py`、`test_tenant_isolation.py`（全端点参数化矩阵 + 路由元测试）、`test_device_heartbeat.py`

**验收**（2026-09-17 实测通过）：真实服务端到端接口验收 **71/71 通过**（派单 → 烧录 → 抽检 → 出货 → 入库 → 分配 → 冻结）；工厂端脱敏红线全部命中（真实客户名 / 联系人 / 电话 / 邮箱在响应中出现 0 次，`unitPrice`/`totalAmount`/`applicantPhone` 等字段名不存在）；跨厂隔离全部 404；`make check` 全绿（**737 passed**、mypy 81 文件、契约 88 路径一致）；`make fe-check` 287 处导入匹配；`alembic check` 零漂移。浏览器实测另复验 12 项（见 `项目进度.md` 的 P6 验收记录）。

**本阶段由端到端验收发现并修复的 3 个真缺陷**：① 工单二维码清单返回订单下**全部**设备（含被冻结 / 已分配的），工厂会多打标签 → 新增 `devices.factory_order_id` 落库归属，清单 / 抽检范围一律以它为准；② 抽检只比对 `order_id`，同订单但**未派工**的设备也能被抽检并计入合格率 → 改用 `factory_order_id`；③ 平台端设备页的 `FREEZABLE` 集合仍是 P5 的 `['IN_STOCK']`，导致后端已允许的「冻结已分配设备」在界面上**点不到** → 集合对齐 + 弹窗文案改写。
另由独立验证代理报出的 1 处口径缺口（工单数量取订单数量导致 `burned_count` 与实际出货设备数对不上）与 1 处不可达错误码（`FACTORY_ORDER_EXISTS` 被状态校验抢先拦下）均已修复。

**已知局限**：
1. **工厂端「批次查询」页未交付**（菜单项显示「建设中」）：`factory:batch:read` 权限与 `/platform/batches` 端点已存在，但未加 `/factory/batches` 端点与页面。工厂当前不需要批次，留待 P9/P10 一并决策。
2. **工单与订单是「一对一」**：`dispatch_order` 拒绝同一订单的第二张工单（`FACTORY_ORDER_EXISTS`）。因此「同一订单分批派给两家工厂」不可用，且被冻结设备造成的差额（合同 5 台 / 派工 3 台）无法通过二次派单补齐——那台设备只能解冻后走**分配单**回到客户名下。
3. **`factory_orders.factory_order_no` 有唯一索引而 `order_id` 没有**：一单一张工单是**服务层**约束而非数据库约束，并发下理论上可能派两次。若要收紧需补一次迁移加 `UNIQUE(order_id)`。
4. 迁移 `0013` **不回填**历史设备的 `factory_order_id`（回填只能靠「订单 + 状态」反推，而那个推断正是它要消除的不可靠来源）。因此旧数据里的设备抽检会被拒，`make reset-db && make seed` 可回到一致状态。
5. 工厂端工作台的「最近操作」卡仍显示「随着后续阶段接口交付…」——工厂端没有 `factory:audit:read` 权限与审计查询端点，该文案在共享模块 `shared/app/dashboard.js` 里，属跨阶段问题。

---

## P7 AI 抽象层与离线模拟引擎 ★

- [ ] 🔑 `app/ai/base.py` — `AIProvider` 抽象基类与 DTO
  - 生命周期：`health_check`
  - 设备侧：`provision_devices` `activate_device` `deactivate_device` `push_ota`
  - 对话侧：`open_session` `chat`（流式 `AsyncIterator[ChatChunk]`）`close_session`
  - 语音侧：`asr` `tts`
- [ ] 🔑 `app/ai/registry.py` — 注册表与解析顺序（`client_product → template.vendor → ai_providers 默认 → AI_DEFAULT_PROVIDER`）
- [ ] 🔑 `app/ai/mock/engine.py` `dialogue.py` `asr.py` `tts.py` `scenarios.py`
  - 规则对话：`故事` → 内容库取故事；`歌/唱` → 唱歌；`天气` → 天气；其余兜底
  - 支持 `role_preset` 与知识库关键词检索影响回复
  - `MOCK_ASR_MODE=echo|fixed`、`MOCK_TTS_MODE=text|wav`、固定随机种子保证可复现
- [x] 🔑 四个真实适配器骨架：`app/ai/{jixian,joyinside,volcano,baidu}.py`
  - 具备签名、HTTP 客户端、超时、重试、字段映射
  - **未配置密钥时 `health_check → DOWN`，端点返回 `VENDOR_UNAVAILABLE` 并写审计，绝不伪造成功**
  - 火山引擎适配器已按官方「硬件对话智能体」文档校对：统一 `POST https://rtc.volcengineapi.com?Action=<Action>&Version=2025-08-01`，`Aibot*` / `IotVoicePrint*` / `TrainTTSVoiceType` 等 Action，以及 `AibotCreate` 的 `Name` / `AccessType(public|private)` / `Config.ASRConfig.*` 结构
- [x] Alembic `0007_ai_dialogue.py` — `ai_providers` `ai_configs` `dialogue_sessions` `dialogue_messages` `voice_profiles` `role_presets` `knowledge_bases` `kb_files`
- [x] `app/api/v1/ai.py` — `/ai/providers` + `/health`、`/ai/chat`（流式）、`/ai/asr`、`/ai/tts`、`/ai/mock/scenarios`
- [x] `tests/unit/test_provider_registry.py`（31 条）
- [x] `tests/integration/test_ai_mock.py`（11 条）
- [x] `tests/integration/test_vendor_unavailable.py` — **断言无密钥时安全失败且不伪造成功**（7 条）

**验收**（2026-09-16 实测通过）：`AI_DEFAULT_PROVIDER=mock` 下 `/ai/chat` `/ai/tts` `/ai/asr` 全通且流式分块 > 1；真实供应商安全返回 `VENDOR_UNAVAILABLE` 并写审计；模拟引擎同种子可复现；`alembic check` 零漂移。共 **42** 条测试通过。

**已知局限（待真实联调消除）**：① 火山引擎**签名算法与签名位置**仍为占位（官方《调用方法》页正文不可读），已收口在 `BaseHttpProvider.sign` 一处并显式标注；② `AibotCreate` 之外的 Body 字段名按 PascalCase 惯例统一、未逐字核对；③ `TTSConfig`/`LLMConfig` 层级为同构推断（仅 `ASRConfig` 逐字来自官方示例）；④ 实时对话真实链路是 RTC/WebSocket 长连接，当前 `chat()` 为「HTTP 取整段 + 本地分块」，联调时替换为帧解析（对外 `AsyncIterator[ChatChunk]` 不变）；⑤ `dialogue_sessions.device_id`/`end_user_id` 刻意未建外键（属 P4/P9），落地后需补一次轻量迁移。

> **本阶段为并行开发产出**：由AI Agent 在主会话推进 P3 的同时完成，迁移编号预先分配为 `0007` 以避免链冲突。

---

## P8 终端用户小程序端 ★

- [x] 🔑 `app/api/v1/miniapp.py` — 扫码解析 / 4G 激活 / Wi-Fi 配网激活 / 设备信息 / 设置 / 解绑
- [x] 🔑 扫码解析：识别 `JX|` 与 `JD|` 前缀，路由到对应租户 / 产品 / 云服务商
  - 解析失败 → 404 `QR_INVALID`；SN 不存在 → 404 `DEVICE_NOT_FOUND`
  - **不可绑定不报错**：返回 `bindable: false` + 人话 `reason`（冻结 / 报废 / 未分配 / 已被他人绑定），前端据此提示而不是弹错误
- [x] 🔑 4G 激活路径：开机 → 4G 上线 → 调集贤激活（未配置密钥 → 退回 `NOT_ACTIVATED` 并 503，ADR-07；已激活则幂等）
- [x] 🔑 Wi-Fi 激活路径：配网 → 上报 SN/MAC → **校验二维码 SN 与设备 SN 一致** → 一致则调京东激活，不一致则 `BIND_FAILED`
  - 不一致时把设备 `activation_status` 落为 `BIND_FAILED`（可观测、可重试），`details` 带 `qrSn` / `reportedSn`
- [x] 重复激活幂等：已激活则提示「已激活」，且**必须同时保证绑定关系**（本轮修的真缺陷，见下方「已知局限」的反面记录）
- [x] 终端用户登录：手机号 + 短信验证码（`MINIAPP_SMS_PROVIDER` 可配；mock 回显验证码并标注，**生产环境启动期拒绝 mock**）
- [x] 🔑 `app/realtime/ws_chat.py` — WebSocket 对话帧协议
  - `session.open` → `session.ready` → `user.text`/`user.audio` → `asr.partial` → `assistant.delta`（流式）→ `assistant.audio` → `assistant.done{messageId, latencyMs}` / `error` → `session.close`
  - SSE 降级端点 `/miniapp/chat/stream`
  - 会话与消息落库（`DialogueSession` / `DialogueMessage`）
  - 令牌与设备走 **query**（浏览器 WebSocket 不能带自定义 header）；非法令牌 close `4401`、越权 close `4403`
- [x] 内容安全三开关在 `assistant.delta` 前过滤，命中写 `safety_flag`（`assistant.done` 带 `safetyFlag` 与 `blocked`）并写 `AuditAction.CONTENT_BLOCKED`
  - 开关开启时**先聚合再放行**（牺牲首字延迟换取「原文绝不出站」）；关闭时真流式
- [x] 4G 充值：套餐列表 + 下单 + mock 支付（**仅 4G 设备展示**；Wi-Fi 返回 `supported: false` + 原因而非报错）
- [x] 🔑 `frontend/pages/miniapp/{scan,login,setup_4g,setup_wifi,activate_done,home,chat,recharge,settings}.js` — 共 9 屏（另拆出 `shell.js` 外壳）
- [x] 设备设置（音量 / 儿童模式 / 唤醒词）：JSON 列 + **服务层键白名单**（设置项会随型号迭代，逐项开列意味着每加一项就要一次迁移）
- [x] `tests/integration/test_activation.py`（20 条：登录与令牌隔离、扫码双分支、激活与绑定、S→SN 不一致、越权矩阵、设置白名单、充值 4G/Wi-Fi 分支、SSE 帧协议、内容安全标注）

**验收**（2026-09-17 实测通过）：真实服务端到端接口验收 **50/50 通过**；JX/JD 分支自动路由正确；SN 不一致返回 `BIND_FAILED` 且落库；重复激活幂等**且补齐绑定**；WS 与 SSE 双通道流式对话落库；仅 4G 显示充值入口；`make check` 全绿（**757 passed**、mypy 87 文件、契约一致）；`make fe-check` 344 处导入匹配；`alembic check` 零漂移。浏览器实测 11 项。

**已知局限**：
1. **`frontend/shared/core/ws.js` 与真实协议四处不符**（URL 少 `/ws/miniapp` 前缀、令牌取后台 `auth.accessToken`、帧名写 `{text,seq}` 而实际是 `{delta,index}`、SSE 降级用 `EventSource` 的 GET 无法携带 `user.text`）。P8 的 `chat.js` 用「继承并覆盖 `ChatSocket`」绕开，**共享模块本身未改**——它现在是「看起来是通用实现、实际不可用」的状态，建议 P9/P10 对齐或删除。
2. **`frontend/shared/core/api.js` 无条件注入后台令牌**（401 时还会用后台 refreshToken 刷新并 `auth.clear()`），终端用户端不能直接用；P8 在 `shell.js` 自建了 `mpApi`（约 15 行重复）。
3. 语音未接：后端 `provider.asr/tts` 与 `user.audio`/`assistant.audio` 帧已就绪，前端无录音与播放。
4. 内容安全是**离线关键词级**（6 个词），完整词表 / LLM 复核 / 商户端三开关面板归 P9。
5. 设备昵称占位 `null`（`devices` 表无该列）。
6. `miniapp_service` 中相邻两行的 `detail=` 与 `details=` 是不同用途参数，命名易看错（无功能影响）。
7. `end_users` / `recharge_plans` / `recharge_orders` 由 P8 落地（原计划 P9），P9 迁移编号顺延为 `0015`。

---

## P9 AI 配置与运营看板 ★

- [x] Alembic `0015_ops_metrics_ota.py` — `metrics_daily` `metrics_hourly` `metrics_region` `content_hot_ranking` `content_items` `ota_packages` `ota_records`（+ `devices.region`、`dialogue_messages.content_item_id`）
  > 编号说明：原计划 `0012`，被 P6 的 `0012`（工厂账号归属）/ `0013`（设备工单归属）与 **P8 的 `0014`**（`end_users` / `recharge_plans` / `recharge_orders`）占用，按「落库先后顺延」为 `0015`；`recharge_*` 与 `end_users` **不由本阶段建表**。
- [x] 商户端 AI 配置：`/products/{id}/ai-config`、`/prompt`、`/role`（**仅 4G**）、`/voice`、`/safety`
  - `/role` 对 Wi-Fi 产品返回 409 并给出**厂商中立**的原因（「对话角色由厂商侧智能体配置」）——不点名某一家厂商，否则换供应商后文案变误导
  - 供应商清单**只回「是否已配置」的布尔，绝不含密钥或片段**；Temperature 在库里是 ×100 整数、出入参换算为 0–2 小数
- [x] 知识库 CRUD + 文件上传/删除（走新的 `app/core/storage.py` 抽象：local 实现 + s3 显式安全失败）
  - 解析**诚实实现**：文本类按段落切块并统计块数；pdf/docx 返回 `FAILED` + 「文本抽取尚未实现」，**绝不假装解析成功**
  - 删除知识库前校验是否被 `ai_configs` 引用（`CASCADE_CONFLICT` + 列出引用它的产品）
  - 知识库**真的影响对话**：`dialogue_service` 把 `PARSED` 文件的文本块作为 `context["knowledge"]` 传给供应商
- [x] 🔑 运营指标聚合：**按 `product_id` 隔离**（修复 P-08），**维度数据真实汇总**而非乘系数（修复 P-07）
  - 四张指标表都把 `client_product_id` 做进**唯一键**，让「漏掉产品维度」在表结构上不成立
  - 概览（当前口径）**实时聚合** / 趋势、24 小时、地域、内容榜**读快照**；两者在响应里用 `source: live|snapshot` 显式标注
  - 热度：新增激活、DAU 设备、总交互、人均交互、Top 内容、24 小时热力、地域分布
  - 留存：D1/D3/D7/D30、流失设备、回访率、平均间隔；**分母为 0 时比率为 `null` 而不是 0**（不把「未知」伪装成「零」）
  - 内容榜的归属来自 `dialogue_messages.content_item_id`（供应商在 `ChatChunk` 里**声明**素材标题），不做「回复文本里出现《标题》」式嗅探
- [x] OTA：固件包管理 + 推送记录（**仅平台端可见**；Wi-Fi 方案显示「不支持（端侧升级）」）
  - 推送能力判定读 `cloud_providers.ota_support`（**数据驱动**，不是写死的厂商 if）：Wi-Fi → 409 `OTA_NOT_SUPPORTED`，`details` 带 `{otaSupport, cloudVendor, cloudProviderName}`
  - 逐台写 `ota_records`（`PENDING → PUSHING → SUCCESS|FAILED`）；成功才更新设备固件版本；已是最新版本计 `skipped`（幂等）
  - **无文件的固件包逐台 FAILED 并说明原因**（ADR-07：不伪造成功）
- [x] 🔑 `frontend/pages/merchant/{products,product_detail,ai_config,knowledge,metrics}.js`（较原计划增加 `products.js` —— 菜单 `/products` 此前一直是「建设中」）
- [x] `frontend/pages/platform/ota.js` + `client_product_detail.js` 追加「运营」Tab
- [x] `frontend/shared/ui/chart.js` —— **P2 已交付**（柱 / 折线 / 环形 / 热力 / 迷你趋势 / 堆叠条 + 悬浮提示），本阶段直接复用，未重复实现
- [x] `tests/integration/test_metrics_isolation.py`（9 条）— **断言两产品的运营数据互不串台（P-07/P-08）**
  - 核心断言：`trend`（快照之和）**必须等于** `overview`（实时聚合）——用系数摊派的实现过不了这一关
  - 另断言：24 小时分布**不能是平的**（种子小时刻意错开）、地域设备数之和 = 该产品地域已知设备数、重建只写被请求的产品且幂等
- [x] `tests/integration/test_ota.py`（14 条）— 断言 Wi-Fi 产品推送被拒绝，且**被拒绝时不留任何推送记录**
- [x] `tests/integration/test_tenant_isolation.py` — 租户隔离矩阵**扩到 26 条 P9 商户路由**（含两类不同越权形态：路径资源 ID 与**查询参数** `productId`）

**验收**（2026-09-17 实测通过）：P-07 与 P-08 的断言测试通过（9 + 14 条）；OTA 仅平台可见（商户 403）且 Wi-Fi 正确提示不支持；`make check` 全绿（**804 passed**、mypy 95 文件、契约一致）；`make fe-check` 399 处导入匹配；`alembic check` 零漂移。浏览器实测 6 项（AI 配置分区与开关保存、角色 4G/Wi-Fi 差异、运营看板实时聚合 + 快照重建 168 行、OTA 的 Wi-Fi 拒绝与 4G 诚实失败）。

**本阶段发现并修复的 3 个真缺陷**：① **小时分布聚合错误**（用 `COUNT` 基座叠加行级列 → `SELECT count(*), created_at` 无 `GROUP BY`，SQLite 只返回 1 行，每小时恒记 1 条）；② **同一路径两个处理函数**（P4 与 P9 都定义 `/merchant/products`，生效取决于 include 顺序）；③ **演示数据时间戳落在未来**（种子把「今天」的会话放在 09:00/14:00，凌晨运行时按 `created_at <= now` 的聚合会排除它们，「今天」恒为空）。另修 3 处**写死厂商名的文案**（角色不支持原因、OTA 弹窗两处）改为厂商中立。

**已知局限**：
1. **快照按 UTC 日期归档**，而商户在中国的本地日期可能差一天（凌晨 8 点前尤其明显）。`tenants.timezone` 字段已存在但未参与聚合——完整方案是按租户时区归档，留待 P10。
2. **内容库只读**（本阶段只做了列表端点）：内容审核流程不在 P9 范围，写能力待后续；`content_items` 的演示数据由种子提供。
3. **知识库解析不做向量化**：只按段落切块并统计块数，检索仍是关键词级（无 embedding 服务）；`KnowledgeBase.embedding_model` 字段保留未用。
4. `dialogue_service` 注入知识库有上限（5 文件 / 20 块），未做分页或增量。
5. `metrics_daily` 等表的行主键用 `new_uuid()`（`app/core/ids.py` 没有为指标表登记前缀）——与其他表的「前缀 + 随机」风格不一致，但对外不可见。
6. 平台端「客户产品详情 → 运营」Tab 只能展示平台端能拿到的摘要（商户端指标端点对平台 token 返回 403），详细指标需在商户端查看——已在页面文案里说明，未为了填满 Tab 而编数据。
7. 演示数据在开发库留下痕迹（AI 配置的安全开关被改为开启大模型审核、7 天快照、一次 409 推送与一次失败推送记录）；`make reset-db && make seed` 可回到纯净态。

---

## P10 部署与质量保障 ★

- [x] `deploy/Dockerfile.backend`（多阶段构建，非 root 运行）
- [x] `deploy/nginx.conf`（SPA 路由回退 + API 反代 + 静态资源缓存策略）
- [x] `docker-compose.yml`（应用 + Nginx 前置 + PostgreSQL 三个 profile；SQLite 默认可跑）
  > **路径与 todolist 的差异（有意）**：编排文件放在**仓库根**而不是 `deploy/`。
  > Compose 的三个相对路径（`build.context`、`env_file: .env`、`${VAR}` 插值读的
  > `.env`）**都以 compose 文件所在目录为基准**——放根目录时三者自然成立，
  > 放 `deploy/` 就要写 `..`/`../.env`。构建资产仍在 `deploy/`（Dockerfile / nginx.conf）。
- [x] `deploy/.env.example` —— **生产部署清单**（不是开发模板的副本）：必须修改的项、
  「不安全就拒绝启动」的三类硬校验、容器编排专属变量（`APP_DATABASE_URL` / `DATA_DIR` /
  `FRONTEND_DIR` / `WORKERS`）、部署后自检与回滚步骤
- [x] `scripts/seed_demo.py`、`scripts/export_openapi.py`（P0/P1 已交付，本阶段复核可用）
- [x] 🔑 `scripts/smoke_test.sh` — **全端点冒烟**：按真实角色登录后逐端点请求（57 项断言），
  只读为主（例外是两个幂等写），带**最低断言数守卫**（0 断言时判定脚本异常而非「通过」）
- [x] `scripts/gen_qrcodes.py` — 演示二维码清单（PNG + CSV/JSON manifest，载荷经 `qrcode_service` 单一实现）
- [x] `scripts/reset_db.sh`（先备份到 `data/backups/<时间戳>/`，需二次确认，`--yes` 供自动化）、`scripts/dev.sh`
- [x] 🔑 `scripts/scan_secrets.py` — 敏感信息扫描：弱口令 / 硬编码密钥 / **出参模型里的明文密钥字段** /
  敏感文件入库（`.env`、`data/`、`learning/`）
- [x] `backend/.dockerignore`（缺失时构建上下文含 229MB 的 `.venv`，且 `.env` 有进镜像的潜在风险）
- [x] GitHub Actions CI（`.github/workflows/ci.yml`）：lint / mypy / test / e2e / 契约快照 /
  前端导入契约 / 敏感信息扫描，五个 job 并行
- [x] 🔑 安全验收（**实测通过**）：扫描 236 个被跟踪文件，**0 命中**；`.env` 未被 git 跟踪；
  弱口令扫描器另有「规则确能命中」的自测（用一个临时文件直调规则函数）
- [x] 验证（**全部实测通过**）：`docker compose up -d --build` → 容器 healthy；
  `make smoke` **57/57 通过**（直连与经 Nginx 各一次）；`down && up` 后数据**完全一致**

**验收**：一键起服务成功；全端点冒烟通过；数据持久化正常；弱口令扫描通过。**四项全部实测达成。**

**本阶段由「真跑一遍 Docker」发现并修复的 12 个真问题**（全部属于「文档写了但实际跑不通」：

| # | 问题 | 为什么集成测试发现不了 |
|---|---|---|
| 1 | `make up` 是坏的：Makefile `cd deploy && docker compose up`，而 compose 在仓库根 | 只跑 `make test` 不会执行 Makefile 的 docker 目标 |
| 2 | nginx 的 WebSocket location 写成 `/api/v1/ws/`，真实路径是 `/ws/miniapp/chat` → 握手落到 SPA 回退、返回 HTML | 本地不经 nginx，WS 直连 8000 端口一切正常 |
| 3 | nginx 对**未指纹化**的 JS/CSS 声明 `immutable` 强缓存 1 年 → 发版后用户卡在旧代码且无法失效 | 本地开发不走缓存 |
| 4 | compose 的 `DATABASE_URL: ${DATABASE_URL:-…}` 读到了开发机 `.env` 的**相对路径** → SQLite 落到镜像层而非卷，`down && up` 丢数据 | 容器不重建时看不出差别 |
| 5 | 缺 `.dockerignore` → 构建上下文 229MB，且 `.env` 有被 `COPY . .` 带进镜像的潜在风险 | 构建慢/泄漏都只在构建时暴露 |
| 6 | `POSTGRES_PASSWORD: ${…:?}` 在**未启用该 profile** 时也参与插值 → 没配 PostgreSQL 口令的 `.env` 让整个 compose 不可用 | 同上 |
| 7 | `--workers 4` 下**每个 worker 各播种一次** → 4 进程并发写同一 SQLite（实测 1 成功 3 失败），且赢得竞争的 worker 可能只写一半 | 本地 `make dev` 是单进程 |
| 8 | `FRONTEND_ROOT` 按「`__file__` 上溯三级」推算 → 容器布局没有 `backend/` 层，数到了 `/` → **整个 UI 404 而 API 正常**，日志只有一条 WARNING | 本地布局恰好是三级 |
| 9 | `DATA_ROOT` 不可配置 + 进程非 root → 容器启动即 `PermissionError: /app/data` 崩溃循环 | 本地开发目录可写 |
| 10 | nginx 缺 `/login`（它是**应用路由**不是磁盘文件）→ 经 nginx 访问登录页 404 | 直连时由 FastAPI 提供 |
| 11 | `smoke_test.sh` 的 `json_get` 用 heredoc 读程序、又想从 stdin 读响应体 → 取值恒为空，**登录永远失败**（而同一口令用 curl 直连成功） | 脚本自身没跑过 |
| 12 | 同一脚本在**误删计数函数**后仍打印「0 通过 / 全部通过」——**会静默通过的检查比没有检查更危险** | 只有真跑并核对断言数才会发现 |

**已知局限**：
1. **CI 未在真实 GitHub 上跑过**（本机无 remote，无法推送到 Actions）。`ci.yml` 的命令与本地
   `make check` 逐字一致，但「YAML 语法 + 表达式」只做了本地静态审查，**首次推送后需要实跑确认**。
2. **未在 CI 里构建镜像**：镜像构建含 pip 全量安装（分钟级），本阶段把它留在部署流程而非每次 CI
   （理由写在 `ci.yml` 的注释里）。
3. **`docker compose --profile postgres` 只验证了配置可解析**，未真跑 PostgreSQL 实例；
   `app.py` 到 asyncpg 的连接串与实际迁移未在 PG 上执行过（SQLite 全链路已实测）。
4. **Nginx 只验证了 HTTP 与 WebSocket 转发**，未验证 TLS（配置里预留了 80→443 的注释段）、
   未验证多实例负载均衡（`upstream` 只有一个 server）。
5. **`scripts/reset_db.sh` 的备份策略是「同盘备份」**：宿主机磁盘故障时备份与数据一起丢，
   生产应改用异机/对象存储备份。
6. 容器内以 4 个 uvicorn worker 运行，而 **SQLite 的写并发能力有限**：演示与轻负载没问题，
   真实多租户并发写应切 PostgreSQL（compose 已提供 profile）。
7. **冒烟脚本只覆盖读路径**（+2 个幂等写）：写路径的验证依赖 `make test`（隔离测试库）。

---

## P11 文档集 / 学习手册 / 复盘 ★

### P11-A 专业文档集（`docs/`，上传 GitHub）

- [x] `docs/README.md` — 文档导航
- [x] `docs/01-安装部署指南.md` — 环境要求 / 本地三步跑通 / Docker 部署 / 环境变量表 / 迁移 / 种子数据 / 升级回滚 / 故障排查 / 生产加固清单
- [x] `docs/02-系统流程图.md` — 全局业务闭环 / 订单状态机 / 设备四维状态机 / 激活分支（JX vs JD）/ 批次导入 / 分配与绑定 / 对话时序（全部 mermaid）
- [x] `docs/03-系统架构图.md` — 分层架构 / 部署拓扑 / 模块依赖 / 多租户隔离层 / 数据流 / 技术选型理由与替代方案
- [x] `docs/04-产品需求文档PRD.md` — 产品定位 / 角色定义 / 术语表 / 页面清单 / 权限矩阵 / 交互说明 / 非功能需求 / 版本变更
- [x] `docs/05-数据模型与ER图.md` — 分域 ER 图 / 每表字段字典 / 索引设计 / 状态枚举 / **`in_stock` 语义对齐说明**
- [x] `docs/06-API接口文档.md` — 约定（认证/错误码/traceId/幂等/分页）/ 各端端点表 / 请求响应示例 / OpenAPI 导出说明
- [x] `docs/07-多租户与权限设计.md` — 租户模型 / 隔离层级 / JWT 声明 / RBAC 权限码 / 强制作用域实现 / 越权测试策略 / 脱敏规则
- [x] `docs/08-AI能力接入设计.md` — 能力矩阵 / Provider 抽象 / 注册与配置 / Mock 引擎 / 集贤 4G 与 JoyInside Wi-Fi 传输差异 / 火山与百度接入指引 / 密钥管理
- [x] `docs/09-系统交付标准.md` — 功能验收清单（逐条可勾选）/ 性能 SLO / 安全基线 / 兼容性 / 文档完备度 / 缺陷等级定义 / 验收流程与签署
- [x] `docs/10-AI验证可用性.md` — 验证目标 / 测试环境 / 场景与数据集 / 指标定义（首字延迟 / 端到端延迟 / 意图命中率 / 安全拦截率）/ **基准数据表** / 复现命令 / **结论与局限**（明确说明基于离线引擎，并给出真实联调步骤）
- [x] `docs/11-测试与质量保障.md` — 测试金字塔 / 覆盖率 / 租户隔离矩阵 / 契约测试 / CI 流程 / **P-01~P-08 修复对照**
- [x] `docs/12-项目复盘.md` — 背景目标 / 三个遗留项目的整合决策 / 架构演进 / 关键问题与解法 / 度量结果 / 不足与路线图 / 经验沉淀
- [x] `docs/13-火山引擎硬件对话智能体配置说明.md` — **已提前产出**（P7/P8 真实联调的前置资料）：控制台四步配置（产品 / License / 智能体 / SDK）、设备端三条接入路径（官方 Demo 板 / 预编译体验包 / 自行移植）、客户端 API 与回调清单、服务端 `Aibot*` OpenAPI、与本项目数据模型的映射、排错速查与**取证边界声明**

- [x] `docs/14-ESP32-S3刷机与联调步骤清单.md` — **已提前产出**（真机联调当天的执行清单）：云端链路自检 → 工具链准备 → 四条接入路径决策树（官方 Demo 板 / 体验包 / Sense 自行移植 / 低负载 WebSocket）→ 配网与首次对话判据 → 失败速查表（F-01~F-10）→ 真机结果回填项

- [x] 🔑 **`scripts/check_docs.py` + `make docs-check`** — 文档一致性门禁（P11 新增，对标既有
  `make fe-check` / `make openapi-check` 的思路）：把「文档悄悄过期」变成红灯。**八类校验**：
  ① `docs/06` 端点表与 OpenAPI 快照**双向**比对（方法与路径）并校验声明的总数；
  ② `docs/05` 字段字典的表名集合与迁移/模型一致（脚本内还交叉校验「迁移集合 == 模型集合」）；
  ③ `docs/06` 错误码与 `ErrorCode` 比对，**并校验文档写的 HTTP 状态与 `_STATUS_MAP` 一致**；
  ④ `docs/07` 权限码与 `permissions.py` 双向比对；⑤ `docs/05` 状态枚举取值与 `enums.py` 成员比对；
  ⑥ 文档引用的 `make <目标>` / `scripts/<文件>` / 相对链接是否存在；
  ⑦ `docs/11` 的逐文件用例数与源码一致（并校验 unit/integration/e2e 三个分组数）；
  ⑧ 每份 `docs/NN-*.md` 是否含「已知局限 / 取证边界」小节、是否引用了不上传目录的内部路径。
  已接入 `make check` 与 CI 的 `contract` job。

**P11-A 验收**：13 份文档（`README` + `01`–`12`）全部产出，并**由门禁而非人眼保证与代码一致**。
实测结果见下方「P11-A 验收记录」。

**P11-A 的已知局限**：
1. **文档集不含截图**（`docs/assets/` 为空）——读者看不到界面，需自己跑起来。截图属 P11-C。
2. **`docs/04` PRD 是事后补写**（描述的是已交付能力，不是事前需求假设），无用户调研与竞品数据支撑。
3. **`docs/10` 的基准数据来自离线模拟引擎**，不是真实厂商链路；真实链路（网络、排队、模型质量）完全未验证。
4. **门禁只保证「名字与数字对得上」，保证不了「叙述正确」**：表述性描述、
   「为什么这样设计」的解释仍需人工评审。改动业务语义时需人工同步。
5. **`docs/05` 的字段名与类型由 ORM 元数据导出，但未逐列核对 SQLite 实际 DDL**
   （SQLite 的类型亲和性会改变实际存储类型）。
6. **新增的 `check_docs.py` 未纳入覆盖率统计**（`[tool.coverage]` 只覆盖 `app`），
   且它自身**没有单元测试**——它靠「报出了 5 处真问题」和「修好后转绿」来证明有效。
7. **`docs/03` 与 `docs/12` 里对「为什么这样分层/这样决策」的解释属于事后归纳**。

### P11-B 学习手册（`learning/`，**不上传 GitHub**）

- [x] `learning/README.md` — 使用说明与学习路径
- [x] `learning/00-学习地图.md` — 全景图 + 里程碑 + 各模块预计时长
- [x] `learning/01-项目业务全景与四端角色.md`
- [x] `learning/02-多租户SaaS基础概念.md`
- [x] `learning/03-需求分析与PRD怎么写.md`
- [x] `learning/04-数据模型与ER图入门.md`
- [x] `learning/05-状态机与流程图：把复杂业务讲清楚.md`
- [x] `learning/06-API与前后端协作.md`
- [x] `learning/07-AI能力：ASR-LLM-TTS与Provider抽象.md`
- [x] `learning/08-设备接入：二维码配网与激活.md`
- [x] `learning/09-权限与安全：多租户隔离与脱敏.md`
- [x] `learning/10-测试与验收标准.md`
- [x] `learning/11-部署与上线常识.md`
- [x] `learning/12-作品集呈现与GitHub规范.md`
- [x] `learning/exercises/01-写一份客户开通PRD.md`
- [x] `learning/exercises/02-画订单状态机.md`
- [x] `learning/exercises/03-设计ER图.md`
- [x] `learning/exercises/04-定义API契约.md`
- [x] `learning/exercises/05-设计AI对话流程.md`
- [x] `learning/exercises/06-编写验收清单.md`

**每个 learning 模块必须包含完整的 12 段结构**（缺一不可）：

| # | 段落 | 要求 |
|---|---|---|
| ① | 学习目标 | 3–5 条可检验的目标 |
| ② | 核心概念 | 通俗定义 + 生活类比 |
| ③ | 本项目中的体现 | **引用真实文件名 / 表名 / 端点** |
| ④ | 分步讲解 | 0 → 1，不省略任何中间步骤 |
| ⑤ | 重点标注与重要节点 🔑 | 明确标出关键节点 |
| ⑥ | 动手练习 | 可操作步骤 + 明确产物 |
| ⑦ | 自测题 | 5–8 题，**附参考答案** |
| ⑧ | 常见错误 | 初级产品经理在本领域的典型踩坑 |
| ⑨ | 与前端沟通要点 | 具体话术与清单 |
| ⑩ | 与后端沟通要点 | 具体话术与清单 |
| ⑪ | 如何避免关键错误 | 可勾选的检查清单 |
| ⑫ | 拓展知识点 | 延伸阅读与更高阶概念 |

**P11-B 验收**：20 个文件全部产出（`README` + `00`–`12` 共 14 份 + `exercises/` 6 份），
**合计 8177 行**。由一次性校验脚本（**刻意不入库**，因为 `learning/` 不上传，
提交的校验器在 CI 或他人机器上会因目录不存在而失败）逐项检查，**四类检查全部通过**：

| # | 检查项 | 实测结果 |
|---|---|---|
| 1 | 20 个文件齐全 | ✔ 2（说明与地图）+ 12（模块）+ 6（练习） |
| 2 | 每个模块含元信息块 + `## 0. 结论先行` + **恰好 12 个二级章节且编号与标题顺序符合规范** | ✔ 12/12 模块 |
| 3 | 自测题含**参考答案** | ✔ 12/12 模块 |
| 4 | 装饰性 emoji | ✔ 0 处（只用了 `🔑 📌 ✅ ⚠️ ★ —`） |
| 5 | 第一人称「我」（排除反引号内代码与引号内话术） | ✔ 0 处 |
| 6 | 篇幅区间（模块 300–650 行） | ✔ 全部在区间内（最短 445、最长 550） |
| 7 | 真实代码标识引用数（去重 ≥ 8） | ✔ 12/12 模块 |
| 8 | **相对链接可解析** | ✔ **198 处**全部可解析（手册大量交叉引用 `docs/`） |

**P11-B 的已知局限**：
1. **`learning/` 不上传 GitHub**（`.gitignore:15`，`git ls-files` 中 0 条）——
   因此本阶段**没有可提交的产物**，只有追踪文件的更新会进提交。
2. **校验只覆盖结构，覆盖不了内容**：12 段齐全、无装饰 emoji、链接可解析都能机械验证；
   但「讲得对不对、类比是否恰当、难度是否适合初级 PM」**只能人工判断**。
3. **篇幅有硬上限**：每个模块压到 550 行以内，导致部分拓展知识点只能一句话带过
   （模块 03 的 RICE/MoSCoW/Kano、模块 12 的自检评分表都被压缩）。
4. **无截图与配图**：手册全部用 mermaid 文本图，没有界面截图。
5. **未做「真人试读」**：没有一个真实初级 PM 读过并反馈「哪一段看不懂」——
   所有难度判断都是作者的自我估计。
6. **模块 04 与 `docs/05` 的索引条数口径**：`docs/05` 的 `## 3.3` 列 **19** 条 `idx_*` 复合索引，
   而 ORM 里共 **20** 处 `Index(...)` 声明（差额来自 `## 3.2` 单列的那条唯一索引）；
   手册按「19 条（表格）+ 20 处（ORM 声明）」写，避免读者按 20 去逐条核对表格。
7. **术语表实际 24 条**（我早期在 `项目进度.md` 与 `CHANGELOG.md` 里误写为 27 条，已修正）。

### P11-C 收尾

- [x] `README.md` 终版（徽章 + 截图 + 快速开始 + 架构图 + 文档索引）
- [x] `CHANGELOG.md` 记录完整版本历史
- [x] 系统截图（各端关键页面）
- [x] 🔑 **最终验证：`git check-ignore -v learning/README.md` 命中；`git ls-files` 中不含任何 `learning/` 路径**
- [x] 🔑 最终验证：陌生环境按 README 操作 **5 分钟内可跑起来**

**P11-C 验收（2026-09-17）**

| 项 | 实测 |
|---|---|
| 截图 | **14 张**：登录页 1 + 平台端 6（工作台 / 租户 / 订单 / 设备 / 工厂订单 / OTA）+ 商户端 4（工作台 / 我的产品 / AI 配置 / 运营看板）+ 工厂端 2（工作台 / 生产订单）+ 小程序 1（扫码页）；合计 2.2MB，逐张人工核验为**真实登录态**（非白屏、非登录页） |
| `README.md` 终版 | 新增**界面预览**（7 张内嵌 + 14 张清单）、规模速查表、**「本项目的验收方式」**、**「已知局限」** 表；徽章含 `coverage-70%`（如实标注）；18 处文档链接全部可解析 |
| 🔑 验证 1 | `git check-ignore -v learning/README.md` → 命中 `.gitignore:15`；`git ls-files \| grep ^learning/` → **0 条** |
| 🔑 验证 2 | 在 `/tmp` 全新克隆（HEAD `9086e4a`）后实测：**照 README 字面步骤 `make setup` 失败（81 秒）**；补齐 6 项密钥后 `make setup` 成功（**72 秒**），四端入口 `/`、`/login`、`/platform/`、`/merchant/`、`/factory/`、`/miniapp/` **全部 200**，从克隆到四端可用 **≈91 秒** |
| `CHANGELOG.md` | `[未发布]` 段已覆盖 P1–P11 全部阶段；⚠️ **未切版本 tag**（无 release），因此不写 `[x.y.z]` 正式版本号 |

**★ 验证 2 发现并修复了一个真实缺陷：README 的快速开始不完整**

启动期安全校验实际要求 **6 项**（`JWT_SECRET_KEY`、`QR_SIGN_SECRET`、
`PLATFORM_ADMIN_PASSWORD`、`MERCHANT_ADMIN_PASSWORD`、`FACTORY_ADMIN_PASSWORD`、
**`PLATFORM_OPERATOR_PASSWORD`**），而 README 原文只让改 3 个管理员口令。
按原文操作会得到：

```
InsecureConfigurationError: 配置未通过安全校验，服务拒绝启动：
  - JWT_SECRET_KEY 仍为占位符，请替换为强随机值
  - QR_SIGN_SECRET 仍为占位符，请替换为强随机值
  - PLATFORM_ADMIN_PASSWORD 未设置（账号 admin）
  - …（共 6 项）
```

→ 已修正 README 的两种方式，列出全部 6 项，并附一段**可选的一行脚本**自动替换。
（`docs/01-安装部署指南.md` 的 `## 5.` 一直列全了 6 类条件，**是根 README 漏了**。）

**P11-C 的已知局限**：
1. **截图由独立 headless Chrome 采集**，不是用户日常浏览器窗口——因为 `bsk screenshot`
   在本机**只能成功一次**，之后持续 `tool RPC timed out after 30s`（同期 `bsk evaluate` 正常）；
   `bsk daemon restart` 能恢复一次但不足以支撑十几张图。**这是上游工具问题，已记录**。
2. **截图是静态画面**，不含交互过程（弹窗、下拉、Toast）；也不含移动端以外的响应式截图。
3. **验证 2 的 72 秒偏乐观**：本机 pip 有缓存；冷机器首次安装依赖会更久。
4. **未在真正的另一台机器上验证**（同机不同目录），因此不能排除环境相关差异。
5. **`CHANGELOG.md` 未切版本号**：全部变更仍在 `[未发布]` 段，仓库也没有 git tag。

### 公开前整理（2026-09-17，用户提出「公开前的待办也帮我处理」）

| # | 待办 | 处理结果 |
|---|---|---|
| 1 | CI 从未在真实 GitHub 上跑过 | ✅ **把能本地验的都验了**：YAML 可解析（5 job）、`run` 引用的脚本/路径全部存在、命令与 Makefile 目标逐字一致；★ **并修掉一处真实冗余**——`pytest -q` 本就会收集 e2e，之后又单独跑一次，**每次 CI 白等约 5 分钟**（改为 `--ignore=tests/e2e`）。⚠️ **仍无法在真实 GitHub 上跑**（本机无 remote），首次推送后必须实跑确认 |
| 2 | `CHANGELOG` 未切版本号 | ✅ 切 **`[1.0.0] - 2026-09-17`**（首个功能完整版本），保留空的 `[未发布]` 段，更新 compare / release 链接，并创建本地 git tag |
| 3 | `docs/12` 的「M5 未达成」已过期 | ✅ 同步 3 处（里程碑表、6.1 未做项、附录第 7/8 条） |
| 4 | AI 未联调、儿童数据合规未实现 | ✅ **无法真正实现**（无密钥 / 无合规能力），改为**让它不可错过**：README 顶部加显著提示（指向「已知局限」与 `SECURITY.md`）；`SECURITY.md` 的「已知的安全边界」与 `docs/09` 的两栏基线均已列明 |
| 5 | 覆盖率 70%、无压测 | ✅ CI 新增**覆盖率下限 `--cov-fail-under=65`**（实测 70，留 5 点余量）——本项目到 P11 才发现「804 用例全绿但覆盖率只有 70%」，**当时没有任何机制会提醒**；✅ **压测已在 P11-D 补做**（见下），⚠️ 但只有一轮短测，**不是容量规划依据** |
| 6 | PostgreSQL / Nginx TLS / 多实例仅部分验证 | ✅ **PostgreSQL 已真跑**（`--profile postgres` 起 PG 16 → 迁移到 `0015` → 种子 → 应用 healthy → **冒烟 57/57 全通过**）★ **并因此抓到一个真实缺陷**（见下）。✅ **Nginx TLS 与多实例已在 P11-D 补做**，且**多实例实验又炸出一个真缺陷**（见下） |

**★★ 公开前整理发现并修复的真缺陷：PG 上运营指标两个端点直接 500**

| | |
|---|---|
| 代码 | `metrics_service.py` 的 `_date_between`：`func.date(col).between(date_from.isoformat(), date_to.isoformat())` |
| 在 SQLite 上 | `func.date()` 返回 **TEXT**，字符串比字符串 → **完全正常，804 个用例全绿** |
| 在 PG 上 | `func.date()` 返回真正的 **date**，与 `varchar` 比较 → `UndefinedFunctionError: operator does not exist: date >= character varying` |
| 后果 | **运营看板的概览与快照重建两个最核心端点直接 500** |
| 修复 | 绑定 `date` 对象而非 `.isoformat()` 字符串（两种方言都正确） |
| 守卫 | 新增 `tests/unit/test_sql_dialect_portability.py`：**不测行为、测绑定参数类型**（断言是 `date` 而非 `str`）。已实测旧写法会被拦下 |
| 发现方式 | **公开前真跑一次 PostgreSQL** → 冒烟报出 2 项失败 |

> ★ **这条修正了本项目早先的判断**：P10 曾把「PostgreSQL 只验证了配置可解析」列为**可接受**的局限，
> 理由是「SQLite 全链路已实测」。事实证明不够——**一个核心页面在 PG 上直接 500**。
> 「配置能解析」与「服务能跑」之间的差距，比想象的更大。

### P11-D 收官验证（2026-09-17，用户选择「A 收官验证」）

把上表第 5、6 条里剩下的 ②③ 真的跑掉：**负载测试 / Nginx TLS / 多实例**三项全部实测。

| # | 实验 | 结果 |
|---|---|---|
| 1 | **负载测试**（`scripts/load_test.py`，20 并发 × 15 秒，SQLite vs PG） | 读 **73.6 / 72.0** QPS；写 **30.7 / 93.0** QPS；SQLite 写失败 **4/487**（`database is locked`），PG **0/1414**。★ **修正了本项目自己的假设**：**读路径 SQLite 并不输**，PG 的优势**只在写路径**（×3 吞吐、失败归零） |
| 2 | **Nginx TLS**（自签证书） | 四项全过：HTTPS API `200`、SPA/静态全 `200`、HTTP→HTTPS **`301`**、★ **`wss://` 握手成功并以 `4401` 关闭**；`TLSv1.3 / AES-256-GCM-SHA384` |
| 3 | **多实例**（2 实例 + 1 Nginx） | 分发 **31 / 29**、WS 经 Nginx → 4401；★ **共享 SQLite 卷并发写：31.4 QPS + 2×500**（单实例 30.7 → **无提升**，冲突翻倍）→ **多实例必须配 PostgreSQL** |

**★★ P11-D 顺带抓到的真缺陷：初始账号播种不幂等 → 改一个账号名就让部署起不来**

| | |
|---|---|
| 现象 | 复用已有数据卷启动容器 → 应用**启动即崩、无限重启**（`Restarting (1)`） |
| 根因 | `_seed_admin_users` **按 `account` 查重、却按固定 `id` 插入**——两个判据不一致 |
| 触发 | `.env` 里把管理员**登录账号换成另一个号码** → 按新名字查不到 → 拿同一个 `id` 再插 → `IntegrityError: UNIQUE constraint failed: user_accounts.id` |
| 为什么严重 | 入口脚本「先播种、再起服务」→ **一个配置项就能让已有库的部署无限重启**；且新库 / 测试库 / CI **全都复现不出来** |
| 修复 | 查重一律按 **`id`**；三条路径：同名 → 空操作（**不动密码**）、改名 → 同步登录名并告警、名字被**别的 `id`** 占用 → 跳过并告警 |
| 验证 | ① 在**真实故障卷**上实测：`Restarting (1)` → `healthy`、改名告警出现、`password_hash` 不变、重启幂等；② 新增 `tests/integration/test_seed_admin_users.py`（5 条），**改名那条在修复前的实现上会失败**（已实测） |

**P11-D 新增资产**：`scripts/load_test.py`（标准库 + `httpx`，不引入 ab/wrk/hey）、
`backend/tests/integration/test_seed_admin_users.py`（5 条）；
文档：`docs/11` 新增 `## 8. 性能与部署验证`（含 `8.5 仍未证明的事`），
`docs/09` `## 2.` 重写为实测基线，`docs/01` / `docs/03` / `docs/07` / `docs/10` / `docs/12` / `README` 同步。
**门禁**：`make check` 全绿 —— **814 passed in 256.49s** + lint / mypy / 契约 / docs-check（8 类）。

**剩余待办（诚实记录）**：① CI 首次推送后必须实跑（本机无 remote）；
② **多实例 + PostgreSQL 组合未验证**（只证明了「SQLite 多实例不行」，反向未验）；
③ 真实厂商联调与儿童数据合规未实现；④ 截图仍由 headless Chrome 采集；
⑤ 压测只有一轮短测（无劣化曲线、未调连接池、证书为自签）。

---

## 附录 A：遗留缺陷修复对照表

来自早期静态原型 PRD 第五章的 P-01 ~ P-08：

| 编号 | 问题 | 修复方案 | 归属阶段 | 验证方式 |
|---|---|---|---|---|
| P-01 | 子页面刷新状态丢失 | 路由参数化 `#/tenant/:id`、`#/product/:id` | P2 | 刷新子页后仍在原页 |
| P-02 | B端产品详情返回路径错误 | `back()` 按角色决定目标 | P2 | 商户端返回「我的产品」 |
| P-03 | 新建产品未绑定客户 | 创建时同步建 `client_product` | P3 | 建后客户详情可见 |
| P-04 | 禁用客户仍可登录 | 登录校验 `tenant.status` | P5 | 禁用租户登录被拒 |
| P-05 | 直接访问子页面异常 | 路由守卫，`_view*` 空则重定向 | P2 | 直访被拦截 |
| P-06 | 删除无级联校验 | 删前校验关联，返回 `CASCADE_CONFLICT` | P3 | 有关联时删除失败 |
| P-07 | 维度数据非真实汇总 | 按产品聚合真实数据 | P9 | 两产品数据不同 |
| P-08 | 运营数据全局共享 | 按 `product_id` 隔离 | P9 | 隔离测试通过 |

## 附录 B：来自早期实现的安全缺陷（不移植 / 需修复）

| 来源 | 问题 | 处理 |
|---|---|---|
| 早期可运行 MVP（服务端） | 静态文件路径穿越漏洞 | **不移植 Python 版**，只移植 `server.js` 语义并加路径规范化 |
| 早期可运行 MVP（环境配置） | 硬编码弱口令 `admin@2024` | **绝不迁移**，新环境强密码 + 首次登录强制改密 |
| 早期可运行 MVP（前端） | 未转义的 `innerHTML` 模板注入 | 前端 `dom.js` 提供安全转义 |
| 早期静态原型（前端） | SecretKey 明文展示在前端 | 后端加密存储，响应永不含 SK |
| 早期静态原型（二维码） | JD 二维码参数顺序错误（`clientId` 误传 `tenant_id`） | `qrcode_service.py` 修正并加断言测试 |
| 早期实现（运维端点） | 生产环境测试端点 | 不实现 |
| 早期实现（迁移脚本） | 开发种子数据进入生产迁移路径 | 种子数据与迁移分离，由 `SEED_DEMO_DATA` 开关控制 |

## 附录 C：关键架构决策记录（ADR 摘要）

| # | 决策 | 理由 |
|---|---|---|
| ADR-01 | 平台管理员 `tenant_id = NULL` | 参考 JAR 把它挂在 `t-001`，与「平台是全局长」矛盾，本计划修正 |
| ADR-02 | 烧录工厂独立 `factories` 表 + `role_type=FACTORY`，账号 `tenant_id = NULL` | 工厂跨租户，不属于任何单一租户 |
| ADR-03 | 设备状态以早期实现的**四维**为权威，原型的 9 态降为前端派生展示 | 四维表达力更强，可组合出 9 态的任意展示标签 |
| ADR-04 | AI 业务标准以早期原型与 PRD-03 为准 | 早期实现无 AI 实现（仅占位视图桩） |
| ADR-05 | 默认 SQLite，PostgreSQL 可选 | 保证「克隆即跑」，同时保留生产级选项 |
| ADR-06 | 前端零构建 ES Module | 无需 `npm install`，克隆即可打开；降低作品集使用门槛 |
| ADR-07 | 未配置密钥的厂商一律 `VENDOR_UNAVAILABLE`，不伪造成功 | 延续参考 MVP 的安全失败语义，避免误导 |
| ADR-08 | 多租户过滤只在 repository 层收口 | 禁止在 router 手写过滤，从架构上杜绝越权 |
| ADR-09 | **设备与工单的归属落库**（`devices.factory_order_id`），不靠「订单 + 状态」推断 | 「归属」是事实不是状态的函数：同一订单下可能有被冻结或已先分配给客户的设备，它们不属于任何工单。靠状态推断会让二维码清单多打标签、抽检范围过宽（P6 端到端验收实测到这两个后果）。派单是归属的唯一诞生点，之后所有工单维度查询都以该列为准 |
| ADR-10 | **可冻结 / 可分配集合按业务能力定义，并与 `ASSET_TRANSITIONS` 严格对称** | 冻结范围 P4 曾收紧为 `{IN_STOCK}`（求对称），却造成「已出货给商户的设备无法停服」的能力缺口；P6 放宽为 `{IN_STOCK, ALLOCATED, BOUND}` 并补齐入边。可分配范围补 `SHIPPED`，接通 P4 就存在却无处使用的 `SHIPPED → ALLOCATED` 边。规则由单测钉成「集合 == 迁移表对应边」，两侧必须同改 |
