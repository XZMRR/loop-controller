from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class DeploymentConformance:
    status: str
    digest: str
    checks: dict[str, bool]
    failures: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "deployment_conformance": {
                "status": self.status,
                "digest": self.digest,
                "checks": self.checks,
                "failures": list(self.failures),
            }
        }


def _container_checks(container: dict[str, Any]) -> dict[str, bool]:
    security = container.get("securityContext") or {}
    resources = container.get("resources") or {}
    limits = resources.get("limits") or {}
    return {
        "run_as_non_root": security.get("runAsNonRoot") is True,
        "read_only_rootfs": security.get("readOnlyRootFilesystem") is True,
        "no_privilege_escalation": security.get("allowPrivilegeEscalation") is False,
        "not_privileged": security.get("privileged") is not True,
        "drop_all_capabilities": "ALL" in ((security.get("capabilities") or {}).get("drop") or []),
        "resources": all(limits.get(name) for name in ("cpu", "memory")),
    }


def validate_kubernetes_manifest(path: str | Path) -> DeploymentConformance:
    raw = Path(path).read_bytes()
    documents = [item for item in yaml.safe_load_all(raw) if isinstance(item, dict)]
    deployments = [item for item in documents if item.get("kind") == "Deployment"]
    policies = [item for item in documents if item.get("kind") == "NetworkPolicy"]
    checks: dict[str, bool] = {
        "has_deployments": bool(deployments),
        "default_deny": any(
            not (item.get("spec", {}).get("podSelector", {}).get("matchLabels"))
            and set(item.get("spec", {}).get("policyTypes") or ()) == {"Ingress", "Egress"}
            and item.get("spec", {}).get("ingress") == []
            and item.get("spec", {}).get("egress") == []
            for item in policies
        ),
        "governance_ingress_only": any(
            item.get("metadata", {}).get("name") == "allow-governance-ingress"
            and bool(item.get("spec", {}).get("ingress"))
            for item in policies
        ),
        "agent_has_no_secret_mount": True,
    }
    for deployment in deployments:
        spec = deployment.get("spec", {}).get("template", {}).get("spec", {})
        name = deployment.get("metadata", {}).get("name", "unknown")
        checks[f"{name}.automount_disabled"] = spec.get("automountServiceAccountToken") is False
        checks[f"{name}.no_host_network"] = spec.get("hostNetwork") is not True
        checks[f"{name}.no_host_pid"] = spec.get("hostPID") is not True
        checks[f"{name}.no_host_ipc"] = spec.get("hostIPC") is not True
        checks[f"{name}.seccomp"] = (
            (spec.get("securityContext") or {}).get("seccompProfile", {}).get("type")
            == "RuntimeDefault"
        )
        containers = spec.get("containers") or []
        checks[f"{name}.has_container"] = bool(containers)
        for container in containers:
            cname = container.get("name", "unknown")
            for key, value in _container_checks(container).items():
                checks[f"{name}.{cname}.{key}"] = value
            mounts = container.get("volumeMounts") or []
            if "agent" in cname:
                checks["agent_has_no_secret_mount"] = checks["agent_has_no_secret_mount"] and not mounts
        volumes = spec.get("volumes") or []
        if any("agent" in item.get("name", "") for item in containers):
            checks["agent_has_no_secret_mount"] = checks["agent_has_no_secret_mount"] and not any(
                "secret" in volume for volume in volumes
            )
    failures = tuple(sorted(name for name, passed in checks.items() if not passed))
    return DeploymentConformance(
        status="conformant" if not failures else "nonconformant",
        digest=hashlib.sha256(raw).hexdigest(),
        checks=dict(sorted(checks.items())),
        failures=failures,
    )


def validate_docker_compose(path: str | Path) -> DeploymentConformance:
    raw = Path(path).read_bytes()
    document = yaml.safe_load(raw) or {}
    services = document.get("services") or {}
    checks: dict[str, bool] = {"has_services": bool(services)}
    for name, service in services.items():
        security_options = service.get("security_opt") or []
        volumes = service.get("volumes") or []
        checks[f"{name}.non_root"] = str(service.get("user", "")).split(":")[0] not in ("", "0", "root")
        checks[f"{name}.not_privileged"] = service.get("privileged") is not True
        checks[f"{name}.no_host_network"] = service.get("network_mode") != "host"
        checks[f"{name}.no_host_pid"] = service.get("pid") != "host"
        checks[f"{name}.no_host_ipc"] = service.get("ipc") != "host"
        checks[f"{name}.read_only"] = service.get("read_only") is True
        checks[f"{name}.drop_all_capabilities"] = "ALL" in (service.get("cap_drop") or [])
        checks[f"{name}.no_new_privileges"] = "no-new-privileges:true" in security_options
        checks[f"{name}.pids_limit"] = bool(service.get("pids_limit"))
        checks[f"{name}.memory_limit"] = bool(service.get("mem_limit"))
        checks[f"{name}.cpu_limit"] = bool(service.get("cpus"))
        checks[f"{name}.no_host_mount"] = not volumes
        if name == "agent":
            checks["agent_has_no_secret"] = not service.get("secrets")
    failures = tuple(sorted(name for name, passed in checks.items() if not passed))
    return DeploymentConformance(
        status="conformant" if not failures else "nonconformant",
        digest=hashlib.sha256(raw).hexdigest(),
        checks=dict(sorted(checks.items())),
        failures=failures,
    )


def write_conformance_result(manifest: str | Path, output: str | Path) -> DeploymentConformance:
    result = validate_kubernetes_manifest(manifest)
    Path(output).write_text(json.dumps(result.as_dict(), sort_keys=True, indent=2), encoding="utf-8")
    return result
