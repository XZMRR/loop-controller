"""执行器抽象：ToolExecutor / ExecutionContext / ExecutorRegistry。"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from loop_controller.execution_mode import ExecutionMode
from loop_controller.secrets.models import ResolvedToolCredentialRef

if TYPE_CHECKING:
    from loop_controller.execution_mode import ExecutionModeResolver
    from loop_controller.models import CapabilityProfile, Tool, ToolResult


def extract_declared_secret_refs(arguments: dict[str, Any]) -> list[str]:
    """递归提取调用参数声明的 Secret 引用（仅作为可信配置的补充）。"""
    refs: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key == "secret_ref":
                    if isinstance(nested, str):
                        refs.add(nested)
                    elif isinstance(nested, dict) and isinstance(nested.get("name"), str):
                        refs.add(nested["name"])
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(arguments)
    return sorted(refs)


class WorkloadAuthMethod(StrEnum):
    MTLS = "mtls"
    TRUSTED_PROXY_MTLS = "trusted-proxy-mtls"
    STATIC_DEV = "static-dev"


class WorkloadIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)

    workload_id: str
    service: str
    principal: str
    tenant_id: str | None = None
    authenticated_at: datetime
    expires_at: datetime | None = None
    auth_method: WorkloadAuthMethod
    authenticated_instance_id: str | None = None


class DelegatedSubject(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_id: str
    user_id: str | None = None
    tenant_id: str
    request_id: str
    task_id: str
    call_id: str
    decision_id: str
    delegation_jti: str | None = None


class ExecutionReceiptType(StrEnum):
    CONTROLLER_EXECUTION_RECORD = "controller_execution_record"
    PROXY_ATTESTATION = "proxy_attestation"
    REMOTE_EXECUTION_ATTESTATION = "remote_execution_attestation"


class ExecutionTerminalStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


_MAX_SAFE_JSON_INTEGER = (1 << 53) - 1


def _canonical_json_subset(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if not -_MAX_SAFE_JSON_INTEGER <= value <= _MAX_SAFE_JSON_INTEGER:
            raise ValueError("JSON integers must be within the interoperable IEEE-754 safe range")
        return str(value)
    if isinstance(value, float):
        raise ValueError("JSON floating-point values are outside the result_sha256 wire subset")
    if isinstance(value, str):
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except UnicodeEncodeError as exc:
            raise ValueError("JSON strings must contain valid Unicode scalar values") from exc
    if isinstance(value, list):
        return "[" + ",".join(_canonical_json_subset(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return "{" + ",".join(
            f"{_canonical_json_subset(key)}:{_canonical_json_subset(value[key])}"
            for key in sorted(value)
        ) + "}"
    raise ValueError(f"unsupported result_sha256 JSON value: {type(value).__name__}")


def canonicalize_execution_result(
    result: Any,
) -> tuple[bytes, str, str | None]:
    """按跨语言 wire 子集或原始文本/二进制生成摘要输入。"""
    if isinstance(result, bytes):
        return result, "application/octet-stream", None
    if isinstance(result, str):
        return result.encode("utf-8"), "text/plain", "utf-8"
    payload = _canonical_json_subset(result).encode("utf-8")
    return payload, "application/json", "utf-8"


def result_sha256(result: Any) -> str:
    """计算稳定的跨语言结果摘要。"""
    payload, _, _ = canonicalize_execution_result(result)
    return hashlib.sha256(payload).hexdigest()


class ExecutionReceipt(BaseModel):
    """受保护出口回执；JSON 摘要使用跨语言受限 canonical JSON wire 子集。"""

    model_config = ConfigDict(frozen=True)

    receipt_id: str
    type: ExecutionReceiptType
    attester_workload_id: str
    authenticated_instance_id: str | None = None
    request_id: str
    interaction_id: str | None = None
    task_id: str
    call_id: str
    decision_id: str
    delegation_jti: str | None = None
    executor: str
    backend: str
    credential_ref_digest: str | None = None
    resolved_credential_version: str | None = None
    resolved_credential_digest: str | None = None
    status: ExecutionTerminalStatus
    result_sha256: str
    media_type: str
    encoding: str | None = None
    issued_at: datetime

    @staticmethod
    def hash_result(result: Any) -> tuple[str, str, str | None]:
        """返回跨语言 wire 合同的 (sha256, media_type, encoding)。"""
        payload, media_type, encoding = canonicalize_execution_result(result)
        return hashlib.sha256(payload).hexdigest(), media_type, encoding


def credential_ref_digest(ref: ResolvedToolCredentialRef) -> str:
    payload, _, _ = canonicalize_execution_result(ref.model_dump(mode="json"))
    return hashlib.sha256(payload).hexdigest()


def issue_execution_receipt(
    *,
    receipt_type: ExecutionReceiptType,
    context: ExecutionContext,
    attester_workload_id: str,
    executor: str,
    backend: str,
    status: ExecutionTerminalStatus,
    result: Any,
    authenticated_instance_id: str | None = None,
    credential: ResolvedToolCredentialRef | None = None,
) -> ExecutionReceipt:
    """由当前证明边界的真实签发方生成 typed receipt。"""
    digest, media_type, encoding = ExecutionReceipt.hash_result(result)
    return ExecutionReceipt(
        receipt_id=uuid.uuid4().hex,
        type=receipt_type,
        attester_workload_id=attester_workload_id,
        authenticated_instance_id=authenticated_instance_id,
        request_id=context.request_id or "",
        interaction_id=context.interaction_id,
        task_id=context.task_id,
        call_id=context.call_id,
        decision_id=context.decision_id or "",
        delegation_jti=context.delegation_jti,
        executor=executor,
        backend=backend,
        credential_ref_digest=credential_ref_digest(credential) if credential else None,
        resolved_credential_version=credential.resolved_version if credential else None,
        status=status,
        result_sha256=digest,
        media_type=media_type,
        encoding=encoding,
        issued_at=datetime.now(UTC),
    )


def verify_execution_receipt(
    receipt: ExecutionReceipt,
    *,
    expected_type: ExecutionReceiptType,
    context: ExecutionContext,
    status: ExecutionTerminalStatus,
    result: Any,
    attester_workload_id: str | None = None,
    executor: str | None = None,
    backend: str | None = None,
) -> None:
    """验证证明类型、可信 attester、全 correlation、终态和结果摘要。"""
    expected = {
        "type": expected_type,
        "request_id": context.request_id or "",
        "interaction_id": context.interaction_id,
        "task_id": context.task_id,
        "call_id": context.call_id,
        "decision_id": context.decision_id or "",
        "delegation_jti": context.delegation_jti,
        "status": status,
        "result_sha256": result_sha256(result),
    }
    if attester_workload_id is not None:
        expected["attester_workload_id"] = attester_workload_id
    if executor is not None:
        expected["executor"] = executor
    if backend is not None:
        expected["backend"] = backend
    mismatches = [name for name, value in expected.items() if getattr(receipt, name) != value]
    if mismatches:
        raise ValueError(f"execution receipt mismatch: {', '.join(mismatches)}")


class ExecutionContext:
    """执行器运行时的治理上下文。"""

    def __init__(
        self,
        *,
        call_id: str,
        task_id: str,
        agent_id: str,
        user_id: str,
        session_id: str | None = None,
        tenant_id: str | None = None,
        request_id: str | None = None,
        interaction_id: str | None = None,
        decision_id: str | None = None,
        delegation_jti: str | None = None,
        workload_identity: WorkloadIdentity | None = None,
        delegated_subject: DelegatedSubject | None = None,
        resolved_credentials: tuple[ResolvedToolCredentialRef, ...] = (),
        security_capabilities: frozenset[str] = frozenset(),
    ) -> None:
        self.call_id = call_id
        self.task_id = task_id
        self.agent_id = agent_id
        self.user_id = user_id
        self.session_id = session_id
        self.tenant_id = tenant_id
        self.request_id = request_id
        self.interaction_id = interaction_id
        self.decision_id = decision_id
        self.delegation_jti = delegation_jti
        self.workload_identity = workload_identity
        self.delegated_subject = delegated_subject
        self.resolved_credentials = resolved_credentials
        self.security_capabilities = security_capabilities


@runtime_checkable
class ToolExecutor(Protocol):
    """工具执行器协议。

    实现者负责把规范化工具名和参数转换为真实副作用，并返回 ToolResult。
    """

    def secret_refs_for(self, tool_name: str) -> list[str]:
        """返回执行器当前配置中工具实际依赖的 Secret 引用。"""
        ...

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult:
        """执行工具调用并返回结果。"""
        ...

    async def list_tools(self, profile: CapabilityProfile) -> list[Tool]:
        """返回按 Profile 过滤后的工具列表。"""
        ...


class ExecutorRegistryError(Exception):
    """执行器注册表错误。"""


class ExecutorRegistry:
    """工具名到执行器的注册表，支持按工具注册和默认执行器回退。

    v0.31.0 新增 execution_mode_resolver：当存在时，resolve_executor()
    根据执行策略决定使用本地执行器还是 Harness 执行器。
    """

    def __init__(self) -> None:
        self._executors: dict[str, ToolExecutor] = {}
        self._default: ToolExecutor | None = None
        self._mode_resolver: ExecutionModeResolver | None = None

    def register(self, tool_name: str, executor: ToolExecutor) -> None:
        """为特定工具名注册执行器。"""
        if not isinstance(executor, ToolExecutor):
            raise TypeError(f"执行器 {executor!r} 不符合 ToolExecutor 协议")
        self._executors[tool_name] = executor

    def set_default(self, executor: ToolExecutor) -> None:
        """设置默认执行器；当工具没有显式注册时回退到默认执行器。"""
        if not isinstance(executor, ToolExecutor):
            raise TypeError(f"执行器 {executor!r} 不符合 ToolExecutor 协议")
        self._default = executor

    def set_mode_resolver(self, resolver: ExecutionModeResolver) -> None:
        """v0.31.0：注入执行模式解析器。"""
        self._mode_resolver = resolver

    def get_executor(self, tool_name: str) -> ToolExecutor:
        """按工具名获取执行器；不存在且没有默认执行器时抛出异常。"""
        executor = self._executors.get(tool_name)
        if executor is not None:
            return executor
        if self._default is not None:
            return self._default
        raise ExecutorRegistryError(f"工具 {tool_name!r} 没有注册执行器")

    def resolve_executor(
        self, tool_name: str, *, security_mode: str = "compatibility"
    ) -> ToolExecutor | None:
        """按策略解析执行器；strict 禁止默认、本地出口和任何 fallback。"""
        if security_mode == "strict":
            executor = self._executors.get(tool_name)
            if executor is None:
                return None
            if not getattr(executor, "supports_strict_security", False):
                return None
            if getattr(executor, "security_egress_type", None) in {
                None,
                "default",
                "local",
                "stdio",
                "subprocess",
            }:
                return None
            if not getattr(executor, "security_capabilities", frozenset()):
                return None
            return executor
        if self._mode_resolver is None:
            return self.get_executor(tool_name)
        mode = self._mode_resolver.resolve(tool_name)
        if mode == ExecutionMode.DENY:
            return None
        if mode == ExecutionMode.HARNESS:
            return self._mode_resolver.harness_executor
        return self.get_executor(tool_name)

    def resolve_secret_refs(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> list[str]:
        """合并执行器可信配置与调用参数补充声明中的 Secret 引用。"""
        executor = self._executors.get(tool_name) or self._default
        trusted_refs = executor.secret_refs_for(tool_name) if executor is not None else []
        declared_refs = extract_declared_secret_refs(arguments)
        return sorted(set(trusted_refs) | set(declared_refs))
