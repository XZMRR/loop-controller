"""RBAC 判定层（v0.52）。

判定顺序（fail-closed，任何解析异常即拒绝）：
1. platform_admin 角色 → 放行；
2. 角色→权限映射不包含所需权限 → 拒绝；
3. 资源租户 == 主体租户 → 放行；
4. 资源为平台级（tenant_id IS NULL）→ 拒绝（仅 platform_admin）；
5. 跨租户：存在有效 cross_tenant_grant 且资源在列明范围内 → 放行，否则拒绝。

叠加规则（v0.52 文档 §4.2）：policy.publish / policy.rollback 在 permission
判定通过后，还须满足 publish_scope 二次检查——本模块提供 check_publish_scope。
"""

from __future__ import annotations

from loop_controller.rbac.models import (
    RESOURCE_PUBLISH_GLOBAL,
    ROLE_PERMISSIONS,
    AuthenticatedPrincipal,
    CrossTenantGrant,
    Decision,
    Role,
    RoleBinding,
)
from loop_controller.rbac.store import RoleBindingStore


class RbacEnforcer:
    def __init__(
        self,
        store: RoleBindingStore,
        *,
        static_bindings: tuple[RoleBinding, ...] = (),
        static_grants: tuple[CrossTenantGrant, ...] = (),
        allow_self_publish: bool = False,
    ) -> None:
        self._store = store
        self._static_bindings = static_bindings
        self._static_grants = static_grants
        self._allow_self_publish = allow_self_publish

    # ------------------------------------------------------------------
    # 角色解析
    # ------------------------------------------------------------------

    def roles_for(self, principal: AuthenticatedPrincipal) -> tuple[Role, ...]:
        """生效角色 = 静态配置绑定 ∪ SQLite 绑定（均未吊销）。"""
        roles: set[Role] = set()
        for binding in self._static_bindings:
            if binding.principal == principal.principal_id and binding.revoked_at is None:
                if binding.tenant_id is None or binding.tenant_id == principal.tenant_id:
                    roles.add(binding.role)
        try:
            for binding in self._store.list_bindings(principal.principal_id):
                if binding.tenant_id is None or binding.tenant_id == principal.tenant_id:
                    roles.add(binding.role)
        except Exception:  # noqa: BLE001 - 存储异常按 fail-closed 处理
            return ()
        return tuple(sorted(roles, key=lambda r: r.value))

    # ------------------------------------------------------------------
    # 授权判定
    # ------------------------------------------------------------------

    def authorize(
        self,
        principal: AuthenticatedPrincipal,
        permission: str,
        tenant_id: str | None,
    ) -> Decision:
        try:
            roles = self.roles_for(principal)
            if Role.PLATFORM_ADMIN in roles:
                return Decision(True, "platform_admin")
            if tenant_id is None:
                return Decision(False, "平台级资源仅 platform_admin 可访问")
            if tenant_id == principal.tenant_id:
                if any(permission in ROLE_PERMISSIONS.get(role, frozenset()) for role in roles):
                    return Decision(True, "same_tenant")
                return Decision(False, f"角色集合缺少权限 {permission}")
            # 跨租户：grant 本身即授权（文档 4.6），不要求源租户角色含该权限
            if self._grant_allows(principal, tenant_id, permission):
                return Decision(True, "cross_tenant_grant")
            return Decision(False, "跨租户访问未授权")
        except Exception as exc:  # noqa: BLE001 - fail-closed
            return Decision(False, f"授权判定异常: {type(exc).__name__}")

    def check_publish_scope(self, principal: AuthenticatedPrincipal) -> Decision:
        """发布/回滚的二次检查：全局 current pointer 是平台级影响面。

        仅 platform_admin，或持有 policy.publish.global 资源的跨租户授权可通过。
        """
        try:
            if Role.PLATFORM_ADMIN in self.roles_for(principal):
                return Decision(True, "platform_admin")
            if self._grant_allows(principal, None, RESOURCE_PUBLISH_GLOBAL):
                return Decision(True, "publish_scope_grant")
            return Decision(False, "发布影响全局 current pointer，需要 platform_admin 或 publish_scope 授权")
        except Exception as exc:  # noqa: BLE001 - fail-closed
            return Decision(False, f"publish_scope 判定异常: {type(exc).__name__}")

    def can_waive_separation(self, principal: AuthenticatedPrincipal) -> bool:
        """双人复核逃生口：仅 platform_admin，且配置显式允许。"""
        return self._allow_self_publish and Role.PLATFORM_ADMIN in self.roles_for(principal)

    def approver_binding_matches(self, principal_id: str, tenant_id: str | None) -> bool:
        """审批 tenant 双轨锚定：approval_auth principal 必须持有落在请求租户上的
        approver 绑定（tenant_id 为 None 的绑定视为平台级，仅匹配平台级请求）。"""
        try:
            for binding in self._bindings_for(principal_id):
                if binding.role is not Role.APPROVER or binding.revoked_at is not None:
                    continue
                if tenant_id is None:
                    if binding.tenant_id is None:
                        return True
                elif binding.tenant_id == tenant_id:
                    return True
            return False
        except Exception:  # noqa: BLE001 - fail-closed
            return False

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _bindings_for(self, principal_id: str) -> list[RoleBinding]:
        bindings = [b for b in self._static_bindings if b.principal == principal_id]
        try:
            bindings.extend(self._store.list_bindings(principal_id))
        except Exception:  # noqa: BLE001
            pass
        return bindings

    def _grant_allows(
        self, principal: AuthenticatedPrincipal, target_tenant: str | None, resource: str
    ) -> bool:
        grants = [g for g in self._static_grants if g.revoked_at is None]
        try:
            grants.extend(self._store.list_grants(principal.principal_id, principal.tenant_id))
        except Exception:  # noqa: BLE001 - 存储异常视为无授权
            return False
        for grant in grants:
            if grant.source_principal != principal.principal_id:
                continue
            if grant.source_tenant != principal.tenant_id:
                continue
            if target_tenant is not None and grant.target_tenant != target_tenant:
                continue
            if resource in grant.resources:
                return True
        return False
