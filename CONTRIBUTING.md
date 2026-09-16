# 贡献指南

感谢你对 ToyVerse Cloud 的关注。本文档说明如何参与本项目的开发。

---

## 目录

- [开发环境准备](#开发环境准备)
- [项目结构约定](#项目结构约定)
- [代码规范](#代码规范)
- [提交规范](#提交规范)
- [测试要求](#测试要求)
- [Pull Request 流程](#pull-request-流程)
- [架构红线](#架构红线)

---

## 开发环境准备

前置条件：

- Python 3.12 及以上
- Git
- Docker 与 Docker Compose（仅部署调试需要）
- **无需 Node.js** —— 前端为零构建 ES Module

```bash
git clone <repo-url>
cd saas_ai_toy_platform
cp .env.example .env    # 必须设置 PLATFORM_ADMIN_PASSWORD 等强密码
make setup              # 创建虚拟环境 + 安装依赖 + 迁移 + 演示数据
make dev
```

---

## 项目结构约定

| 目录 | 职责 | 约定 |
|---|---|---|
| `backend/app/core/` | 配置、鉴权、依赖注入、错误码、日志 | 不含业务逻辑 |
| `backend/app/models/` | SQLAlchemy 模型 | 仅描述数据结构，不含业务方法 |
| `backend/app/schemas/` | Pydantic 模型 | 与 `models/` 一一对应，命名加 `...Create` / `...Update` / `...Out` |
| `backend/app/services/` | 领域服务 | **业务逻辑只写在这里** |
| `backend/app/api/v1/` | 路由层 | 只做参数解析与调用服务，**不写业务逻辑** |
| `backend/app/ai/` | AI Provider 抽象与适配器 | 新厂商实现 `AIProvider` 接口后注册 |
| `backend/tests/` | 测试 | 按 `unit/` `integration/` `e2e/` 分层 |
| `frontend/shared/` | 四端共享代码 | 改动会影响全部四端，需谨慎 |
| `frontend/pages/<端>/` | 各端页面 | 不跨端引用 |

---

## 代码规范

本项目使用 **Ruff** 做代码检查与格式化，**mypy（strict 模式）** 做静态类型检查。

```bash
make lint        # 检查
make format      # 自动格式化
make typecheck   # 类型检查
```

关键要求：

- 行宽上限 **110** 字符
- 类型注解**必需**（mypy strict 模式，`tests/` 除外）
- 使用 `async`/`await` 异步风格，不使用同步数据库调用
- 异常统一使用 `app.core.errors` 中的 `AppException` 子类，携带项目定义错误码
- 日志使用 `app.core.logging` 中的 logger，不使用 `print`

---

## 提交规范

采用 [Conventional Commits](https://www.conventionalcommits.org/) 规范：

```
<类型>(<范围>): <简短描述>

<可选正文>

<可选脚注>
```

**类型**：

| 类型 | 用途 |
|---|---|
| `feat` | 新功能 |
| `fix` | 缺陷修复 |
| `docs` | 文档变更 |
| `refactor` | 重构（不改变行为） |
| `test` | 测试相关 |
| `chore` | 构建、依赖、工具链 |
| `perf` | 性能优化 |
| `security` | 安全相关修复 |

**范围**：`platform` / `merchant` / `factory` / `miniapp` / `auth` / `device` / `order` / `ai` / `deploy` / `docs`

**示例**：

```
feat(device): 实现设备四维状态机的冻结与解冻

冻结时记录 previous_asset_status，解冻时恢复原状态。
新增 device_events 时间线记录。

Closes #42
```

---

## 测试要求

| 变更类型 | 最低测试要求 |
|---|---|
| 新增 API 端点 | 集成测试（含成功、校验失败、权限不足三类） |
| **商户端 / 工厂端端点** | **必须包含租户隔离测试**（断言 A 租户读不到 B 租户数据） |
| 新增领域服务 | 单元测试覆盖主要分支与边界 |
| 状态机变更 | 单元测试覆盖全部合法迁移 + 非法迁移被拒绝 |
| 缺陷修复 | 先写能复现缺陷的测试，再修复 |

```bash
make test              # 提交前必须全绿
make coverage          # 查看覆盖率
```

---

## Pull Request 流程

1. 从 `main` 创建分支：`git checkout -b feat/device-freeze`
2. 完成开发，确保 `make check` 全绿（lint + typecheck + test）
3. 同步更新文档（`docs/`）与任务清单（`todolist.md`、`项目进度.md`）
4. 若变更了 API，运行 `make openapi` 更新契约快照并一并提交
5. 提交 PR，描述中说明：**变更内容、原因、测试方式、影响的端**
6. 等待评审通过后合并

---

## 架构红线

以下约定**不可违反**，评审时会被直接驳回：

| # | 红线 | 原因 |
|---|---|---|
| 1 | **不得在路由层手写租户过滤**，必须走 repository 层的 `tenant_scope` | 防止遗漏导致越权 |
| 2 | **任何响应不得包含云服务商 SecretKey** | 密钥泄漏 |
| 3 | **工厂端接口不得返回真实客户名、金额、联系方式** | 商业信息隔离 |
| 4 | **不得硬编码任何密码、密钥或 Token** | 历史教训：曾出现硬编码弱口令 |
| 5 | **未配置密钥的厂商必须返回 `VENDOR_UNAVAILABLE`，不得伪造成功** | 避免误导使用者 |
| 6 | **不得提交 `learning/` 目录内容** | 该目录为个人学习资料，不上传 |
| 7 | **不得引入前端构建步骤** | 保持「克隆即可运行」 |
| 8 | 领域状态只能通过状态机服务迁移，不得直接赋值 | 保证状态一致性 |
