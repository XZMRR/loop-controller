import hashlib
import hmac
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from loop_controller.deployment_proof import DeploymentProof, DeploymentProofVerifier


def _proof(now: datetime) -> DeploymentProof:
    proof = DeploymentProof(
        provider="release-ci",
        workload="loop-controller",
        instance="lc-1",
        profile_sha256="a" * 64,
        environment_digest="b" * 64,
        issued_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=5),
        signature="",
    )
    return replace(proof, signature=hmac.new(b"proof-key", proof.payload(), hashlib.sha256).hexdigest())


def test_deployment_proof_binding_and_freshness(monkeypatch) -> None:
    monkeypatch.setenv("LC_PROOF_KEY", "proof-key")
    now = datetime.now(UTC)
    proof = _proof(now)
    verifier = DeploymentProofVerifier("LC_PROOF_KEY")
    expected = dict(provider="release-ci", workload="loop-controller", instance="lc-1", profile_sha256="a" * 64, environment_digest="b" * 64, now=now)
    assert verifier.verify(proof, **expected)
    assert not verifier.verify(replace(proof, signature="0" * 64), **expected)
    assert not verifier.verify(proof, **{**expected, "workload": "attacker"})
    assert not verifier.verify(proof, **{**expected, "environment_digest": "c" * 64})
    assert not verifier.verify(replace(proof, expires_at=now), **expected)
    assert not verifier.verify(
        replace(
            proof,
            issued_at=now - timedelta(minutes=10),
            expires_at=now + timedelta(minutes=5),
        ),
        **expected,
    )
    assert not verifier.verify(
        replace(
            proof,
            issued_at=now + timedelta(minutes=1),
            expires_at=now + timedelta(minutes=5),
        ),
        **expected,
    )
    assert not verifier.verify(
        replace(proof, issued_at=now.replace(tzinfo=None)),
        **expected,
    )
