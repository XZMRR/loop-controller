from __future__ import annotations

import gzip
import json
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from loop_controller.infra.policy_delivery import (
    ArtifactConflictError,
    CandidateLimitError,
    CandidateLimits,
    CandidateState,
    CandidateStateError,
    InvalidCandidateError,
    PolicyCASConflictError,
    PolicyDelivery,
    check_transition,
)
from loop_controller.infra.state_db import StateDatabase


@pytest.fixture
def delivery(tmp_path: Path) -> PolicyDelivery:
    return PolicyDelivery(tmp_path, StateDatabase(tmp_path / "state.db"))


def _files(suffix: str = "") -> dict[str, str]:
    return {
        "default.rego": f"package loop_controller.tool_permission\n{suffix}",
        "interaction/default.rego": "package loop_controller.interaction.delegation\n",
        "data/rules.json": '{"enabled":true}',
    }


def test_snapshot_manifest_is_sorted_recursive_and_immutable(delivery: PolicyDelivery) -> None:
    candidate = delivery.create_candidate(_files(), base_revision=None, actor="admin")
    assert [item.path for item in candidate.source_manifest] == [
        "data/rules.json",
        "default.rego",
        "interaction/default.rego",
    ]
    assert len(candidate.source_sha256) == 64
    source = delivery.candidates_dir / candidate.candidate_id / "source"
    assert (source / "interaction" / "default.rego").is_file()
    assert not (source / "default.rego").stat().st_mode & 0o222

    # 即使高权限测试进程绕过只读位修改文件，后续构建仍由清单 hash fail-closed。
    (source / "default.rego").chmod(0o644)
    (source / "default.rego").write_text("tampered", encoding="utf-8")
    with pytest.raises(InvalidCandidateError, match="已被修改"):
        delivery.build_artifact(candidate.candidate_id)


@pytest.mark.parametrize(
    "path",
    [
        "../x.rego",
        "a/../x.rego",
        "/x.rego",
        "C:/x.rego",
        "a\\x.rego",
        "./x.rego",
        ".hidden.rego",
        "a/.hidden/x.rego",
        ".manifest",
        "x.txt",
        "x.rego\x00evil",
        "a//x.rego",
    ],
)
def test_rejects_path_attacks(delivery: PolicyDelivery, path: str) -> None:
    with pytest.raises(InvalidCandidateError):
        delivery.create_candidate({path: "package x"}, base_revision=None, actor="admin")


def test_rejects_invalid_json_utf8_and_limits(tmp_path: Path) -> None:
    delivery = PolicyDelivery(
        tmp_path,
        StateDatabase(tmp_path / "state.db"),
        limits=CandidateLimits(max_files=1, max_file_bytes=4, max_total_bytes=4),
    )
    with pytest.raises(CandidateLimitError, match="文件数"):
        delivery.create_candidate({"a.rego": "a", "b.rego": "b"}, base_revision=None, actor="a")
    with pytest.raises(CandidateLimitError, match="单文件"):
        delivery.create_candidate({"a.rego": "12345"}, base_revision=None, actor="a")
    with pytest.raises(InvalidCandidateError, match="UTF-8/JSON"):
        delivery.create_candidate({"a.json": "{"}, base_revision=None, actor="a")
    with pytest.raises(InvalidCandidateError, match="UTF-8/JSON"):
        delivery.create_candidate({"a.rego": b"\xff"}, base_revision=None, actor="a")


def test_bundle_is_byte_deterministic_with_fixed_metadata(tmp_path: Path) -> None:
    first = PolicyDelivery(tmp_path / "one", StateDatabase(tmp_path / "one.db"))
    second = PolicyDelivery(tmp_path / "two", StateDatabase(tmp_path / "two.db"))
    c1 = first.create_candidate(_files(), base_revision=None, actor="a", candidate_id="one")
    c2 = second.create_candidate(_files(), base_revision=None, actor="a", candidate_id="two")
    a1 = first.build_artifact(c1.candidate_id)
    a2 = second.build_artifact(c2.candidate_id)
    assert a1.revision == a2.revision
    assert a1.sha256 == a2.sha256
    assert a1.path.read_bytes() == a2.path.read_bytes()
    assert len(a1.revision) == len(a1.sha256) == 64
    with a1.path.open("rb") as stream:
        assert stream.read(2) == b"\x1f\x8b"
        stream.seek(4)
        assert int.from_bytes(stream.read(4), "little") == 0
    with gzip.open(a1.path, "rb") as stream, tarfile.open(fileobj=stream, mode="r:") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == sorted(
            [member.name for member in members], key=lambda value: value.encode()
        )
        assert all(
            (member.mtime, member.uid, member.gid, member.uname, member.gname, member.mode)
            == (0, 0, 0, "", "", 0o644)
            for member in members
        )
        manifest = json.load(archive.extractfile(".manifest"))
        assert manifest == {"revision": a1.revision, "roots": ["loop_controller"]}


def test_revision_changes_for_name_content_and_roots(tmp_path: Path) -> None:
    revisions = []
    cases = [
        (_files(), ("loop_controller",)),
        (_files("# changed"), ("loop_controller",)),
        ({**_files(), "data/other.json": "{}"}, ("loop_controller",)),
        (_files(), ("other",)),
    ]
    for index, (files, roots) in enumerate(cases):
        delivery = PolicyDelivery(
            tmp_path / str(index), StateDatabase(tmp_path / f"{index}.db"), roots=roots
        )
        candidate = delivery.create_candidate(files, base_revision=None, actor="a")
        revisions.append(delivery.build_artifact(candidate.candidate_id).revision)
    assert len(set(revisions)) == len(revisions)


