"""MCP 工具执行器：stdio 兼容路径与受保护 Streamable HTTP 客户端。"""

from __future__ import annotations

import hashlib
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography import x509
from mcp import ClientSession  # type: ignore[import-untyped]
from mcp.client.streamable_http import streamable_http_client  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict

from loop_controller.executors.base import (
    DelegatedSubject,
    ExecutionContext,
    ExecutionReceipt,
    ExecutionReceiptType,
    ExecutionTerminalStatus,
    ToolExecutor,
    verify_execution_receipt,
)
from loop_controller.mcp_gateway import MCPGateway
from loop_controller.models import CapabilityProfile, Tool, ToolResult
from loop_controller.secrets.models import ResolvedToolCredentialRef

PROTECTED_MCP_META_KEY = "io.loop-controller/protected-mcp-v1"
PROTECTED_MCP_CAPABILITIES = frozenset(
    {
        "execution_receipt_v1",
        "protected_mcp_network_v1",
        "delegated_subject_binding_v1",
    }
)


class ProtectedMCPEnvelope(BaseModel):
    """放入 MCP ``_meta`` 的受保护调用合同；只含逻辑凭证引用。"""

    model_config = ConfigDict(frozen=True)

    delegated_subject: DelegatedSubject
    interaction_id: str | None = None
    required_capabilities: frozenset[str]
    credential_refs: tuple[ResolvedToolCredentialRef, ...] = ()


class ProtectedMCPReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    supported_capabilities: frozenset[str]
    proxy_attestation: ExecutionReceipt


@dataclass(frozen=True)
class NetworkMCPResponse:
    content: Any
    is_error: bool
    error_code: str | None
    supported_capabilities: frozenset[str]
    proxy_attestation: ExecutionReceipt


class _PeerCertificateBindingTransport(httpx.AsyncBaseTransport):
    """在 httpx 已完成链/主机名校验后，再绑定 URI SAN 或证书指纹。"""

    def __init__(
        self,
        ssl_context: ssl.SSLContext,
        *,
        expected_workload: str | None,
        cert_sha256: str | None,
    ) -> None:
        self._transport = httpx.AsyncHTTPTransport(verify=ssl_context)
        self._expected_workload = expected_workload
        self._cert_sha256 = cert_sha256.lower().replace(":", "") if cert_sha256 else None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._transport.handle_async_request(request)
        stream = response.extensions.get("network_stream")
        ssl_object = stream.get_extra_info("ssl_object") if stream is not None else None
        der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
        if not der:
            await response.aclose()
            raise ssl.SSLError("TLS peer certificate unavailable")
        if self._cert_sha256 and hashlib.sha256(der).hexdigest() != self._cert_sha256:
            await response.aclose()
            raise ssl.SSLError("TLS peer certificate fingerprint mismatch")
        if self._expected_workload:
            cert = x509.load_der_x509_certificate(der)
            try:
                sans = cert.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName
                ).value.get_values_for_type(x509.UniformResourceIdentifier)
            except x509.ExtensionNotFound:
                sans = []
            if self._expected_workload not in sans:
                await response.aclose()
                raise ssl.SSLError("TLS peer URI SAN workload mismatch")
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()


