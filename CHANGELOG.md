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

**P7 AI 抽象与离线模拟引擎**

- `app/ai/base.py`：`AIProvider` 抽象（生命周期 / 设备侧 / 对话侧流式 / 语音侧）与 DTO
- `app/ai/registry.py`：四层供应商解析顺序（`client_product → template.vendor → ai_providers 默认 → AI_DEFAULT_PROVIDER`）
- `app/ai/mock/*`：规则对话（故事 / 歌 / 天气 + 兜底）、ASR `echo|fixed`、TTS `text|wav`，固定随机种子可复现，**无任何密钥即可跑通全流程**
- 四个厂商适配器骨架（集贤 / JoyInside / 火山引擎 / 百度）；**火山引擎按官方「硬件对话智能体」文档校对**：统一 `POST https://rtc.volcengineapi.com?Action=<Action>&Version=2025-08-01`、`Aibot*` / `IotVoicePrint*` / `TrainTTSVoiceType` 等 Action 与 `AibotCreate` 的 `Name` / `AccessType(public|private)` / `Config.ASRConfig.*` 结构
- Alembic `0007_ai_dialogue`（8 张表）；`app/api/v1/ai.py` 六个端点（`/ai/chat` 为流式）
- **已知局限**：厂商签名算法与部分 Body 字段名仍为占位（官方《调用方法》页不可读），已在代码中显式标注，待联调替换；`ADR-07` 安全失败行为已由测试钉住

### 修复

- **P-06（删除无级联校验）**：租户 / 云服务商 / 产品模板 / 授权的删除端点先校验关联数据，存在时返回 `CASCADE_CONFLICT` 并在 `details` 中给出各项数量
- **P-03（新建产品未绑定客户）**：客户产品的 `tenant_id` 为 `NOT NULL`，创建时必须指定租户与模板且校验授权（`PRODUCT_NOT_AUTHORIZED`），租户详情可直接看到其产品
- **前端列表页事件监听器累积**：`createListPage` 的事件委托原绑在应用壳长期持有的容器上，跨导航不断叠加，表现为「点一次按钮弹出多个弹窗」；改为绑定到每次渲染新建的根节点
- **`updated_at` 导致异步惰性加载异常**：原用 SQL 侧 `onupdate=func.now()`，UPDATE 后属性被标记过期，紧随其后的 ORM 序列化触发 `MissingGreenlet`（5 个 PUT 接口 500）；改为 Python 侧 `default` / `onupdate`，同时保留 `server_default` 供非 ORM 路径
- **模板详情字段缺失**：`GET /platform/templates/{id}` 未装配 `cloudProviderName` 与两个计数，与列表接口不一致；新增 `get_template_detail()` 并让创建 / 详情 / 更新统一走它
- **错误码契约不一致**：模板 / 云服务商 / 客户产品编码重复原返回 400 `VALIDATION_ERROR`，与既有 409 约定不符
- **`utcnow()` 前向引用**：`TimestampMixin` 引用 `utcnow` 而定义在其后，导入期 `NameError`

### 文档

- 新增 `docs/13-火山引擎硬件对话智能体配置说明.md`：≥60% 内容来自**控制台实测**（账号、免费额度、已有产品与智能体）与官方文档正文；明确标注取证边界（《获取开发凭证》《调用方法》两页正文不可读，签名算法待校对）

### 计划中

- P4 订单与设备生成：订单状态机、二维码双格式、批次导入
- P5 设备/分配/绑定：四维状态机、幂等绑定、租户隔离矩阵
- P6 烧录工厂端：脱敏序列化、烧录上报、抽检
- P8 终端用户小程序端：扫码激活双分支、WebSocket 流式对话
- P9 AI 配置与运营看板：知识库、音色、内容安全、指标聚合
- P10 部署与质量保障：Docker、CI、端到端全闭环测试
- P11 文档集与学习手册

### 说明

**迁移编号调整**：P1 落地时实际占用了 `0001`–`0004`（与原始计划的 0001/0002 不同），因此目录域顺延为 `0005`/`0006`，P7 使用 `0007`；P4 起顺延为 `0008`–`0010`（订单 / 设备 / 批次），P5 为 `0011`，P9 为 `0012`。

**测试规模**：后端 118 → **251** 条（P3 新增 82 条覆盖闭环、密钥不泄漏、级联冲突、租户作用域、筛选、JSON 入参、一次性密码；P7 新增 49 条覆盖注册表、模拟引擎可复现、无密钥安全失败）。

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
