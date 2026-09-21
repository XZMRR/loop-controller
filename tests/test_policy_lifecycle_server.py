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


def test_standard_opa_failure_payload_without_status_field_is_accepted(tmp_path: Path) -> None:
    """真实 OPA 加载失败时的标准 status：无 status 字段，只有 code/errors。

    回归：v0.55 本地起真实 OPA 实例时 status 上报被 400 拒绝，实例错误态
    无法呈现在策略生命周期页。
    """
    client, _delivery, _lifecycle, _runtime, revision = _setup(tmp_path)
    response = client.post(
        "/v1/opa/status",
        headers={"Authorization": "Bearer status-secret"},
        json={
            "labels": {"id": "opa-a", "version": "1.19.0"},
            "bundles": {
                "lc": {
                    "name": "lc",
                    "active_revision": revision,
                    "code": "bundle_activation_failed",
                    "errors": [{"code": "rego_parse_error", "message": "unexpected token"}],
                    "last_successful_download": "2026-09-21T14:00:00Z",
                    "last_successful_activation": "2026-09-21T13:00:00Z",
                }
            },
            "timestamp": "2026-09-21T14:00:00.123456789Z",
            "sequence": 1,
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["error_instances"] == ["opa-a"]
    assert response.json()["expected_revision"] == revision


def test_standard_opa_payload_prefers_custom_instance_id_over_uuid(tmp_path: Path) -> None:
    """真实 OPA 会用实例 UUID 覆盖 labels.id，自定义 instance_id 标签优先。

    回归：v0.55 本地真实 OPA 实例的 status 上报被 403（未知 OPA instance）。
    """
    client, _delivery, _lifecycle, _runtime, revision = _setup(tmp_path)
    headers = {"Authorization": "Bearer status-secret"}

    def _standard(instance_id: str) -> dict[str, Any]:
        return {
            "labels": {"id": "6456c6d7-b12b-492b-96bb-602ebc56098d", "instance_id": instance_id},
            "bundles": {
                "lc": {
                    "name": "lc",
                    "active_revision": revision,
                    "last_successful_activation": "2026-09-21T14:57:24.7351751Z",
                }
            },
            "timestamp": datetime.now(UTC).isoformat(),
        }

    # OPA 1.x 成功态不带 status 字段：仅 instance_id 命中、active_revision 匹配即 loaded
    one = client.post("/v1/opa/status", headers=headers, json=_standard("opa-a"))
    assert one.status_code == 202, one.text
    assert one.json()["active_revision"] is None
    two = client.post("/v1/opa/status", headers=headers, json=_standard("opa-b"))
    assert two.status_code == 202, two.text
    assert two.json()["active_revision"] == revision


def test_standard_opa_payload_without_top_level_timestamp_is_accepted(tmp_path: Path) -> None:
    """真实 OPA 1.x status 顶层没有 timestamp/sequence 字段。

    抓包确认 OPA 1.19 的 status 顶层只有 labels/bundles/metrics/plugins，
    时间证据在 bundle.last_successful_activation；版本在 labels.version。
    回归：v0.55 本地真实 OPA 实例上报被 400（必须包含 timestamp 或 sequence）。
    """
    client, _delivery, _lifecycle, _runtime, revision = _setup(tmp_path)
    response = client.post(
        "/v1/opa/status",
        headers={"Authorization": "Bearer status-secret"},
        json={
            "labels": {"id": "27c25a07-1e4e-43c2-b904-c45ac5187d73", "instance_id": "opa-a", "version": "1.19.0"},
            "bundles": {
                "lc": {
                    "name": "lc",
                    "active_revision": revision,
                    "last_successful_activation": "2026-09-21T15:12:59.1925153Z",
                    "last_successful_download": "2026-09-21T15:12:59.1901372Z",
                    "type": "snapshot",
                    "size": 2426,
                }
            },
            "metrics": {},
            "plugins": {"bundle": {"state": "OK"}, "status": {"state": "OK"}},
        },
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["loaded_instances"] == 1
    assert body["instances"]["opa-a"]["opa_reported_at"] == "2026-09-21T15:12:59.192515+00:00"
    assert body["instances"]["opa-a"]["opa_version"] == "1.19.0"


def test_periodic_report_with_same_activation_time_is_heartbeat(tmp_path: Path) -> None:
    """OPA 周期上报的 last_successful_activation 不变（bundle 未更新）。

    抓包确认 OPA 每轮重发激活时间相同、仅 last_request/metrics 变化；
    必须按心跳接受并刷新 received_at，否则实例在 TTL 后变 stale、
    后续上报全部被 400（replay）拒绝，页面永远 0/1。
    """
    client, _delivery, _lifecycle, _runtime, revision = _setup(tmp_path)
    headers = {"Authorization": "Bearer status-secret"}

    def _report(last_request: str) -> dict[str, Any]:
        return {
            "labels": {"id": "27c25a07-1e4e-43c2-b904-c45ac5187d73", "instance_id": "opa-a", "version": "1.19.0"},
            "bundles": {
                "lc": {
                    "name": "lc",
                    "active_revision": revision,
                    "last_successful_activation": "2026-09-21T15:12:59.1925153Z",
                    "last_request": last_request,
                }
            },
            "metrics": {},
        }

    first = client.post("/v1/opa/status", headers=headers, json=_report("2026-09-21T15:12:59.2000000Z"))
    assert first.status_code == 202, first.text
    # 第二轮：激活时间相同、版本相同，仅 last_request 前进——必须接受为心跳
    second = client.post("/v1/opa/status", headers=headers, json=_report("2026-09-21T15:12:59.9000000Z"))
    assert second.status_code == 202, second.text
    assert second.json()["loaded_instances"] == 1
    assert second.json()["fresh_instances"] == 1
    # 回退时间仍被拒绝（防重放语义保留）
    stale = client.post(
        "/v1/opa/status",
        headers=headers,
        json={
            "labels": {"id": "27c25a07-1e4e-43c2-b904-c45ac5187d73", "instance_id": "opa-a"},
            "bundles": {
                "lc": {
                    "active_revision": revision,
                    "last_successful_activation": "2026-09-21T15:11:00.0000000Z",
                }
            },
        },
    )
    assert stale.status_code == 400
