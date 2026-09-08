"""HTTP Executor 配置模型（v0.21.0）。

定义 HTTP 工具的声明式配置：请求模板、认证、响应映射、安全约束。
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from loop_controller.models import Tool
from loop_controller.secrets import SecretBroker, SecretNotFoundError, SecretRef

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

RiskLevel = Literal["low", "medium", "high", "critical"]
HTTPMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
HTTPAuthType = Literal[
    "none",
    "bearer_token",
    "api_key_header",
    "api_key_query",
    "basic",
    "mtls",
]


def _resolve_env_ref(value: str) -> str:
    """解析单个 ${ENV_NAME} 引用；未设置则返回原值。"""
    match = _ENV_PATTERN.fullmatch(value)
    if not match:
        return value
    env_name = match.group(1)
    env_value = os.environ.get(env_name)
    if env_value is None:
        raise ValueError(f"环境变量 {env_name} 未设置")
    return env_value


def resolve_env_refs(value: Any) -> Any:
    """递归解析字符串中的 ${ENV_NAME} 引用。

    支持字符串、字典、列表嵌套结构。未解析的引用保留原样（用于延迟解析场景）。
    """
    if isinstance(value, str):
        if _ENV_PATTERN.fullmatch(value):
            return _resolve_env_ref(value)
        return _ENV_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), m.group(0)), value
        )
    if isinstance(value, dict):
        return {k: resolve_env_refs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_env_refs(item) for item in value]
    return value


class HTTPAuthConfig(BaseModel):
    """HTTP 工具认证配置。

    v0.22.0 起支持 ``secret_ref`` 运行时从 Secret Broker 注入凭证；
    ``secret_ref`` 与直接值（``token`` / ``username`` / ``password``）并存时，
    ``secret_ref`` 优先。
    """

    model_config = ConfigDict(frozen=True)

    type: HTTPAuthType = "none"

    # bearer / api_key
    token: str | None = None
    key_name: str | None = None

    # basic
    username: str | None = None
    password: str | None = None

    # mTLS / Secret Broker
    cert_ref: str | None = None
    secret_ref: SecretRef | None = None

    @model_validator(mode="after")
    def _check_required_fields(self) -> HTTPAuthConfig:
        has_secret = self.secret_ref is not None
        if self.type in ("bearer_token", "api_key_header", "api_key_query"):
            if not has_secret and not self.token:
                raise ValueError(f"auth.type={self.type} 需要 token 或 secret_ref")
            if self.type in ("api_key_header", "api_key_query") and not self.key_name:
                raise ValueError(f"auth.type={self.type} 需要 key_name")
        if self.type == "basic":
            if not has_secret and (not self.username or not self.password):
                raise ValueError("auth.type=basic 需要 username + password 或 secret_ref")
        return self


class HTTPResponseMapping(BaseModel):
    """HTTP 响应映射配置。"""

    model_config = ConfigDict(frozen=True)

    success_status: list[int] = Field(default_factory=lambda: [200, 201, 202, 204])
    extract: dict[str, str] = Field(default_factory=dict)
    raw_body_field: str | None = None
    error_codes: dict[int, str] = Field(default_factory=dict)


class HTTPToolSpec(BaseModel):
    """单个 HTTP 工具的完整规格。"""

    model_config = ConfigDict(frozen=True)

    tool_name: str
    base_url: str
    method: HTTPMethod = "GET"
    path: str = ""
    query_template: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body_template: dict[str, Any] | str | None = None
    auth: HTTPAuthConfig = Field(default_factory=HTTPAuthConfig)
    response_mapping: HTTPResponseMapping = Field(default_factory=HTTPResponseMapping)
    default_risk: RiskLevel = "high"
    allowed_hosts: list[str] = Field(default_factory=list)
    # 是否对域名做 DNS 解析后二次校验（防 DNS 重绑定）。默认 True。
    require_dns_resolution: bool = True
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    retry: dict[str, Any] = Field(default_factory=dict)
    cost_per_call: int = 0
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _base_url_no_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="before")
    @classmethod
    def _derive_allowed_hosts(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("allowed_hosts"):
            from urllib.parse import urlparse

            base_url = data.get("base_url", "")
            host = urlparse(base_url).hostname
            if host:
                data["allowed_hosts"] = [host]
        return data

    def render_path(self, arguments: dict[str, Any]) -> str:
        """渲染 path 模板中的 {arg} 占位符。"""
        return self.path.format(**arguments)

    def render_query(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """渲染 query_template 模板。"""
        if not self.query_template:
            return {}
        rendered: dict[str, Any] = {}
        for key, template in self.query_template.items():
            if isinstance(template, str):
                rendered[key] = template.format(**arguments)
            else:
                rendered[key] = template
        return rendered

    def render_headers(self, arguments: dict[str, Any]) -> dict[str, str]:
        """渲染 headers 模板。"""
        rendered: dict[str, str] = {}
        for key, value in self.headers.items():
            rendered[key] = value.format(**arguments)
        return rendered

    def render_body(self, arguments: dict[str, Any]) -> Any:
        """渲染 body_template；dict 递归渲染字符串模板。"""
        if self.body_template is None:
            return None
        if isinstance(self.body_template, str):
            return self.body_template.format(**arguments)
        return self._render_nested(self.body_template, arguments)

    @staticmethod
    def _render_nested(value: Any, arguments: dict[str, Any]) -> Any:
        if isinstance(value, str):
            return value.format(**arguments)
        if isinstance(value, dict):
            return {k: HTTPToolSpec._render_nested(v, arguments) for k, v in value.items()}
        if isinstance(value, list):
            return [HTTPToolSpec._render_nested(item, arguments) for item in value]
        return value

    async def _resolve_auth_secret(
        self,
        secret_broker: SecretBroker | None,
        tenant_id: str | None,
    ) -> dict[str, Any]:
        """解析认证所需凭证；优先 secret_ref，否则回退直接值。"""
        ref = self.auth.secret_ref
        if ref is None:
            return {
                "token": self.auth.token,
                "username": self.auth.username,
                "password": self.auth.password,
            }

        if secret_broker is None:
            raise SecretNotFoundError(
                f"auth.secret_ref={ref.name} 需要 SecretBroker，但未提供",
                ref_name=ref.name,
            )

        resolved_ref = ref
        if ref.tenant_id is None and tenant_id is not None:
            resolved_ref = ref.model_copy(update={"tenant_id": tenant_id})

        secret = await secret_broker.get(resolved_ref)
        if secret is None or secret.is_expired():
            raise SecretNotFoundError(
                f"secret {ref.name} 不存在或已过期",
                ref_name=ref.name,
            )

        value = secret.value
        if self.auth.type == "basic":
            if isinstance(value, dict):
                return {
                    "username": value.get("username", self.auth.username),
                    "password": value.get("password", self.auth.password),
                    "token": self.auth.token,
                }
            raise SecretNotFoundError(
                f"secret {ref.name} 必须是包含 username/password 的对象",
                ref_name=ref.name,
            )

        if isinstance(value, str):
            return {
                "token": value,
                "username": self.auth.username,
                "password": self.auth.password,
            }
        raise SecretNotFoundError(
            f"secret {ref.name} 类型不支持直接作为 token",
            ref_name=ref.name,
        )

    async def build_request(
        self,
        arguments: dict[str, Any],
        secret_broker: SecretBroker | None = None,
        tenant_id: str | None = None,
    ) -> tuple[str, dict[str, str], dict[str, Any] | None]:
        """构造完整 URL、header、body（已注入认证信息）。"""
        from urllib.parse import urlencode, urljoin

        # 先解析认证信息，api_key_query 需要在拼 URL 前注入
        creds = await self._resolve_auth_secret(secret_broker, tenant_id)

        path = self.render_path(arguments)
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        query = self.render_query(arguments)

        if self.auth.type == "api_key_query":
            key_name = cast(str, self.auth.key_name)
            query[key_name] = creds["token"]

        if query:
            url += "?" + urlencode(query, doseq=True)

        headers = self.render_headers(arguments)
        body = self.render_body(arguments)

        # 校验认证信息非空，避免空 token / 空密码被误用
        if self.auth.type in ("bearer_token", "api_key_header", "api_key_query"):
            token = creds.get("token")
            if not token or not str(token).strip():
                raise SecretNotFoundError(
                    f"HTTP 工具 {self.tool_name} 的 auth token 为空",
                    ref_name=self.auth.secret_ref.name if self.auth.secret_ref else None,
                )
        if self.auth.type == "basic":
            username = creds.get("username")
            password = creds.get("password")
            if not username or not str(username).strip() or not password or not str(password).strip():
                raise SecretNotFoundError(
                    f"HTTP 工具 {self.tool_name} 的 basic 认证用户名或密码为空",
                    ref_name=self.auth.secret_ref.name if self.auth.secret_ref else None,
                )

        # 注入认证
        if self.auth.type == "bearer_token":
            headers["Authorization"] = f"Bearer {creds['token']}"
        elif self.auth.type == "api_key_header":
            key_name = cast(str, self.auth.key_name)
            headers[key_name] = creds["token"]
        elif self.auth.type == "basic":
            import base64

            username = cast(str, creds["username"])
            password = cast(str, creds["password"])
            creds_str = f"{username}:{password}"
            encoded = base64.b64encode(creds_str.encode("utf-8")).decode("utf-8")
            headers["Authorization"] = f"Basic {encoded}"

        return url, headers, body

    def to_tool(self) -> Tool:
        """转换为治理链路使用的 Tool 元数据。"""
        return Tool(
            canonical_name=self.tool_name,
            mcp_name=self.tool_name,
            description=self.description,
            input_schema=self.input_schema,
        )


def extract_jsonpath(value: Any, path: str) -> Any:
    """极简 JSONPath 实现：支持 $.a.b[0].c 形式。

    不引入 jsonpath-ng 等第三方依赖；v0.22 可替换为完整实现。
    """
    if not path.startswith("$."):
        return None
    parts = path[2:].split(".")
    current: Any = value
    for part in parts:
        if current is None:
            return None
        # 支持数组下标：a[0]
        match = re.match(r"^(\w+)\[(\d+)\]$", part)
        if match:
            key = match.group(1)
            idx = int(match.group(2))
            if isinstance(current, dict) and key in current:
                current = current[key]
            if isinstance(current, list) and 0 <= idx < len(current):
                current = current[idx]
            else:
                return None
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current
