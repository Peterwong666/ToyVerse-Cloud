# 变更日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [未发布]

### 新增

**P1 后端地基**

- 配置与启动期安全校验：弱口令 / 默认密钥一律拒绝启动（`InsecureConfigurationError`）
- 统一错误码（25 个）与统一响应契约 `{code, message, traceId}`；`traceId` 中间件贯穿日志与响应头
- 权限目录（6 类角色）与四层 Alembic 迁移，SQLAlchemy 2.0 模型共 11 张表
- 鉴权：bcrypt 口令 + JWT 访问/刷新令牌，含登录失败锁定（5 次 / 15 分钟）与刷新令牌重放检测
- 租户作用域在 repository 层收口（ADR-08）、幂等键、分页、审计服务，8 个 API 端点
- 幂等种子数据；修复遗留缺陷 **P-04**（禁用租户登录被误判为账号问题）

**P2 前端地基**

- 全新视觉设计系统（零构建 ES Module）：靛紫 `#4F46E5` 主色、浅色侧导轨、Inter + Noto Sans SC
- `shared/core`：契约先行的 API 客户端（`Authorization` / `x-trace-id` / 统一错误解析 / 401 自动刷新）、token 与权限、**参数化 hash 路由**、路由守卫、pub/sub store、WebSocket 客户端（含 SSE 降级）
- `shared/ui`：组件库、表格、表单、Modal/Drawer、通知、纯 SVG 图表、**Canvas 手写二维码编码器**、⌘K 命令面板、DOM 安全转义
- 四端入口与统一登录页（对接真实 `/auth/login`，移除原型「任意密码可登录」）
- 修复遗留缺陷 **P-01**（刷新子页面状态丢失）、**P-02**（`back()` 目标错误）、**P-05**（无路由守卫）
- 新增质量门禁 `make fe-check`（77 处 ES Module 导入契约）与 `make qr-verify`（104 组二维码矩阵与独立实现比对）

**P3 目录域**

- **数据层**：Alembic `0005_catalog` / `0006_client_product_miniapp`，新增 5 张表（`cloud_providers` / `product_templates` / `product_authorizations` / `client_products` / `miniapp_configs`），与 ORM 零漂移、升降可逆
- **密钥保护**：新增 `app/core/crypto.py`（Fernet 对称加密）；云服务商 `accessKey` / `secretKey` 与小程序 `appSecret` **只存密文 + 掩码提示**，任何响应不含明文——附录 B 中「SecretKey 明文下发前端」问题的架构性修复
- **服务层**：`tenant_service`（客户开通 / 启停 / 账号与一次性密码）、`catalog_service`（云服务商 CRUD + 连通性检测、模板 CRUD + 授权、客户产品 CRUD + 小程序配置）
- **API**：平台端 17 个端点（`/platform/tenants`、`/clouds`、`/templates`、`/client-products` 及授权、小程序配置等子资源）；OpenAPI 契约快照已更新（31 个路径）
- **安全语义**：连通性检测在未配置密钥时返回 `NOT_CONFIGURED` 且**跳过真实调用**（ADR-07，浏览器实测验证）；目录域「编码重复」统一为 409（新增 `CLOUD_CODE_EXISTS` / `TEMPLATE_CODE_EXISTS`，复用既有 `PRODUCT_CODE_EXISTS`）
- **前端**：平台端新增 6 个页面（租户列表 / 租户详情 / 产品模板 / 云服务商 / 客户产品 / 客户产品详情）与页面共用工具 `pages/platform/common.js`；**首次启用参数化路由** `#/tenants/:id`、`#/client-products/:id`
- **演示数据**：种子数据补充 2 个云服务商（含**火山引擎智能云 · 硬件对话智能体**）、2 个产品模板、3 条授权、2 个客户产品

**P4 订单与设备生成**

