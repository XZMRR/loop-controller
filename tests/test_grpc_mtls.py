"""gRPC mTLS 身份认证测试（v0.20.0）。

验证 `_extract_client_cert_identity` 能从 gRPC auth_context 解析证书 CN/SAN，
以及 ToolGovernanceServicer 在 require_auth 开启时的证书校验行为。
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("grpc")

from loop_controller.controller import LoopController
from loop_controller.grpc_server import (
    ToolGovernanceServicer,
    _extract_client_cert_identity,
)
from loop_controller.identity import MTLSIdentityProvider
from loop_controller.models import Agent, GovernanceResult


class _MockContext:
    """模拟 grpc_aio.ServicerContext。"""

    def __init__(self, auth_context: dict[str, list[Any]] | None = None):
        self._auth_context = auth_context or {}
        self.code: Any | None = None
        self.details: str | None = None
        self.invocation_metadata = []

    def auth_context(self) -> dict[str, list[Any]]:
        return self._auth_context

    def set_code(self, code: Any) -> None:
        self.code = code

    def set_details(self, details: str) -> None:
        self.details = details


class _MockController(LoopController):
    def __init__(self) -> None:
        self.tool_calls: list[dict[str, Any]] = []
        self._tool_response = GovernanceResult(
            status="allow",
            call_id="c1",
            tool_name="send_email",
            arguments={},
            content="email sent",
        )

    async def evaluate_and_execute(
        self,
        *,
        agent_id: str,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        **kwargs: Any,
    ) -> GovernanceResult:
        self.tool_calls.append(
            {
                "agent_id": agent_id,
                "user_id": user_id,
                "tool_name": tool_name,
                "arguments": arguments,
                "kwargs": kwargs,
            }
        )
        return self._tool_response


def _mtls_provider() -> MTLSIdentityProvider:
    agent = Agent(
        agent_id="researcher_001",
        name="RA",
        profile_id="research_assistant_v1",
        owner_id="zhang_manager",
    )
    return MTLSIdentityProvider(
        agents={"researcher_001": agent},
        users={"researcher_001": "Alice"},
        cert_subject_template="agent-{agent_id}-prod-{harness_id}",
    )


class TestExtractClientCertIdentity:
    """测试从 gRPC auth_context 提取证书信息。"""

    def test_extract_cn_and_sans(self) -> None:
        ctx = _MockContext(
            {
                "x509_common_name": [b"CN=agent-researcher_001"],
                "x509_subject_alternative_name": [b"agent.example.com"],
            }
        )
        credential = _extract_client_cert_identity(ctx)
        assert credential is not None
        assert credential.cert_cn == "agent-researcher_001"
        assert credential.cert_sans == ["agent.example.com"]

    def test_extract_cn_from_pem(self) -> None:
        # PEM 文本中只要包含 CN=... 即可触发 fallback 解析。
        pem = (
            "-----BEGIN CERTIFICATE-----\n"
            "subject=/CN=agent-researcher_001/O=company\n"
            "-----END CERTIFICATE-----"
        )
        ctx = _MockContext({"x509_pem_cert": [pem.encode("utf-8")]})
        credential = _extract_client_cert_identity(ctx)
        assert credential is not None
        assert credential.cert_cn == "agent-researcher_001"

    def test_no_cert_returns_none(self) -> None:
        ctx = _MockContext({})
        assert _extract_client_cert_identity(ctx) is None


class TestGRPCMTLSAuth:
    """gRPC servicer mTLS 认证测试。"""

    @pytest.fixture
    def controller(self) -> _MockController:
        return _MockController()

    @pytest.fixture
    def request_proto(self) -> Any:
        from loop_controller.v1 import governance_pb2

        return governance_pb2.EvaluateToolCallRequest(
            agent_id="researcher_001",
            user_id="researcher_001",
            tool_name="send_email",
            arguments_json='{"to":"zhang@company.com"}',
        )

    @pytest.mark.asyncio
    async def test_require_auth_without_cert_unauthenticated(
        self, controller: _MockController, request_proto: Any
    ) -> None:
        servicer = ToolGovernanceServicer(
            controller,
            identity_provider=_mtls_provider(),
            entrypoints_config={"grpc": {"require_auth": True}},
        )
        context = _MockContext({})
        await servicer.EvaluateToolCall(request_proto, context)
        assert context.code is not None
        assert "UNAUTHENTICATED" in str(context.code)
        assert "client certificate" in context.details

    @pytest.mark.asyncio
    async def test_require_auth_with_valid_cert_executes(
        self, controller: _MockController, request_proto: Any
    ) -> None:
        servicer = ToolGovernanceServicer(
            controller,
            identity_provider=_mtls_provider(),
            entrypoints_config={"grpc": {"require_auth": True}},
        )
        context = _MockContext(
            {
                "x509_common_name": [b"CN=agent-researcher_001-prod-001"],
                "x509_subject_alternative_name": [b"agent.example.com"],
            }
        )
        response = await servicer.EvaluateToolCall(request_proto, context)
        assert context.code is None
        assert response.status == "allow"
        assert response.result == "email sent"
        assert len(controller.tool_calls) == 1
        assert controller.tool_calls[0]["agent_id"] == "researcher_001"

    @pytest.mark.asyncio
    async def test_inconsistent_agent_id_rejected(
        self, controller: _MockController, request_proto: Any
    ) -> None:
        servicer = ToolGovernanceServicer(
            controller,
            identity_provider=_mtls_provider(),
            entrypoints_config={"grpc": {"require_auth": True}},
        )
        context = _MockContext(
            {
                "x509_common_name": [b"CN=agent-researcher_001-prod-001"],
            }
        )
        request_proto.agent_id = "other_agent"
        await servicer.EvaluateToolCall(request_proto, context)
        assert context.code is not None
        assert "INVALID_ARGUMENT" in str(context.code)
        assert "agent_id inconsistent" in context.details

    @pytest.mark.asyncio
    async def test_no_auth_config_allows_insecure_call(
        self, controller: _MockController, request_proto: Any
    ) -> None:
        servicer = ToolGovernanceServicer(
            controller,
            identity_provider=_mtls_provider(),
            entrypoints_config={},
        )
        context = _MockContext({})
        response = await servicer.EvaluateToolCall(request_proto, context)
        assert context.code is None
        assert response.status == "allow"
        assert controller.tool_calls[0]["agent_id"] == "researcher_001"
