"""统一访问控制平面（v0.52）：多租户、企业身份与 RBAC。"""

from loop_controller.rbac.credentials import StaticCredential, StaticCredentialResolver
from loop_controller.rbac.enforcer import RbacEnforcer
from loop_controller.rbac.models import (
    ROLE_PERMISSIONS,
    AuthenticatedPrincipal,
    CrossTenantGrant,
    Decision,
    RbacDenial,
    Role,
    RoleBinding,
)
from loop_controller.rbac.store import RoleBindingStore, SqliteRoleBindingStore

__all__ = [
    "AuthenticatedPrincipal",
    "CrossTenantGrant",
    "Decision",
    "RbacDenial",
    "RbacEnforcer",
    "Role",
    "RoleBinding",
    "ROLE_PERMISSIONS",
    "RoleBindingStore",
    "SqliteRoleBindingStore",
    "StaticCredential",
    "StaticCredentialResolver",
]
