"""Harness 工具与后端配置模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from loop_controller.models import RiskLevel, Tool


class HarnessSandboxConfig(BaseModel):
    """Harness 工具的沙箱限制参数。"""

    model_config = ConfigDict(frozen=True)

    timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    max_output_bytes: int = Field(default=64 * 1024, ge=1024)
    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    env_whitelist: list[str] = Field(default_factory=list)


class HarnessToolSpec(BaseModel):
    """Harness 工具规格。"""

    model_config = ConfigDict(frozen=True)

    tool_name: str
    harness: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    default_risk: RiskLevel = "critical"
    cost_per_call: int = 0
    secret_refs: list[str] = Field(default_factory=list)
    sandbox: HarnessSandboxConfig = Field(default_factory=HarnessSandboxConfig)

    def to_tool(self) -> Tool:
        return Tool(
            canonical_name=self.tool_name,
            mcp_name=self.tool_name,
            description=self.description,
            input_schema=self.input_schema,
        )


class HarnessAuthConfig(BaseModel):
    """远程 Harness 认证配置。"""

    model_config = ConfigDict(frozen=True)

    type: Literal["none", "api_key", "hmac_sha256"] = "none"
    key_env: str | None = None
    key_id: str | None = None
    max_clock_skew_seconds: int = Field(default=60, ge=1)

    @model_validator(mode="after")
    def validate_required_fields(self) -> HarnessAuthConfig:
        if self.type != "none" and not self.key_env:
            raise ValueError("Harness 认证必须配置 key_env")
        if self.type == "hmac_sha256" and not self.key_id:
            raise ValueError("HMAC Harness 认证必须配置 key_id")
        return self


class HarnessTLSConfig(BaseModel):
    """远程 Harness TLS/mTLS 客户端配置。"""

    model_config = ConfigDict(frozen=True)

    verify: bool = True
    ca_file: str | None = None
    client_cert_file: str | None = None
    client_key_file: str | None = None

    @model_validator(mode="after")
    def validate_client_certificate_pair(self) -> HarnessTLSConfig:
        if bool(self.client_cert_file) != bool(self.client_key_file):
            raise ValueError("mTLS client_cert_file 与 client_key_file 必须成对配置")
        return self


class HarnessHealthConfig(BaseModel):
    """远程 Harness 健康检查配置。"""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    path: str = "/health"
    startup_required: bool = True
    interval_seconds: float = Field(default=15.0, gt=0)
    timeout_seconds: float = Field(default=3.0, gt=0)
    unhealthy_threshold: int = Field(default=3, ge=1)


class SubprocessBackendConfig(BaseModel):
    """子进程 Harness 后端配置。"""

    model_config = ConfigDict(frozen=True)

    name: str
    type: Literal["subprocess"] = "subprocess"
    command: list[str]
    env: dict[str, str] = Field(default_factory=dict)
    max_concurrent_calls: int = Field(default=10, ge=1)
    acquire_timeout_seconds: float = Field(default=2.0, gt=0)


class DockerBackendConfig(BaseModel):
    """Docker Harness 后端配置。"""

    model_config = ConfigDict(frozen=True)

    name: str
    type: Literal["docker"] = "docker"
    image: str
    network_mode: str | None = "none"
    env: dict[str, str] = Field(default_factory=dict)
    mounts: list[dict[str, Any]] = Field(default_factory=list)
    max_concurrent_calls: int = Field(default=5, ge=1)
    acquire_timeout_seconds: float = Field(default=2.0, gt=0)


class HTTPBackendConfig(BaseModel):
    """远程 HTTP Harness 后端配置。"""

    model_config = ConfigDict(frozen=True)

    name: str
    type: Literal["http"] = "http"
    base_url: str
    timeout_seconds: float = Field(default=30.0, ge=1.0)
    max_concurrent_calls: int = Field(default=10, ge=1)
    acquire_timeout_seconds: float = Field(default=2.0, gt=0)
    auth: HarnessAuthConfig = Field(default_factory=HarnessAuthConfig)
    tls: HarnessTLSConfig = Field(default_factory=HarnessTLSConfig)
    health: HarnessHealthConfig = Field(default_factory=HarnessHealthConfig)
    allow_insecure_http: bool = False
    api_key_env: str | None = None

    @model_validator(mode="after")
    def normalize_legacy_api_key(self) -> HTTPBackendConfig:
        if self.api_key_env:
            if "auth" in self.model_fields_set:
                raise ValueError("api_key_env 与 auth 不得同时配置")
            object.__setattr__(
                self,
                "auth",
                HarnessAuthConfig(type="api_key", key_env=self.api_key_env),
            )
        return self


HarnessBackendConfig = (
    SubprocessBackendConfig | DockerBackendConfig | HTTPBackendConfig
)
