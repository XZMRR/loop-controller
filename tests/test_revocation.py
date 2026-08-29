from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from loop_controller.audit.evidence import EvidenceChain, HMACEvidenceSigner, SignedEvidence
from loop_controller.audit.evidence_backends import LocalFileEvidenceBackend
from loop_controller.controller import build_controller
from loop_controller.identity import MTLSIdentityProvider
from loop_controller.identity.models import AgentIdentity
from loop_controller.identity.revocation import (
    KillSwitchConfig,
    RevocationEntry,
    RevocationList,
    RevocationType,
)
from loop_controller.infra.approval_store import JsonlApprovalStore
from loop_controller.infra.audit_store import JsonlAuditStore
from loop_controller.infra.config_loader import ConfigLoader
from loop_controller.infra.durable_io import DurableIOError
from loop_controller.infra.hot_reload import HotReloader
from loop_controller.models import Agent, ApprovalRecord
from loop_controller.server import build_app

REPO_ROOT = Path(__file__).resolve().parent.parent


def _identity() -> AgentIdentity:
    return AgentIdentity(
        agent_id="agent-1", user_id="user-1", profile_id="profile-1", tenant_id="tenant-1"
    )


@pytest.mark.parametrize(
    ("entry_type", "entry_id", "tool_name", "secret_refs"),
    [
        (RevocationType.AGENT, "agent-1", "search", []),
        (RevocationType.USER, "user-1", "search", []),
        (RevocationType.TOOL, "search", "search", []),
        (RevocationType.SECRET, "api-key", "search", ["api-key"]),
    ],
)
def test_agent_user_tool_and_secret_revocations(
    entry_type: RevocationType, entry_id: str, tool_name: str, secret_refs: list[str]
) -> None:
    revocations = RevocationList(
        [RevocationEntry(type=entry_type, id=entry_id, reason="compromised")]
    )

    assert revocations.is_revoked(_identity(), tool_name, secret_refs) == (True, "compromised")


def test_revocation_datetimes_require_timezone_and_normalize_to_utc() -> None:
    with pytest.raises(ValueError, match="datetime must include timezone"):
        RevocationEntry(type="agent", id="agent-1", revoked_at="2026-08-28T12:00:00")
    with pytest.raises(ValueError, match="datetime must include timezone"):
        RevocationEntry(type="agent", id="agent-1", expires_at="2026-08-28T12:00:00")

    zulu = RevocationEntry(type="agent", id="agent-1", expires_at="2026-08-28T12:00:00Z")
    offset = RevocationEntry(
        type="agent", id="agent-1", expires_at="2026-08-28T15:00:00+03:00"
    )
    assert zulu.expires_at == datetime(2026, 8, 28, 12, tzinfo=UTC)
    assert offset.expires_at == datetime(2026, 8, 28, 12, tzinfo=UTC)


def test_structured_revocation_match() -> None:
    revocations = RevocationList(
        [RevocationEntry(type="secret", id="api-key", reason="compromised")]
    )
    match = revocations.match(_identity(), "search", ["api-key"])
    assert match.revoked
    assert match.type == RevocationType.SECRET
    assert match.id == "api-key"

    killed = RevocationList(
        kill_switch=KillSwitchConfig(enabled=True, reason="emergency")
    ).match(_identity(), "search")
    assert killed.type == "kill_switch"
    assert killed.id == "global"


def test_expired_and_other_tenant_revocations_do_not_match() -> None:
    revocations = RevocationList(
        [
            RevocationEntry(
                type="agent",
                id="agent-1",
                reason="expired",
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            ),
            RevocationEntry(
                type="agent", id="agent-1", reason="other tenant", tenant_id="tenant-2"
            ),
        ]
    )

    assert revocations.is_revoked(_identity(), "search") == (False, None)


def test_kill_switch_blocks_all_calls_except_configured_tools_and_agents() -> None:
    revocations = RevocationList(
        kill_switch=KillSwitchConfig(
            enabled=True,
            reason="emergency",
            except_tools=["health_check"],
            except_agents=["admin-agent"],
        )
    )

    assert revocations.is_revoked(_identity(), "search") == (True, "emergency")
    assert revocations.is_revoked(_identity(), "health_check") == (False, None)
    admin = _identity().model_copy(update={"agent_id": "admin-agent"})
    assert revocations.is_revoked(admin, "search") == (False, None)