- **数据层**：Alembic `0008_orders` / `0009_devices_batches` / `0010_factory_production`，新增 9 张表（`orders` / `device_batches` / `devices` / `device_batch_lines` / `device_credentials` / `device_events` / `factory_orders` / `burn_reports` / `inspections`），与 ORM 零漂移、升降可逆
- **二维码双格式**：`qrcode_service` 生成与解析 —— 集贤 4G `JX|{SN}|{IMEI}|{ICCID}|{deviceId}`、京东 Wi-Fi `JD|{tenantId}|{productId}|{sn}|{sign}`（HMAC-SHA256 + `compare_digest` 验签）；**修正原型把 `clientId` 误传入 `tenantId` 位置的缺陷**，并用单测钉死字段顺序
- **订单状态机**：`PENDING_AUDIT → APPROVED|REJECTED → GENERATING → GENERATED → IN_STOCK → PRODUCING → SHIPPED_TO_CLIENT → COMPLETED`（另有厂商失败时的 `GENERATING → APPROVED` 回退边），迁移一律经 `ORDER_TRANSITIONS` 校验
- **设备四维状态流转**：`device_service` 提供状态迁移、冻结/解冻/报废（原因必填）与**流转时间线**（`device_events` 记录维度、前后状态、操作者与 traceId）
- **批次 CSV 导入**：上传 → SHA-256 去重 → 预检 → 导入 → 错误报告 → 幂等重跑；三重安全限额（5000 行 / 5MB / 按字段长度上限）
- **API**：平台端 20 个端点（订单审核/生成/二维码、设备列表/详情/事件/冻结解冻报废、批次上传/导入/明细/错误报告）+ 商户端 4 个（下单、查单、产品列表）
- **前端**：平台端订单 / 订单详情 / 设备库存 / 批次 4 页 + 商户端「我的订单」；订单详情含**真实二维码渲染**与 CSV 导出
- **安全语义**：4G 生成设备在厂商未配置密钥时返回 `VENDOR_UNAVAILABLE`，订单退回 `APPROVED`、**设备表零新增**、审计留痕（ADR-07，浏览器与集成测试双重验证）

**P7 AI 抽象与离线模拟引擎**

- `app/ai/base.py`：`AIProvider` 抽象（生命周期 / 设备侧 / 对话侧流式 / 语音侧）与 DTO
- `app/ai/registry.py`：四层供应商解析顺序（`client_product → template.vendor → ai_providers 默认 → AI_DEFAULT_PROVIDER`）
- `app/ai/mock/*`：规则对话（故事 / 歌 / 天气 + 兜底）、ASR `echo|fixed`、TTS `text|wav`，固定随机种子可复现，**无任何密钥即可跑通全流程**
- 四个厂商适配器骨架（集贤 / JoyInside / 火山引擎 / 百度）；**火山引擎按官方「硬件对话智能体」文档校对**：统一 `POST https://rtc.volcengineapi.com?Action=<Action>&Version=2025-08-01`、`Aibot*` / `IotVoicePrint*` / `TrainTTSVoiceType` 等 Action 与 `AibotCreate` 的 `Name` / `AccessType(public|private)` / `Config.ASRConfig.*` 结构
- Alembic `0007_ai_dialogue`（8 张表）；`app/api/v1/ai.py` 六个端点（`/ai/chat` 为流式）
- **已知局限**：厂商签名算法与部分 Body 字段名仍为占位（官方《调用方法》页不可读），已在代码中显式标注，待联调替换；`ADR-07` 安全失败行为已由测试钉住

**P5 设备 / 分配 / 绑定**

- **数据层**：Alembic `0011_allocations_bindings`，新增 3 张表（`allocation_orders` / `allocation_items` / `device_bindings`），与 ORM 零漂移、升降可逆
  - 5 分钟 confirm-token **不额外建表**：以 `device_bindings.status=PENDING` + `confirm_token_hash` 落在绑定行上，`bind` 成功时把摘要置空即「销毁」；`UNIQUE(device_id)` 把**单绑约束下沉到数据库层**（并发下也不依赖服务层的「先查后写」）
  - 新增 `BindingRecordStatus`（PENDING/BOUND/UNBOUND）与设备维度的 `BindingStatus`（BOUND/UNBOUND）**分开**：合成一个枚举会造出「设备已绑定、但记录还在等待确认」这种类型上无法自洽的状态
- **分配链路** `allocation_service`：创建（校验租户 ACTIVE、客户产品归属）/ 列表 / 详情 / 明细 / 幂等执行
  - 逐行校验并**部分失败不回滚**（成功的行必须保留，否则重跑会重复分配）；`FAILED → EXECUTING` 允许重跑（只补未成功的行），`COMPLETED` 再次调用是**幂等回放**；单数与明细行逐条给出失败原因
- **绑定链路** `binding_service`：`precheck` → `bind` 两步
  - `precheck`：解析格式 → 按 SN 查设备 → **SHA-256 命中**（用设备记录重算规范载荷比对摘要，因此无签名的集贤 `JX` 格式同样无法被篡改）→ 租户匹配 → 冻结拦截 → 单绑约束 → 可绑状态（只接受 `ALLOCATED`）→ 产品授权校验
  - `bind`：幂等键（**重放必须先于令牌校验**，否则第二次会因令牌已销毁而报过期，就不是「重放返回同一条」了）→ 冻结 → 单绑 → 令牌存在/未过期/摘要匹配 → 销毁令牌并写设备四维与事件与审计
  - 解绑：原因必填，恢复 `BOUND → ALLOCATED`，保留 `bindCount` 供识别反复解绑重绑
