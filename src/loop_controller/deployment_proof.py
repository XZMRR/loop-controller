from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class DeploymentProof:
    provider: str
    workload: str
    instance: str
    profile_sha256: str
    environment_digest: str
    issued_at: datetime
    expires_at: datetime
    signature: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DeploymentProof:
        return cls(
            provider=str(value["provider"]),
            workload=str(value["workload"]),
            instance=str(value["instance"]),
            profile_sha256=str(value["profile_sha256"]),
            environment_digest=str(value["environment_digest"]),
            issued_at=datetime.fromisoformat(str(value["issued_at"]).replace("Z", "+00:00")),
            expires_at=datetime.fromisoformat(str(value["expires_at"]).replace("Z", "+00:00")),
            signature=str(value["signature"]),
        )

    def payload(self) -> bytes:
        value = {
            "environment_digest": self.environment_digest,
            "expires_at": self.expires_at.astimezone(UTC).isoformat(),
            "instance": self.instance,
            "issued_at": self.issued_at.astimezone(UTC).isoformat(),
            "profile_sha256": self.profile_sha256,
            "provider": self.provider,
            "workload": self.workload,
        }
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class DeploymentProofVerifier:
    def __init__(
        self,
        key_env: str,
        *,
        max_clock_skew_seconds: int = 30,
        max_age_seconds: int = 300,
    ) -> None:
        self._key_env = key_env
        self._max_clock_skew = timedelta(seconds=max_clock_skew_seconds)
        self._max_age = timedelta(seconds=max_age_seconds)

    def verify(
        self,
        proof: DeploymentProof,
        *,
        provider: str,
        workload: str,
        instance: str,
        profile_sha256: str,
        environment_digest: str,
        now: datetime | None = None,
    ) -> bool:
        key = os.environ.get(self._key_env)
        if not key:
            return False
        now = now or datetime.now(UTC)
        if (
            now.tzinfo is None
            or proof.issued_at.tzinfo is None
            or proof.expires_at.tzinfo is None
        ):
            return False
        if (
            proof.expires_at <= proof.issued_at
            or proof.issued_at > now + self._max_clock_skew
            or proof.issued_at < now - self._max_age
            or proof.expires_at <= now
        ):
            return False
        expected_fields = (
            (proof.provider, provider),
            (proof.workload, workload),
            (proof.instance, instance),
            (proof.profile_sha256, profile_sha256),
            (proof.environment_digest, environment_digest),
        )
        if any(not hmac.compare_digest(actual, expected) for actual, expected in expected_fields):
            return False
        expected_signature = hmac.new(key.encode(), proof.payload(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(proof.signature, expected_signature)
