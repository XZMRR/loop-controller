"""Loop Controller ↔ Harness HTTP/JSON 协议。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

HARNESS_PROTOCOL_VERSION = "2"
HARNESS_EXECUTE_PATH = "/harness/v2/execute"
HarnessErrorCode = Literal[
    "harness_backend_unavailable",
    "harness_overloaded",
    "harness_request_timeout",
    "harness_auth_required",
    "harness_auth_failed",
    "harness_replay_detected",
    "harness_protocol_unsupported",
    "harness_invalid_request",
    "harness_tool_not_found",
    "harness_sandbox_violation",
    "harness_sandbox_unsupported",
    "harness_timeout",
    "harness_output_limit_exceeded",
    "harness_invalid_response",
    "harness_sandbox_attestation_missing",
]


class HarnessContext(BaseModel):
    """随每次工具调用透传给 Harness 的治理上下文。"""

    model_config = ConfigDict(frozen=True)

    call_id: str
    task_id: str
    agent_id: str
    user_id: str
    session_id: str | None = None
    tenant_id: str | None = None


class ResourceLimits(BaseModel):
    """Harness 执行资源上限。"""

    model_config = ConfigDict(frozen=True)

    max_memory_bytes: int | None = Field(default=None, ge=1)
    cpu_seconds: float | None = Field(default=None, ge=0.0)


class HarnessSandbox(BaseModel):
    """Harness 执行时的沙箱限制。"""

    model_config = ConfigDict(frozen=True)

    timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    max_output_bytes: int = Field(default=64 * 1024, ge=1024)
    network_policy: Literal["deny_all", "allow_list", "loopback_only"] = "deny_all"
    allowed_hosts: list[str] = Field(default_factory=list)
    file_policy: Literal["deny_all", "read_only_list", "read_write_list"] = "deny_all"
    allowed_paths: list[str] = Field(default_factory=list)
    readonly_paths: list[str] = Field(default_factory=list)
    env_whitelist: list[str] = Field(default_factory=list)
    process_policy: Literal["deny_all", "allow_list"] = "deny_all"
    allowed_commands: list[str] = Field(default_factory=list)
    evidence_capture: Literal["none", "stdout", "all"] = "none"
    resource_limits: ResourceLimits | None = None


class HarnessExecuteRequest(BaseModel):
    """Loop Controller → Harness 执行请求。"""

    model_config = ConfigDict(frozen=True)

    tool: str
    arguments: dict[str, Any]
    context: HarnessContext
    sandbox: HarnessSandbox = Field(default_factory=HarnessSandbox)


class NetworkAttempt(BaseModel):
    """Harness 记录的网络访问尝试。"""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    host: str
    port: int | None = None
    action: Literal["blocked", "allowed"]


class FileAttempt(BaseModel):
    """Harness 记录的文件访问尝试。"""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    path: str
    operation: Literal["read", "write", "execute", "list"]
    action: Literal["blocked", "allowed"]


class HarnessEvidence(BaseModel):
    """Harness 返回的结构化执行证据。"""

    model_config = ConfigDict(frozen=True)

    started_at: datetime
    finished_at: datetime
    exit_code: int | None = None
    max_memory_bytes: int | None = None
    cpu_milliseconds: int | None = None
    network_attempts: list[NetworkAttempt] = Field(default_factory=list)
    file_attempts: list[FileAttempt] = Field(default_factory=list)
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None


class HarnessExecuteResponse(BaseModel):
    """Harness → Loop Controller 执行响应。"""

    model_config = ConfigDict(frozen=True)

    status: Literal["success", "error"]
    content: Any | None = None
    error_code: HarnessErrorCode | None = None
    effective_sandbox: HarnessSandbox | None = None
    evidence: HarnessEvidence | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class HarnessBackendStatus(BaseModel):
    """可安全向运维接口暴露的后端状态。"""

    model_config = ConfigDict(frozen=True)

    name: str
    type: Literal["subprocess", "docker", "isolated_subprocess", "http"]
    status: Literal["unknown", "healthy", "degraded", "unhealthy"]
    max_concurrent_calls: int
    checked_at: datetime | None = None
    consecutive_failures: int = 0
    last_error_code: str | None = None
    in_flight: int = 0
    draining: bool = False