- **心跳与在线判定** `heartbeat_service`：`POST /device/heartbeat` **不使用 JWT**，以 `sn` + 设备密钥（SHA-256 摘要）鉴权；`online_status` 与 180 秒窗口口径统一到 `last_heartbeat_at` 单一事实来源
  - 新增 `POST /platform/devices/{id}/credentials` 签发 `DEVICE_SECRET`：明文仅回一次、库内只存摘要、续签覆盖同类型旧密钥（设备丢失时正是靠这个作废旧钥）——否则「任何人拿到 SN 就能把设备刷成在线」，而 SN 印在机身与包装上
  - 响应新增派生布尔 `online`；列表筛选与 `byOnlineStatus` 统计同样按窗口口径，避免「掉线了但枚举还写着 ONLINE」
  - `POST /platform/devices/{id}/simulate-heartbeat` 返回 `simulated: true`，`online=false` 可模拟窗口超时（否则验收要干等三分钟）
- **API**：新增 14 个端点 —— 平台端分配 5（列表/创建/详情/明细/执行）+ 设备 2（模拟心跳、签发密钥）+ 商户端 6（设备列表/详情/事件、绑定记录列表、precheck、bind、unbind）+ 设备侧 1（心跳）；`GET /platform/devices` 新增 `unallocated` 筛选（平台库存）；OpenAPI 快照 51 → **65 个路径**
- **前端**：平台端新增设备详情页（3 tab：概览 / 绑定 / 事件时间线）与分配管理页（新建弹窗含按租户联动的产品选择与设备多选、执行结果逐条展示失败原因）；商户端新增「我的设备」（两步扫码绑定弹窗，含预检回显与确认码倒计时）与「绑定管理」；设备库存页改造（新增「在线」列与「模拟心跳」）
- **演示数据**：种子新增 4 台**平台自有库存**设备（`tenant_id` 为空 + `IN_STOCK`）——订单生成的设备建时就带 `tenant_id`、批次导入的停在 `GENERATED`，没有这批数据时分配页的「选择设备」永远是空的
- **P-04 复核**：todo 中「修复 P-04（登录校验租户状态）」**已在 P1 修复**，本阶段复核确认（登录与刷新两条路径都校验 `tenant.status`，`test_auth.py` 已覆盖），未重复实现

### 修复

