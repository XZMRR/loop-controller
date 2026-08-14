"""策略引擎.

PolicyEngine 负责把业务输入交给 OPA/Rego 并返回结构化判定结果。
MVP 提供：
- OPAPolicyEngine：调用本地 OPA HTTP 服务；
- MockPolicyEngine：用于测试和快速原型，不依赖外部 OPA 进程。
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable


@runtime_checkable
class PolicyEngine(Protocol):
    """策略引擎接口."""

    def evaluate(self, package: str, input_doc: dict[str, Any]) -> dict[str, Any]:
        """评估输入文档，返回策略判定结果.

        Args:
            package: Rego 包名，如 "loop_controller.tool_permission"。
            input_doc: 输入数据，包含 proposal、profile 等。

        Returns:
            至少包含 verdict 和 reason 的字典。
        """
        ...


class OPAPolicyEngine:
    """调用本地 OPA HTTP 服务的策略引擎.

    使用 OPA REST API：`POST /v1/data/<package>`。
    需要先在本地启动 OPA：`opa run --server --bundle policies/`
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8181") -> None:
        """初始化.

        Args:
            base_url: OPA HTTP 服务地址。
        """
        self._base_url = base_url.rstrip("/")

    def evaluate(self, package: str, input_doc: dict[str, Any]) -> dict[str, Any]:
        """通过 HTTP 调用 OPA.

        直接查询 `{package}/result`，返回顶层 Decision 结构，避免 OPA
        在查询包根路径时偶尔返回空文档的问题。
        """
        import http.client
        import urllib.parse

        parsed = urllib.parse.urlparse(f"{self._base_url}/v1/data/{package.replace('.', '/')}/result")
        path = parsed.path
        data = self._serialize(input_doc)

        conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=5)
        try:
            conn.request(
                "POST",
                path,
                body=data,
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            body = response.read().decode("utf-8")
        finally:
            conn.close()

        return self._parse(body)

    @staticmethod
    def _serialize(input_doc: dict[str, Any]) -> bytes:
        import json

        return json.dumps({"input": input_doc}).encode("utf-8")

    @staticmethod
    def _parse(body: str) -> dict[str, Any]:
        import json

        response = json.loads(body)
        # 查询 /result 时，OPA 返回 {"result": {"verdict": ..., "reason": ...}}
        return response.get("result", {})


class MockPolicyEngine:
    """内存版策略引擎，用于测试和不依赖 OPA 的场景.

    根据简单规则返回 verdict：
    - 工具不在 allowed_tools 中 → deny；
    - send_email 外部邮箱 → require_approval；
    - 其他 → allow。
    """

    def evaluate(self, package: str, input_doc: dict[str, Any]) -> dict[str, Any]:
        """基于输入文档中的 profile 和 proposal 做简化判定."""
        proposal = input_doc.get("proposal", {})
        profile = input_doc.get("profile", {})
        tool_name = proposal.get("tool_name", "")
        allowed_tools = profile.get("allowed_tools", [])
        args = proposal.get("arguments", {})

        if tool_name not in allowed_tools:
            return {"verdict": "deny", "reason": f"Tool {tool_name} not in allowed_tools"}

        if tool_name == "send_email":
            to = str(args.get("to", "")).lower()
            if "@company.com" not in to:
                return {
                    "verdict": "require_approval",
                    "reason": "External email address requires R0-delegate approval",
                }

        if tool_name == "write_file":
            path = str(args.get("path", ""))
            if "/tmp/" not in path and "/allowed/" not in path:
                return {"verdict": "deny", "reason": f"Write path {path} not allowed"}

        return {"verdict": "allow", "reason": "Policy allows this action"}


class PolicyEngineError(Exception):
    """策略引擎调用异常."""


PolicyVerdict = Literal["allow", "deny", "modify", "require_approval"]
