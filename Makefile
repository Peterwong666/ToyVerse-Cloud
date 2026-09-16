# ============================================================
# ToyVerse Cloud — 多租户 AI 智能玩具 SaaS 平台
# Makefile：统一开发入口
# 运行 `make` 或 `make help` 查看全部可用命令
# ============================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help

BACKEND_DIR  := backend
FRONTEND_DIR := frontend
VENV         := $(BACKEND_DIR)/.venv
PY           := $(VENV)/bin/python
PIP          := $(VENV)/bin/pip

# 颜色 —— 用 printf 生成真实转义字节，使普通 echo 也能正确着色
C_RESET := $(shell printf '\033[0m')
C_BOLD  := $(shell printf '\033[1m')
C_CYAN  := $(shell printf '\033[36m')
C_GREEN := $(shell printf '\033[32m')

.PHONY: help
help: ## 显示全部可用命令
	@printf '\n$(C_BOLD)ToyVerse Cloud — 可用命令$(C_RESET)\n\n'
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  $(C_CYAN)%-22s$(C_RESET) %s\n", $$1, $$2}'
	@printf '\n'

# ------------------------------------------------------------
# 环境准备
# ------------------------------------------------------------

.PHONY: setup
setup: ## 一键初始化：创建虚拟环境 + 安装依赖 + 生成 .env + 迁移 + 种子数据
	@$(MAKE) venv
	@$(MAKE) install
	@$(MAKE) env
	@$(MAKE) migrate
	@$(MAKE) seed
	@echo "$(C_GREEN)✔ 初始化完成，运行 'make dev' 启动服务$(C_RESET)"

.PHONY: venv
venv: ## 创建 Python 虚拟环境
	@if [ ! -d "$(VENV)" ]; then \
		echo "→ 创建虚拟环境 $(VENV) ..."; \
		python3 -m venv $(VENV); \
		echo "$(C_GREEN)✔ 虚拟环境已创建$(C_RESET)"; \
	else \
		echo "✔ 虚拟环境已存在，跳过"; \
	fi

.PHONY: install
install: venv ## 安装依赖（含开发依赖）
	@echo "→ 安装依赖 ..."
	@$(PIP) install --upgrade pip -q
	@$(PIP) install -r $(BACKEND_DIR)/requirements.txt -q
	@$(PIP) install -r $(BACKEND_DIR)/requirements-dev.txt -q
	@echo "$(C_GREEN)✔ 依赖安装完成$(C_RESET)"

.PHONY: env
env: ## 由 .env.example 生成 .env（已存在则不覆盖）
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "$(C_GREEN)✔ 已生成 .env$(C_RESET)"; \
		echo "  ⚠️  请编辑 .env，为以下变量设置强密码："; \
		echo "     PLATFORM_ADMIN_PASSWORD / MERCHANT_ADMIN_PASSWORD / FACTORY_ADMIN_PASSWORD"; \
	else \
		echo "✔ .env 已存在，跳过（如需重置请手动删除）"; \
	fi

# ------------------------------------------------------------
# 数据库
# ------------------------------------------------------------

.PHONY: migrate
migrate: ## 执行数据库迁移到最新版本
	@cd $(BACKEND_DIR) && ../$(PY) -m alembic upgrade head

.PHONY: migration
migration: ## 新建迁移（用法：make migration m="描述"）
	@if [ -z "$(m)" ]; then echo "✗ 请提供描述：make migration m=\"add xxx table\""; exit 1; fi
	@cd $(BACKEND_DIR) && ../$(PY) -m alembic revision --autogenerate -m "$(m)"

.PHONY: downgrade
downgrade: ## 回滚一个迁移版本
	@cd $(BACKEND_DIR) && ../$(PY) -m alembic downgrade -1

.PHONY: seed
seed: ## 写入演示数据（幂等，可重复执行）
	@$(PY) scripts/seed_demo.py