- **所有页头按钮显示成字面文本 `<span>刷新</span>`**（P2 遗留）：`shared/ui/components.js` 的 `button()` 嵌套 `html` 模板时漏了 `raw()`，整段被二次转义；P4 的设备页同样中招，此前验收未发现。已补 `raw()` + `esc(label)`
- **分配单弹窗选完租户后「可选设备」永远为空**：租户 `change` 处理把设备列表清空却没重新加载，而「先选租户」是必然动作 —— **分配功能实际完全不可用**。可选设备是「平台库存中已入库且未分配给任何租户」的那批，与目标租户无关，因此只清勾选、不清列表
- **设备详情时间线的事件明细显示字面 `<div>…</div>`**：调用方按「body 是 HTML」传参，而 `timeline()` 的契约是纯文本，被二次转义。改为调用方传纯文本并给 `.timeline-body` 补 `white-space: pre-line`；**刻意不把组件改成 `raw`**——body 含用户填写的解绑原因，组件转义是必要的 XSS 防线
- **签发设备密钥后详情页不刷新**：凭证区块仍显示「尚未签发设备密钥」，用户会误以为签发失败；且弹窗打开期间背景表格显示的是**上一轮的掩码**，看起来像「弹窗里的密钥与页面上不是同一个」。已把 `reload()` 挪到弹窗之前（先刷新、再弹窗）
- **空白串可当「原因」落库**：`BindingUnbindRequest` / `DeviceFreezeRequest` / `DeviceRetireRequest` 只有 `min_length=1`、不 strip，`"   "` 能过关并写进记录，时间线里出现「解绑设备 X：   」使责任追溯失效（P4 的 `rejectReason` 已是「空白不算填了」口径）。三处都加 strip 校验器 → 400 `VALIDATION_ERROR`
- **二维码前缀大小写导致自相矛盾**：`parse_payload` 用 `.upper()` 容忍 `jd|`（P4 单测已钉死该宽容行为），但 precheck 的 SHA-256 命中按字节严格比对，于是同一份输入先被判「格式合法」再被判「伪造」，报错文案误导排查。现只把**前缀段**归一为大写，其余字节仍严格比对（前缀是格式标签、不承载安全语义）
- **工作台阶段说明文案过期**：平台端仍写「P3/P4 已交付」、商户端仍写「当前处于 P2 前端地基阶段：商户端接口将在后续阶段交付」，与已交付的能力不符，已更新到 P5
- **P-06（删除无级联校验）**：租户 / 云服务商 / 产品模板 / 授权的删除端点先校验关联数据，存在时返回 `CASCADE_CONFLICT` 并在 `details` 中给出各项数量
- **P-03（新建产品未绑定客户）**：客户产品的 `tenant_id` 为 `NOT NULL`，创建时必须指定租户与模板且校验授权（`PRODUCT_NOT_AUTHORIZED`），租户详情可直接看到其产品
- **前端列表页事件监听器累积**：`createListPage` 的事件委托原绑在应用壳长期持有的容器上，跨导航不断叠加，表现为「点一次按钮弹出多个弹窗」；改为绑定到每次渲染新建的根节点
- **`updated_at` 导致异步惰性加载异常**：原用 SQL 侧 `onupdate=func.now()`，UPDATE 后属性被标记过期，紧随其后的 ORM 序列化触发 `MissingGreenlet`（5 个 PUT 接口 500）；改为 Python 侧 `default` / `onupdate`，同时保留 `server_default` 供非 ORM 路径
- **模板详情字段缺失**：`GET /platform/templates/{id}` 未装配 `cloudProviderName` 与两个计数，与列表接口不一致；新增 `get_template_detail()` 并让创建 / 详情 / 更新统一走它
- **错误码契约不一致**：模板 / 云服务商 / 客户产品编码重复原返回 400 `VALIDATION_ERROR`，与既有 409 约定不符
- **`utcnow()` 前向引用**：`TimestampMixin` 引用 `utcnow` 而定义在其后，导入期 `NameError`

- **厂商失败的审计口径与可检索性**：4G 生成因厂商未配置失败时原写 `GENERATE_DEVICES`，导致 `VENDOR_CALL_FAILED` 查不到；现按失败根因分类，并给 `record_failure()` 补 `resource_type` / `resource_id`，失败审计可按订单反查
- **批次文件错配**：同一份 CSV 用于另一个订单时原会静默复用旧批次，导致导入的设备挂到旧订单/旧租户；现校验订单一致性，不一致返回新增的 `BATCH_FILE_CONFLICT`(409) 并给出批次号与订单号
- **状态相关的页头动作不随状态更新**：订单详情页的页头按钮只在首屏算一次，审核通过后仍显示「审核通过/驳回」，必须手动刷新才出现「生成设备」；`createDetailPage` 新增 `setActions()` 并在重载时按新状态重算
- **`api.raw()` 形同虚设**（P2 遗留）：`request()` 形参里没有 `raw`（仅写在 JSDoc 中），且即便补上也会先消费 body 使调用方 `blob()` 抛 `Body is unusable`；现 `raw` 时跳过 body 消费，`api.raw()` 自身隐含该选项
- **冻结语义不对称**（P1 遗留）：原允许冻结 `GENERATED` 但解冻只能回到 `IN_STOCK`，「恢复原状态」无法成立；收紧为**只允许冻结 `IN_STOCK`**，并新增 14 条单元断言把该决策钉死
- **批次字段上限与数据库列长不匹配**：原统一 128 大于 `sn String(64)` 与 `imei/iccid/mac String(32)`，换 PostgreSQL 会写库报错；改为按字段各自上限校验
- **`duplicatedRows` 口径**：原把「库内已存在」与「文件内重复」合并计数，现只统计文件内重复（库内冲突计入无效行）
- **冻结端点文档过期**：OpenAPI 文案仍写「GENERATED / IN_STOCK 可冻结」，已与实现对齐
- **批次读路径租户收口**：`error-report` 与 `import` 未走 `assert_visible`，破坏 ADR-08 读路径统一收口的不变式，已补齐
- **「审核通过」确认框文案**：沿用了驳回流程的「驳回是不可逆的」说明，已改为符合通过语义

### 文档

