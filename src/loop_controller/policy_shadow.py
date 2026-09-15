"""显式脱敏样本上的隔离 OPA shadow 比较。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from loop_controller.infra.policy_delivery import ArtifactConflictError, PolicyDelivery
from loop_controller.policy_validation import DEFAULT_DENY_PACKAGES, OPACLIRunner
from loop_controller.utils.canonical import canonical_json

_ALLOWED_VERDICTS = {"allow", "deny", "modify", "require_approval"}
_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "access_token",
    "api_key",
    "apikey",
}
_HIGH_RISK_VALUE = re.compile(
    r"(?i)(bearer\s+[a-z0-9._~+/=-]{8,}|-----BEGIN [A-Z ]+PRIVATE KEY-----|"
    r"(?:api[-_]?key|password|secret|token)\s*[:=]\s*\S+)"
)


class ShadowInputError(ValueError):
    pass


class ShadowEvaluator(Protocol):
    async def evaluate(self, package: str, input_doc: dict[str, Any]) -> dict[str, Any]: ...


class ShadowSample(BaseModel):
    model_config = ConfigDict(frozen=True)

    sample_id: str = Field(min_length=1, max_length=128)
    package: str
    input: dict[str, Any]
    expected: Any = None
    redaction_attestation: dict[str, str]


class ShadowItemResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    sample_id: str
    same: bool
    baseline_verdict: str
    candidate_verdict: str
    baseline_result_sha256: str
    candidate_result_sha256: str


class ShadowRunResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    total: int
    same_count: int
    changed_count: int
    allow_to_deny: int
    deny_to_allow: int
    approval_transitions: int
    modify_transitions: int
    error_count: int
    input_set_digest: str
    result_sha256: str
    failure_code: str | None = None
    items: list[ShadowItemResult] = Field(default_factory=list)


def _decision_summary(decision: Mapping[str, Any]) -> dict[str, Any]:
    verdict = decision.get("verdict")
    if verdict not in _ALLOWED_VERDICTS:
        raise ValueError("invalid decision")
    return {
        "verdict": verdict,
        "reason": decision.get("reason") if isinstance(decision.get("reason"), str) else "",
        "policy_hits": decision.get("policy_hits")
        if isinstance(decision.get("policy_hits"), list)
        else [],
        "modified_args_sha256": hashlib.sha256(
            canonical_json(decision.get("modified_args")).encode()
        ).hexdigest()
        if "modified_args" in decision
        else None,
    }


def _inspect_redacted(value: Any, *, depth: int, max_depth: int, max_string: int) -> None:
    if depth > max_depth:
        raise ShadowInputError("shadow input 嵌套深度超限")
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS:
                raise ShadowInputError("shadow input 包含敏感字段")
            _inspect_redacted(item, depth=depth + 1, max_depth=max_depth, max_string=max_string)
    elif isinstance(value, list):
        for item in value:
            _inspect_redacted(item, depth=depth + 1, max_depth=max_depth, max_string=max_string)
    elif isinstance(value, str):
        if len(value) > max_string:
            raise ShadowInputError("shadow input 字符串超限")
        if _HIGH_RISK_VALUE.search(value):
            raise ShadowInputError("shadow input 包含高风险明文模式")


class HTTPShadowEvaluator:
    def __init__(self, base_url: str, *, timeout_seconds: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def evaluate(self, package: str, input_doc: dict[str, Any]) -> dict[str, Any]:
        import httpx

        url = f"{self._base_url}/v1/data/{package.replace('.', '/')}"
        async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as client:
            response = await client.post(url, json={"input": input_doc})
            response.raise_for_status()
            body = response.json().get("result", {})
        if isinstance(body, dict) and "decision" in body:
            body = body["decision"]
        if not isinstance(body, dict):
            raise ValueError("invalid evaluator response")
        return body


def delivery_artifact(delivery: PolicyDelivery, revision: str) -> tuple[bytes, str, int, str]:
    with delivery.store._db._connect() as conn:
        row = conn.execute("""SELECT artifact_path,artifact_sha256,artifact_size FROM policy_candidates
            WHERE revision=? AND artifact_ready=1 ORDER BY loaded_at DESC LIMIT 1""", (revision,)).fetchone()
    if row is None:
        raise ShadowInputError("baseline revision 不存在")
    path = Path(row["artifact_path"])
    if path.is_symlink() or not path.is_file():
        raise ArtifactConflictError("shadow artifact 类型非法")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != row["artifact_sha256"] or len(data) != row["artifact_size"]:
        raise ArtifactConflictError("shadow artifact 完整性失败")
    return data, digest, len(data), revision


class BundleShadowEvaluator:
    """使用指定不可变 bundle 的本地 OPA 进程求值，不接触线上 OPA。"""

    def __init__(self, runner: OPACLIRunner, artifact: bytes) -> None:
        self._runner = runner
        self._artifact = artifact

    async def evaluate(self, package: str, input_doc: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._evaluate, package, input_doc)

    def _evaluate(self, package: str, input_doc: dict[str, Any]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="lc-shadow-") as root:
            bundle = Path(root) / "bundle.tar.gz"
            input_path = Path(root) / "input.json"
            bundle.write_bytes(self._artifact)
            input_path.write_text(canonical_json(input_doc), encoding="utf-8")
            stage, output = self._runner.run("shadow_eval", ["eval", "--format=json", "--bundle",
                str(bundle), "--input", str(input_path), f"data.{package}.decision"])
            if not stage.ok:
                raise ValueError("OPA shadow eval failed")
            body = json.loads(output)
            value = body["result"][0]["expressions"][0]["value"]
            if not isinstance(value, dict):
                raise ValueError("invalid OPA shadow decision")
            return value


class PolicyShadowService:
    """分别调用 baseline 与隔离 candidate evaluator，不访问任何审计存储。"""

    def __init__(
        self,
        baseline_evaluator: ShadowEvaluator,
        candidate_evaluator: ShadowEvaluator,
        *,
        max_samples: int = 1000,
        max_input_bytes: int = 64 * 1024,
        max_depth: int = 16,
        max_string: int = 8192,
    ) -> None:
        if baseline_evaluator is candidate_evaluator:
            raise ValueError("baseline 与 candidate evaluator 必须隔离")
        if min(max_samples, max_input_bytes, max_depth, max_string) <= 0:
            raise ValueError("shadow 限制必须为正数")
        self._baseline = baseline_evaluator
        self._candidate = candidate_evaluator
        self._max_samples = max_samples
        self._max_input_bytes = max_input_bytes
        self._max_depth = max_depth
        self._max_string = max_string

    def validate_samples(self, raw_samples: Sequence[ShadowSample | Mapping[str, Any]]) -> list[ShadowSample]:
        if not raw_samples or len(raw_samples) > self._max_samples:
            raise ShadowInputError("shadow 样本数非法或超限")
        samples = [
            item if isinstance(item, ShadowSample) else ShadowSample.model_validate(item)
            for item in raw_samples
        ]
        seen: set[str] = set()
        for sample in samples:
            if sample.sample_id in seen:
                raise ShadowInputError("sample_id 重复")
            seen.add(sample.sample_id)
            if sample.package not in DEFAULT_DENY_PACKAGES:
                raise ShadowInputError("shadow package 不在 allowlist")
            attestation = sample.redaction_attestation
            if attestation.get("profile") != "policy-shadow-v1" or not attestation.get("attested_by"):
                raise ShadowInputError("缺少有效脱敏声明")
            encoded = canonical_json(sample.input).encode()
            if len(encoded) > self._max_input_bytes:
                raise ShadowInputError("shadow input 大小超限")
            _inspect_redacted(
                sample.input, depth=0, max_depth=self._max_depth, max_string=self._max_string
            )
        return samples

    @classmethod
    def for_candidate(
        cls, delivery: PolicyDelivery, runner: OPACLIRunner, candidate_id: str, revision: str, artifact_sha256: str,
        **limits: Any,
    ) -> PolicyShadowService:
        candidate = delivery.store.get_candidate(candidate_id)
        if candidate is None or candidate.revision != revision or candidate.artifact_sha256 != artifact_sha256:
            raise ShadowInputError("candidate/revision/artifact 不匹配")
        if candidate.base_revision is None:
            raise ShadowInputError("candidate 缺少 baseline revision")
        candidate_data, digest, _, _ = delivery_artifact(delivery, revision)
        if digest != artifact_sha256:
            raise ArtifactConflictError("candidate artifact hash 不匹配")
        baseline_data, _, _, _ = delivery_artifact(delivery, candidate.base_revision)
        return cls(BundleShadowEvaluator(runner, baseline_data), BundleShadowEvaluator(runner, candidate_data), **limits)

    async def run(
        self, raw_samples: Sequence[ShadowSample | Mapping[str, Any]]
    ) -> ShadowRunResult:
        samples = self.validate_samples(raw_samples)
        input_set_digest = hashlib.sha256(
            canonical_json(
                [
                    {"sample_id": item.sample_id, "package": item.package, "input": item.input}
                    for item in samples
                ]
            ).encode()
        ).hexdigest()
        items: list[ShadowItemResult] = []
        counters = {
            "same_count": 0,
            "changed_count": 0,
            "allow_to_deny": 0,
            "deny_to_allow": 0,
            "approval_transitions": 0,
            "modify_transitions": 0,
        }
        for sample in samples:
            try:
                baseline = _decision_summary(
                    await self._baseline.evaluate(sample.package, dict(sample.input))
                )
                candidate = _decision_summary(
                    await self._candidate.evaluate(sample.package, dict(sample.input))
                )
            except Exception:
                return self._failed(len(samples), input_set_digest, len(items))
            same = baseline == candidate
            counters["same_count" if same else "changed_count"] += 1
            old = baseline["verdict"]
            new = candidate["verdict"]
            if old == "allow" and new == "deny":
                counters["allow_to_deny"] += 1
            if old == "deny" and new == "allow":
                counters["deny_to_allow"] += 1
            if (old == "require_approval") != (new == "require_approval"):
                counters["approval_transitions"] += 1
            if (old == "modify") != (new == "modify") or (
                old == new == "modify"
                and baseline["modified_args_sha256"] != candidate["modified_args_sha256"]
            ):
                counters["modify_transitions"] += 1
            items.append(
                ShadowItemResult(
                    sample_id=sample.sample_id,
                    same=same,
                    baseline_verdict=old,
                    candidate_verdict=new,
                    baseline_result_sha256=hashlib.sha256(canonical_json(baseline).encode()).hexdigest(),
                    candidate_result_sha256=hashlib.sha256(canonical_json(candidate).encode()).hexdigest(),
                )
            )
        payload: dict[str, Any] = {
            "ok": True,
            "total": len(samples),
            **counters,
            "error_count": 0,
            "input_set_digest": input_set_digest,
            "failure_code": None,
            "items": [item.model_dump(mode="json") for item in items],
        }
        return ShadowRunResult.model_validate(
            {
                **payload,
                "result_sha256": hashlib.sha256(canonical_json(payload).encode()).hexdigest(),
            }
        )

    @staticmethod
    def _failed(total: int, input_set_digest: str, completed: int) -> ShadowRunResult:
        payload = {
            "ok": False,
            "total": total,
            "same_count": 0,
            "changed_count": 0,
            "allow_to_deny": 0,
            "deny_to_allow": 0,
            "approval_transitions": 0,
            "modify_transitions": 0,
            "error_count": 1,
            "input_set_digest": input_set_digest,
            "failure_code": "candidate_evaluator_failed",
            "items": [],
        }
        return ShadowRunResult.model_validate(
            {
                **payload,
                "result_sha256": hashlib.sha256(canonical_json(payload).encode()).hexdigest(),
            }
        )
