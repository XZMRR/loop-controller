"""
T3 MCP 工具权限边界测试

测试问题：
1. MCP Server 自己声明的权限范围是否会被严格执行？
2. Agent/Client 能否越过 Server 声明的范围？
3. 如果 Client 侧（Loop Controller）再加一层策略校验，能否在调用前拦截越权请求？
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

# 当前脚本所在目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


class MCPPolicyGateway:
    """模拟 Loop Controller 在 MCP Client 侧的策略网关。"""

    def __init__(self, allowed_read_prefix: str = "/data/reports/"):
        self.allowed_read_prefix = allowed_read_prefix
        self.audit_log: list[dict] = []

    def check(self, tool_name: str, arguments: dict) -> tuple[bool, str]:
        """在调用 MCP Server 前执行策略校验。"""
        path = arguments.get("path", "")

        if tool_name == "read_file":
            if not path.startswith(self.allowed_read_prefix):
                return False, f"Policy 拒绝：禁止读取 {path}"

        if tool_name == "delete_file":
            return False, "Policy 拒绝：delete_file 在任何情况下都被禁止"

        if tool_name == "list_directory":
            if path != self.allowed_read_prefix:
                return False, f"Policy 拒绝：禁止列出 {path}"

        return True, "Policy 允许"

    def log(self, tool_name: str, arguments: dict, decision: str, reason: str):
        self.audit_log.append(
            {
                "tool": tool_name,
                "arguments": arguments,
                "decision": decision,
                "reason": reason,
            }
        )


async def call_tool_with_policy(
    session: ClientSession,
    gateway: MCPPolicyGateway,
    tool_name: str,
    arguments: dict,
):
    """带策略网关的工具调用。"""
    print(f"\n调用工具：{tool_name}({arguments})")

    # 第一步：Client 侧策略校验（模拟 Loop Controller）
    allowed, reason = gateway.check(tool_name, arguments)
    gateway.log(tool_name, arguments, "BLOCK" if not allowed else "ALLOW", reason)

    if not allowed:
        print(f"🚫 Client Policy 拦截：{reason}")
        return

    print(f"✅ Client Policy 放行：{reason}")

    # 第二步：调用 MCP Server
    try:
        result = await session.call_tool(tool_name, arguments)
        for content in result.content:
            if content.type == "text":
                print(f"📤 Server 返回：{content.text}")
    except Exception as e:
        print(f"❌ Server 调用异常：{type(e).__name__}: {str(e)[:300]}")


async def main():
    print("=" * 60)
    print("T3 MCP 工具权限边界测试")
    print("=" * 60)

    server_params = StdioServerParameters(
        command=sys.executable,  # 使用当前 venv 的 Python 解释器
        args=[os.path.join(SCRIPT_DIR, "t3_mcp_server.py")],
        env=None,
    )

    gateway = MCPPolicyGateway()

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # 列出可用工具
            tools = await session.list_tools()
            print(f"\n可用工具：{[tool.name for tool in tools.tools]}")

            # 测试用例
            test_cases = [
                ("read_file", {"path": "/data/reports/q3_sales.txt"}),  # 合法读取
                ("read_file", {"path": "/etc/passwd"}),  # 越权读取
                (
                    "delete_file",
                    {"path": "/data/reports/q3_sales.txt"},
                ),  # 删除（任何路径都禁止）
                ("list_directory", {"path": "/data/reports/"}),  # 合法列出
                ("list_directory", {"path": "/etc/"}),  # 越权列出
            ]

            print("\n" + "=" * 60)
            print("第一部分：带 Client Policy Gateway 的调用")
            print("=" * 60)

            for tool_name, arguments in test_cases:
                await call_tool_with_policy(session, gateway, tool_name, arguments)
                await asyncio.sleep(0.5)

            # 打印审计日志
            print("\n" + "=" * 60)
            print("审计日志（Loop Controller 视角）")
            print("=" * 60)
            for entry in gateway.audit_log:
                print(json.dumps(entry, ensure_ascii=False))

            # 对照实验：直接调用 Server，验证 Server 自身权限控制
            print("\n" + "=" * 60)
            print("第二部分：绕过 Client Policy，直接调用 Server")
            print("=" * 60)

            direct_test_cases = [
                ("read_file", {"path": "/data/reports/q3_sales.txt"}),  # 应成功
                ("read_file", {"path": "/etc/passwd"}),  # 应被 Server 拒绝
                (
                    "delete_file",
                    {"path": "/data/reports/q3_sales.txt"},
                ),  # 应被 Server 拒绝
            ]

            for tool_name, arguments in direct_test_cases:
                print(f"\n直接调用：{tool_name}({arguments})")
                try:
                    result = await session.call_tool(tool_name, arguments)
                    for content in result.content:
                        if content.type == "text":
                            print(f"📤 Server 返回：{content.text}")
                except Exception as e:
                    print(f"❌ 异常：{type(e).__name__}: {str(e)[:300]}")
                await asyncio.sleep(0.5)


if __name__ == "__main__":
    asyncio.run(main())
