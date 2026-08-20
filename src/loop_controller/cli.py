"""Loop Controller CLI（v0.3.0 Iteration 5 / v0.5.0 扩展 proxy）。

审批入口：

    lc approvals list --config-dir config/
    lc approvals approve <decision_id> --approver <id> [--comment <text>]
    lc approvals deny <decision_id> --approver <id> [--comment <text>]

Proxy 入口（v0.5.0）：

    lc proxy --agent-id <id> --user-id <id> [--transport stdio|sse] [--port 8080]

CLI 直接读写 ``JsonlApprovalStore``，不经过 Runtime，确保审批人与执行进程解耦。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from loop_controller.infra.approval_store import JsonlApprovalStore
from loop_controller.infra.config_loader import AppConfig, ConfigLoader
from loop_controller.models import ApprovalRecord
from loop_controller.proxy_server import LoopControllerProxyServer, ProxyIdentity
from loop_controller.runtime import build_runtime


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

    return parser


def _cmd_list(store: JsonlApprovalStore, args: argparse.Namespace) -> int:
    pending = store.get_pending()
    if not pending:
        print("没有待审批请求")
        return 0

    if args.format == "json":

        for req in pending:
            print(req.model_dump_json())
        return 0

    # table format
    print(f"{'decision_id':<32} {'tool_name':<20} {'requester':<16} {'reason'}")
    print("-" * 90)
    for req in pending:
        print(
            f"{req.decision_id:<32} "
            f"{req.tool_name:<20} "
            f"{req.requester_id:<16} "
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
    if request is None:
        print(f"错误：未找到 decision_id={decision_id} 的审批请求", file=sys.stderr)
        return 1

    if store.get_record(decision_id) is not None:
        print(f"错误：decision_id={decision_id} 已审批", file=sys.stderr)
        return 1

    # v0.3.0：审批人不能是请求者或执行 Agent，且必须存在于用户列表
    if args.approver == request.requester_id:
        print("错误：审批人不能是请求者本人", file=sys.stderr)
        return 1
    if args.approver == request.agent_id:
        print("错误：审批人不能是执行 Agent", file=sys.stderr)
        return 1
    if args.approver not in config.users:
        print(f"错误：审批人 {args.approver} 不存在", file=sys.stderr)
        return 1

    # v0.3.0：deny 必须填写审批意见
    if verdict == "deny" and not args.comment.strip():
        print("错误：deny 必须提供 --comment 审批意见", file=sys.stderr)
        return 1

    record = ApprovalRecord(
        request_id=request.request_id,
        decision_id=decision_id,
        verdict=verdict,  # type: ignore[arg-type]
        approver_id=args.approver,
        comment=args.comment,
        decided_at=datetime.now(UTC),
    )
    store.record_response(record)
    action = "批准" if verdict == "approve" else "拒绝"
    print(f"已{action} decision_id={decision_id}")
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


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    config = ConfigLoader().load(args.config_dir)

    if args.command == "proxy":
        return _cmd_proxy(config, args)

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