.PHONY: reset-db
reset-db: ## ⚠️ 删除数据库并重建（会丢失全部数据）
	@echo "⚠️  即将删除数据库并重建，5 秒内可按 Ctrl+C 取消 ..."
	@sleep 5
	@rm -f data/*.db data/*.sqlite3 2>/dev/null || true
	@$(MAKE) migrate
	@$(MAKE) seed
	@echo "$(C_GREEN)✔ 数据库已重置$(C_RESET)"

# ------------------------------------------------------------
# 运行
# ------------------------------------------------------------

.PHONY: dev
dev: ## 启动后端开发服务器（热重载，含前端静态托管）
	@echo "→ 后端： http://localhost:8000"
	@echo "→ API 文档： http://localhost:8000/docs"
	@echo "→ 平台端： http://localhost:8000/platform/"
	@echo "→ 商户端： http://localhost:8000/merchant/"
	@echo "→ 工厂端： http://localhost:8000/factory/"
	@echo "→ 小程序： http://localhost:8000/miniapp/"
	@cd $(BACKEND_DIR) && ../$(PY) -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

.PHONY: serve
serve: ## 启动后端服务（生产模式，无热重载）
	@cd $(BACKEND_DIR) && ../$(PY) -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4

# ------------------------------------------------------------
# 测试与质量
# ------------------------------------------------------------

.PHONY: test
test: ## 运行全部测试
	@cd $(BACKEND_DIR) && ../$(PY) -m pytest -q

.PHONY: test-unit
test-unit: ## 仅运行单元测试
	@cd $(BACKEND_DIR) && ../$(PY) -m pytest tests/unit -q

.PHONY: test-integration
test-integration: ## 仅运行集成测试（含租户隔离矩阵）
	@cd $(BACKEND_DIR) && ../$(PY) -m pytest tests/integration -q

.PHONY: test-e2e
test-e2e: ## 仅运行端到端全闭环测试
	@cd $(BACKEND_DIR) && ../$(PY) -m pytest tests/e2e -q

.PHONY: coverage
coverage: ## 运行测试并生成覆盖率报告
	@cd $(BACKEND_DIR) && ../$(PY) -m pytest --cov=app --cov-report=term-missing --cov-report=html -q
	@echo "$(C_GREEN)✔ HTML 报告：$(BACKEND_DIR)/htmlcov/index.html$(C_RESET)"

.PHONY: lint
lint: ## 代码检查（ruff）
	@cd $(BACKEND_DIR) && ../$(PY) -m ruff check app tests ../scripts

.PHONY: format
format: ## 代码格式化（ruff format）
	@cd $(BACKEND_DIR) && ../$(PY) -m ruff format app tests ../scripts
	@cd $(BACKEND_DIR) && ../$(PY) -m ruff check --fix app tests ../scripts

.PHONY: typecheck
typecheck: ## 静态类型检查（mypy）
	@cd $(BACKEND_DIR) && ../$(PY) -m mypy app

.PHONY: check
check: lint typecheck test openapi-check ## 执行全部质量检查
	@echo "$(C_GREEN)✔ 全部检查通过$(C_RESET)"

# ------------------------------------------------------------
# 工具脚本
# ------------------------------------------------------------

.PHONY: openapi
openapi: ## 导出 OpenAPI 契约快照到 tests/contract/
	@$(PY) scripts/export_openapi.py

.PHONY: openapi-check
openapi-check: ## 校验 API 契约与快照是否一致（有差异则失败）
	@$(PY) scripts/export_openapi.py --check

.PHONY: smoke
smoke: ## 对运行中的服务执行冒烟测试
	@bash scripts/smoke_test.sh

.PHONY: qrcodes
qrcodes: ## 生成演示二维码清单
	@$(PY) scripts/gen_qrcodes.py

.PHONY: qr-verify
qr-verify: ## 验证前端手写二维码编码器的正确性（与独立实现逐模块比对）
	@$(PY) scripts/verify_qrcode.py

.PHONY: fe-check
fe-check: ## 校验前端 ES Module 导入契约（路径与具名导出）
	@$(PY) scripts/verify_frontend_imports.py

# ------------------------------------------------------------
# Docker 部署
# ------------------------------------------------------------

.PHONY: up
up: ## 启动完整 Docker 环境（应用 + PostgreSQL）
	@cd deploy && docker compose up -d --build
	@echo "$(C_GREEN)✔ 已启动$(C_RESET)"
	@echo "→ 应用： http://localhost:8000"
	@echo "→ 平台端： http://localhost:8000/platform/"

.PHONY: down
down: ## 停止 Docker 环境（保留数据卷）
	@cd deploy && docker compose down

.PHONY: down-clean
down-clean: ## ⚠️ 停止 Docker 环境并删除数据卷
	@cd deploy && docker compose down -v

.PHONY: logs
logs: ## 查看 Docker 日志
	@cd deploy && docker compose logs -f --tail=100

.PHONY: ps
ps: ## 查看 Docker 服务状态
	@cd deploy && docker compose ps

# ------------------------------------------------------------
# 清理
# ------------------------------------------------------------

.PHONY: clean
clean: ## 清理缓存与构建产物
	@find . -type d -name "__pycache__" -not -path "./backend/.venv/*" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	@rm -rf $(BACKEND_DIR)/htmlcov $(BACKEND_DIR)/.coverage 2>/dev/null || true
	@echo "$(C_GREEN)✔ 清理完成$(C_RESET)"

.PHONY: clean-all
clean-all: clean ## ⚠️ 清理全部产物（含虚拟环境与数据库）
	@rm -rf $(VENV)
	@rm -rf data
	@echo "$(C_GREEN)✔ 已清理虚拟环境与数据$(C_RESET)"
