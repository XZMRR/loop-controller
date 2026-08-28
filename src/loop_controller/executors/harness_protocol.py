"""Loop Controller ↔ Harness HTTP/JSON 协议。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

HARNESS_PROTOCOL_VERSION = "1"
HARNESS_EXECUTE_PATH = "/harness/v1/execute"
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


class HarnessSandbox(BaseModel):
    """Harness 执行时的沙箱限制。"""

    model_config = ConfigDict(frozen=True)

    timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    max_output_bytes: int = Field(default=64 * 1024, ge=1024)
    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    env_whitelist: list[str] = Field(default_factory=list)


class HarnessExecuteRequest(BaseModel):
    """Loop Controller → Harness 执行请求。"""

    model_config = ConfigDict(frozen=True)

    tool: str
    arguments: dict[str, Any]
    context: HarnessContext
    sandbox: HarnessSandbox = Field(default_factory=HarnessSandbox)


class HarnessExecuteResponse(BaseModel):
    """Harness → Loop Controller 执行响应。"""

    model_config = ConfigDict(frozen=True)

    status: Literal["success", "error"]
    content: Any | None = None
    error_code: HarnessErrorCode | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class HarnessBackendStatus(BaseModel):
    """可安全向运维接口暴露的后端状态。"""

    model_config = ConfigDict(frozen=True)

    name: str
    type: Literal["subprocess", "docker", "http"]
    status: Literal["unknown", "healthy", "degraded", "unhealthy"]
    max_concurrent_calls: int
    checked_at: datetime | None = None
    consecutive_failures: int = 0
    last_error_code: str | None = None
    in_flight: int = 0