- 新增 `docs/13-火山引擎硬件对话智能体配置说明.md`（控制台实测 + 官方文档取证，含取证边界声明）
- 新增 `docs/14-ESP32-S3刷机与联调步骤清单.md`（真机联调当天的可执行清单：四条接入路径决策树、首次对话判据、失败速查 F-01~F-10）：≥60% 内容来自**控制台实测**（账号、免费额度、已有产品与智能体）与官方文档正文；明确标注取证边界（《获取开发凭证》《调用方法》两页正文不可读，签名算法待校对）

### 计划中

- P6 烧录工厂端：脱敏序列化、烧录上报、抽检、固件；**并需决策两件 P5 遗留**：①「冻结已分配/已绑定设备」的能力（放宽 `ASSET_TRANSITIONS[FROZEN]` 需同步改 P4 已钉死的 14 条单测）；② `GENERATED → IN_STOCK` 的「入库」动作端点（批次导入的设备当前进不了分配链路）
- P5 待补测试：`tests/integration/test_allocation.py`、`test_tenant_isolation.py`（租户隔离参数化矩阵）、`test_device_heartbeat.py`（`test_binding.py` 的 53 条已落地）
- P8 终端用户小程序端：扫码激活双分支、WebSocket 流式对话
- P9 AI 配置与运营看板：知识库、音色、内容安全、指标聚合
- P10 部署与质量保障：Docker、CI、端到端全闭环测试
- P11 文档集与学习手册

### 说明

**迁移编号调整**：P1 落地时实际占用了 `0001`–`0004`（与原始计划的 0001/0002 不同），因此目录域顺延为 `0005`/`0006`，P7 使用 `0007`；P4 起顺延为 `0008`–`0010`（订单 / 设备 / 批次），P5 为 `0011`，P9 为 `0012`。

**测试规模**：后端 118 → **397** 条（P3 82 条：闭环、密钥不泄漏、级联冲突、租户作用域、筛选、JSON 入参、一次性密码；P7 49 条：注册表、模拟引擎可复现、无密钥安全失败；P4 146 条：订单状态机、二维码格式断言、设备四维与冻结语义、订单闭环与幂等、批次导入与安全限额、租户隔离矩阵）。

---

## [0.1.0] - 2026-09-16

### 新增

**项目脚手架**

- 完整目录结构（后端 / 前端 / 部署 / 脚本 / 文档 / 测试）
- `.gitignore`，排除 `.env`、`learning/`、`data/`、`__pycache__/`、`*.jar`
- `.env.example`，涵盖应用、数据库、鉴权、AI 厂商、对象存储、可观测性共十一大类配置，全部含中文注释且无真实密钥
- `Makefile`，提供 30+ 个统一开发命令（`help` / `setup` / `dev` / `test` / `migrate` / `seed` / `up` / `down` 等）
- `LICENSE`（MIT）
- `README.md`，含项目简介、四端角色、快速开始、架构图、技术栈、文档索引
- `CONTRIBUTING.md`，含代码规范、提交规范、测试要求与**八条架构红线**
- `SECURITY.md`，含安全设计说明、生产部署安全清单与已知安全边界
- `docker-compose.yml`，支持 SQLite 零依赖模式与 PostgreSQL 可选模式
- `deploy/Dockerfile.backend`，多阶段构建、非 root 运行、内置健康检查
- `deploy/nginx.conf`，含 API 反代、WebSocket 升级、流式响应免缓冲、静态资源缓存策略
- `backend/requirements.txt` 与 `requirements-dev.txt`
- `backend/pyproject.toml`，配置 Ruff、mypy（strict）、pytest、coverage

**项目管理**

- `todolist.md`：P0–P11 全量任务清单，含遗留缺陷修复对照表、安全缺陷处理表、架构决策记录（ADR）
- `项目进度.md`：进度追踪表，含总体进度条、里程碑、阻塞登记与更新约定

### 说明

- 本项目由三个遗留项目整合而成，整合策略为「各取所长」：
  - 静态原型提供 UI/交互规范、领域模型与 PRD
  - 参考站编译产物提供权威数据库 Schema 与多租户 RBAC 设计
  - 可运行 MVP 提供业务闭环语义（幂等绑定、confirm-token、脱敏、审计）
- **安全变更**：彻底移除历史版本中的硬编码弱口令，改为必须通过环境变量注入强密码，未配置时服务拒绝启动
- **安全变更**：厂商适配器在未配置密钥时返回 `VENDOR_UNAVAILABLE`，不再返回伪造的成功结果
- **安全变更**：修正了历史实现中二维码生成函数的参数顺序缺陷（租户标识曾被错误传入 `tenant_id` 位置）

---

[未发布]: ../../compare/v0.1.0...HEAD
[0.1.0]: ../../releases/tag/v0.1.0
