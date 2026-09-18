"""RBAC 领域模型（v0.52）。

角色集冻结（见 v0.52.0 开发文档 §4.2）；权限粒度为"端点 × 租户"，
不引入 ABAC/ReBAC。executor 身份不在本模型内（沿用既有 AgentIdentity）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    PLATFORM_ADMIN = "platform_admin"
    TENANT_ADMIN = "tenant_admin"
    POLICY_CREATOR = "policy_creator"
    POLICY_VALIDATOR = "policy_validator"
    POLICY_PUBLISHER = "policy_publisher"
    POLICY_AUDITOR = "policy_auditor"
    APPROVER = "approver"


# 权限命名空间（端点 × 租户；bundle.read / status 上报保持独立机器凭证，不在其内）
PERM_CANDIDATE_CREATE = "policy.candidate.create"
PERM_CANDIDATE_READ = "policy.candidate.read"
PERM_CANDIDATE_UPDATE = "policy.candidate.update"
PERM_VALIDATE_RUN = "policy.validate.run"
PERM_SHADOW_RUN = "policy.shadow.run"
PERM_PUBLISH = "policy.publish"
PERM_ROLLBACK = "policy.rollback"
PERM_AUDIT_READ = "policy.audit.read"
PERM_STATUS_READ = "policy.status.read"
PERM_APPROVAL_DECIDE = "approval.decide"
PERM_RBAC_MANAGE = "rbac.binding.manage"
PERM_KILL_SWITCH = "admin.kill_switch"
PERM_REVOKE = "admin.revoke"
# 跨租户发布的二次检查资源标记（非端点权限，grant resources 专用）
RESOURCE_PUBLISH_GLOBAL = "policy.publish.global"

ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.PLATFORM_ADMIN: frozenset({
        PERM_CANDIDATE_CREATE, PERM_CANDIDATE_READ, PERM_CANDIDATE_UPDATE,
        PERM_VALIDATE_RUN, PERM_SHADOW_RUN, PERM_PUBLISH, PERM_ROLLBACK,
        PERM_AUDIT_READ, PERM_STATUS_READ, PERM_APPROVAL_DECIDE,
        PERM_RBAC_MANAGE, PERM_KILL_SWITCH, PERM_REVOKE,
    }),
    Role.TENANT_ADMIN: frozenset({
        PERM_APPROVAL_DECIDE, PERM_KILL_SWITCH, PERM_REVOKE,
        PERM_RBAC_MANAGE, PERM_STATUS_READ,
    }),
    Role.POLICY_CREATOR: frozenset({
        PERM_CANDIDATE_CREATE, PERM_CANDIDATE_READ, PERM_CANDIDATE_UPDATE,
        PERM_SHADOW_RUN,
    }),
    Role.POLICY_VALIDATOR: frozenset({
        PERM_CANDIDATE_READ, PERM_VALIDATE_RUN, PERM_SHADOW_RUN,
    }),
    Role.POLICY_PUBLISHER: frozenset({
        PERM_CANDIDATE_READ, PERM_PUBLISH, PERM_ROLLBACK,
    }),
    Role.POLICY_AUDITOR: frozenset({
        PERM_CANDIDATE_READ, PERM_AUDIT_READ, PERM_STATUS_READ,
    }),
    Role.APPROVER: frozenset({PERM_APPROVAL_DECIDE}),
}


@dataclass(frozen=True)
class RoleBinding:
    binding_id: str
    principal: str
    tenant_id: str | None  # None = 平台级绑定
    role: Role
    granted_by: str
    created_at: str
    revoked_at: str | None = None


@dataclass(frozen=True)
class CrossTenantGrant:
    grant_id: str
    source_principal: str
    source_tenant: str
    target_tenant: str
    resources: tuple[str, ...]
    granted_by: str
    created_at: str
    revoked_at: str | None = None


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """认证后的管理面主体。roles 为生效绑定角色（非 claim 断言）。"""

    principal_id: str
    tenant_id: str | None
    roles: tuple[str, ...]
    auth_method: str  # "jwt" | "static-credential" | "legacy-key"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class RbacDenial:
    """一次授权拒绝事件（落 rbac_denials 表，无自动清理）。"""

    denial_id: str
    actor: str
    endpoint: str
    required_permission: str
    principal_tenant: str | None
    reason: str
    created_at: str
