# Go 交互治理内核（v0.36.1）

本目录包含 Loop Controller 的 Go 交互治理内核，负责 Agent 之间的横向交互治理（A2A）。

## 结构

```
go/
├── cmd/kernel/main.go       # HTTP 服务入口
├── internal/
│   ├── models/              # JSON 模型
│   ├── registry/            # Agent Card 注册表
│   ├── task/                # 交互任务管理
│   ├── router/              # 消息路由
│   ├── delegation/          # 委托决策
│   └── api/                 # HTTP handlers
```

## 运行

```bash
cd go
go run ./cmd/kernel -addr :8080
```

## 测试

```bash
cd go
go test ./...
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/a2a/v1/agents` | 注册 Agent |
| GET | `/a2a/v1/agents` | 列出 Agent |
| GET | `/a2a/v1/agents/{id}` | 查询 Agent |
| POST | `/a2a/v1/tasks` | 创建任务 |
| GET | `/a2a/v1/tasks/{id}` | 查询任务 |
| POST | `/a2a/v1/messages` | 发送消息 |
| POST | `/a2a/v1/delegations` | 请求委托 |
| GET | `/health` | 健康检查 |

## 与 Python 层的关系

Python 工具治理层（R1→R2→R3）继续负责单次工具调用的策略、审批、执行与审计。
Go 内核只负责 Agent 间交互治理，两者通过 `src/loop_controller/go_kernel_bridge.py` 通信。
