"""OPA 候选策略的隔离校验服务。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from loop_controller.infra.policy_delivery import (
    Candidate,
    CandidateState,
    InvalidCandidateError,
    PolicyDelivery,
)
from loop_controller.utils.canonical import canonical_json

TOOL_PERMISSION_PACKAGE = "loop_controller.tool_permission"
INTERACTION_DELEGATION_PACKAGE = "loop_controller.interaction.delegation"
DEFAULT_DENY_PACKAGES = (TOOL_PERMISSION_PACKAGE, INTERACTION_DELEGATION_PACKAGE)
_SECRET_PATTERN = re.compile(
    r"(?i)(authorization|bearer|token|secret|password|api[-_]?key)\s*[:=]\s*[^\s,;]+"
)


class OPACommandResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    stage: str
    ok: bool
    exit_code: int | None = None
    elapsed_ms: int = 0
    output_summary: str = ""
    failure_code: str | None = None


class CandidateValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    opa_version: str | None = None
    stages: list[OPACommandResult] = Field(default_factory=list)
    package_default_deny: dict[str, bool] = Field(default_factory=dict)
    failure_code: str | None = None
    result_sha256: str


class CandidateEvaluator(Protocol):
    def __call__(self, package: str, input_doc: dict[str, Any], snapshot: Path) -> Any: ...


def _sanitize(value: bytes | bytearray | str, limit: int) -> str:
    text = bytes(value).decode("utf-8", errors="replace") if not isinstance(value, str) else value
    text = _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = " ".join(text.split())
    return text[:limit]


class OPACLIRunner:
    """只执行锁定 OPA binary 的受限命令 runner。"""

    def __init__(
        self,
        binary: Path | str,
        *,
        timeout_seconds: float = 10.0,
        max_output_bytes: int = 64 * 1024,
        environment: dict[str, str] | None = None,
    ) -> None:
        binary_path = Path(binary)
        if not binary_path.is_absolute():
            raise ValueError("OPA binary 必须是绝对路径")
        self.binary = binary_path.resolve(strict=True)
        if not self.binary.is_file():
            raise ValueError("OPA binary 不是普通文件")
        if timeout_seconds <= 0 or max_output_bytes <= 0:
            raise ValueError("timeout 和 max_output_bytes 必须为正数")
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.environment = dict(environment or {})

    def run(self, stage: str, arguments: Sequence[str]) -> tuple[OPACommandResult, bytes]:
        started = time.monotonic()
        argv = [str(self.binary), *arguments]
        env = {"PATH": str(self.binary.parent), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
        env.update(self.environment)
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=env,
            )
        except OSError:
            return self._failed(stage, started, "opa_start_failed"), b""

        output = bytearray()
        lock = threading.Lock()
        exceeded = threading.Event()

        def drain(pipe: Any) -> None:
            while chunk := pipe.read(4096):
                with lock:
                    remaining = self.max_output_bytes - len(output)
                    if remaining > 0:
                        output.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        exceeded.set()
                        proc.kill()
                        return

        threads = [
            threading.Thread(target=drain, args=(proc.stdout,), daemon=True),
            threading.Thread(target=drain, args=(proc.stderr,), daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            exit_code = proc.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            for thread in threads:
                thread.join(timeout=1)
            return self._failed(stage, started, "opa_timeout", output), bytes(output)
        for thread in threads:
            thread.join(timeout=1)
        if exceeded.is_set():
            return self._failed(stage, started, "opa_output_limit", output), bytes(output)
        elapsed = int((time.monotonic() - started) * 1000)
        return (
            OPACommandResult(
                stage=stage,
                ok=exit_code == 0,
                exit_code=exit_code,
                elapsed_ms=elapsed,
                output_summary=_sanitize(output, min(self.max_output_bytes, 2048)),
                failure_code=None if exit_code == 0 else "opa_nonzero_exit",
            ),
            bytes(output),
        )

    def _failed(
        self,
        stage: str,
        started: float,
        code: str,
        output: bytes | bytearray = b"",
    ) -> OPACommandResult:
        return OPACommandResult(
            stage=stage,
            ok=False,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            output_summary=_sanitize(bytes(output), min(self.max_output_bytes, 2048)),
            failure_code=code,
        )


class OPACandidateValidator:
    """在候选目录的只读临时副本中执行严格校验。"""

    def __init__(
        self,
        runner: OPACLIRunner,
        *,
        evaluator: CandidateEvaluator | None = None,
        require_tests: bool = True,
    ) -> None:
        self._runner = runner
        self._evaluator = evaluator
        self._require_tests = require_tests

    def validate(self, snapshot: Path | str) -> CandidateValidationResult:
        source = Path(snapshot).resolve(strict=True)
        if not source.is_dir():
            raise ValueError("candidate snapshot 必须是目录")
        stages: list[OPACommandResult] = []
        package_results: dict[str, bool] = {}
        opa_version: str | None = None
        failure_code: str | None = None

        with tempfile.TemporaryDirectory(prefix="lc-opa-candidate-") as temp_dir:
            candidate = Path(temp_dir) / "candidate"
            shutil.copytree(source, candidate, symlinks=False)
            self._make_read_only(candidate)

            version_stage, version_output = self._runner.run("version", ["version"])
            stages.append(version_stage)
            if version_stage.ok:
                try:
                    version_body = json.loads(version_output)
                    opa_version = str(version_body["Version"])
                except (json.JSONDecodeError, KeyError, TypeError):
                    match = re.search(rb"(?im)^Version:\s*([^\r\n]+)", version_output)
                    if match:
                        opa_version = match.group(1).decode("utf-8", errors="replace").strip()
                    else:
                        version_stage = version_stage.model_copy(
                            update={"ok": False, "failure_code": "opa_version_invalid"}
                        )
                        stages[-1] = version_stage
            if not version_stage.ok:
                failure_code = version_stage.failure_code
            else:
                check_stage, _ = self._runner.run("check", ["check", "--strict", str(candidate)])
                stages.append(check_stage)
                if not check_stage.ok:
                    failure_code = check_stage.failure_code
                else:
                    test_stage, test_output = self._runner.run(
                        "test", ["test", "--format=json", str(candidate)]
                    )
                    if test_stage.ok:
                        try:
                            tests = json.loads(test_output)
                            if not isinstance(tests, list):
                                raise TypeError
                            if self._require_tests and not tests:
                                test_stage = test_stage.model_copy(
                                    update={"ok": False, "failure_code": "opa_no_tests"}
                                )
                        except (json.JSONDecodeError, TypeError):
                            test_stage = test_stage.model_copy(
                                update={"ok": False, "failure_code": "opa_test_output_invalid"}
                            )
                    stages.append(test_stage)
                    if not test_stage.ok:
                        failure_code = test_stage.failure_code
                    else:
                        for package in DEFAULT_DENY_PACKAGES:
                            stage, valid = self._validate_default_deny(package, candidate, Path(temp_dir))
                            stages.append(stage)
                            package_results[package] = valid
                            if not valid:
                                failure_code = stage.failure_code
                                break

        payload = {
            "ok": failure_code is None,
            "opa_version": opa_version,
            "stages": [stage.model_dump(mode="json") for stage in stages],
            "package_default_deny": package_results,
            "failure_code": failure_code,
        }
        return CandidateValidationResult.model_validate(
            {
                **payload,
                "result_sha256": hashlib.sha256(canonical_json(payload).encode()).hexdigest(),
            }
        )

    def _validate_default_deny(
        self, package: str, snapshot: Path, temp_root: Path
    ) -> tuple[OPACommandResult, bool]:
        started = time.monotonic()
        if self._evaluator is not None:
            try:
                decision = self._evaluator(package, {}, snapshot)
            except Exception:
                return self._contract_failure(package, started, "opa_eval_failed"), False
        else:
            input_path = temp_root / f"input-{package.rsplit('.', 1)[-1]}.json"
            input_path.write_text("{}\n", encoding="utf-8")
            query = f"data.{package}.decision"
            stage, output = self._runner.run(
                f"default_deny:{package}",
                ["eval", "--format=json", "--data", str(snapshot), "--input", str(input_path), query],
            )
            if not stage.ok:
                return stage, False
            try:
                body = json.loads(output)
                expressions = body["result"][0]["expressions"]
                if len(body["result"]) != 1 or len(expressions) != 1:
                    raise ValueError
                decision = expressions[0]["value"]
            except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
                return self._contract_failure(package, started, "opa_eval_output_invalid"), False
        valid = isinstance(decision, dict) and decision.get("verdict") == "deny"
        if not valid:
            return self._contract_failure(package, started, "default_deny_contract_failed"), False
        return (
            OPACommandResult(
                stage=f"default_deny:{package}",
                ok=True,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            ),
            True,
        )

    @staticmethod
    def _contract_failure(package: str, started: float, code: str) -> OPACommandResult:
        return OPACommandResult(
            stage=f"default_deny:{package}",
            ok=False,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            failure_code=code,
        )

    @staticmethod
    def _make_read_only(root: Path) -> None:
        for path in sorted(root.rglob("*"), reverse=True):
            path.chmod(stat.S_IREAD | (stat.S_IEXEC if path.is_dir() else 0))
        root.chmod(stat.S_IREAD | stat.S_IEXEC)


class CandidateValidationService:
    """适配 PolicyDelivery 的不可变 candidate，并只在全部校验通过后安装 artifact。"""

    def __init__(self, delivery: PolicyDelivery, validator: OPACandidateValidator) -> None:
        self._delivery = delivery
        self._validator = validator

    def validate(self, candidate_id: str, *, actor: str) -> tuple[Candidate, CandidateValidationResult]:
        candidate = self._delivery.store.get_candidate(candidate_id)
        if candidate is None:
            raise InvalidCandidateError("candidate 不存在")
        if not actor:
            raise InvalidCandidateError("validate actor 不能为空")
        persisted = self._delivery.store.validation(candidate_id)
        if persisted is not None:
            return candidate, CandidateValidationResult.model_validate(persisted)
        if candidate.state is not CandidateState.DRAFT:
            raise InvalidCandidateError("仅 draft candidate 可执行 OPA 校验")

        # 在调用外部进程前按 SQLite 清单复核 snapshot，防止校验被篡改副本。
        self._delivery._source_bytes(candidate)
        snapshot = self._delivery.candidates_dir / candidate.candidate_id / "source"
        result = self._validator.validate(snapshot)
        # 外部校验完成后再次复核，关闭校验期间 snapshot 被替换的竞态窗口。
        self._delivery._source_bytes(candidate)
        if not result.ok:
            failed = self._delivery.store.record_validation(candidate_id, None, result, actor=actor)
            return failed, result

        artifact = self._delivery.build_artifact(candidate_id)
        validated = self._delivery.store.record_validation(candidate_id, artifact, result, actor=actor)
        return validated, result
