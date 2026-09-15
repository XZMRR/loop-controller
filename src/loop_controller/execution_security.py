from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Awaitable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import jwt

from loop_controller.executors.base import WorkloadAuthMethod, WorkloadIdentity

REQUIRED_SECURITY_CAPABILITY_UNAVAILABLE = "required_security_capability_unavailable"


@dataclass(frozen=True)
class ExecutionRequestContext:
    workload_identity: WorkloadIdentity
    request_id: str
    interaction_id: str | None
    decision_id: str
    task_id: str
    call_id: str
    delegation_jti: str | None
    tenant_id: str
    security_capabilities: frozenset[str]


current_execution_request: ContextVar[ExecutionRequestContext | None] = ContextVar(
    "current_execution_request", default=None
)


def unsupported_security_capabilities(
    required: Iterable[str], supported: Iterable[str]
) -> frozenset[str]:
    return frozenset(required) - frozenset(supported)


def supports_security_capabilities(
    required: Iterable[str], supported: Iterable[str]
) -> bool:
    return not unsupported_security_capabilities(required, supported)


@dataclass(frozen=True)
class WorkloadRegistration:
    workload_id: str
    service: str
    principal: str
    agent_ids: frozenset[str] = frozenset()
    tenant_ids: frozenset[str] = frozenset()
    authenticated_instance_ids: frozenset[str] = frozenset()
    lifecycle_kernel: bool = False


class WorkloadRegistry:
    def __init__(self, registrations: Iterable[WorkloadRegistration] = ()) -> None:
        self._registrations = {item.workload_id: item for item in registrations}

    def get(self, workload_id: str) -> WorkloadRegistration | None:
        return self._registrations.get(workload_id)

    def authorize(
        self,
        identity: WorkloadIdentity,
        *,
        agent_id: str,
        tenant_id: str,
        target_instance_id: str | None = None,
        lifecycle: bool = False,
    ) -> bool:
        registration = self.get(identity.workload_id)
        if registration is None or registration.service != identity.service:
            return False
        if lifecycle and not registration.lifecycle_kernel:
            return False
        if agent_id not in registration.agent_ids or tenant_id not in registration.tenant_ids:
            return False
        if identity.tenant_id is not None and identity.tenant_id != tenant_id:
            return False
        if target_instance_id is not None:
            return (
                identity.authenticated_instance_id == target_instance_id
                and target_instance_id in registration.authenticated_instance_ids
            )
        return True


class WorkloadIdentityResolver(Protocol):
    def __call__(self, request: Any) -> WorkloadIdentity | Awaitable[WorkloadIdentity | None] | None: ...


class TLSWorkloadIdentityResolver:
    """Resolve identity only from the server-provided TLS peer certificate."""

    def __init__(self, registry: WorkloadRegistry) -> None:
        self._registry = registry

    def __call__(self, request: Any) -> WorkloadIdentity | None:
        ssl_object = request.scope.get("ssl_object")
        if ssl_object is None:
            return None
        certificate = ssl_object.getpeercert()
        if not certificate:
            return None
        uri_sans = [value for kind, value in certificate.get("subjectAltName", ()) if kind == "URI"]
        for workload_id in uri_sans:
            registration = self._registry.get(workload_id)
            if registration is None:
                continue
            expires_at = None
            if certificate.get("notAfter"):
                expires_at = datetime.fromtimestamp(
                    __import__("ssl").cert_time_to_seconds(certificate["notAfter"]), UTC
                )
            return WorkloadIdentity(
                workload_id=workload_id,
                service=registration.service,
                principal=registration.principal,
                tenant_id=next(iter(registration.tenant_ids), None),
                authenticated_at=datetime.now(UTC),
                expires_at=expires_at,
                auth_method=WorkloadAuthMethod.MTLS,
            )
        return None


async def resolve_workload_identity(
    resolver: WorkloadIdentityResolver | None, request: Any
) -> WorkloadIdentity | None:
    if resolver is None:
        return None
    result = resolver(request)
    if inspect.isawaitable(result):
        return await result
    return result


class DelegationTokenVerifier(Protocol):
    def verify(self, token: str) -> dict[str, Any]: ...


class HS256DelegationTokenVerifier:
    def __init__(
        self,
        secret: str,
        *,
        issuer: str = "loop-controller-go-kernel",
    ) -> None:
        if not secret:
            raise ValueError("delegation token secret is empty")
        self._secret = secret
        self._issuer = issuer

    def verify(self, token: str) -> dict[str, Any]:
        unverified = jwt.decode(
            token,
            options={"verify_signature": False, "verify_exp": False},
            algorithms=["HS256"],
        )
        audience = unverified.get("aud")
        if not isinstance(audience, str) or not audience:
            raise ValueError("delegation token audience missing")
        return jwt.decode(
            token,
            self._secret,
            algorithms=["HS256"],
            audience=audience,
            issuer=self._issuer,
            options={"require": ["exp", "iat", "iss", "aud", "jti"]},
        )


def canonical_arguments_sha256(arguments: dict[str, Any]) -> str:
    payload = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ExecutionSecurityPolicy:
    mode: str = "compatibility"
    supported_security_capabilities: frozenset[str] = field(default_factory=frozenset)
    workload_registry: WorkloadRegistry = field(default_factory=WorkloadRegistry)
    require_instance_identity: bool = False
    delegation_token_verifier: DelegationTokenVerifier | None = None

    @property
    def strict(self) -> bool:
        return self.mode == "strict"
