"""
T3 MCP 工具权限边界测试（真实攻击场景版）

测试真实 filesystem server 会面临的攻击：
1. 直接越权读取
2. 路径遍历攻击（../../../etc/passwd）
3. 符号链接绕过
4. 在允许目录外创建文件
5. 删除操作
6. Client Policy Gateway vs Server enforcement 的对比
"""

import asyncio
import json
import os
import posixpath
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


class MCPPolicyGateway:
    """模拟 Loop Controller 在 MCP Client 侧的策略网关。"""

    def __init__(self, allowed_directories: list[str] = None):
        self.allowed_directories = allowed_directories or ["/data/reports"]
        self.audit_log: list[dict] = []

    def normalize(self, path_str: str) -> str:
        """按 POSIX 语义规范化路径，与 server 保持一致。"""
        if path_str.startswith("~"):
            path_str = "/home/user" + path_str[1:]
        normalized = posixpath.normpath(path_str)
        if not posixpath.isabs(normalized):
            normalized = posixpath.join("/data/reports", normalized)
        return normalized

    def is_allowed(self, path_str: str) -> bool:
        normalized = self.normalize(path_str)
        for allowed in self.allowed_directories:
            normalized_allowed = posixpath.normpath(allowed)
            if normalized == normalized_allowed:
                return True
            prefix = normalized_allowed.rstrip("/") + "/"
            if normalized.startswith(prefix):
                return True
        return False

    def check(self, tool_name: str, arguments: dict) -> tuple[bool, str]:
        path = arguments.get("path", "")

        if tool_name in ("read_file", "write_file", "list_directory", "delete_file"):
            if not self.is_allowed(path):
                return False, f"Policy 拒绝：路径 {path} 不在允许目录内"

        if tool_name == "delete_file":
            return False, "Policy 拒绝：delete_file 在任何情况下都被禁止"

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

    allowed, reason = gateway.check(tool_name, arguments)
    gateway.log(tool_name, arguments, "BLOCK" if not allowed else "ALLOW", reason)

    if not allowed:
        print(f"🚫 Client Policy 拦截：{reason}")
        return

    print(f"✅ Client Policy 放行：{reason}")

    try:
        result = await session.call_tool(tool_name, arguments)
        for content in result.content:
            if content.type == "text":
                print(f"📤 Server 返回：{content.text}")
    except Exception as e:
        print(f"❌ Server 调用异常：{type(e).__name__}: {str(e)[:300]}")


async def main():
    print("=" * 70)
    print("T3 MCP 工具权限边界测试（真实攻击场景版）")
    print("=" * 70)

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(SCRIPT_DIR, "t3_mcp_server_realistic.py")],
        env=None,
    )

    gateway = MCPPolicyGateway()

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            tools = await session.list_tools()
            print(f"\n可用工具：{[tool.name for tool in tools.tools]}")

            print("\n" + "=" * 70)
            print("第一部分：带 Client Policy Gateway 的调用")
            print("=" * 70)

            test_cases = [
                ("read_file", {"path": "/data/reports/q3_sales.txt"}),  # 合法
                ("read_file", {"path": "/etc/passwd"}),  # 直接越权
                ("read_file", {"path": "/data/reports/../../etc/passwd"}),  # 路径遍历
                ("read_file", {"path": "/data/reports/link_to_passwd"}),  # 符号链接绕过
                ("write_file", {"path": "/data/reports/new_file.txt", "content": "test"}),  # 合法写入
                ("write_file", {"path": "/etc/malicious.txt", "content": "test"}),  # 越权写入
                ("delete_file", {"path": "/data/reports/q3_sales.txt"}),  # 删除
                ("list_directory", {"path": "/data/reports/"}),  # 合法列出
                ("list_directory", {"path": "/etc/"}),  # 越权列出
            ]

            for tool_name, arguments in test_cases:
                await call_tool_with_policy(session, gateway, tool_name, arguments)
                await asyncio.sleep(0.3)

            print("\n" + "=" * 70)
            print("审计日志（Loop Controller 视角）")
            print("=" * 70)
            for entry in gateway.audit_log:
                print(json.dumps(entry, ensure_ascii=False))

            print("\n" + "=" * 70)
            print("第二部分：绕过 Client Policy，直接调用 Server")
            print("=" * 70)

            direct_test_cases = [
                ("read_file", {"path": "/data/reports/q3_sales.txt"}),
                ("read_file", {"path": "/etc/passwd"}),
                ("read_file", {"path": "/data/reports/../../etc/passwd"}),
                ("read_file", {"path": "/data/reports/link_to_passwd"}),
                ("write_file", {"path": "/etc/malicious.txt", "content": "test"}),
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
                await asyncio.sleep(0.3)


if __name__ == "__main__":
    asyncio.run(main())
