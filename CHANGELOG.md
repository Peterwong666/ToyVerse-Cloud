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

### 计划中

- P3 目录域：云服务商、产品模板、授权、客户产品
- P4 订单与设备生成：订单状态机、二维码双格式、批次导入
- P5 设备/分配/绑定：四维状态机、幂等绑定、租户隔离矩阵
- P6 烧录工厂端：脱敏序列化、烧录上报、抽检
- P7 AI 抽象与离线模拟引擎：Provider 注册表、四个厂商骨架
- P8 终端用户小程序端：扫码激活双分支、WebSocket 流式对话
- P9 AI 配置与运营看板：知识库、音色、内容安全、指标聚合
- P10 部署与质量保障：Docker、CI、端到端全闭环测试
- P11 文档集与学习手册

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
