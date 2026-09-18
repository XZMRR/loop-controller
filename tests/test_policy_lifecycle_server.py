from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from loop_controller.infra.policy_delivery import PolicyDelivery
from loop_controller.infra.state_db import StateDatabase
from loop_controller.policy_lifecycle import PolicyLifecycleConfig, PolicyLifecycleService
from loop_controller.server import build_app


class Audit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append_async(self, event: Any) -> None:
        self.events.append(event)


class Controller:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def aclose(self) -> None:
        pass


class Runtime:
    def __init__(self, delivery: PolicyDelivery, lifecycle: PolicyLifecycleService) -> None:
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



def _setup(tmp_path: Path) -> tuple[TestClient, PolicyDelivery, PolicyLifecycleService, Runtime, str]:
    db = StateDatabase(tmp_path / "state.db")
    delivery = PolicyDelivery(tmp_path, db)
    candidate = delivery.create_candidate(
        {"default.rego": "package loop_controller.tool_permission"},
        base_revision=None,
        actor="trusted",
    )
    validated = delivery.validate_without_opa(candidate.candidate_id)
    delivery.store.publish(candidate.candidate_id, None, "trusted")
    lifecycle = PolicyLifecycleService(
        db, PolicyLifecycleConfig(bundle_name="lc", required_instance_ids=("opa-a", "opa-b"), status_ttl_seconds=60)
    )
    runtime = Runtime(delivery, lifecycle)
    app = build_app(Controller(runtime), api_key="admin-secret", configure_logs=False)
    return TestClient(app), delivery, lifecycle, runtime, validated.revision or ""


def test_policy_api_auth_bundle_etag_and_admin_actor(tmp_path: Path) -> None:
    client, _delivery, _lifecycle, runtime, revision = _setup(tmp_path)
    assert client.get("/v1/admin/policy/status").status_code == 401
    assert client.get("/v1/opa/bundles/current", headers={"Authorization": "Bearer admin-secret"}).status_code == 401
    response = client.get("/v1/opa/bundles/current", headers={"Authorization": "Bearer bundle-secret"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/gzip"
    assert response.headers["x-opa-bundle-revision"] == revision
    etag = response.headers["etag"]
    cached = client.get(
        "/v1/opa/bundles/current",
        headers={"Authorization": "Bearer bundle-secret", "If-None-Match": etag},
    )
    assert cached.status_code == 304 and cached.content == b""
    head = client.head("/v1/opa/bundles/current", headers={"Authorization": "Bearer bundle-secret"})
    assert head.status_code == 200 and head.content == b"" and head.headers["etag"] == etag

    created = client.post(
        "/v1/admin/policy/candidates",
        headers={"X-API-Key": "admin-secret"},
        json={"actor": "forged", "base_revision": revision, "files": {"x.rego": "package x"}},
    )
    assert created.status_code == 201
    assert created.json()["created_by"].startswith("api-key:")
    assert runtime.audit_store.events[-1].actor_id.startswith("api-key:")
    assert "package x" not in runtime.audit_store.events[-1].model_dump_json()


def test_status_requires_all_instances_rejects_replay_and_exposes_error(tmp_path: Path) -> None:
    client, delivery, lifecycle, _runtime, revision = _setup(tmp_path)
    headers = {"Authorization": "Bearer status-secret"}
    now = datetime.now(UTC)
    one = client.post(
        "/v1/opa/status", headers=headers,
        json={"schema_version": "loop-controller-status-v1", "instance_id": "opa-a", "bundle_name": "lc", "revision": revision, "state": "loaded", "timestamp": now.isoformat(), "opa_version": "1.0"},
    )
    assert one.status_code == 202
    assert one.json()["active_revision"] is None
    two = client.post(
        "/v1/opa/status", headers=headers,
        json={"schema_version": "loop-controller-status-v1", "instance_id": "opa-b", "bundle_name": "lc", "revision": revision, "state": "loaded", "timestamp": now.isoformat()},
    )
    assert two.status_code == 202
    assert two.json()["active_revision"] == revision
    assert delivery.store.get_candidate(delivery.store.current()["candidate_id"]).state.value == "loaded"

    replay = client.post(
        "/v1/opa/status", headers=headers,
        json={"instance_id": "opa-a", "bundle_name": "lc", "revision": revision, "state": "error", "timestamp": (now - timedelta(seconds=1)).isoformat(), "error": "token=do-not-store"},
    )
    assert replay.status_code == 400
    with lifecycle._db._connect() as conn:
        assert "do-not-store" not in str(conn.execute("SELECT * FROM opa_instance_status").fetchall())


def test_status_error_marks_expected_failed_without_changing_pointer(tmp_path: Path) -> None:
    client, delivery, _lifecycle, _runtime, revision = _setup(tmp_path)
    response = client.post(
        "/v1/opa/status",
        headers={"Authorization": "Bearer status-secret"},
        json={"schema_version": "loop-controller-status-v1", "instance_id": "opa-a", "bundle_name": "lc", "revision": revision, "state": "error", "error_code": "compile_error", "sequence": 1},
    )
    assert response.status_code == 202
    assert response.json()["expected_revision"] == revision
    assert response.json()["active_revision"] is None
    assert response.json()["error_instances"] == ["opa-a"]
    assert delivery.store.current()["revision"] == revision
