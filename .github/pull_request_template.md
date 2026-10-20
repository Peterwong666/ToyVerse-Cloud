## 变更内容

<!-- 简要说明做了什么、为什么做。关联 issue 请写 Closes #N -->

## 影响范围

<!-- 影响的端 / 模块：platform / merchant / factory / miniapp / auth / device / order / ai / deploy / docs -->

## 测试方式

<!-- 如何验证本变更：新增/修改了哪些测试、手工验收路径 -->

## 提交前自查

- [ ] 本地 `make check` 全绿（ruff + mypy strict + pytest + 契约 + 文档一致性）
- [ ] 变更了 API 时已运行 `make openapi` 并一并提交契约快照
- [ ] 同步更新了相关文档（`docs/`），`make docs-check` 通过
- [ ] 商户端/工厂端端点包含租户隔离测试（如适用）
- [ ] 未违反 [CONTRIBUTING.md](../blob/main/CONTRIBUTING.md#架构红线) 的架构红线
