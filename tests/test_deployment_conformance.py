from pathlib import Path

import yaml

from loop_controller.deployment_conformance import (
    validate_docker_compose,
    validate_kubernetes_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy" / "examples" / "kubernetes-strict.yaml"
COMPOSE = ROOT / "deploy" / "examples" / "docker-compose.strict.yml"


def test_strict_manifest_is_conformant() -> None:
    result = validate_kubernetes_manifest(MANIFEST)
    assert result.status == "conformant", result.failures
    assert len(result.digest) == 64


def test_strict_docker_compose_is_conformant() -> None:
    result = validate_docker_compose(COMPOSE)
    assert result.status == "conformant", result.failures


def test_manifest_validator_fails_closed(tmp_path: Path) -> None:
    documents = list(yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")))
    documents[0]["spec"]["template"]["spec"]["containers"][0]["securityContext"][
        "readOnlyRootFilesystem"
    ] = False
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump_all(documents), encoding="utf-8")
    result = validate_kubernetes_manifest(path)
    assert result.status == "nonconformant"
    assert "agent.agent.read_only_rootfs" in result.failures


def test_manifest_rejects_host_namespaces_and_privileged(tmp_path: Path) -> None:
    documents = list(yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")))
    deployment = next(item for item in documents if item.get("kind") == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    pod.update(hostNetwork=True, hostPID=True, hostIPC=True)
    pod["containers"][0]["securityContext"]["privileged"] = True
    path = tmp_path / "host.yaml"
    path.write_text(yaml.safe_dump_all(documents), encoding="utf-8")
    failures = validate_kubernetes_manifest(path).failures
    assert {"agent.no_host_network", "agent.no_host_pid", "agent.no_host_ipc", "agent.agent.not_privileged"} <= set(failures)


def test_compose_rejects_host_namespaces_and_privileged(tmp_path: Path) -> None:
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    service = document["services"]["agent"]
    service.update(privileged=True, network_mode="host", pid="host", ipc="host")
    path = tmp_path / "compose.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    failures = validate_docker_compose(path).failures
    assert {"agent.not_privileged", "agent.no_host_network", "agent.no_host_pid", "agent.no_host_ipc"} <= set(failures)
