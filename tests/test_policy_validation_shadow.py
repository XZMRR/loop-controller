from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from loop_controller.infra.policy_delivery import CandidateState, PolicyDelivery
from loop_controller.infra.state_db import StateDatabase
from loop_controller.policy_shadow import PolicyShadowService, ShadowInputError
from loop_controller.policy_validation import (
    DEFAULT_DENY_PACKAGES,
    CandidateValidationService,
    OPACandidateValidator,
    OPACLIRunner,
)
from tests.conftest import resolve_opa_bin


class FakeRunner:
    def __init__(self, *, failure: str | None = None) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.failure = failure

    def run(self, stage: str, arguments: list[str]):
        from loop_controller.policy_validation import OPACommandResult

        self.calls.append((stage, arguments))
        if stage == self.failure:
            return OPACommandResult(stage=stage, ok=False, exit_code=1, failure_code="failed"), b""
        outputs = {
            "version": json.dumps({"Version": "1.0.0"}).encode(),
            "test": b'[{"name":"test_default","pass":true}]',
        }
        return OPACommandResult(stage=stage, ok=True, exit_code=0), outputs.get(stage, b"")


def _snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "snapshot"
    root.mkdir(exist_ok=True)
    (root / "default.rego").write_text("package loop_controller.tool_permission\n", encoding="utf-8")
    return root


def test_validator_uses_fixed_order_and_both_default_deny_packages(tmp_path: Path) -> None:
    runner = FakeRunner()
    evaluated: list[tuple[str, dict[str, Any], Path]] = []

    def evaluator(package: str, input_doc: dict[str, Any], snapshot: Path) -> dict[str, str]:
        evaluated.append((package, input_doc, snapshot))
        return {"verdict": "deny", "reason": "default"}

    result = OPACandidateValidator(runner, evaluator=evaluator).validate(_snapshot(tmp_path))  # type: ignore[arg-type]
    assert result.ok
    assert result.opa_version == "1.0.0"
    assert [stage for stage, _ in runner.calls] == ["version", "check", "test"]
    assert [package for package, _, _ in evaluated] == list(DEFAULT_DENY_PACKAGES)
    assert all(input_doc == {} for _, input_doc, _ in evaluated)
    assert all(path.name == "candidate" for _, _, path in evaluated)


def test_validator_rejects_no_tests_and_invalid_default_decision(tmp_path: Path) -> None:
    runner = FakeRunner()
    validator = OPACandidateValidator(
        runner, evaluator=lambda package, input_doc, snapshot: {"verdict": "allow"}
    )  # type: ignore[arg-type]
    result = validator.validate(_snapshot(tmp_path))
    assert not result.ok
    assert result.failure_code == "default_deny_contract_failed"
    assert result.package_default_deny[DEFAULT_DENY_PACKAGES[0]] is False

    runner = FakeRunner()
    original = runner.run

    def empty_tests(stage: str, arguments: list[str]):
        result, output = original(stage, arguments)
        return (result, b"[]") if stage == "test" else (result, output)

    runner.run = empty_tests  # type: ignore[method-assign]
    result = OPACandidateValidator(runner).validate(_snapshot(tmp_path))  # type: ignore[arg-type]
    assert not result.ok
    assert result.failure_code == "opa_no_tests"


def test_candidate_validation_service_marks_failure_without_artifact(tmp_path: Path) -> None:
    delivery = PolicyDelivery(tmp_path, StateDatabase(tmp_path / "state.db"))
    candidate = delivery.create_candidate(
        {"default.rego": "package loop_controller.tool_permission\n"},
        base_revision=None,
        actor="admin",
    )
    service = CandidateValidationService(
        delivery, OPACandidateValidator(FakeRunner(failure="check"))  # type: ignore[arg-type]
    )
    updated, result = service.validate(candidate.candidate_id, actor="validator-bob")
    assert not result.ok
    assert updated.state == CandidateState.FAILED
    assert not updated.artifact_ready
    assert list(delivery.artifacts_dir.iterdir()) == []


def test_cli_runner_limits_output_and_redacts_secrets(tmp_path: Path) -> None:
    executable = shutil.which("python")
    assert executable
    runner = OPACLIRunner(Path(executable), timeout_seconds=5, max_output_bytes=128)
    result, _ = runner.run(
        "probe", ["-c", "print('token=super-secret ' + 'x' * 1000)"]
    )
    assert not result.ok
    assert result.failure_code == "opa_output_limit"
    assert "super-secret" not in result.output_summary