class _AdminController:
    def __init__(self, revocations: RevocationList, audit_store=None) -> None:
        self._runtime = SimpleNamespace(
            revocation_list=revocations, audit_store=audit_store
        )

    async def start(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


def test_http_admin_crud_and_kill_switch(tmp_path: Path) -> None:
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    revocations = RevocationList(path=tmp_path / "revocation.yaml")
    evidence_path = tmp_path / "http-evidence"
    audit_store = JsonlAuditStore(
        tmp_path / "http-audit.jsonl",
        evidence_chain=EvidenceChain(
            LocalFileEvidenceBackend(evidence_path),
            HMACEvidenceSigner(b"test-key", key_id="hmac-1"),
        ),
    )
    app = build_app(
        _AdminController(revocations, audit_store),
        api_key="admin-key",
        configure_logs=False,
    )  # type: ignore[arg-type]
    with TestClient(app) as client:
        headers = {"x-api-key": "admin-key"}
        assert client.post("/admin/revoke", json={}, headers={"x-api-key": "wrong"}).status_code == 401
        added = client.post(
            "/admin/revoke",
            json={"type": "tool", "id": "search", "reason": "vulnerable"},
            headers=headers,
        )
        assert added.status_code == 200
        assert client.post(
            "/admin/revoke",
            json={
                "type": "tool",
                "id": "naive-time",
                "reason": "invalid",
                "expires_at": "2026-08-28T12:00:00",
            },
            headers=headers,
        ).status_code == 422
        listing = client.get("/admin/revocation-list", headers=headers).json()
        assert listing["revocations"][0]["id"] == "search"

        enabled = client.post(
            "/admin/kill-switch",
            json={"enabled": True, "reason": "emergency", "except_tools": ["health_check"]},
            headers=headers,
        )
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True

        removed = client.delete(
            "/admin/revoke?type=tool&id=search", headers=headers
        )
        assert removed.json() == {"removed": True}
        assert client.get("/admin/revocation-list", headers=headers).json()["revocations"] == []

    evidence = [
        SignedEvidence.model_validate_json(line)
        for line in (evidence_path / "default.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [record.event.reason for record in evidence] == [
        "revocation_added",
        "kill_switch_updated",
        "revocation_removed",
    ]


def test_http_admin_revocation_endpoints_require_api_key_when_unconfigured(tmp_path: Path) -> None:
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    revocations = RevocationList(path=tmp_path / "revocation.yaml")
    app = build_app(_AdminController(revocations), configure_logs=False)  # type: ignore[arg-type]
    with TestClient(app) as client:
        assert client.post("/admin/revoke", json={"type": "tool", "id": "search"}).status_code == 401
        assert client.get("/admin/revocation-list").status_code == 401
        assert client.post("/admin/kill-switch", json={"enabled": True}).status_code == 401


@pytest.mark.asyncio
async def test_grpc_admin_methods(tmp_path: Path) -> None:
    grpc = pytest.importorskip("grpc")
    from loop_controller.grpc_server import ToolGovernanceServicer
    from loop_controller.v1 import governance_pb2

    class Context:
        def __init__(self) -> None:
            self._code = None
            self.details = ""

        def auth_context(self):
            return {"x509_common_name": [b"CN=admin-agent"]}

        def code(self):
            return self._code

        def set_code(self, code) -> None:
            self._code = code

        def set_details(self, details: str) -> None:
            self.details = details

    revocations = RevocationList()
    evidence_backend = LocalFileEvidenceBackend(tmp_path / "grpc-evidence")
    audit_store = JsonlAuditStore(
        tmp_path / "grpc-audit.jsonl",
        evidence_chain=EvidenceChain(
            evidence_backend,
            HMACEvidenceSigner(b"test-key", key_id="hmac-1"),
        ),
    )
    controller = _AdminController(revocations, audit_store)
    admin = Agent(
        agent_id="admin-agent",
        name="Admin",
        profile_id="admin-profile",
        owner_id="admin-user",
    )
    identity_provider = MTLSIdentityProvider(
        agents={admin.agent_id: admin},
        users={admin.agent_id: admin.owner_id},
        cert_mappings=[{"cn": "admin-agent", "agent_id": admin.agent_id}],
    )
    unauthorized = ToolGovernanceServicer(  # type: ignore[arg-type]
        controller, identity_provider=identity_provider
    )
    denied_context = Context()
    denied = await unauthorized.GetRevocationList(
        governance_pb2.GetRevocationListRequest(), denied_context
    )
    assert not denied.revocations
    assert denied_context.code() is grpc.StatusCode.PERMISSION_DENIED

    servicer = ToolGovernanceServicer(  # type: ignore[arg-type]
        controller,
        identity_provider=identity_provider,
        entrypoints_config={"grpc": {"admin_agent_ids": ["admin-agent"]}},
    )
    context = Context()
    response = await servicer.Revoke(
        governance_pb2.RevokeRequest(type="agent", id="agent-1", reason="compromised"),
        context,
    )
    assert response.success and context.code() is not grpc.StatusCode.INVALID_ARGUMENT

    kill_switch = await servicer.SetKillSwitch(
        governance_pb2.SetKillSwitchRequest(enabled=True, reason="emergency"), context
    )
    assert kill_switch.enabled
    evidence = [record async for record in evidence_backend.iter_evidence(None)]
    evidence_reasons = [record.event.reason for record in evidence]
    assert "revocation_added" in evidence_reasons
    assert "kill_switch_updated" in evidence_reasons
    listing = await servicer.GetRevocationList(
        governance_pb2.GetRevocationListRequest(), context
    )
    assert listing.revocations[0].id == "agent-1"

    removed = await servicer.Revoke(
        governance_pb2.RevokeRequest(type="agent", id="agent-1", remove=True), context
    )
    assert removed.success and removed.removed

    invalid_time_context = Context()
    invalid_time = await servicer.Revoke(
        governance_pb2.RevokeRequest(
            type="agent", id="agent-1", expires_at="2026-08-28T12:00:00"
        ),
        invalid_time_context,
    )
    assert not invalid_time.success
    assert invalid_time_context.code() is grpc.StatusCode.INVALID_ARGUMENT

    revocations.add(RevocationEntry(type="agent", id="admin-agent", reason="compromised"))
    revoked_context = Context()
    denied = await servicer.GetRevocationList(
        governance_pb2.GetRevocationListRequest(), revoked_context
    )
    assert not denied.revocations
    assert revoked_context.code() is grpc.StatusCode.PERMISSION_DENIED
    assert revoked_context.details == "admin identity is revoked"


@pytest.mark.asyncio
async def test_persistence_and_hot_reload(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "revocation.yaml"
    revocations = RevocationList(path=path)
    revocations.add(RevocationEntry(type="tool", id="search", reason="first"))
    assert RevocationList.from_file(path).is_revoked(_identity(), "search")[0]

    path.write_text(
        yaml.safe_dump(
            {
                "kill_switch": {"enabled": True, "reason": "reloaded"},
                "revocations": [],
            }
        ),
        encoding="utf-8",
    )
    reloader = HotReloader(
        config_dir=config_dir,
        config_loader=ConfigLoader(),
        http_executor=Mock(),
        secret_broker=SimpleNamespace(reload=AsyncMock()),
        revocation_list=revocations,
    )
    reloader._loader.reload_http_tools = Mock(return_value={})
    await reloader._reload()

    assert revocations.entries == []
    assert revocations.kill_switch.enabled
    assert revocations.kill_switch.reason == "reloaded"


def test_match_refreshes_latest_yaml_before_decision(tmp_path: Path) -> None:
    path = tmp_path / "revocation.yaml"
    path.write_text("revocations: []\n", encoding="utf-8")
    revocations = RevocationList.from_file(path)

    path.write_text(
        yaml.safe_dump(
            {
                "revocations": [
                    {
                        "type": "tool",
                        "id": "search",
                        "reason": "revoked by another process",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    match = revocations.match(_identity(), "search")
    assert match.revoked
    assert match.reason == "revoked by another process"


@pytest.mark.parametrize("operation", ["remove", "disable_kill_switch"])
def test_persistence_failure_keeps_memory_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    entry = RevocationEntry(type="tool", id="search", reason="protected")
    revocations = RevocationList(
        entries=[entry],
        kill_switch=KillSwitchConfig(enabled=True, reason="emergency"),
        path=tmp_path / "revocation.yaml",
    )

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr("loop_controller.infra.durable_io.os.replace", fail_replace)

    with pytest.raises(DurableIOError, match="durable atomic replace failed"):
        if operation == "remove":
            revocations.remove("tool", "search")
        else:
            revocations.set_kill_switch(KillSwitchConfig(enabled=False))

    assert revocations.entries == [entry]
    assert revocations.kill_switch.enabled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    [
        None,
        "",
        "not: [valid",
        "revocations:\n  - type: tool\n    id: search\n"
        "    revoked_at: '2026-08-28T12:00:00'\n",
    ],
)
async def test_hot_reload_error_or_deletion_keeps_memory_protection(
    tmp_path: Path, replacement: str | None
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "revocation.yaml"
    path.write_text("revocations: []\n", encoding="utf-8")
    entry = RevocationEntry(type="tool", id="search", reason="protected")
    revocations = RevocationList(entries=[entry], path=path)
    reloader = HotReloader(
        config_dir=config_dir,
        config_loader=ConfigLoader(),
        http_executor=Mock(),
        secret_broker=SimpleNamespace(reload=AsyncMock()),
        revocation_list=revocations,
    )
    reloader._loader.reload_http_tools = Mock(return_value={})

    if replacement is None:
        path.unlink()
    else:
        path.write_text(replacement, encoding="utf-8")
    await reloader._reload()

    assert revocations.entries == [entry]
    revoked, reason = revocations.is_revoked(_identity(), "search")
    assert revoked
    assert reason == "protected" or reason.startswith("revocation config unavailable:")


@pytest.fixture
def approval_workdir(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    shutil.copytree(REPO_ROOT / "config", root / "config")
    shutil.copytree(REPO_ROOT / "policies", root / "policies")
    (root / "data").mkdir()
    (root / "config" / "profiles.yaml").write_text(
        """
profiles:
  - profile_id: research_assistant_v1
    session_block_threshold: 10
    session_risk_threshold: 0.95
    tools:
      send_email:
        allowed: true
        require_approval: true
        allowed_args:
          to: ["*@company.com"]
""",
        encoding="utf-8",
    )
    (root / "config" / "mcp_servers.yaml").write_text(
        """
servers:
  email_mock:
    command: ["python", "-m", "loop_controller.mocks.email_server"]
    transport: stdio
tool_mapping:
  send_email: {server: email_mock, mcp_name: send_email, cost_per_call: 1}
""",
        encoding="utf-8",
    )
    return root


@pytest.mark.asyncio
async def test_revocation_during_approval_wait_blocks_execution(
    approval_workdir: Path, opa_server: str
) -> None:
    (approval_workdir / "config" / "evidence.yaml").unlink(missing_ok=True)
    config = ConfigLoader().load(approval_workdir / "config", opa_base_url=opa_server)
    controller = await build_controller(
        config, opa_url=opa_server, env_extra={"PYTHONPATH": str(REPO_ROOT / "src")}
    )
    controller._runtime.approval_manager._store = JsonlApprovalStore(
        str(approval_workdir / "data" / "approvals.jsonl")
    )
    await controller.start()
    try:
        pending = await controller.evaluate_and_execute(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="send_email",
            arguments={"to": "user@company.com"},
        )
        assert pending.status == "require_approval"
        assert pending.request_id is not None
        store = controller._runtime.approval_manager._store
        request = store.get_request_by_id(pending.request_id)
        store.record_response(
            ApprovalRecord(
                request_id=request.request_id,
                decision_id=request.decision_id,
                verdict="approve",
                approver_id=request.approver_id,
                comment="approved before revocation",
            )
        )
        controller._runtime.revocation_list.add(
            RevocationEntry(type="agent", id="researcher_001", reason="revoked while waiting")
        )

        result = await controller.resume_after_approval(pending.request_id)

        assert result.status == "blocked"
        assert result.error_code == "revoked"
        assert result.content == "revoked while waiting"
        reservation = controller._runtime.reservation_store.get_by_call_id(pending.call_id)
        assert reservation is not None and reservation.state == "refunded"
        blocked_events = [
            event
            for event in controller._runtime.audit_store.query_by_trace(request.task_id)
            if event.action == "revocation_blocked"
        ]
        assert len(blocked_events) == 1
        assert blocked_events[0].decision == "blocked"
        assert blocked_events[0].metadata == {
            "revocation_type": "agent",
            "revocation_id": "researcher_001",
            "stage": "approval_resume",
        }
    finally:
        await controller.aclose()