def test_state_machine_allows_only_documented_transitions() -> None:
    legal = {
        ("draft", "validated"), ("draft", "failed"),
        ("validated", "published"),
        ("published", "loaded"), ("published", "failed"),
        ("published", "superseded"), ("loaded", "superseded"),
        ("failed", "superseded"),
    }
    for source in CandidateState:
        for target in CandidateState:
            if (source.value, target.value) in legal:
                check_transition(source, target)
            else:
                with pytest.raises(CandidateStateError):
                    check_transition(source, target)


def test_publish_cas_concurrency_and_atomic_audit(delivery: PolicyDelivery) -> None:
    first = delivery.create_candidate(_files("# a"), base_revision=None, actor="a")
    second = delivery.create_candidate(_files("# b"), base_revision=None, actor="b")
    delivery.validate_without_opa(first.candidate_id)
    delivery.validate_without_opa(second.candidate_id)

    def publish(candidate_id: str) -> str:
        try:
            delivery.store.publish(candidate_id, None, candidate_id)
            return "ok"
        except PolicyCASConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, [first.candidate_id, second.candidate_id]))
    assert sorted(results) == ["conflict", "ok"]
    current = delivery.store.current()
    assert current["generation"] == 1
    with delivery.store._db._connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM policy_change_audit WHERE event_type = 'publish'"
        ).fetchone()[0] == 1
        states = dict(conn.execute("SELECT candidate_id, state FROM policy_candidates"))
    assert states[current["candidate_id"]] == "published"
    loser = second.candidate_id if current["candidate_id"] == first.candidate_id else first.candidate_id
    assert states[loser] == "validated"


def test_publish_integrity_failure_keeps_pointer_and_audit_unchanged(delivery: PolicyDelivery) -> None:
    candidate = delivery.create_candidate(_files(), base_revision=None, actor="a")
    validated = delivery.validate_without_opa(candidate.candidate_id)
    assert validated.artifact_path
    artifact_path = Path(validated.artifact_path)
    artifact_path.chmod(0o644)
    artifact_path.write_bytes(b"corrupt")
    with pytest.raises(ArtifactConflictError):
        delivery.store.publish(candidate.candidate_id, None, "a")
    assert delivery.store.current()["revision"] is None
    with delivery.store._db._connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM policy_change_audit WHERE event_type = 'publish'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT state FROM policy_candidates WHERE candidate_id = ?", (candidate.candidate_id,)
        ).fetchone()[0] == "validated"


def test_audit_write_failure_rolls_back_pointer_and_state(delivery: PolicyDelivery) -> None:
    candidate = delivery.create_candidate(_files(), base_revision=None, actor="a")
    delivery.validate_without_opa(candidate.candidate_id)
    with delivery.store._db._connect() as conn:
        conn.execute(
            """CREATE TRIGGER reject_policy_audit BEFORE INSERT ON policy_change_audit
               BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"""
        )
    with pytest.raises(Exception, match="injected audit failure"):
        delivery.store.publish(candidate.candidate_id, None, "a")
    assert delivery.store.current()["revision"] is None
    assert delivery.store.get_candidate(candidate.candidate_id).state == CandidateState.VALIDATED


def test_existing_revision_with_different_bytes_is_rejected(delivery: PolicyDelivery) -> None:
    candidate = delivery.create_candidate(_files(), base_revision=None, actor="a")
    artifact = delivery.build_artifact(candidate.candidate_id)
    artifact.path.chmod(0o644)
    artifact.path.write_bytes(b"conflict")
    with pytest.raises(ArtifactConflictError, match="hash 冲突"):
        delivery.build_artifact(candidate.candidate_id)


def test_rollback_reuses_artifact_but_creates_new_intent_and_generation(
    delivery: PolicyDelivery,
) -> None:
    old = delivery.create_candidate(_files("# old"), base_revision=None, actor="a")
    old_validated = delivery.validate_without_opa(old.candidate_id)
    delivery.store.publish(old.candidate_id, None, "a")
    delivery.store.transition(old.candidate_id, CandidateState.LOADED)
    with delivery.store._db._connect() as conn:
        conn.execute(
            """INSERT INTO policy_change_audit (audit_id,event_type,actor,candidate_id,
               base_revision,target_revision,result,detail_json,created_at)
               VALUES ('loaded-evidence-1','loaded','opa-status',?,?,?, 'success','{}',?)""",
            (old.candidate_id, None, old_validated.revision, "2024-01-01T00:00:00+00:00"),
        )

    new = delivery.create_candidate(
        _files("# new"), base_revision=old_validated.revision, actor="b"
    )
    new_validated = delivery.validate_without_opa(new.candidate_id)
    delivery.store.publish(new.candidate_id, old_validated.revision, "b")

    rollback_candidate, result = delivery.store.rollback(
        old_validated.revision, new_validated.revision, "operator"
    )
    assert rollback_candidate.candidate_id != old.candidate_id
    assert rollback_candidate.artifact_path == old_validated.artifact_path
    assert rollback_candidate.rollback_of_revision == old_validated.revision
    assert result == {"revision": old_validated.revision, "generation": 3}
    assert delivery.store.get_candidate(old.candidate_id).state == CandidateState.SUPERSEDED
    with delivery.store._db._connect() as conn:
        audit = conn.execute(
            "SELECT event_type, detail_json FROM policy_change_audit ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert audit["event_type"] == "rollback"
    assert json.loads(audit["detail_json"])["rollback_of_revision"] == old_validated.revision
