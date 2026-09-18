"""RBAC enforcement 服务端矩阵测试（v0.52 P52-04/07/08）。

enforcement=disabled 时维持 legacy api key 行为（零变化）；
enforcement=enabled 时：静态角色凭证认证、端点 × 租户 permission 映射、
publish_scope 叠加检查、双人复核 409/waiver、rbac_denials 拒绝审计、
/v1/admin/rbac/* 绑定管理。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from loop_controller.infra.policy_delivery import PolicyDelivery
from loop_controller.infra.state_db import StateDatabase
from loop_controller.policy_lifecycle import PolicyLifecycleConfig, PolicyLifecycleService
from loop_controller.rbac.credentials import StaticCredential, StaticCredentialResolver
from loop_controller.rbac.enforcer import RbacEnforcer
from loop_controller.rbac.models import Role, RoleBinding
from loop_controller.rbac.store import SqliteRoleBindingStore
from loop_controller.server import build_app

TOKENS = {
    "admin": "admin-token-0123456789",
    "creator-a": "creator-a-token-0123456",
    "creator-b": "creator-b-token-0123456",
    "validator-a": "validator-a-token-0123",
    "publisher-a": "publisher-a-token-0123",
    "auditor-a": "auditor-a-token-0123456",
    "auditor-b": "auditor-b-token-0123456",
    "auditor-no-tenant": "auditor-no-tenant-token-0123456",
}


class AuditEvent:
    def __init__(
        self,
        event_id: str,
        tenant_id: str | None,
        *,
        session_id: str = "session-shared",
        task_id: str = "task-shared",
        correlation_id: str = "correlation-shared",
        interaction_id: str = "interaction-shared",
    ) -> None:
        self.event_id = event_id
        self.tenant_id = tenant_id
        self.session_id = session_id
        self.task_id = task_id
        self.correlation_id = correlation_id
        self.interaction_id = interaction_id

    def model_dump(self, mode: str | None = None) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "correlation_id": self.correlation_id,
            "interaction_id": self.interaction_id,
        }


class Audit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append_async(self, event: Any) -> None:
        self.events.append(event)

    def list_recent(self, limit: int) -> list[Any]:
        return self.events[-limit:]

    def query_by_session(self, session_id: str) -> list[Any]:
        return [event for event in self.events if event.session_id == session_id]

    def query_by_task(self, task_id: str) -> list[Any]:
        return [event for event in self.events if event.task_id == task_id]

    def query_by_correlation(self, correlation_id: str, limit: int = 100) -> list[Any]:
        return [
            event for event in self.events if event.correlation_id == correlation_id
        ][-limit:]

    def query_interactions(
        self,
        *,
        interaction_id: str | None = None,
        source_agent_id: str | None = None,
        target_agent_id: str | None = None,
        verdict: str | None = None,
        limit: int = 100,
    ) -> list[Any]:
        del source_agent_id, target_agent_id, verdict
        return [
            event
            for event in self.events
            if interaction_id is None or event.interaction_id == interaction_id
        ][-limit:]


class Controller:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    async def start(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


class _RbacConfig:
    dynamic_role_bindings = False
    legacy_key_role = "platform_admin"


class _Config:
    rbac = _RbacConfig()


class Runtime:
    def __init__(
        self,
        delivery: PolicyDelivery,
        lifecycle: PolicyLifecycleService,
        store: SqliteRoleBindingStore,
        enforcer: RbacEnforcer | None,
        resolver: StaticCredentialResolver | None,
    ) -> None:
        self.policy_delivery = delivery
        self.policy_validation = None
        self.policy_shadow = None
        self.policy_lifecycle = lifecycle
        self.bundle_reader_token = "bundle-secret"
        self.status_writer_token = "status-secret"
        self.audit_store = Audit()
        self.harness_executor = None
        self.evidence_anchor = None
        self.checkpoint = type("Checkpoint", (), {"_policy_engine": None, "degraded_backends": ()})()
        self.rbac_store = store
        self.rbac_enforcer = enforcer
        self.rbac_credential_resolver = resolver
        self.config = _Config()


def _credentials() -> tuple[StaticCredential, ...]:
    return (
        StaticCredential("admin", None, "LC_TOK_ADMIN", ("platform_admin",)),
        StaticCredential("creator-a", "tenant-a", "LC_TOK_CREATOR_A", ("policy_creator",)),
        StaticCredential("creator-b", "tenant-b", "LC_TOK_CREATOR_B", ("policy_creator",)),
        StaticCredential("validator-a", "tenant-a", "LC_TOK_VALIDATOR_A", ("policy_validator",)),
        StaticCredential("publisher-a", "tenant-a", "LC_TOK_PUBLISHER_A", ("policy_publisher",)),
        StaticCredential("auditor-a", "tenant-a", "LC_TOK_AUDITOR_A", ("policy_auditor",)),
        StaticCredential("auditor-b", "tenant-b", "LC_TOK_AUDITOR_B", ("policy_auditor",)),
        StaticCredential(
            "auditor-no-tenant",
            None,
            "LC_TOK_AUDITOR_NO_TENANT",
            ("policy_auditor",),
        ),
    )


def _environ() -> dict[str, str]:
    return {f"LC_TOK_{name.upper().replace('-', '_')}": token for name, token in TOKENS.items()}


def _setup(tmp_path: Path, *, allow_self_publish: bool = False) -> tuple[TestClient, SqliteRoleBindingStore, Runtime]:
    db = StateDatabase(tmp_path / "state.db")
    db.init_schema()
    store = SqliteRoleBindingStore(db)
    delivery = PolicyDelivery(tmp_path, db)
    lifecycle = PolicyLifecycleService(
        db, PolicyLifecycleConfig(bundle_name="lc", required_instance_ids=("opa-a",), status_ttl_seconds=60)
    )
    credentials = _credentials()
    resolver = StaticCredentialResolver(credentials, environ=_environ())
    # 与 runtime 装配一致：静态凭证 roles 展开为 enforcer 静态绑定
    static_bindings = tuple(
        RoleBinding(
            binding_id=f"static-{cred.principal}-{role_name}",
            principal=cred.principal,
            tenant_id=cred.tenant_id,
            role=Role(role_name),
            granted_by="static-config",
            created_at="",
        )
        for cred in credentials
        for role_name in cred.roles
    )
    enforcer = RbacEnforcer(
        store, static_bindings=static_bindings, allow_self_publish=allow_self_publish
    )
    runtime = Runtime(delivery, lifecycle, store, enforcer, resolver)
    app = build_app(Controller(runtime), api_key="admin-secret", configure_logs=False)
    return TestClient(app), store, runtime


def _auth(name: str) -> dict[str, str]:
    return {
        "x-lc-principal": name,
        "Authorization": f"Bearer {TOKENS[name]}",
    }


def _create_candidate(client: TestClient, who: str) -> dict[str, Any]:
    response = client.post(
        "/v1/admin/policy/candidates",
        headers=_auth(who),
        json={"files": {"default.rego": "package loop_controller.tool_permission"}},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _validated_candidate(client: TestClient, runtime: Runtime, who: str = "creator-a") -> dict[str, Any]:
    """经 HTTP 创建，用 validate_without_opa 以 validator-a 身份写入双人复核证据。"""
    payload = _create_candidate(client, who)
    runtime.policy_delivery.validate_without_opa(payload["candidate_id"], actor="validator-a")
    return payload


def _denials(store: SqliteRoleBindingStore) -> list[Any]:
    with store._db._connect() as conn:
        return conn.execute("SELECT * FROM rbac_denials ORDER BY created_at").fetchall()


class TestDisabledZeroBehaviorChange:
    def test_legacy_api_key_still_works(self, tmp_path: Path) -> None:
        db = StateDatabase(tmp_path / "state.db")
        db.init_schema()
        store = SqliteRoleBindingStore(db)
        delivery = PolicyDelivery(tmp_path, db)
        lifecycle = PolicyLifecycleService(
            db, PolicyLifecycleConfig(bundle_name="lc", required_instance_ids=("opa-a",), status_ttl_seconds=60)
        )
        # enforcement 未启用：enforcer/resolver 为 None
        runtime = Runtime(delivery, lifecycle, store, None, None)
        app = build_app(Controller(runtime), api_key="admin-secret", configure_logs=False)
        client = TestClient(app)
        response = client.get("/v1/admin/policy/candidates", headers={"x-api-key": "admin-secret"})
        assert response.status_code == 200
        assert client.get("/v1/admin/policy/candidates").status_code == 401


class TestAuthentication:
    def test_missing_credentials_401(self, tmp_path: Path) -> None:
        client, _store, _runtime = _setup(tmp_path)
        assert client.get("/v1/admin/policy/candidates").status_code == 401

    def test_wrong_token_401(self, tmp_path: Path) -> None:
        client, _store, _runtime = _setup(tmp_path)
        headers = {"x-lc-principal": "creator-a", "Authorization": "Bearer wrong-token-01234567890"}
        assert client.get("/v1/admin/policy/candidates", headers=headers).status_code == 401

    def test_unknown_principal_401(self, tmp_path: Path) -> None:
        client, _store, _runtime = _setup(tmp_path)
        headers = {"x-lc-principal": "ghost", "Authorization": f"Bearer {TOKENS['admin']}"}
        assert client.get("/v1/admin/policy/candidates", headers=headers).status_code == 401


class TestPermissionMatrix:
    def test_creator_create_and_list_own_tenant(self, tmp_path: Path) -> None:
        client, _store, _runtime = _setup(tmp_path)
        payload = _create_candidate(client, "creator-a")
        assert payload["tenant_id"] == "tenant-a"
        assert payload["created_by"] == "creator-a"
        listed = client.get("/v1/admin/policy/candidates", headers=_auth("creator-a"))
        assert listed.status_code == 200
        assert [c["candidate_id"] for c in listed.json()["candidates"]] == [payload["candidate_id"]]

    def test_cross_tenant_candidate_read_denied(self, tmp_path: Path) -> None:
        client, store, _runtime = _setup(tmp_path)
        payload = _create_candidate(client, "creator-a")
        response = client.get(
            f"/v1/admin/policy/candidates/{payload['candidate_id']}", headers=_auth("creator-b")
        )
        assert response.status_code == 403
        denials = _denials(store)
        assert len(denials) == 1
        assert denials[0]["actor"] == "creator-b"
        assert denials[0]["principal_tenant"] == "tenant-b"

    def test_missing_permission_denied_and_recorded(self, tmp_path: Path) -> None:
        client, store, _runtime = _setup(tmp_path)
        response = client.get("/v1/admin/policy/audit", headers=_auth("creator-a"))
        assert response.status_code == 403
        assert _denials(store)[0]["required_permission"] == "policy.audit.read"

    def test_auditor_reads_own_tenant_only(self, tmp_path: Path) -> None:
        client, _store, _runtime = _setup(tmp_path)
        candidate_a = _create_candidate(client, "creator-a")
        candidate_b = _create_candidate(client, "creator-b")
        _runtime.policy_delivery.validate_without_opa(
            candidate_a["candidate_id"], actor="validator-a"
        )
        _runtime.policy_delivery.validate_without_opa(
            candidate_b["candidate_id"], actor="validator-b"
        )
        response = client.get("/v1/admin/policy/audit", headers=_auth("auditor-a"))
        assert response.status_code == 200
        events = response.json()["events"]
        assert len(events) == 1  # 仅 tenant-a 的 candidate_create 审计
        admin_view = client.get("/v1/admin/policy/audit", headers=_auth("admin"))
        assert len(admin_view.json()["events"]) == 2

    def test_status_requires_role(self, tmp_path: Path) -> None:
        client, _store, _runtime = _setup(tmp_path)
        assert client.get("/v1/admin/policy/status", headers=_auth("creator-a")).status_code == 403
        assert client.get("/v1/admin/policy/status", headers=_auth("auditor-a")).status_code == 200


class TestAdminAuditRbac:
    @staticmethod
    def _seed(runtime: Runtime) -> None:
        runtime.audit_store.events = [
            AuditEvent("event-a", "tenant-a"),
            AuditEvent("event-b", "tenant-b"),
            AuditEvent("event-platform", None),
        ]

    def test_missing_audit_permission_denied_and_recorded(self, tmp_path: Path) -> None:
        client, store, _runtime = _setup(tmp_path)

        response = client.get("/v1/admin/audit", headers=_auth("creator-a"))

        assert response.status_code == 403
        denial = _denials(store)[-1]
        assert denial["actor"] == "creator-a"
        assert denial["required_permission"] == "policy.audit.read"
        assert denial["endpoint"] == "/v1/admin/audit"

    def test_auditor_sees_only_same_tenant_for_every_query_path(
        self, tmp_path: Path
    ) -> None:
        client, _store, runtime = _setup(tmp_path)
        self._seed(runtime)
        queries = (
            {},
            {"session_id": "session-shared"},
            {"task_id": "task-shared"},
            {"interaction_id": "interaction-shared"},
            {"correlation_id": "correlation-shared"},
        )

        for params in queries:
            response = client.get(
                "/v1/admin/audit",
                params={**params, "limit": 10},
                headers=_auth("auditor-a"),
            )
            assert response.status_code == 200, (params, response.text)
            assert [event["event_id"] for event in response.json()["events"]] == ["event-a"]

    def test_correlation_limit_cannot_expose_cross_tenant_event(self, tmp_path: Path) -> None:
        client, _store, runtime = _setup(tmp_path)
        self._seed(runtime)

        response = client.get(
            "/v1/admin/audit",
            params={"correlation_id": "correlation-shared", "limit": 1},
            headers=_auth("auditor-a"),
        )

        assert response.status_code == 200
        assert response.json()["events"] == []

    def test_non_platform_principal_without_tenant_fails_closed(self, tmp_path: Path) -> None:
        client, store, runtime = _setup(tmp_path)
        self._seed(runtime)

        response = client.get("/v1/admin/audit", headers=_auth("auditor-no-tenant"))

        assert response.status_code == 403
        denial = _denials(store)[-1]
        assert denial["actor"] == "auditor-no-tenant"
        assert denial["required_permission"] == "policy.audit.read"

    def test_platform_admin_sees_all_tenants(self, tmp_path: Path) -> None:
        client, _store, runtime = _setup(tmp_path)
        self._seed(runtime)

        response = client.get("/v1/admin/audit", headers=_auth("admin"))

        assert response.status_code == 200
        assert [event["event_id"] for event in response.json()["events"]] == [
            "event-a",
            "event-b",
            "event-platform",
        ]


class TestPublishScopeAndSeparation:
    def test_tenant_publisher_blocked_by_publish_scope(self, tmp_path: Path) -> None:
        client, store, runtime = _setup(tmp_path)
        payload = _validated_candidate(client, runtime)
        response = client.post(
            f"/v1/admin/policy/candidates/{payload['candidate_id']}/publish",
            headers=_auth("publisher-a"),
            json={"base_revision": None},
        )
        assert response.status_code == 403
        assert _denials(store)[-1]["required_permission"] == "policy.publish.global"

    def test_self_publish_violates_separation(self, tmp_path: Path) -> None:
        client, _store, runtime = _setup(tmp_path)
        payload = _create_candidate(client, "admin")
        # admin 自己写入校验证据（与 created_by 相同）→ separation_ok=0
        runtime.policy_delivery.validate_without_opa(payload["candidate_id"], actor="admin")
        response = client.post(
            f"/v1/admin/policy/candidates/{payload['candidate_id']}/publish",
            headers=_auth("admin"),
            json={"base_revision": None},
        )
        assert response.status_code == 409
        assert response.json()["error"] == "separation_of_duties_violation"

    def test_two_person_separation_allows_publish(self, tmp_path: Path) -> None:
        client, _store, runtime = _setup(tmp_path)
        payload = _validated_candidate(client, runtime)
        response = client.post(
            f"/v1/admin/policy/candidates/{payload['candidate_id']}/publish",
            headers=_auth("admin"),
            json={"base_revision": None},
        )
        assert response.status_code == 202, response.text

    def test_waiver_only_for_platform_admin(self, tmp_path: Path) -> None:
        client, store, runtime = _setup(tmp_path, allow_self_publish=True)
        payload = _create_candidate(client, "admin")
        runtime.policy_delivery.validate_without_opa(payload["candidate_id"], actor="admin")
        # 无 waiver_reason → 409
        denied = client.post(
            f"/v1/admin/policy/candidates/{payload['candidate_id']}/publish",
            headers=_auth("admin"),
            json={"base_revision": None},
        )
        assert denied.status_code == 409
        # platform_admin + 理由必填 → 202，且写 separation_waived 审计
        waived = client.post(
            f"/v1/admin/policy/candidates/{payload['candidate_id']}/publish",
            headers=_auth("admin"),
            json={"base_revision": None, "waiver_reason": "紧急修复，单人复核"},
        )
        assert waived.status_code == 202, waived.text
        with store._db._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM policy_change_audit WHERE event_type = 'separation_waived'"
            ).fetchone()
        assert row is not None

    def test_validation_records_actor_and_separation(self, tmp_path: Path) -> None:
        client, store, runtime = _setup(tmp_path)
        payload = _create_candidate(client, "creator-a")
        runtime.policy_delivery.validate_without_opa(payload["candidate_id"], actor="validator-a")
        with store._db._connect() as conn:
            row = conn.execute(
                "SELECT validated_by, separation_ok FROM policy_validations WHERE candidate_id = ?",
                (payload["candidate_id"],),
            ).fetchone()
        assert row["validated_by"] == "validator-a"
        assert row["separation_ok"] == 1


class TestRbacAdminEndpoints:
    def test_binding_crud(self, tmp_path: Path) -> None:
        client, _store, _runtime = _setup(tmp_path)
        # creator 无 rbac.binding.manage → 403
        denied = client.post(
            "/v1/admin/rbac/bindings",
            headers=_auth("creator-a"),
            json={"principal": "eve", "tenant_id": "tenant-a", "role": "policy_auditor"},
        )
        assert denied.status_code == 403
        # admin 创建绑定
        created = client.post(
            "/v1/admin/rbac/bindings",
            headers=_auth("admin"),
            json={"principal": "eve", "tenant_id": "tenant-a", "role": "policy_auditor"},
        )
        assert created.status_code == 201, created.text
        binding_id = created.json()["binding_id"]
        listed = client.get("/v1/admin/rbac/bindings", headers=_auth("admin"))
        assert [b["binding_id"] for b in listed.json()["bindings"]] == [binding_id]
        revoked = client.post(
            f"/v1/admin/rbac/bindings/{binding_id}/revoke", headers=_auth("admin")
        )
        assert revoked.status_code == 200
        assert client.get("/v1/admin/rbac/bindings", headers=_auth("admin")).json()["bindings"] == []

    def test_tenant_admin_scope_enforced(self, tmp_path: Path) -> None:
        client, store, _runtime = _setup(tmp_path)
        # 通过 SQLite 动态绑定授予 creator-a tenant_admin
        store.add_binding("creator-a", "tenant-a", Role.TENANT_ADMIN, "admin")
        foreign = client.post(
            "/v1/admin/rbac/bindings",
            headers=_auth("creator-a"),
            json={"principal": "eve", "tenant_id": "tenant-b", "role": "policy_auditor"},
        )
        assert foreign.status_code == 403
        own = client.post(
            "/v1/admin/rbac/bindings",
            headers=_auth("creator-a"),
            json={"principal": "eve", "tenant_id": "tenant-a", "role": "policy_auditor"},
        )
        assert own.status_code == 201, own.text

    def test_tenant_admin_cannot_escalate_to_platform_admin(self, tmp_path: Path) -> None:
        client, store, runtime = _setup(tmp_path)
        store.add_binding("creator-a", "tenant-a", Role.TENANT_ADMIN, "admin")
        for actor in ("creator-a", "eve"):
            denied = client.post(
                "/v1/admin/rbac/bindings", headers=_auth("creator-a"),
                json={"principal": actor, "tenant_id": "tenant-a", "role": "platform_admin"},
            )
            assert denied.status_code == 403
        assert Role.PLATFORM_ADMIN not in runtime.rbac_enforcer.roles_for(
            runtime.rbac_credential_resolver.resolve("creator-a", TOKENS["creator-a"])
        )
        # 历史租户级平台绑定也不得在判定阶段产生平台权限。
        store.add_binding("creator-a", "tenant-a", Role.PLATFORM_ADMIN, "admin")
        assert client.get("/v1/admin/rbac/grants", headers=_auth("creator-a")).status_code == 200
        assert Role.PLATFORM_ADMIN not in runtime.rbac_enforcer.roles_for(
            runtime.rbac_credential_resolver.resolve("creator-a", TOKENS["creator-a"])
        )
        admin = client.post(
            "/v1/admin/rbac/bindings", headers=_auth("admin"),
            json={"principal": "eve", "role": "platform_admin"},
        )
        assert admin.status_code == 201

    def test_admin_a2a_task_scope_and_delegation_source(self, tmp_path: Path) -> None:
        client, store, runtime = _setup(tmp_path)
        store.add_binding("creator-a", "tenant-a", Role.TENANT_ADMIN, "admin")
        class Bridge:
            canceled = False
            streamed = False
            async def query_task(self, task_id: str) -> dict[str, str]:
                return {"task_id": task_id, "tenant_id": "tenant-b" if task_id == "foreign" else "tenant-a"}
            async def cancel_task(self, task_id: str, reason: str = "") -> dict[str, str]:
                self.canceled = True
                return {"task_id": task_id}
            async def stream_task(self, task_id: str, *, cursor=None, include_sse=False):
                assert cursor is None
                assert include_sse is True
                self.streamed = True
                yield ({"task_id": task_id}, None, None)
        runtime.go_kernel_bridge = Bridge()
        for path in ("foreign", "own"):
            expected = 403 if path == "foreign" else 200
            assert client.get(f"/v1/admin/a2a/tasks/{path}", headers=_auth("creator-a")).status_code == expected
            assert client.get(f"/v1/admin/a2a/tasks/{path}/stream", headers=_auth("creator-a")).status_code == expected
            assert client.post(f"/v1/admin/a2a/tasks/{path}/cancel", headers=_auth("creator-a")).status_code == expected
        assert runtime.go_kernel_bridge.canceled
        runtime.go_kernel_bridge.canceled = False
        runtime.go_kernel_bridge.streamed = False
        assert client.post("/v1/admin/a2a/tasks/foreign/cancel", headers=_auth("creator-a")).status_code == 403
        assert not runtime.go_kernel_bridge.canceled
        assert client.get("/v1/admin/a2a/tasks/foreign/stream", headers=_auth("creator-a")).status_code == 403
        assert not runtime.go_kernel_bridge.streamed
        runtime.config = type("Config", (), {"rbac": _RbacConfig(), "agents": {
            "source": type("Agent", (), {"tenant_id": "tenant-a", "owner_id": "other"})(),
            "target": type("Agent", (), {"tenant_id": "tenant-b", "owner_id": "other"})(),
        }})()
        denied = client.post("/v1/admin/a2a/delegations", headers=_auth("creator-a"), json={
            "source_agent_id": "source", "target_agent_id": "target", "tool_name": "echo",
        })
        assert denied.status_code == 403
        runtime.config.agents["source"].owner_id = "creator-a"
        assert client.post("/v1/admin/a2a/delegations", headers=_auth("creator-a"), json={
            "source_agent_id": "source", "target_agent_id": "target", "tool_name": "echo",
        }).status_code == 403

    def test_rbac_rejects_unbound_api_key_session(self, tmp_path: Path) -> None:
        client, _store, _runtime = _setup(tmp_path)
        assert client.post("/v1/admin/session/login", json={"api_key": "admin-secret"}).status_code == 403
        assert client.get("/v1/admin/rbac/bindings", headers={"x-api-key": "admin-secret"}).status_code == 401

    def test_grants_platform_admin_only(self, tmp_path: Path) -> None:
        client, _store, _runtime = _setup(tmp_path)
        denied = client.post(
            "/v1/admin/rbac/grants",
            headers=_auth("creator-a"),
            json={
                "source_principal": "creator-a",
                "source_tenant": "tenant-a",
                "target_tenant": "tenant-b",
                "resources": ["policy.audit.read"],
            },
        )
        assert denied.status_code == 403
        created = client.post(
            "/v1/admin/rbac/grants",
            headers=_auth("admin"),
            json={
                "source_principal": "creator-a",
                "source_tenant": "tenant-a",
                "target_tenant": "tenant-b",
                "resources": ["policy.audit.read"],
            },
        )
        assert created.status_code == 201, created.text
        grant_id = created.json()["grant_id"]
        revoked = client.post(f"/v1/admin/rbac/grants/{grant_id}/revoke", headers=_auth("admin"))
        assert revoked.status_code == 200
