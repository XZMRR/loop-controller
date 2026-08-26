"""Loop Controller ↔ Harness 通信协议（v0.25.0）。

当前采用轻量级 HTTP/JSON 协议，未来可扩展为 gRPC。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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
    error_code: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
