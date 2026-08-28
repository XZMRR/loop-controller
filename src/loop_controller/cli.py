"""Loop Controller CLI（v0.3.0 Iteration 5 / v0.5.0 扩展 proxy）。

审批入口：

    lc approvals list --config-dir config/
    lc approvals approve <decision_id> --approver <id> [--comment <text>]
    lc approvals deny <decision_id> --approver <id> [--comment <text>]

审计分析入口（v0.12.0）：

    lc audit analyze --task-id <id>
    lc audit analyze --session-id <id>
    lc audit list-alerts --task-id <id>
    lc audit list-alerts --session-id <id>

Proxy 入口（v0.5.0）：

    lc proxy --agent-id <id> --user-id <id> [--transport stdio|sse] [--port 8080]

stdio 模式的身份 token 仅通过环境变量 LOOP_CONTROLLER_IDENTITY_TOKEN 读取，
不再提供 --identity-token 参数，避免敏感 token 进入 shell history / ps 列表。

HTTP 服务入口（v0.17.0）：

    lc server [--host 127.0.0.1] [--port 8080] [--opa-url http://127.0.0.1:8181]

CLI 直接读写 ``JsonlApprovalStore``，不经过 Runtime，确保审批人与执行进程解耦。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime

from loop_controller.approval_service import ApprovalServiceError, build_approval_record
from loop_controller.audit_analyzer import RuleBasedAuditAnalyzer
from loop_controller.infra.alert_store import JsonlAlertStore
from loop_controller.infra.approval_store import ApprovalStoreError, JsonlApprovalStore
from loop_controller.infra.audit_store import JsonlAuditStore
from loop_controller.infra.config_loader import AppConfig, ConfigLoader
from loop_controller.proxy_server import LoopControllerProxyServer, ProxyIdentity
from loop_controller.runtime import build_runtime

# v0.17.0：server 依赖是可选的，导入延迟到命令执行时


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lc",
        description="Loop Controller 命令行入口",
    )
    parser.add_argument(
        "--config-dir",
        default="config",
        help="配置目录路径（默认：config）",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    approvals = subparsers.add_parser("approvals", help="审批管理")
    app_sub = approvals.add_subparsers(dest="approval_cmd", required=True)

    list_cmd = app_sub.add_parser("list", help="列出待审批请求")
    list_cmd.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="输出格式",
    )

    approve_cmd = app_sub.add_parser("approve", help="批准一个请求")
    approve_cmd.add_argument("decision_id", help="需要审批的 decision_id")
    approve_cmd.add_argument("--approver", required=True, help="审批人 ID")
    approve_cmd.add_argument("--comment", default="", help="审批意见")

    deny_cmd = app_sub.add_parser("deny", help="拒绝一个请求")
    deny_cmd.add_argument("decision_id", help="需要审批的 decision_id")
    deny_cmd.add_argument("--approver", required=True, help="审批人 ID")
    deny_cmd.add_argument("--comment", default="", help="审批意见")

    audit = subparsers.add_parser("audit", help="审计分析（v0.12.0）")
    audit_sub = audit.add_subparsers(dest="audit_cmd", required=True)

    analyze_cmd = audit_sub.add_parser("analyze", help="分析指定 task/session 的审计日志")
    analyze_cmd.add_argument("--task-id", default=None, help="Task ID")
    analyze_cmd.add_argument("--session-id", default=None, help="Session ID")

    alerts_cmd = audit_sub.add_parser("list-alerts", help="列出告警")
    alerts_cmd.add_argument("--task-id", default=None, help="Task ID")
    alerts_cmd.add_argument("--session-id", default=None, help="Session ID")
    alerts_cmd.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="输出格式",
    )

    proxy = subparsers.add_parser("proxy", help="启动 MCP Proxy（v0.5.0）")
    proxy.add_argument("--agent-id", required=True, help="映射到 agents.yaml 的 agent_id")
    proxy.add_argument("--user-id", required=True, help="外部 Agent 代表的用户 ID")
    proxy.add_argument("--session-id", default=None, help="复用已有 Session（可选）")
    proxy.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="传输协议（默认 stdio）",
    )
    proxy.add_argument("--host", default="127.0.0.1", help="SSE 模式 host（默认 127.0.0.1）")
    proxy.add_argument("--port", type=int, default=8080, help="SSE 模式端口（默认 8080）")
    proxy.add_argument(
        "--identity-cert",
        default=None,
        help="SSE 模式服务器 TLS 证书路径",
    )
    proxy.add_argument(
        "--identity-key",
        default=None,
        help="SSE 模式服务器 TLS 私钥路径",
    )
    proxy.add_argument(
        "--client-ca-cert",
        default=None,
        help="SSE 模式要求客户端 mTLS 的 CA 证书路径",
    )

    server = subparsers.add_parser("server", help="启动 HTTP 治理服务（v0.17.0）")
    server.add_argument("--host", default="127.0.0.1", help="监听 host（默认 127.0.0.1）")
    server.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    server.add_argument(
        "--opa-url",
        default="http://127.0.0.1:8181",
        help="OPA 服务地址（默认 http://127.0.0.1:8181）",
    )
    server.add_argument(
        "--require-auth",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="覆盖 entrypoints.http.require_auth",
    )

    grpc_server = subparsers.add_parser("grpc-server", help="启动 gRPC 治理服务（v0.19.0）")
    grpc_server.add_argument("--port", type=int, default=50051, help="监听端口（默认 50051）")
    grpc_server.add_argument(
        "--opa-url",
        default="http://127.0.0.1:8181",
        help="OPA 服务地址（默认 http://127.0.0.1:8181）",
    )
    grpc_server.add_argument(
        "--require-auth",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="覆盖 entrypoints.grpc.require_auth",
    )
    grpc_server.add_argument(
        "--key",
        default=None,
        help="gRPC 服务端 TLS 私钥路径",
    )
    grpc_server.add_argument(
        "--cert",
        default=None,
        help="gRPC 服务端 TLS 证书路径",
    )
    grpc_server.add_argument(
        "--client-ca",
        default=None,
        help="gRPC 客户端 mTLS CA 证书路径",
    )

    return parser


def _cmd_list(store: JsonlApprovalStore, args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    pending = store.get_pending()
    if not pending:
        print("没有待审批请求")
        return 0

    if args.format == "json":
        for req in pending:
            print(req.model_dump_json())
        return 0

    # table format
    print(f"{'decision_id':<32} {'tool_name':<20} {'requester':<16} {'status':<12} {'reason'}")
    print("-" * 105)
    for req in pending:
        expired = (
            req.original_decision is not None
            and req.original_decision.expires_at is not None
            and req.original_decision.expires_at < now
        )
        status = "[expired]" if expired else "pending"
        print(
            f"{req.decision_id:<32} "
            f"{req.tool_name:<20} "
            f"{req.requester_id:<16} "
            f"{status:<12} "
            f"{req.reason[:40]}"
        )
    return 0


def _cmd_approve_or_deny(
    store: JsonlApprovalStore,
    config: AppConfig,
    args: argparse.Namespace,
    verdict: str,
) -> int:
    decision_id = args.decision_id
    request = store.get_request(decision_id)
    existing_record = store.get_record(decision_id)

    try:
        record = build_approval_record(
            request,
            existing_record,
            args.approver,
            verdict,
            args.comment,
            approver_exists=lambda user_id: user_id in config.users,
        )
    except ApprovalServiceError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    try:
        store.record_response(record)
    except ApprovalStoreError as exc:
        print(f"错误：审批结果写入失败：{exc}", file=sys.stderr)
        return 1
    action = "批准" if verdict == "approve" else "拒绝"
    print(f"已{action} decision_id={decision_id}")
    return 0


def _build_audit_analyzer(config: AppConfig) -> RuleBasedAuditAnalyzer:
    """构造基于配置的 RuleBasedAuditAnalyzer（不启动 Runtime）。"""
    audit_key: bytes | None = None
    if config.audit_hash_algo == "hmac-sha256":
        audit_key = ConfigLoader.resolve_audit_key(config)
    audit_store = JsonlAuditStore(
        config.audit_log_path,
        hash_algo=config.audit_hash_algo,
        hmac_key=audit_key,
        key_id=config.audit_key_id,
    )
    alert_store = JsonlAlertStore(config.alert_store_path)
    return RuleBasedAuditAnalyzer(
        rules=config.audit_rules,
        audit_store=audit_store,
        alert_store=alert_store,
    )


async def _cmd_audit_analyze(config: AppConfig, args: argparse.Namespace) -> int:
    if not args.task_id and not args.session_id:
        print("错误：必须指定 --task-id 或 --session-id", file=sys.stderr)
        return 1
    analyzer = _build_audit_analyzer(config)
    if args.task_id:
        report = await analyzer.analyze_task(args.task_id)
    else:
        report = await analyzer.analyze_session(args.session_id)
    print(report.model_dump_json(indent=2))
    return 0


def _cmd_audit_list_alerts(config: AppConfig, args: argparse.Namespace) -> int:
    alert_store = JsonlAlertStore(config.alert_store_path)
    alerts = alert_store.list_alerts(
        session_id=args.session_id,
        task_id=args.task_id,
    )
    if not alerts:
        print("没有告警")
        return 0
    if args.format == "json":
        for alert in alerts:
            print(alert.model_dump_json())
        return 0
    print(f"{'alert_id':<32} {'rule_id':<24} {'severity':<10} {'description'}")
    print("-" * 100)
    for alert in alerts:
        print(f"{alert.alert_id:<32} {alert.rule_id:<24} {alert.severity:<10} {alert.description}")
    return 0


def _cmd_proxy(config: AppConfig, args: argparse.Namespace) -> int:
    """启动 MCP Proxy。"""
    runtime = build_runtime(config)

    async def start_and_run() -> None:
        await runtime.start()
        try:
            proxy = LoopControllerProxyServer(
                runtime,
                ProxyIdentity(
                    agent_id=args.agent_id,
                    user_id=args.user_id,
                    session_id=args.session_id,
                ),
                identity_token=os.environ.get("LOOP_CONTROLLER_IDENTITY_TOKEN"),
                identity_cert=args.identity_cert,
                identity_key=args.identity_key,
                client_ca_cert=args.client_ca_cert,
                entrypoints_config=config.entrypoints_config,
            )
            if args.transport == "stdio":
                await proxy.run_stdio()
            else:
                proxy.run_sse(host=args.host, port=args.port)
        finally:
            await runtime.aclose()

    try:
        asyncio.run(start_and_run())
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_server(config_dir: str, args: argparse.Namespace) -> int:
    """启动 HTTP 治理服务。"""
    try:
        import uvicorn

        from loop_controller.controller import build_controller
        from loop_controller.server import build_app, load_api_key
    except ImportError:
        print(
            "错误：启动 server 需要安装 server 依赖：uv pip install 'loop-controller[server]'",
            file=sys.stderr,
        )
        return 1

    async def run() -> None:
        config = ConfigLoader().load(config_dir, opa_base_url=args.opa_url)
        entrypoints_config = config.entrypoints_config
        if args.require_auth is not None:
            entrypoints_config = dict(entrypoints_config)
            http_cfg = dict(entrypoints_config.get("http") or {})
            http_cfg["require_auth"] = args.require_auth
            entrypoints_config["http"] = http_cfg
        controller = await build_controller(config, opa_url=args.opa_url)
        app = build_app(
            controller,
            api_key=load_api_key(),
            entrypoints_config=entrypoints_config,
        )
        server = uvicorn.Server(
            uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
        )
        await server.serve()

    asyncio.run(run())
    return 0


def _cmd_grpc_server(config_dir: str, args: argparse.Namespace) -> int:
    """启动 gRPC 治理服务。"""
    try:
        from loop_controller.controller import build_controller
        from loop_controller.grpc_server import serve
    except ImportError:
        print(
            "错误：启动 grpc-server 需要安装 grpc 依赖：uv pip install 'loop-controller[grpc]'",
            file=sys.stderr,
        )
        return 1

    async def run() -> None:
        config = ConfigLoader().load(config_dir, opa_base_url=args.opa_url)
        entrypoints_config = config.entrypoints_config
        if args.require_auth is not None:
            entrypoints_config = dict(entrypoints_config)
            grpc_cfg = dict(entrypoints_config.get("grpc") or {})
            grpc_cfg["require_auth"] = args.require_auth
            entrypoints_config["grpc"] = grpc_cfg
        controller = await build_controller(config, opa_url=args.opa_url)
        server = await serve(
            controller,
            port=args.port,
            entrypoints_config=entrypoints_config,
            server_key=args.key,
            server_cert=args.cert,
            client_ca_cert=args.client_ca,
            require_client_cert=args.client_ca is not None,
        )
        await server.wait_for_termination()

    asyncio.run(run())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    config_dir = args.config_dir

    if args.command == "proxy":
        config = ConfigLoader().load(config_dir)
        return _cmd_proxy(config, args)

    if args.command == "server":
        return _cmd_server(config_dir, args)

    if args.command == "grpc-server":
        return _cmd_grpc_server(config_dir, args)

    config = ConfigLoader().load(config_dir)

    if args.command == "audit":
        if args.audit_cmd == "analyze":
            return asyncio.run(_cmd_audit_analyze(config, args))
        if args.audit_cmd == "list-alerts":
            return _cmd_audit_list_alerts(config, args)
        parser.print_help()
        return 1

    store = JsonlApprovalStore(config.approval_store_path)

    if args.approval_cmd == "list":
        return _cmd_list(store, args)
    if args.approval_cmd == "approve":
        return _cmd_approve_or_deny(store, config, args, "approve")
    if args.approval_cmd == "deny":
        return _cmd_approve_or_deny(store, config, args, "deny")

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