class NetworkMCPClient:
    """真实 MCP Streamable HTTP 客户端，固定使用 HTTPS+mTLS。"""

    transport = "streamable_http+https+mtls"

    def __init__(
        self,
        *,
        url: str,
        ca_cert: str,
        client_cert: str,
        client_key: str,
        expected_workload: str | None = None,
        cert_sha256: str | None = None,
        required_capabilities: frozenset[str] = PROTECTED_MCP_CAPABILITIES,
        timeout_seconds: float = 30.0,
    ) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("protected MCP URL must use HTTPS")
        if not expected_workload and not cert_sha256:
            raise ValueError("protected MCP requires expected_workload or cert_sha256 pin")
        for value in (ca_cert, client_cert, client_key):
            if not Path(value).is_file():
                raise ValueError(f"protected MCP TLS file unavailable: {value}")
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca_cert)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_cert_chain(client_cert, client_key)
        self.ssl_context = context
        self.url = url
        self.authenticated_proxy_workload_id = expected_workload or f"cert-sha256:{cert_sha256}"
        self.required_capabilities = required_capabilities
        self._http_client = httpx.AsyncClient(
            transport=_PeerCertificateBindingTransport(
                context,
                expected_workload=expected_workload,
                cert_sha256=cert_sha256,
            ),
            timeout=httpx.Timeout(timeout_seconds, read=timeout_seconds * 10),
            trust_env=False,
        )

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any], context: ExecutionContext
    ) -> NetworkMCPResponse:
        subject = context.delegated_subject or DelegatedSubject(
            agent_id=context.agent_id,
            user_id=context.user_id or None,
            tenant_id=context.tenant_id or "",
            request_id=context.request_id or "",
            task_id=context.task_id,
            call_id=context.call_id,
            decision_id=context.decision_id or "",
            delegation_jti=context.delegation_jti,
        )
        envelope = ProtectedMCPEnvelope(
            delegated_subject=subject,
            interaction_id=context.interaction_id,
            required_capabilities=self.required_capabilities | context.security_capabilities,
            credential_refs=context.resolved_credentials,
        )
        async with streamable_http_client(
            self.url, http_client=self._http_client, terminate_on_close=True
        ) as (read_stream, write_stream, _session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    tool_name,
                    arguments,
                    meta={PROTECTED_MCP_META_KEY: envelope.model_dump(mode="json")},
                )
        receipt_raw = (result.meta or {}).get(PROTECTED_MCP_META_KEY)
        if not isinstance(receipt_raw, dict):
            raise ValueError("protected MCP response missing receipt metadata")
        receipt = ProtectedMCPReceipt.model_validate(receipt_raw)
        missing = envelope.required_capabilities - receipt.supported_capabilities
        if missing:
            raise ValueError(f"protected MCP proxy lacks capabilities: {sorted(missing)}")
        content = [item.model_dump(mode="json") for item in result.content]
        terminal = receipt.proxy_attestation.status
        if result.isError != (terminal != ExecutionTerminalStatus.SUCCESS):
            raise ValueError("protected MCP response terminal status mismatch")
        return NetworkMCPResponse(
            content=content,
            is_error=result.isError,
            error_code="proxy_error" if result.isError else None,
            supported_capabilities=receipt.supported_capabilities,
            proxy_attestation=receipt.proxy_attestation,
        )

    async def aclose(self) -> None:
        await self._http_client.aclose()


class MCPExecutor(ToolExecutor):
    """通过 MCPGateway 或受保护 MCP Proxy 执行工具调用。"""

    security_capabilities = PROTECTED_MCP_CAPABILITIES

    def __init__(
        self,
        gateway: MCPGateway,
        *,
        security_mode: str = "compatibility",
        network_client: NetworkMCPClient | None = None,
        credential_refs: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._gateway = gateway
        self._security_mode = security_mode
        self._network_client = network_client
        self._credential_refs = credential_refs or {}

    @property
    def security_egress_type(self) -> str:
        return "protected_mcp_network" if self._network_client is not None else "stdio"

    @property
    def supports_strict_security(self) -> bool:
        return (
            self._security_mode == "strict"
            and self._network_client is not None
            and self._network_client.transport == "streamable_http+https+mtls"
        )

    def secret_refs_for(self, tool_name: str) -> list[str]:
        return list(self._credential_refs.get(tool_name, ()))

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult:
        """compatibility 使用 stdio gateway；strict 仅接受认证网络 Proxy 回执。"""
        if self._security_mode == "strict":
            if not self.supports_strict_security:
                return ToolResult(
                    call_id=context.call_id,
                    task_id=context.task_id,
                    tool_name=tool_name,
                    status="error",
                    content="protected MCP network client unavailable",
                    error_code="protected_mcp_unavailable",
                )
            assert self._network_client is not None
            response = await self._network_client.call_tool(tool_name, arguments, context)
            terminal = response.proxy_attestation.status
            verify_execution_receipt(
                response.proxy_attestation,
                expected_type=ExecutionReceiptType.PROXY_ATTESTATION,
                context=context,
                status=terminal,
                result=response.content,
                attester_workload_id=self._network_client.authenticated_proxy_workload_id,
                executor="mcp",
            )
            return ToolResult(
                call_id=context.call_id,
                task_id=context.task_id,
                tool_name=tool_name,
                status="success" if terminal == ExecutionTerminalStatus.SUCCESS else "error",
                content=response.content,
                terminal_status=terminal.value,
                error_code=response.error_code,
                execution_receipt=response.proxy_attestation,
            )
        return await self._gateway.call_tool(
            tool_name,
            arguments,
            context.call_id,
            context.task_id,
            agent_id=context.agent_id,
            user_id=context.user_id,
            session_id=context.session_id,
            tenant_id=context.tenant_id,
        )

    async def list_tools(self, profile: CapabilityProfile) -> list[Tool]:
        return await self._gateway.list_tools(profile)