class SequenceEvaluator:
    def __init__(self, decisions: list[dict[str, Any] | Exception]) -> None:
        self.decisions = decisions
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def evaluate(self, package: str, input_doc: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((package, input_doc))
        value = self.decisions.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _sample(sample_id: str, input_doc: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "sample_id": sample_id,
        "package": "loop_controller.tool_permission",
        "input": input_doc or {"tool_name": "safe"},
        "redaction_attestation": {"profile": "policy-shadow-v1", "attested_by": "admin"},
    }


async def test_shadow_counts_transitions_and_never_returns_plain_inputs() -> None:
    baseline = SequenceEvaluator(
        [
            {"verdict": "allow", "reason": "a"},
            {"verdict": "deny", "reason": "b"},
            {"verdict": "require_approval", "reason": "c"},
            {"verdict": "modify", "reason": "d", "modified_args": {"x": 1}},
        ]
    )
    candidate = SequenceEvaluator(
        [
            {"verdict": "deny", "reason": "a"},
            {"verdict": "allow", "reason": "b"},
            {"verdict": "deny", "reason": "c"},
            {"verdict": "modify", "reason": "d", "modified_args": {"x": 2}},
        ]
    )
    service = PolicyShadowService(baseline, candidate)
    result = await service.run([_sample(str(index), {"private": f"value-{index}"}) for index in range(4)])
    assert result.ok
    assert (result.total, result.same_count, result.changed_count) == (4, 0, 4)
    assert result.allow_to_deny == result.deny_to_allow == 1
    assert result.approval_transitions == 1
    assert result.modify_transitions == 1
    assert len(result.input_set_digest) == len(result.result_sha256) == 64
    serialized = result.model_dump_json()
    assert "value-" not in serialized
    assert {item.sample_id for item in result.items} == {"0", "1", "2", "3"}
    assert len(baseline.calls) == len(candidate.calls) == 4


@pytest.mark.parametrize(
    "input_doc",
    [
        {"Authorization": "Bearer abcdefgh"},
        {"nested": {"password": "visible"}},
        {"text": "api_key=abcdefghijk"},
    ],
)
def test_shadow_rejects_sensitive_plaintext(input_doc: dict[str, Any]) -> None:
    service = PolicyShadowService(SequenceEvaluator([]), SequenceEvaluator([]))
    with pytest.raises(ShadowInputError):
        service.validate_samples([_sample("one", input_doc)])


async def test_shadow_candidate_error_is_fail_closed_and_stops_run() -> None:
    baseline = SequenceEvaluator([{"verdict": "allow"}])
    candidate = SequenceEvaluator([TimeoutError("candidate leaked secret")])
    result = await PolicyShadowService(baseline, candidate).run([_sample("one")])
    assert not result.ok
    assert result.error_count == 1
    assert result.failure_code == "candidate_evaluator_failed"
    assert result.items == []
    assert "secret" not in result.model_dump_json()


def test_shadow_requires_distinct_evaluators() -> None:
    evaluator = SequenceEvaluator([])
    with pytest.raises(ValueError, match="必须隔离"):
        PolicyShadowService(evaluator, evaluator)


@pytest.mark.integration
def test_real_opa_candidate_validation(tmp_path: Path) -> None:
    opa = resolve_opa_bin()
    if not opa.exists():
        pytest.skip("OPA 未安装")
    source = tmp_path / "policies"
    source.mkdir()
    (source / "tool.rego").write_text(
        """package loop_controller.tool_permission

default decision := {"verdict": "deny", "reason": "default"}

test_default_deny if {
    decision.verdict == "deny"
}
""",
        encoding="utf-8",
    )
    (source / "interaction.rego").write_text(
        """package loop_controller.interaction.delegation

default decision := {"verdict": "deny", "reason": "default"}

test_default_deny if {
    decision.verdict == "deny"
}
""",
        encoding="utf-8",
    )
    result = OPACandidateValidator(OPACLIRunner(opa, timeout_seconds=20)).validate(source)
    assert result.ok, result.model_dump()
    assert result.package_default_deny == {package: True for package in DEFAULT_DENY_PACKAGES}
