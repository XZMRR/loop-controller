"""Python bridge to the Go interaction governance kernel (v0.48.0)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Current A2A HTTP/JSON protocol version. Patch differences are tolerated;
# major/minor differences are fail-closed.
CURRENT_PROTOCOL_VERSION = "0.48.0"


def check_protocol_version(version: str) -> None:
    """Validate that *version* is compatible with ``CURRENT_PROTOCOL_VERSION``.

    版本必须是严格的 ``major.minor.patch``；major/minor 不一致时拒绝。
    """
    parts = version.split(".")
    current = CURRENT_PROTOCOL_VERSION.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise ValueError(f"invalid protocol version {version!r}")
    if version == CURRENT_PROTOCOL_VERSION:
        return
    if parts[0] != current[0] or parts[1] != current[1]:
        raise ValueError(
            f"incompatible protocol version {version!r}, expected {CURRENT_PROTOCOL_VERSION}"
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
        version: str = CURRENT_PROTOCOL_VERSION,
    ) -> None:
        self.agent_id = agent_id
        self.name = name
        self.description = description
        self.entrypoint = entrypoint or AgentEntrypoint("http", "")
        self.capabilities = capabilities or []
        self.trust_domain = trust_domain
        self.version = version

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
            version=data.get("version", CURRENT_PROTOCOL_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "description": self.description,
            "entrypoint": {"type": self.entrypoint.type, "url": self.entrypoint.url},
            "capabilities": self.capabilities,
            "trust_domain": self.trust_domain,
            "version": self.version,
        }


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

    def to_dict(self) -> dict[str, Any]:
        return {
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
        for key in ("root_task_id", "parent_task_id", "approver_id", "reason", "task_id"):
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
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client
        self._owned_client = client is None

    async def _client_context(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout))
        return self._client

    async def aclose(self) -> None:
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def register_agent(self, card: AgentCard) -> bool:
        """向 Go 内核注册 Agent Card。"""
        url = f"{self._base_url}/a2a/v1/agents"
        try:
            client = await self._client_context()
            response = await client.post(url, json=card.to_dict())
            return response.status_code in (200, 201)
        except httpx.RequestError as exc:
            logger.warning("Go kernel register_agent unreachable: %s", exc)
            return False

    async def request_delegation(self, req: DelegationRequest) -> DelegationResponse:
        """请求委托执行；Go 内核不可用时返回 allowed=False。"""
        url = f"{self._base_url}/a2a/v1/delegations"
        try:
            client = await self._client_context()
            response = await client.post(url, json=req.to_dict())
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("invalid Go kernel delegation response")
            check_protocol_version(str(data.get("protocol_version", "")))
            return DelegationResponse.from_dict(data)
        except ValueError as exc:
            logger.warning("Go kernel protocol rejected, fail-closed: %s", exc)
            return DelegationResponse(allowed=False, reason="incompatible_protocol_version")
        except httpx.HTTPStatusError as exc:
            logger.warning("Go kernel delegation rejected: %s", exc)
            return DelegationResponse(allowed=False, reason="go_kernel_rejected")
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
            client = await self._client_context()
            response = await client.post(url, json=msg.to_dict())
            if response.status_code != 200:
                return False
            data = response.json()
            return bool(isinstance(data, dict) and data.get("accepted", False))
        except httpx.RequestError as exc:
            logger.warning("Go kernel route_message unreachable: %s", exc)
            return False

    async def get_agent(self, agent_id: str) -> AgentCard | dict[str, Any] | None:
        """查询已注册 Agent Card；不可达时返回 None。"""
        url = f"{self._base_url}/a2a/v1/agents/{agent_id}"
        try:
            client = await self._client_context()
            response = await client.get(url)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict):
                return AgentCard.from_dict(result)
            logger.warning("Go kernel get_agent returned non-object: %s", type(result))
            return None
        except httpx.RequestError as exc:
            logger.warning("Go kernel get_agent unreachable: %s", exc)
            return None

    async def query_task(self, task_id: str) -> dict[str, Any] | None:
        """查询任务状态。"""
        url = f"{self._base_url}/a2a/v1/tasks/{task_id}"
        try:
            client = await self._client_context()
            response = await client.get(url)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict):
                return result
            logger.warning("Go kernel query_task returned non-object: %s", type(result))
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
        """请求取消委托任务；内核不可达或拒绝时返回 None。"""
        url = f"{self._base_url}/a2a/v1/tasks/{task_id}/cancel"
        headers = {"Authorization": f"Bearer {delegation_token}"} if delegation_token else None
        try:
            client = await self._client_context()
            response = await client.post(
                url,
                json={
                    "protocol_version": CURRENT_PROTOCOL_VERSION,
                    "reason": reason,
                },
                headers=headers,
            )
            response.raise_for_status()
            result = response.json()
            return result if isinstance(result, dict) else None
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            logger.warning("Go kernel cancel_task failed: %s", exc)
            return None

    async def stream_task(
        self, task_id: str, timeout: float = 30.0
    ) -> AsyncIterator[dict[str, Any]]:
        """订阅任务 SSE 流式更新。"""
        url = f"{self._base_url}/a2a/v1/tasks/{task_id}/stream"
        try:
            client = await self._client_context()
            async with client.stream("GET", url, timeout=timeout) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        payload = line[len("data: "):]
                        try:
                            yield json.loads(payload)
                        except json.JSONDecodeError:
                            continue
        except httpx.RequestError as exc:
            logger.warning("Go kernel stream_task unreachable: %s", exc)
        except httpx.HTTPStatusError as exc:
            logger.warning("Go kernel stream_task rejected: %s", exc)
