# 贡献指南

感谢你对 Loop Controller 的关注。本文档说明如何参与本项目：环境搭建、
开发流程、测试要求与提交规范。

## 项目结构

本仓库为单仓库三栈结构：

| 目录 | 栈 | 说明 |
|---|---|---|
| `src/loop_controller/` | Python 3.12+ | 治理层核心（R1/R2/R3、三入口、执行器、IIGE、Go 内核桥接） |
| `go/` | Go | A2A 交互治理内核（调度、委托、死信、SSE） |
| `frontend/` | Vue 3 + TypeScript + Element Plus | 管理治理台 |
| `config/` | YAML | 运行配置样例（含开发用占位凭据，生产必须替换） |
| `tests/` | pytest | Python 单元与集成测试 |
| `policies/` | Rego | OPA 策略 |

## 环境搭建

```powershell
# Python（推荐 uv）
uv sync --dev --extra server

# OPA 二进制（集成测试需要）
mkdir tools
Invoke-WebRequest -Uri "https://openpolicyagent.org/downloads/v1.19.0/opa_windows_amd64.exe" -OutFile "tools\opa.exe"

# Go：安装 1.23+，go.mod 已锁定工具链

# 前端
cd frontend
npm install
npx playwright install chromium
```

## 开发流程

1. 从 `backend/v0.55-dev` 切功能分支（命名如 `feat/xxx`、`fix/xxx`）。
2. 开发完成后本地跑通下方全部相关检查。
3. 提交 PR，CI 绿灯后方可合并。

## 提交前必须通过的检查

### Python

```powershell
$env:PYTHONPATH="src"
$env:LOOP_CONTROLLER_AUDIT_HMAC_KEY="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
.venv\Scripts\python.exe -m pytest tests/ -q
uv run ruff check src tests
uv run mypy src
```

### Go

```powershell
cd go
go vet ./...
go test -race ./...
```

### 前端

```powershell
cd frontend
npm run test          # vitest 83 用例
npm run typecheck     # vue-tsc，必须 0 错误
npm run build         # 生产构建
npm run test:e2e      # E2E 19 用例（后端依赖全部 stub，无需启动 Python/Go）
```

### 真实栈联调（涉及前后端契约变更时）

Vue 前端（:5173）→ Python 代理（:8000）→ Go 内核（:8080）三进程联调，
确认端到端链路后再提 PR。详见
[frontend/docs/frontend_development_plan.md](frontend/docs/frontend_development_plan.md)。

## 提交规范

- 提交信息使用英文 conventional 前缀：`feat:` / `fix:` / `docs:` /
  `test:` / `refactor:` / `chore:`，一行摘要说明动机。
- 文档（含进度记录）不出现人员指称与日期，以轮次号或版本号锚定。
- 不提交运行期产物：`data/*.db`、`*.jsonl`、`*.lock` 已被
  `.gitignore` 覆盖，请勿强行添加。
- 不提交任何真实凭据；`config/` 中的 token/key 一律为开发占位值，
  生产凭据通过环境变量注入（配置中的 `*_token_env` 声明）。

## 行为准则

- 尊重问题报告者：复现步骤比结论更重要。
- 安全相关问题请私下报告，不要公开开 issue（见
  [README](README.md) 安全注意事项）。

## 许可证

提交即表示你同意你的贡献以 Apache-2.0 许可证发布（见 [LICENSE](LICENSE)）。
