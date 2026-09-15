from __future__ import annotations

import ssl
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from mcp import types

from loop_controller.executors.base import (
    DelegatedSubject,
    ExecutionContext,
    ExecutionReceiptType,
    ExecutionTerminalStatus,
    issue_execution_receipt,
)
from loop_controller.executors.mcp_executor import (
    PROTECTED_MCP_CAPABILITIES,
    PROTECTED_MCP_META_KEY,
    NetworkMCPClient,
    ProtectedMCPReceipt,
)


def _write_identity(tmp_path: Path) -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.UniformResourceIdentifier("spiffe://test/proxy")]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


def test_network_client_requires_https_and_builds_verified_mtls_context(tmp_path: Path) -> None:
    cert, key = _write_identity(tmp_path)
    with pytest.raises(ValueError, match="HTTPS"):
        NetworkMCPClient(
            url="http://localhost/mcp",
            ca_cert=cert,
            client_cert=cert,
            client_key=key,
            expected_workload="spiffe://test/proxy",
        )
    client = NetworkMCPClient(
        url="https://localhost/mcp",
        ca_cert=cert,
        client_cert=cert,
        client_key=key,
        expected_workload="spiffe://test/proxy",
    )
    assert client.ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert client.ssl_context.check_hostname is True


@pytest.mark.asyncio
async def test_network_client_sends_typed_meta_without_secret_values(tmp_path: Path) -> None:
    cert, key = _write_identity(tmp_path)
    client = NetworkMCPClient(
        url="https://localhost/mcp",
        ca_cert=cert,
        client_cert=cert,
        client_key=key,
        expected_workload="spiffe://test/proxy",
    )
    subject = DelegatedSubject(
        agent_id="agent",
        user_id="user",
        tenant_id="tenant",
        request_id="request",
        task_id="task",
        call_id="call",
        decision_id="decision",
        delegation_jti="jti",
    )
    context = ExecutionContext(
        call_id="call",
        task_id="task",
        agent_id="agent",
        user_id="user",
        tenant_id="tenant",
        request_id="request",
        interaction_id="interaction",
        decision_id="decision",
        delegation_jti="jti",
        delegated_subject=subject,
    )
    content = [{"type": "text", "text": "ok"}]
    receipt = issue_execution_receipt(
        receipt_type=ExecutionReceiptType.PROXY_ATTESTATION,
        context=context,
        attester_workload_id="spiffe://test/proxy",
        executor="mcp",
        backend="proxy",
        status=ExecutionTerminalStatus.SUCCESS,
        result=content,
    )
    call_tool = AsyncMock(
        return_value=types.CallToolResult(
            content=[types.TextContent(type="text", text="ok")],
            _meta={
                PROTECTED_MCP_META_KEY: ProtectedMCPReceipt(
                    supported_capabilities=PROTECTED_MCP_CAPABILITIES,
                    proxy_attestation=receipt,
                ).model_dump(mode="json")
            },
        )
    )
    class FakeSession:
        def __init__(self) -> None:
            self.initialize = AsyncMock()
            self.call_tool = call_tool

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    session = FakeSession()

    @asynccontextmanager
    async def fake_transport(*_args: object, **_kwargs: object):
        yield object(), object(), lambda: None

    with (
        patch("loop_controller.executors.mcp_executor.streamable_http_client", fake_transport),
        patch("loop_controller.executors.mcp_executor.ClientSession", return_value=session),
    ):
        response = await client.call_tool("tool", {"input": "safe"}, context)
    meta = call_tool.await_args.kwargs["meta"][PROTECTED_MCP_META_KEY]
    assert meta["delegated_subject"] == subject.model_dump(mode="json")
    assert set(meta["required_capabilities"]) == PROTECTED_MCP_CAPABILITIES
    assert "secret" not in str(meta).lower()
    assert response.proxy_attestation == receipt
    await client.aclose()
