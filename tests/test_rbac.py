"""RBAC 判定层 / 存储 / 静态凭证单元测试（v0.52）。

覆盖：角色→权限映射、租户隔离、跨租户 grant、publish_scope 叠加检查、
双人复核 waiver 资格、approver 双轨锚定、SQLite 绑定/授权/拒绝事件读写、
静态凭证解析（fail-closed、长度预检、即时轮换）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from loop_controller.infra.state_db import StateDatabase
from loop_controller.rbac.credentials import StaticCredential, StaticCredentialResolver
from loop_controller.rbac.enforcer import RbacEnforcer
from loop_controller.rbac.models import (
    PERM_APPROVAL_DECIDE,
    PERM_AUDIT_READ,
    PERM_CANDIDATE_CREATE,
    PERM_CANDIDATE_READ,
    PERM_PUBLISH,
    PERM_RBAC_MANAGE,
    PERM_VALIDATE_RUN,
    RESOURCE_PUBLISH_GLOBAL,
    AuthenticatedPrincipal,
    CrossTenantGrant,
    RbacDenial,
    Role,
    RoleBinding,
)
from loop_controller.rbac.store import SqliteRoleBindingStore


def _principal(
    principal_id: str,
    tenant_id: str | None,
    roles: tuple[str, ...] = (),
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        principal_id=principal_id,
        tenant_id=tenant_id,
        roles=roles,
        auth_method="static-credential",
    )


@pytest.fixture
def store(tmp_path: Path) -> SqliteRoleBindingStore:
    db = StateDatabase(tmp_path / "state.db")
    db.init_schema()
    return SqliteRoleBindingStore(db)


def _binding(
    principal: str, tenant_id: str | None, role: Role, binding_id: str = "b1"
) -> RoleBinding:
    return RoleBinding(
        binding_id=binding_id,
        principal=principal,
        tenant_id=tenant_id,
        role=role,
        granted_by="platform-admin",
        created_at="2026-09-10T00:00:00+00:00",
    )


class TestAuthorize:
    def test_platform_admin_always_allowed(self, store: SqliteRoleBindingStore) -> None:
        enforcer = RbacEnforcer(store, static_bindings=(_binding("root", None, Role.PLATFORM_ADMIN),))
        principal = _principal("root", None, (Role.PLATFORM_ADMIN.value,))
        decision = enforcer.authorize(principal, PERM_PUBLISH, "tenant-a")
        assert decision.allowed

    def test_same_tenant_allowed(self, store: SqliteRoleBindingStore) -> None:
        enforcer = RbacEnforcer(
            store, static_bindings=(_binding("alice", "tenant-a", Role.POLICY_CREATOR),)
        )
        principal = _principal("alice", "tenant-a")
        assert enforcer.authorize(principal, PERM_CANDIDATE_CREATE, "tenant-a").allowed

    def test_cross_tenant_denied_without_grant(self, store: SqliteRoleBindingStore) -> None:
        enforcer = RbacEnforcer(
            store, static_bindings=(_binding("alice", "tenant-a", Role.POLICY_CREATOR),)
        )
        principal = _principal("alice", "tenant-a")
        decision = enforcer.authorize(principal, PERM_CANDIDATE_CREATE, "tenant-b")
        assert not decision.allowed

    def test_platform_level_resource_denied_for_tenant_role(
        self, store: SqliteRoleBindingStore
    ) -> None:
        enforcer = RbacEnforcer(
            store, static_bindings=(_binding("alice", "tenant-a", Role.POLICY_AUDITOR),)
        )
        principal = _principal("alice", "tenant-a")
        decision = enforcer.authorize(principal, PERM_AUDIT_READ, None)
        assert not decision.allowed

    def test_cross_tenant_grant_allows(self, store: SqliteRoleBindingStore) -> None:
        grant = CrossTenantGrant(
            grant_id="g1",
            source_principal="auditor-a",
            source_tenant="tenant-a",
            target_tenant="tenant-b",
            resources=(PERM_AUDIT_READ,),
            granted_by="platform-admin",
            created_at="2026-09-10T00:00:00+00:00",
        )
        enforcer = RbacEnforcer(store, static_grants=(grant,))
        principal = _principal("auditor-a", "tenant-a")
        assert enforcer.authorize(principal, PERM_AUDIT_READ, "tenant-b").allowed
        # grant 未列明的权限仍拒绝
        assert not enforcer.authorize(principal, PERM_VALIDATE_RUN, "tenant-b").allowed

    def test_permission_missing_denied(self, store: SqliteRoleBindingStore) -> None:
        enforcer = RbacEnforcer(
            store, static_bindings=(_binding("alice", "tenant-a", Role.POLICY_VALIDATOR),)
        )
        principal = _principal("alice", "tenant-a")
        assert not enforcer.authorize(principal, PERM_PUBLISH, "tenant-a").allowed

    def test_store_exception_fail_closed(self, store: SqliteRoleBindingStore) -> None:
        class _BrokenStore:
            def list_bindings(self, principal: str):
                raise RuntimeError("boom")

        enforcer = RbacEnforcer(_BrokenStore())  # type: ignore[arg-type]
        principal = _principal("alice", "tenant-a")
        assert not enforcer.authorize(principal, PERM_CANDIDATE_READ, "tenant-a").allowed
        assert enforcer.roles_for(principal) == ()


class TestPublishScope:
    def test_platform_admin_passes(self, store: SqliteRoleBindingStore) -> None:
        enforcer = RbacEnforcer(store, static_bindings=(_binding("root", None, Role.PLATFORM_ADMIN),))
        assert enforcer.check_publish_scope(_principal("root", None)).allowed

    def test_tenant_publisher_blocked(self, store: SqliteRoleBindingStore) -> None:
        enforcer = RbacEnforcer(
            store, static_bindings=(_binding("pub", "tenant-a", Role.POLICY_PUBLISHER),)
        )
        decision = enforcer.check_publish_scope(_principal("pub", "tenant-a"))
        assert not decision.allowed

    def test_publish_scope_grant_passes(self, store: SqliteRoleBindingStore) -> None:
        grant = CrossTenantGrant(
            grant_id="g1",
            source_principal="pub",
            source_tenant="tenant-a",
            target_tenant="tenant-a",
            resources=(RESOURCE_PUBLISH_GLOBAL,),
            granted_by="platform-admin",
            created_at="2026-09-10T00:00:00+00:00",
        )
        enforcer = RbacEnforcer(store, static_grants=(grant,))
        assert enforcer.check_publish_scope(_principal("pub", "tenant-a")).allowed


class TestSeparationWaiver:
    def test_only_platform_admin_with_config(self, store: SqliteRoleBindingStore) -> None:
        admin = _principal("root", None)
        publisher = _principal("pub", "tenant-a")
        bindings = (
            _binding("root", None, Role.PLATFORM_ADMIN, "b1"),
            _binding("pub", "tenant-a", Role.POLICY_PUBLISHER, "b2"),
        )
        strict = RbacEnforcer(store, static_bindings=bindings, allow_self_publish=False)
        assert not strict.can_waive_separation(admin)
        assert not strict.can_waive_separation(publisher)
        relaxed = RbacEnforcer(store, static_bindings=bindings, allow_self_publish=True)
        assert relaxed.can_waive_separation(admin)
        assert not relaxed.can_waive_separation(publisher)


class TestApproverBinding:
    def test_matches(self, store: SqliteRoleBindingStore) -> None:
        enforcer = RbacEnforcer(
            store, static_bindings=(_binding("bob", "tenant-a", Role.APPROVER),)
        )
        assert enforcer.approver_binding_matches("bob", "tenant-a")
        assert not enforcer.approver_binding_matches("bob", "tenant-b")
        assert not enforcer.approver_binding_matches("carol", "tenant-a")

    def test_platform_binding_only_matches_platform_request(
        self, store: SqliteRoleBindingStore
    ) -> None:
        enforcer = RbacEnforcer(
            store, static_bindings=(_binding("root-approver", None, Role.APPROVER),)
        )
        assert enforcer.approver_binding_matches("root-approver", None)
        # 平台级绑定不匹配具体租户请求（fail-closed 双轨锚定）
        assert not enforcer.approver_binding_matches("root-approver", "tenant-a")


class TestSqliteStore:
    def test_binding_roundtrip_and_revoke(self, store: SqliteRoleBindingStore) -> None:
        binding = store.add_binding("alice", "tenant-a", Role.POLICY_CREATOR, "root")
        listed = store.list_bindings("alice")
        assert [b.binding_id for b in listed] == [binding.binding_id]
        assert listed[0].role is Role.POLICY_CREATOR
        assert store.list_all_bindings("tenant-a")
        assert not store.list_all_bindings("tenant-b")
        assert store.revoke_binding(binding.binding_id, "root")
        assert store.list_bindings("alice") == []
        assert not store.revoke_binding(binding.binding_id, "root")

    def test_grant_roundtrip_and_revoke(self, store: SqliteRoleBindingStore) -> None:
        grant = store.add_grant(
            "auditor-a", "tenant-a", "tenant-b", (PERM_AUDIT_READ,), "root"
        )
        assert store.list_grants("auditor-a", "tenant-a")[0].grant_id == grant.grant_id
        # SQLite 参数化 NULL 安全比较：source_tenant 不匹配即查不到
        assert store.list_grants("auditor-a", None) == []
        assert store.revoke_grant(grant.grant_id, "root")
        assert store.list_all_grants() == []

    def test_record_denial(self, store: SqliteRoleBindingStore, tmp_path: Path) -> None:
        store.record_denial(
            RbacDenial(
                denial_id="d1",
                actor="alice",
                endpoint="/v1/admin/policy/candidates",
                required_permission=PERM_CANDIDATE_READ,
                principal_tenant="tenant-a",
                reason="角色集合缺少权限",
                created_at="2026-09-10T00:00:00+00:00",
            )
        )
        db = StateDatabase(tmp_path / "state.db")
        with db._connect() as conn:
            row = conn.execute("SELECT actor, reason FROM rbac_denials WHERE denial_id = 'd1'").fetchone()
        assert row["actor"] == "alice"


class TestStaticCredentials:
    def test_resolve_success(self) -> None:
        resolver = StaticCredentialResolver(
            (StaticCredential("alice", "tenant-a", "LC_TOKEN_ALICE", ("policy_creator",)),),
            environ={"LC_TOKEN_ALICE": "s3cret-token"},
        )
        principal = resolver.resolve("alice", "s3cret-token")
        assert principal is not None
        assert principal.tenant_id == "tenant-a"
        assert principal.auth_method == "static-credential"

    def test_resolve_fail_closed(self) -> None:
        resolver = StaticCredentialResolver(
            (StaticCredential("alice", "tenant-a", "LC_TOKEN_ALICE"),),
            environ={"LC_TOKEN_ALICE": "s3cret-token"},
        )
        assert resolver.resolve("alice", "wrong-token") is None
        assert resolver.resolve("alice", None) is None
        assert resolver.resolve(None, "s3cret-token") is None
        # 长度预检：不同长度直接拒绝（不进入 compare_digest）
        assert resolver.resolve("alice", "short") is None

    def test_unset_env_denied(self) -> None:
        resolver = StaticCredentialResolver(
            (StaticCredential("alice", "tenant-a", "LC_TOKEN_MISSING"),),
            environ={},
        )
        assert resolver.resolve("alice", "anything-here") is None

    def test_rotation_invalidates_old_token(self) -> None:
        environ = {"LC_TOKEN_ALICE": "old-token"}
        resolver = StaticCredentialResolver(
            (StaticCredential("alice", "tenant-a", "LC_TOKEN_ALICE"),),
            environ=environ,
        )
        assert resolver.resolve("alice", "old-token") is not None
        environ["LC_TOKEN_ALICE"] = "new-token"
        assert resolver.resolve("alice", "old-token") is None
        assert resolver.resolve("alice", "new-token") is not None


class TestRolePermissions:
    def test_approver_only_decide(self) -> None:
        from loop_controller.rbac.models import ROLE_PERMISSIONS

        assert ROLE_PERMISSIONS[Role.APPROVER] == frozenset({PERM_APPROVAL_DECIDE})
        assert PERM_RBAC_MANAGE in ROLE_PERMISSIONS[Role.TENANT_ADMIN]
        assert PERM_PUBLISH not in ROLE_PERMISSIONS[Role.POLICY_VALIDATOR]
