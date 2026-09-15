"""Python bridge to the Go interaction governance kernel (v0.54.0)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

import httpx

logger = logging.getLogger(__name__)

# Current A2A HTTP/JSON protocol version. Patch differences are tolerated;
# major/minor differences are fail-closed.
CURRENT_PROTOCOL_VERSION = "0.54.0"
COMPATIBLE_PROTOCOL_VERSIONS = frozenset({"0.53.0", CURRENT_PROTOCOL_VERSION})
CURRENT_EVENT_SCHEMA_VERSION = 1
RETRYABLE_STREAM_STATUS_CODES = frozenset({500, 502, 503, 504})


class GoKernelBridgeError(RuntimeError):
    """Stable base error for Go kernel failures."""

    code = "go_kernel_bridge_error"

    def __init__(self, message: str = "", *, response_code: str = "") -> None:
        super().__init__(message or response_code or self.code)
        self.response_code = response_code or self.code


class GoKernelAuthenticationError(GoKernelBridgeError):
    code = "authentication_error"


class GoKernelAuthorizationError(GoKernelBridgeError):
    code = "authorization_error"


class GoKernelNotFoundError(GoKernelBridgeError):
    code = "not_found"


class GoKernelConflictError(GoKernelBridgeError):
    code = "conflict"


class GoKernelProtocolError(GoKernelBridgeError):
    code = "protocol_error"


class GoKernelServerError(GoKernelBridgeError):
    code = "server_error"


class GoKernelClosedError(GoKernelBridgeError):
    code = "bridge_closed"


class EventCursorExpiredError(GoKernelBridgeError):
    code = "event_cursor_expired"


class EventCursorInvalidError(GoKernelBridgeError):
    code = "event_cursor_invalid"


class EventCursorFutureError(EventCursorInvalidError):
    code = "event_cursor_future"


class EventSequenceGapError(GoKernelBridgeError):
    code = "event_sequence_gap"


class EventSequenceOutOfOrderError(GoKernelBridgeError):
    code = "event_sequence_out_of_order"


class UnsupportedEventSchemaError(GoKernelBridgeError):
    code = "unsupported_event_schema"


class InvalidTaskEventError(GoKernelBridgeError):
    code = "invalid_task_event"


@dataclass
class _SSEEvent:
    data: str
    event_id: str | None
    event: str | None


async def _iter_sse(response: httpx.Response, retry_ms: list[int]) -> AsyncIterator[_SSEEvent]:
    data: list[str] = []
    event_id: str | None = None
    event: str | None = None
    async for line in response.aiter_lines():
        if line == "":
            if data:
                yield _SSEEvent("\n".join(data), event_id, event)
            data, event_id, event = [], None, None
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "data":
            data.append(value)
        elif field == "id" and "\x00" not in value:
            event_id = value
        elif field == "event":
            event = value
        elif field == "retry" and value.isdigit():
            retry_ms[0] = int(value)
    if data:
        yield _SSEEvent("\n".join(data), event_id, event)


def check_protocol_version(version: str) -> None:
    """Validate that *version* is compatible with ``CURRENT_PROTOCOL_VERSION``.

    版本必须是严格的 ``major.minor.patch``；major/minor 不一致时拒绝。
    """
    parts = version.split(".")
    current = CURRENT_PROTOCOL_VERSION.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise ValueError(f"invalid protocol version {version!r}")
    if version in COMPATIBLE_PROTOCOL_VERSIONS:
        return
    if parts[0] == current[0] and parts[1] == current[1]:
        return
    if parts[0] == "0" and parts[1] == "53":
        return
    raise ValueError(
        f"incompatible protocol version {version!r}, expected 0.53.x or 0.54.x"
    )


class AgentEntrypoint:
    """Go 内核返回的目标 Agent 入口。"""

    def __init__(self, type_: str, url: str) -> None:
        self.type = type_
        self.url = url

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentEntrypoint:
        return cls(type_=data.get("type", "http"), url=data.get("url", ""))


class AgentCard:
    """A2A Agent Card。"""

    def __init__(
        self,
        agent_id: str,
        *,
        name: str = "",
        description: str = "",
        entrypoint: AgentEntrypoint | None = None,
        capabilities: list[str] | None = None,
        trust_domain: str = "",
        version: str = "",
        tenant_id: str | None = None,
        protocol_version: str | None = CURRENT_PROTOCOL_VERSION,
    ) -> None:
        self.agent_id = agent_id
        self.name = name
        self.description = description
        self.entrypoint = entrypoint or AgentEntrypoint("http", "")
        self.capabilities = capabilities or []
        self.trust_domain = trust_domain
        self.version = version
        self.tenant_id = tenant_id
        self.protocol_version = protocol_version

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentCard:
        ep = data.get("entrypoint")
        return cls(
            agent_id=data.get("agent_id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            entrypoint=AgentEntrypoint.from_dict(ep) if isinstance(ep, dict) else None,
            capabilities=data.get("capabilities") or [],
            trust_domain=data.get("trust_domain", ""),
            version=data.get("version", ""),
            tenant_id=data.get("tenant_id"),
            protocol_version=data.get("protocol_version"),
        )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "agent_id": self.agent_id,
            "name": self.name,
            "description": self.description,
            "entrypoint": {"type": self.entrypoint.type, "url": self.entrypoint.url},
            "capabilities": self.capabilities,
            "trust_domain": self.trust_domain,
            "version": self.version,
        }
        if self.tenant_id is not None:
            data["tenant_id"] = self.tenant_id
        if self.protocol_version is not None:
            data["protocol_version"] = self.protocol_version
        return data


class A2AMessage:
    """A2A 消息。"""

    def __init__(
        self,
        *,
        message_id: str,
        task_id: str,
        from_agent_id: str,
        to_agent_id: str,
        role: str = "user",
        parts: list[dict[str, Any]] | None = None,
        timestamp: str = "",
        protocol_version: str = CURRENT_PROTOCOL_VERSION,
    ) -> None:
        self.message_id = message_id
        self.task_id = task_id
        self.from_agent_id = from_agent_id
        self.to_agent_id = to_agent_id
        self.role = role
        self.parts = parts or []
        self.timestamp = timestamp
        self.protocol_version = protocol_version

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "message_id": self.message_id,
            "task_id": self.task_id,
            "from_agent_id": self.from_agent_id,
            "to_agent_id": self.to_agent_id,
            "role": self.role,
            "parts": self.parts,
            "protocol_version": self.protocol_version,
        }
        if self.timestamp:
            data["timestamp"] = self.timestamp
        return data


class DelegationRequest:
    """委托请求。"""

    def __init__(
        self,
        *,
        request_id: str,
        initiator_agent_id: str,
        target_agent_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        session_id: str = "",
        task_id: str = "",
        risk_level: str = "critical",
        allowed_tools: list[str] | None = None,
        allowed_capabilities: list[str] | None = None,
        allow_redelegation: bool = False,
        parent_task_id: str = "",
        budget: dict[str, Any] | None = None,
        deadline: str | None = None,
        protocol_version: str = CURRENT_PROTOCOL_VERSION,
        tenant_id: str | None = None,
        target_workload_id: str | None = None,
        target_instance_id: str | None = None,
    ) -> None:
        self.request_id = request_id
        self.initiator_agent_id = initiator_agent_id
        self.target_agent_id = target_agent_id
        self.tool_name = tool_name
        self.arguments = arguments or {}
        self.session_id = session_id
        self.task_id = task_id
        self.risk_level = risk_level
        self.allowed_tools = list(allowed_tools) if allowed_tools is not None else [tool_name]
        self.allowed_capabilities = list(allowed_capabilities or [])
        self.allow_redelegation = allow_redelegation
        self.parent_task_id = parent_task_id
        self.budget = dict(budget or {})
        self.deadline = deadline
        self.protocol_version = protocol_version
        self.tenant_id = tenant_id
        self.target_workload_id = target_workload_id
        self.target_instance_id = target_instance_id

    def to_dict(self) -> dict[str, Any]:
        data = {
            "request_id": self.request_id,
            "initiator_agent_id": self.initiator_agent_id,
            "target_agent_id": self.target_agent_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "risk_level": self.risk_level,
            "allowed_tools": self.allowed_tools,
            "allowed_capabilities": self.allowed_capabilities,
            "allow_redelegation": self.allow_redelegation,
            "parent_task_id": self.parent_task_id,
            "budget": self.budget,
            "deadline": self.deadline,
            "protocol_version": self.protocol_version,
        }
        if self.tenant_id is not None:
            data["tenant_id"] = self.tenant_id
        if self.target_workload_id is not None:
            data["target_workload_id"] = self.target_workload_id
        if self.target_instance_id is not None:
            data["target_instance_id"] = self.target_instance_id
        return data


class DelegationApproval:
    """委托审批公开 DTO；不包含有效参数明文。"""

    def __init__(
        self,
        *,
        approval_id: str,
        request_id: str,
        decision_id: str,
        request_hash: str,
        initiator_agent_id: str,
        target_agent_id: str,
        session_id: str = "",
        tenant_id: str = "",
        target_workload_id: str = "",
        target_instance_id: str = "",
        root_task_id: str = "",
        parent_task_id: str = "",
        delegation_depth: int = 0,
        allowed_tools: list[str] | None = None,
        allowed_capabilities: list[str] | None = None,
        allow_redelegation: bool = False,
        budget: dict[str, Any] | None = None,
        task_deadline: str | None = None,
        expires_at: str,
        status: str,
        approver_id: str = "",
        reason: str = "",
        created_at: str,
        updated_at: str,
        decided_at: str | None = None,
        task_id: str = "",
        version: int = 1,
        protocol_version: str = CURRENT_PROTOCOL_VERSION,
    ) -> None:
        self.__dict__.update(locals())
        del self.__dict__["self"]
        self.allowed_tools = list(allowed_tools or [])
        self.allowed_capabilities = list(allowed_capabilities or [])
        self.budget = dict(budget or {})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DelegationApproval:
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        for key in (
            "tenant_id",
            "target_workload_id",
            "target_instance_id",
            "root_task_id",
            "parent_task_id",
            "approver_id",
            "reason",
            "task_id",
        ):
            if not data[key]:
                data.pop(key)
        for key in ("task_deadline", "decided_at"):
            if data[key] is None:
                data.pop(key)
        return data


class ApprovalActionRequest:
    """审批动作请求 DTO。"""

    def __init__(self, request_id: str, reason: str = "") -> None:
        self.request_id = request_id
        self.reason = reason

    def to_dict(self) -> dict[str, str]:
        data = {"request_id": self.request_id}
        if self.reason:
            data["reason"] = self.reason
        return data


class DelegationResponse:
    """委托响应。"""

    def __init__(
        self,
        *,
        allowed: bool,
        verdict: str = "",
        approval_id: str = "",
        interaction_id: str = "",
        decision_id: str = "",
        task_id: str = "",
        target_entrypoint: AgentEntrypoint | None = None,
        delegation_token: str = "",
        original_args: dict[str, Any] | None = None,
        modified_args: dict[str, Any] | None = None,
        effective_args: dict[str, Any] | None = None,
        reason: str = "",
        protocol_version: str = CURRENT_PROTOCOL_VERSION,
    ) -> None:
        self.allowed = allowed
        self.verdict = verdict
        self.approval_id = approval_id
        self.interaction_id = interaction_id
        self.decision_id = decision_id
        self.task_id = task_id
        self.target_entrypoint = target_entrypoint
        self.delegation_token = delegation_token
        self.original_args = original_args
        self.modified_args = modified_args
        self.effective_args = effective_args
        self.reason = reason
        self.protocol_version = protocol_version

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DelegationResponse:
        ep = data.get("target_entrypoint")
        return cls(
            allowed=data.get("allowed", False),
            verdict=data.get("verdict", ""),
            approval_id=data.get("approval_id", ""),
            interaction_id=data.get("interaction_id", ""),
            decision_id=data.get("decision_id", ""),
            task_id=data.get("task_id", ""),
            target_entrypoint=AgentEntrypoint.from_dict(ep) if ep else None,
            delegation_token=data.get("delegation_token", ""),
            original_args=data.get("original_args"),
            modified_args=data.get("modified_args"),
            effective_args=data.get("effective_args"),
            reason=data.get("reason", ""),
            protocol_version=data.get("protocol_version", CURRENT_PROTOCOL_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "allowed": self.allowed,
            "task_id": self.task_id,
            "reason": self.reason,
            "protocol_version": self.protocol_version,
        }
        if self.verdict:
            data["verdict"] = self.verdict
        if self.approval_id:
            data["approval_id"] = self.approval_id
        if self.interaction_id:
            data["interaction_id"] = self.interaction_id
        if self.decision_id:
            data["decision_id"] = self.decision_id
        if self.target_entrypoint is not None:
            data["target_entrypoint"] = {
                "type": self.target_entrypoint.type,
                "url": self.target_entrypoint.url,
            }
        if self.delegation_token:
            data["delegation_token"] = self.delegation_token
        if self.original_args is not None:
            data["original_args"] = self.original_args
        if self.modified_args is not None:
            data["modified_args"] = self.modified_args
        if self.effective_args is not None:
            data["effective_args"] = self.effective_args
        return data


class GoKernelBridge:
    """与 Go 交互治理内核通信的 HTTP/JSON 桥接客户端。

    当 Go 内核不可用时，所有请求 fail-closed（返回 allowed=False 或抛出可识别的异常）。
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        *,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
        headers: dict[str, str] | None = None,
        control_token: str = "",
        approver_token: str = "",
        verify: Any = True,
        cert: Any = None,
        min_reconnect_delay: float = 0.05,
        max_reconnect_delay: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client
        self._owned_client = client is None
        self._headers = {
            key: value
            for key, value in (headers or {}).items()
            if key.lower() != "authorization"
        }
        self._control_headers = dict(self._headers)
        if control_token:
            self._control_headers["Authorization"] = f"Bearer {control_token}"
        self._approver_headers = dict(self._headers)
        if approver_token:
            self._approver_headers["Authorization"] = f"Bearer {approver_token}"
        self._verify = verify
        self._cert = cert
        self._min_reconnect_delay = max(0.001, min_reconnect_delay)
        self._max_reconnect_delay = max(self._min_reconnect_delay, max_reconnect_delay)
        self._closed = False

    async def _client_context(self) -> httpx.AsyncClient:
        if self._closed:
            raise GoKernelClosedError()
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                verify=self._verify,
                cert=self._cert,
            )
        return self._client

    def _auth_headers(self, *, approver: bool = False) -> dict[str, str]:
        return dict(self._approver_headers if approver else self._control_headers)

    @staticmethod
    def _decode_object(response: httpx.Response) -> dict[str, Any]:
        try:
            result = response.json()
        except ValueError as exc:
            raise GoKernelProtocolError("invalid JSON response") from exc
        if not isinstance(result, dict):
            raise GoKernelProtocolError("response must be a JSON object")
        version = str(result.get("protocol_version", ""))
        try:
            check_protocol_version(version)
        except ValueError as exc:
            raise GoKernelProtocolError(str(exc)) from exc
        return result

    @classmethod
    def _raise_response_error(cls, response: httpx.Response) -> None:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        code = str(payload.get("code", "")) if isinstance(payload, dict) else ""
        message = str(payload.get("error", "")) if isinstance(payload, dict) else ""
        error_type: type[GoKernelBridgeError]
        if response.status_code == 401:
            error_type = GoKernelAuthenticationError
        elif response.status_code == 403:
            error_type = GoKernelAuthorizationError
        elif response.status_code == 404:
            error_type = GoKernelNotFoundError
        elif response.status_code == 409:
            error_type = GoKernelConflictError
        elif response.status_code >= 500:
            error_type = GoKernelServerError
        else:
            error_type = GoKernelProtocolError
        raise error_type(message, response_code=code)

    async def _request_object(
        self, method: str, url: str, *, approver: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        client = await self._client_context()
        supplied = kwargs.pop("headers", {}) or {}
        headers = self._auth_headers(approver=approver)
        headers.update(
            (key, value)
            for key, value in supplied.items()
            if key.lower() != "authorization"
        )
        response = await client.request(method, url, headers=headers, **kwargs)
        if response.is_error:
            self._raise_response_error(response)
        return self._decode_object(response)

    async def aclose(self) -> None:
        self._closed = True
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def readiness(self) -> dict[str, Any]:
        """查询 Go 内核结构化 readiness；非 ready 响应仍稳定解析。"""
        client = await self._client_context()
        response = await client.get(f"{self._base_url}/ready", headers=self._auth_headers())
        if response.status_code not in {200, 503}:
            self._raise_response_error(response)
        data = self._decode_object(response)
        if data.get("schema_version") != "p54-09.v1":
            raise GoKernelProtocolError("unsupported readiness schema_version")
        if data.get("status") not in {"ready", "not_ready"}:
            raise GoKernelProtocolError("invalid readiness status")
        if not isinstance(data.get("checks"), dict) or not isinstance(data.get("reasons"), list):
            raise GoKernelProtocolError("invalid readiness response")
        return data

    async def register_agent(self, card: AgentCard) -> bool:
        """向 Go 内核注册 Agent Card。"""
        url = f"{self._base_url}/a2a/v1/agents"
        try:
            data = await self._request_object("POST", url, json=card.to_dict())
            return data.get("agent_id") == card.agent_id
        except GoKernelBridgeError:
            return False
        except httpx.RequestError as exc:
            logger.warning("Go kernel register_agent unreachable: %s", exc)
            return False

    async def request_delegation(self, req: DelegationRequest) -> DelegationResponse:
        """请求委托执行；Go 内核不可用时返回 allowed=False。"""
        url = f"{self._base_url}/a2a/v1/delegations"
        try:
            client = await self._client_context()
            response = await client.post(
                url, json=req.to_dict(), headers=self._auth_headers()
            )
            # A policy denial is a successful DelegationResponse, not an error envelope.
            if response.status_code == 403:
                try:
                    data = self._decode_object(response)
                except GoKernelProtocolError:
                    self._raise_response_error(response)
                if "allowed" in data:
                    return DelegationResponse.from_dict(data)
                self._raise_response_error(response)
            if response.is_error:
                self._raise_response_error(response)
            return DelegationResponse.from_dict(self._decode_object(response))
        except GoKernelProtocolError as exc:
            logger.warning("Go kernel protocol rejected, fail-closed: %s", exc)
            return DelegationResponse(allowed=False, reason="incompatible_protocol_version")
        except GoKernelBridgeError as exc:
            return DelegationResponse(allowed=False, reason=exc.response_code)
        except httpx.RequestError as exc:
            logger.warning("Go kernel unreachable, fail-closed: %s", exc)
            return DelegationResponse(
                allowed=False,
                reason="go_kernel_unreachable",
            )

    async def route_message(self, msg: A2AMessage) -> bool:
        """向目标 Agent 路由一条消息。"""
        url = f"{self._base_url}/a2a/v1/messages"
        try:
            data = await self._request_object("POST", url, json=msg.to_dict())
            return bool(data.get("accepted", False))
        except GoKernelBridgeError:
            return False
        except httpx.RequestError as exc:
            logger.warning("Go kernel route_message unreachable: %s", exc)
            return False

    async def get_agent(self, agent_id: str) -> AgentCard | dict[str, Any] | None:
        """查询已注册 Agent Card；不可达时返回 None。"""
        url = f"{self._base_url}/a2a/v1/agents/{agent_id}"
        try:
            return AgentCard.from_dict(await self._request_object("GET", url))
        except GoKernelNotFoundError:
            return None
        except GoKernelBridgeError:
            return None
        except httpx.RequestError as exc:
            logger.warning("Go kernel get_agent unreachable: %s", exc)
            return None

    async def query_task(self, task_id: str) -> dict[str, Any] | None:
        """查询任务状态。"""
        url = f"{self._base_url}/a2a/v1/tasks/{task_id}"
        try:
            return await self._request_object("GET", url)
        except GoKernelNotFoundError:
            return None
        except GoKernelBridgeError:
            return None
        except httpx.RequestError as exc:
            logger.warning("Go kernel query_task unreachable: %s", exc)
            return None

    async def cancel_task(
        self,
        task_id: str,
        reason: str = "",
        delegation_token: str = "",
    ) -> dict[str, Any] | None:
        """请求取消控制面任务；始终使用 control credential。"""
        del delegation_token  # Kept only for source compatibility; never used as control auth.
        url = f"{self._base_url}/a2a/v1/tasks/{task_id}/cancel"
        try:
            return await self._request_object(
                "POST",
                url,
                json={"protocol_version": CURRENT_PROTOCOL_VERSION, "reason": reason},
            )
        except (httpx.RequestError, GoKernelBridgeError) as exc:
            logger.warning("Go kernel cancel_task failed: %s", exc)
            return None

    async def get_delegation_approval(self, approval_id: str) -> DelegationApproval:
        data = await self._request_object(
            "GET", f"{self._base_url}/a2a/v1/delegation-approvals/{approval_id}"
        )
        return DelegationApproval.from_dict(data)

    async def get_approval(self, approval_id: str) -> DelegationApproval:
        return await self.get_delegation_approval(approval_id)

    async def approve_delegation(
        self, approval_id: str, request_id: str, reason: str = ""
    ) -> DelegationApproval:
        return cast(
            DelegationApproval,
            await self._approval_action(approval_id, "approve", request_id, reason, True),
        )

    async def approve_approval(
        self, approval_id: str, request_id: str, reason: str = ""
    ) -> DelegationApproval:
        return await self.approve_delegation(approval_id, request_id, reason)

    async def reject_delegation(
        self, approval_id: str, request_id: str, reason: str = ""
    ) -> DelegationApproval:
        return cast(
            DelegationApproval,
            await self._approval_action(approval_id, "reject", request_id, reason, True),
        )

    async def reject_approval(
        self, approval_id: str, request_id: str, reason: str = ""
    ) -> DelegationApproval:
        return await self.reject_delegation(approval_id, request_id, reason)

    async def cancel_delegation_approval(
        self, approval_id: str, request_id: str, reason: str = ""
    ) -> DelegationApproval | dict[str, Any]:
        return await self._approval_action(approval_id, "cancel", request_id, reason, False)

    async def cancel_approval(
        self, approval_id: str, request_id: str, reason: str = ""
    ) -> DelegationApproval | dict[str, Any]:
        return await self.cancel_delegation_approval(approval_id, request_id, reason)

    async def _approval_action(
        self, approval_id: str, action: str, request_id: str, reason: str, approver: bool
    ) -> DelegationApproval | dict[str, Any]:
        data = await self._request_object(
            "POST",
            f"{self._base_url}/a2a/v1/delegation-approvals/{approval_id}/{action}",
            approver=approver,
            json=ApprovalActionRequest(request_id, reason).to_dict(),
        )
        if "approval_id" in data:
            return DelegationApproval.from_dict(data)
        return data

    async def stream_task(
        self, task_id: str, timeout: float = 30.0
    ) -> AsyncGenerator[dict[str, Any], None]:
        """订阅任务 SSE 更新；游标仅在本 generator 内保存，因此同任务可并发独立订阅。"""
        url = f"{self._base_url}/a2a/v1/tasks/{task_id}/stream"
        cursor: str | None = None
        last_sequence: int | None = None
        seen_identities: set[str] = set()
        retry_ms = [int(self._min_reconnect_delay * 1000)]
        local_delay = self._min_reconnect_delay

        if self._closed:
            raise GoKernelClosedError()
        while not self._closed:
            headers = self._auth_headers()
            headers["Accept"] = "text/event-stream"
            if cursor is not None:
                headers["Last-Event-ID"] = cursor
            try:
                client = await self._client_context()
                async with client.stream(
                    "GET", url, headers=headers, timeout=timeout
                ) as response:
                    if response.status_code in (400, 410):
                        await self._raise_cursor_error(response)
                    response.raise_for_status()
                    local_delay = self._min_reconnect_delay
                    async for frame in _iter_sse(response, retry_ms):
                        try:
                            decoded = json.loads(frame.data)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(decoded, dict):
                            raise InvalidTaskEventError("task stream event must be a JSON object")

                        identity_value = decoded.get("event_id") or frame.event_id
                        identity = str(identity_value) if identity_value else ""
                        sequence_value = decoded.get("sequence")
                        schema_value = decoded.get("schema_version")
                        if identity and identity in seen_identities:
                            continue
                        if sequence_value is not None or schema_value is not None:
                            if (
                                not isinstance(sequence_value, int)
                                or isinstance(sequence_value, bool)
                                or sequence_value < 1
                            ):
                                raise InvalidTaskEventError("invalid task event sequence")
                            if (
                                not isinstance(schema_value, int)
                                or isinstance(schema_value, bool)
                                or schema_value != CURRENT_EVENT_SCHEMA_VERSION
                            ):
                                raise UnsupportedEventSchemaError(
                                    f"unsupported task event schema {schema_value!r}"
                                )
                            if last_sequence is not None:
                                if sequence_value <= last_sequence:
                                    raise EventSequenceOutOfOrderError(
                                        f"task event sequence {sequence_value} follows {last_sequence}"
                                    )
                                if sequence_value != last_sequence + 1:
                                    raise EventSequenceGapError(
                                        f"task event sequence gap: expected {last_sequence + 1}, "
                                        f"got {sequence_value}"
                                    )

                        # Commit stream state only after the complete frame and JSON contract are valid.
                        if sequence_value is not None:
                            last_sequence = sequence_value
                        if identity:
                            seen_identities.add(identity)
                        if frame.event_id is not None:
                            cursor = frame.event_id
                        yield decoded
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in RETRYABLE_STREAM_STATUS_CODES:
                    raise
                logger.warning(
                    "Go kernel stream_task reconnecting after HTTP %s",
                    exc.response.status_code,
                )
            except httpx.RequestError as exc:
                logger.warning("Go kernel stream_task reconnecting after transport error: %s", exc)

            if self._closed:
                return
            server_delay = max(0.0, retry_ms[0] / 1000)
            delay = min(self._max_reconnect_delay, max(local_delay, server_delay))
            await asyncio.sleep(delay)
            local_delay = min(self._max_reconnect_delay, local_delay * 2)

    @staticmethod
    async def _raise_cursor_error(response: httpx.Response) -> None:
        try:
            body = await response.aread()
            payload = json.loads(body)
            code = payload.get("code") if isinstance(payload, dict) else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            code = None
        errors: dict[str, type[GoKernelBridgeError]] = {
            "event_cursor_expired": EventCursorExpiredError,
            "event_cursor_invalid": EventCursorInvalidError,
            "event_cursor_future": EventCursorFutureError,
        }
        error_type = errors.get(str(code))
        if error_type is not None:
            raise error_type()
