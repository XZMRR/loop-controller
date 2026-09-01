"""config_loader 单元测试.

覆盖 §4.1 全部 8 条启动校验（每条一个正例 + 一个反例）：
1. profile_id 存在性；2. 工具映射存在性；3. default.rego + OPA 试查询；
4. 日志目录可写；5. glob 编译；6. 正则编译；7. approver 存在性；
8. HMAC key 存在性与格式（P0）。
"""

from __future__ import annotations

import copy
import dataclasses
import shutil
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from loop_controller.infra.config_loader import ConfigLoader, ConfigValidationError
from tests.conftest import _start_opa, _terminate_proc, resolve_opa_bin

REPO_ROOT = Path(__file__).resolve().parent.parent
OPA_BIN = resolve_opa_bin(REPO_ROOT)

MINIMAL_REGO = (
    "package loop_controller.tool_permission\n"
    "import rego.v1\n"
    'default decision := {"verdict": "deny", "reason": "no policy allows this action",'
    ' "policy_hits": ["default_deny"]}\n'
)


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def build_config_dir(root: Path) -> Path:
    """在 root 下构造一份完整合法的配置树，返回 config/ 目录。"""
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (root / "policies").mkdir(exist_ok=True)
    (root / "data").mkdir(exist_ok=True)

    _write_yaml(config / "agents.yaml", {
        "agents": [
            {"agent_id": "researcher_001", "name": "RA",
             "profile_id": "research_assistant_v1", "owner_id": "zhang_manager"},
        ],
        "users": [
            {"user_id": "alice", "display_name": "Alice"},
            {"user_id": "zhang_manager", "display_name": "张经理"},
        ],
    })
    _write_yaml(config / "profiles.yaml", {
        "profiles": [{
            "profile_id": "research_assistant_v1",
            "description": "研究助手岗位说明书",
            "max_budget_token": 100000,
            "tools": {
                "read_file": {"allowed": True, "allowed_args": {"path": ["/data/kb/**"]}},
                "write_file": {"allowed": True, "allowed_args": {"path": ["/data/output/**"]}},
                "web_search": {"allowed": True},
                "send_email": {"allowed": True, "require_approval": True,
                               "allowed_args": {"to": ["*@company.com"]}},
            },
        }]
    })
    _write_yaml(config / "mcp_servers.yaml", {
        "servers": {
            "filesystem": {"command": ["npx", "-y", "@modelcontextprotocol/server-filesystem",
                                       "/data/kb", "/data/output"], "transport": "stdio"},
            "email_mock": {"command": ["python", "-m", "loop_controller.mocks.email_server"],
                           "transport": "stdio"},
        },
        "tool_mapping": {
            "read_file": {"server": "filesystem", "mcp_name": "read_text_file"},
            "write_file": {"server": "filesystem", "mcp_name": "write_file"},
            "web_search": {"server": "email_mock", "mcp_name": "web_search"},
            "send_email": {"server": "email_mock", "mcp_name": "send_email"},
        },
    })
    _write_yaml(config / "permission_rules.yaml", {"rules": []})
    _write_yaml(config / "harness_tools.yaml", {
        "execution": {
            "default_mode": "trusted_local",
            "trusted_local_tools": ["read_file", "write_file", "web_search", "send_email"],
        }
    })
    _write_yaml(config / "masking_rules.yaml", {
        "field_name_blacklist": ["password"],
        "value_patterns": [{"name": "email_address", "pattern": r"[\w.+-]+@[\w-]+\.[\w.]+",
                            "replacement": "***@***"}],
        "masking_applies_to": {
            "audit_log": ["field_name_blacklist", "value_patterns"],
            "approval_request": ["field_name_blacklist"],
        },
    })
    _write_yaml(config / "approval.yaml", {
        "approvers": {"default": "zhang_manager"},
        "rules": [{"tool_name": "send_email", "approver": "zhang_manager"}],
    })
    (root / "policies" / "default.rego").write_text(MINIMAL_REGO, encoding="utf-8")
    return config


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    return build_config_dir(tmp_path)


@pytest.fixture(scope="module")
def opa_server(tmp_path_factory) -> str:
    """启动一个临时 OPA server，返回 base_url。"""
    policy_dir = tmp_path_factory.mktemp("opa_policies")
    (policy_dir / "default.rego").write_text(MINIMAL_REGO, encoding="utf-8")

    if not OPA_BIN.exists():
        pytest.skip("OPA 未安装，跳过依赖 OPA 的用例")

    proc, base_url = _start_opa(OPA_BIN, policy_dir=policy_dir)
    yield base_url
    _terminate_proc(proc)


def _valid_anchor_config(config_dir: Path) -> dict:
    public_key = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    (config_dir / "anchor-service.pub").write_bytes(public_key)
    return {
        "evidence": {
            "enabled": True,
            "backend": "local",
            "anchor": {
                "enabled": True,
                "type": "http",
                "stream_id": "deployment-01/default",
                "base_url": "https://anchor.internal.example",
                "connect_timeout_seconds": 1.0,
                "request_timeout_seconds": 3.0,
                "every_n_events": 1,
                "auth": {"type": "bearer", "token_env": "TEST_ANCHOR_TOKEN"},
                "tls": {
                    "verify": True,
                    "ca_file": None,
                    "client_cert_file": None,
                    "client_key_file": None,
                },
                "receipt": {
                    "algorithm": "ed25519",
                    "service_key_id": "anchor-service-01",
                    "public_key_file": "config/anchor-service.pub",
                },
                "startup": {
                    "unavailable_policy": "degrade",
                    "conflict_policy": "block_writes",
                },
            },
        }
    }


def test_load_valid_config(config_dir):
    config = ConfigLoader().load(config_dir)  # 无 opa_base_url，仅跳过 OPA 试查询
    assert "researcher_001" in config.agents
    assert config.agents["researcher_001"].profile_id == "research_assistant_v1"
    assert "research_assistant_v1" in config.profiles
    assert config.users["zhang_manager"] == "张经理"
    assert set(config.profiles["research_assistant_v1"].tools) == {
        "read_file", "write_file", "web_search", "send_email",
    }
    assert config.tool_mapping["send_email"].mcp_name == "send_email"
    assert config.approval.default == "zhang_manager"
    assert config.persistence.fsync_enabled is True
    assert config.persistence.lock_timeout_seconds == 5.0
    assert config.persistence.repair_incomplete_tail is True


def test_persistence_config_loads(config_dir):
    _write_yaml(config_dir / "persistence.yaml", {
        "persistence": {
            "fsync_enabled": False,
            "lock_timeout_seconds": 2.5,
            "repair_incomplete_tail": False,
            "enforce_permissions": False,
            "fail_on_unsafe_permissions": False,
        }
    })

    config = ConfigLoader().load(config_dir)

    assert config.persistence.fsync_enabled is False
    assert config.persistence.lock_timeout_seconds == 2.5
    assert config.persistence.repair_incomplete_tail is False
    assert config.persistence.enforce_permissions is False
    assert config.persistence.fail_on_unsafe_permissions is False


@pytest.mark.parametrize("timeout", [0, -1])
def test_persistence_lock_timeout_must_be_positive(config_dir, timeout):
    _write_yaml(config_dir / "persistence.yaml", {
        "persistence": {"lock_timeout_seconds": timeout}
    })

    with pytest.raises(ConfigValidationError, match="lock_timeout_seconds 必须大于 0"):
        ConfigLoader().load(config_dir)


def test_persistence_unknown_option_is_rejected(config_dir):
    _write_yaml(config_dir / "persistence.yaml", {
        "persistence": {"unknown": True}
    })

    with pytest.raises(ConfigValidationError, match="persistence 配置非法"):
        ConfigLoader().load(config_dir)


def test_anchor_valid_config_loads(config_dir, monkeypatch):
    monkeypatch.setenv("TEST_ANCHOR_TOKEN", "secret-token")
    _write_yaml(config_dir / "evidence.yaml", _valid_anchor_config(config_dir))

    config = ConfigLoader().load(config_dir)

    assert config.evidence_config["evidence"]["anchor"]["stream_id"] == "deployment-01/default"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data["evidence"].update(enabled=False), "evidence.enabled"),
        (lambda data: data["evidence"].update(backend="remote"), "evidence.backend"),
        (lambda data: data["evidence"]["anchor"].update(type="file"), "只能是 http"),
        (lambda data: data["evidence"]["anchor"].update(stream_id="../default"), "stream_id"),
        (lambda data: data["evidence"]["anchor"].update(base_url="http://anchor.example"), "HTTPS URL"),
        (lambda data: data["evidence"]["anchor"].update(base_url="https://u:p@anchor.example"), "HTTPS URL"),
        (lambda data: data["evidence"]["anchor"].update(base_url="https://anchor.example?q=1"), "HTTPS URL"),
        (lambda data: data["evidence"]["anchor"].update(connect_timeout_seconds=0), "connect_timeout"),
        (lambda data: data["evidence"]["anchor"].update(request_timeout_seconds=61), "request_timeout"),
        (lambda data: data["evidence"]["anchor"].update(every_n_events=2), "every_n_events"),
        (lambda data: data["evidence"]["anchor"]["auth"].update(type="none"), "auth.type"),
        (lambda data: data["evidence"]["anchor"]["tls"].update(verify=False), "tls.verify"),
        (lambda data: data["evidence"]["anchor"]["tls"].update(ca_file="missing.pem"), "TLS 文件"),
        (lambda data: data["evidence"]["anchor"]["tls"].update(client_cert_file="cert.pem"), "必须同时配置"),
        (lambda data: data["evidence"]["anchor"]["receipt"].update(algorithm="hmac-sha256"), "只能是 ed25519"),
        (lambda data: data["evidence"]["anchor"]["receipt"].update(service_key_id=""), "service_key_id"),
        (lambda data: data["evidence"]["anchor"]["receipt"].update(public_key_file="missing.pub"), "公钥"),
        (lambda data: data["evidence"]["anchor"]["startup"].update(unavailable_policy="block"), "unavailable_policy"),
        (lambda data: data["evidence"]["anchor"]["startup"].update(conflict_policy="ignore"), "conflict_policy"),
    ],
)
def test_anchor_invalid_config_fails_before_runtime(config_dir, monkeypatch, mutate, message):
    monkeypatch.setenv("TEST_ANCHOR_TOKEN", "secret-token")
    data = copy.deepcopy(_valid_anchor_config(config_dir))
    mutate(data)
    _write_yaml(config_dir / "evidence.yaml", data)

    with pytest.raises(ConfigValidationError, match=message):
        ConfigLoader().load(config_dir)


def test_anchor_missing_token_fails_without_leaking_secret(config_dir, monkeypatch):
    monkeypatch.delenv("TEST_ANCHOR_TOKEN", raising=False)
    _write_yaml(config_dir / "evidence.yaml", _valid_anchor_config(config_dir))

    with pytest.raises(ConfigValidationError) as captured:
        ConfigLoader().load(config_dir)

    assert "secret-token" not in str(captured.value)
    assert "TEST_ANCHOR_TOKEN" in str(captured.value)


def test_anchor_unparseable_public_key_fails(config_dir, monkeypatch):
    monkeypatch.setenv("TEST_ANCHOR_TOKEN", "secret-token")
    data = _valid_anchor_config(config_dir)
    (config_dir / "anchor-service.pub").write_text("not-a-public-key", encoding="utf-8")
    _write_yaml(config_dir / "evidence.yaml", data)

    with pytest.raises(ConfigValidationError, match="公钥"):
        ConfigLoader().load(config_dir)


# 校验 1：profile_id 存在性
def test_check_profile_missing(config_dir):
    agents_path = config_dir / "agents.yaml"
    data = yaml.safe_load(agents_path.read_text(encoding="utf-8"))
    data["agents"][0]["profile_id"] = "ghost_profile"
    _write_yaml(agents_path, data)
    with pytest.raises(ConfigValidationError, match="profile_id ghost_profile 不存在"):
        ConfigLoader().load(config_dir)


# 校验 2：工具名映射存在性
def test_check_tool_mapping_missing(config_dir):
    profiles_path = config_dir / "profiles.yaml"
    data = yaml.safe_load(profiles_path.read_text(encoding="utf-8"))
    data["profiles"][0]["tools"]["deploy_app"] = {"allowed": True}
    _write_yaml(profiles_path, data)
    with pytest.raises(ConfigValidationError, match="deploy_app 不在 tool_mapping"):
        ConfigLoader().load(config_dir)


# 校验 3：default.rego 存在且 OPA 试查询返回结构合法 deny
def test_check_policy_missing_rego(config_dir):
    (config_dir.parent / "policies" / "default.rego").unlink()
    with pytest.raises(ConfigValidationError, match="缺少 default.rego"):
        ConfigLoader().load(config_dir, opa_base_url="http://127.0.0.1:1")


def test_check_policy_opa_unreachable(config_dir):
    with pytest.raises(ConfigValidationError, match="OPA 试查询失败"):
        ConfigLoader().load(config_dir, opa_base_url="http://127.0.0.1:1")


def test_check_policy_ok(config_dir, opa_server):
    config = ConfigLoader().load(config_dir, opa_base_url=opa_server)
    assert config.policy_dir  # 加载成功即通过


# 校验 4：日志目录可写
def test_check_dir_unwritable(config_dir):
    root = config_dir.parent
    shutil.rmtree(root / "data")
    (root / "data").write_text("i am a file", encoding="utf-8")
    with pytest.raises(ConfigValidationError, match="不可写"):
        ConfigLoader().load(config_dir)


# 校验 5：glob 编译
def test_check_glob_invalid(config_dir):
    profiles_path = config_dir / "profiles.yaml"
    data = yaml.safe_load(profiles_path.read_text(encoding="utf-8"))
    data["profiles"][0]["tools"]["read_file"]["allowed_args"]["path"] = ["[bad"]
    _write_yaml(profiles_path, data)
    with pytest.raises(ConfigValidationError, match="非法 glob"):
        ConfigLoader().load(config_dir)


# 校验 6：正则编译
def test_check_regex_invalid(config_dir):
    masking_path = config_dir / "masking_rules.yaml"
    data = yaml.safe_load(masking_path.read_text(encoding="utf-8"))
    data["value_patterns"] = [{"name": "bad", "pattern": "[abc"}]
    _write_yaml(masking_path, data)
    with pytest.raises(ConfigValidationError, match="非法掩码正则"):
        ConfigLoader().load(config_dir)


# 校验 7：approver 存在性
def test_check_approver_missing(config_dir):
    approval_path = config_dir / "approval.yaml"
    data = yaml.safe_load(approval_path.read_text(encoding="utf-8"))
    data["approvers"]["default"] = "ghost_manager"
    _write_yaml(approval_path, data)
    with pytest.raises(ConfigValidationError, match="审批人 ghost_manager 不存在"):
        ConfigLoader().load(config_dir)


# 校验 7（v1.1）：approver 不得等于任何 agent_id
def test_check_approver_is_agent(config_dir):
    # 该 agent 同时登记为 users 成员，确保通过"存在性"校验后命中"不等于 agent_id"校验
    agents_path = config_dir / "agents.yaml"
    agents_data = yaml.safe_load(agents_path.read_text(encoding="utf-8"))
    agents_data["users"].append({"user_id": "researcher_001", "display_name": "RA"})
    _write_yaml(agents_path, agents_data)

    approval_path = config_dir / "approval.yaml"
    data = yaml.safe_load(approval_path.read_text(encoding="utf-8"))
    data["approvers"]["default"] = "researcher_001"
    _write_yaml(approval_path, data)
    with pytest.raises(ConfigValidationError, match="不能作为审批人"):
        ConfigLoader().load(config_dir)


# ---------------------------------------------------------------------------
# 校验 8：HMAC key 存在性与格式（P0）
# ---------------------------------------------------------------------------


def test_hmac_sha256_requires_key(config_dir):
    config = ConfigLoader().load(config_dir)
    config_with_hmac = dataclasses.replace(config, audit_hash_algo="hmac-sha256")
    # 直接测试 key 解析：用独立上下文清除全局 fixture 注入的 key。
    with pytest.MonkeyPatch().context() as mp:
        mp.delenv("LOOP_CONTROLLER_AUDIT_HMAC_KEY", raising=False)
        with pytest.raises(ValueError, match="未设置"):
            ConfigLoader.resolve_audit_key(config_with_hmac)


def test_hmac_sha256_hex_key_ok(config_dir, monkeypatch):
    monkeypatch.setenv("LOOP_CONTROLLER_AUDIT_HMAC_KEY", "a" * 64)
    config = ConfigLoader().load(config_dir)
    config_with_hmac = dataclasses.replace(config, audit_hash_algo="hmac-sha256")
    ConfigLoader()._check_audit_key(config_with_hmac)  # 不抛即通过
    key = ConfigLoader.resolve_audit_key(config_with_hmac)
    assert len(key) == 32


def test_hmac_sha256_base64_key_ok(config_dir, monkeypatch):
    import base64
    raw = base64.b64encode(b"x" * 32).decode("ascii")
    monkeypatch.setenv("LOOP_CONTROLLER_AUDIT_HMAC_KEY", raw)
    config = ConfigLoader().load(config_dir)
    config_with_hmac = dataclasses.replace(config, audit_hash_algo="hmac-sha256")
    key = ConfigLoader.resolve_audit_key(config_with_hmac)
    assert len(key) == 32


def test_hmac_sha256_rejects_short_key(config_dir):
    config = ConfigLoader().load(config_dir)
    config_with_hmac = dataclasses.replace(config, audit_hash_algo="hmac-sha256")
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("LOOP_CONTROLLER_AUDIT_HMAC_KEY", "a" * 16)
        with pytest.raises(ValueError, match="长度"):
            ConfigLoader.resolve_audit_key(config_with_hmac)


def test_hmac_sha256_rejects_invalid_encoding(config_dir):
    config = ConfigLoader().load(config_dir)
    config_with_hmac = dataclasses.replace(config, audit_hash_algo="hmac-sha256")
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("LOOP_CONTROLLER_AUDIT_HMAC_KEY", "not-hex-or-base64!!!")
        with pytest.raises(ValueError, match="无法解析"):
            ConfigLoader.resolve_audit_key(config_with_hmac)


# 校验 identity/entrypoints 配置

def test_identity_unknown_provider(config_dir):
    _write_yaml(
        config_dir / "identity.yaml",
        {"identity": {"provider": "oauth2"}},
    )
    with pytest.raises(ConfigValidationError, match="identity.provider 必须是"):
        ConfigLoader().load(config_dir)


def test_identity_jwt_missing_issuer(config_dir):
    _write_yaml(
        config_dir / "identity.yaml",
        {"identity": {"provider": "jwt", "jwt": {"public_key": "pk"}}},
    )
    with pytest.raises(ConfigValidationError, match="jwt.issuer"):
        ConfigLoader().load(config_dir)


def test_identity_jwt_missing_key_source(config_dir):
    _write_yaml(
        config_dir / "identity.yaml",
        {"identity": {"provider": "jwt", "jwt": {"issuer": "https://auth"}}},
    )
    with pytest.raises(ConfigValidationError, match="jwks_url 或 jwt.public_key"):
        ConfigLoader().load(config_dir)


def test_identity_mtls_missing_template_or_mappings(config_dir):
    _write_yaml(
        config_dir / "identity.yaml",
        {"identity": {"provider": "mtls", "mtls": {}}},
    )
    with pytest.raises(ConfigValidationError, match="mtls.cert_subject_template"):
        ConfigLoader().load(config_dir)


def test_identity_static_missing_fields(config_dir):
    _write_yaml(
        config_dir / "identity.yaml",
        {
            "identity": {
                "provider": "static",
                "static": {"allowed_tokens": [{"token": "t", "agent_id": "a"}]},
            }
        },
    )
    with pytest.raises(ConfigValidationError, match="缺少或空字段 user_id"):
        ConfigLoader().load(config_dir)


def test_entrypoints_unknown_auth(config_dir):
    _write_yaml(
        config_dir / "entrypoints.yaml",
        {"entrypoints": {"http": {"auth": "basic", "require_auth": True}}},
    )
    with pytest.raises(ConfigValidationError, match="entrypoints.http.auth 必须是"):
        ConfigLoader().load(config_dir)


def test_entrypoints_require_auth_not_bool(config_dir):
    _write_yaml(
        config_dir / "entrypoints.yaml",
        {"entrypoints": {"http": {"auth": "jwt", "require_auth": "yes"}}},
    )
    with pytest.raises(ConfigValidationError, match="require_auth 必须是布尔值"):
        ConfigLoader().load(config_dir)


# v0.27.0 Harness 阶段 1 配置校验


def _write_harness_config(config_dir: Path, backend: dict, tool: dict | None = None) -> None:
    data = {
        "execution": {"default_mode": "trusted_local"},
        "backends": {"remote": backend},
        "tools": {},
    }
    if tool is not None:
        data["tools"] = {"deploy": tool}
    _write_yaml(config_dir / "harness_tools.yaml", data)


def test_harness_legacy_api_key_env_is_normalized(config_dir, monkeypatch):
    monkeypatch.setenv("HARNESS_API_KEY", "secret")
    _write_harness_config(
        config_dir,
        {"type": "http", "base_url": "https://harness.example", "api_key_env": "HARNESS_API_KEY"},
    )
    backend = ConfigLoader().load(config_dir).harness_backends["remote"]
    assert backend.auth.type == "api_key"
    assert backend.auth.key_env == "HARNESS_API_KEY"


def test_harness_rejects_legacy_and_new_auth_together(config_dir, monkeypatch):
    monkeypatch.setenv("HARNESS_API_KEY", "secret")
    _write_harness_config(
        config_dir,
        {
            "type": "http",
            "base_url": "https://harness.example",
            "api_key_env": "HARNESS_API_KEY",
            "auth": {"type": "api_key", "key_env": "HARNESS_API_KEY"},
        },
    )
    with pytest.raises(ConfigValidationError, match="api_key_env 与 auth 不得同时配置"):
        ConfigLoader().load(config_dir)


def test_harness_tool_rejects_missing_backend(config_dir):
    _write_yaml(
        config_dir / "harness_tools.yaml",
        {
            "execution": {"default_mode": "trusted_local"},
            "tools": {"deploy": {"harness": "missing", "input_schema": {"type": "object"}}},
        },
    )
    with pytest.raises(ConfigValidationError, match="backend missing 不存在"):
        ConfigLoader().load(config_dir)


def test_harness_rejects_duplicate_backend_name(config_dir):
    (config_dir / "harness_tools.yaml").write_text(
        "backends:\n"
        "  remote:\n"
        "    type: subprocess\n"
        "    command: [python, first.py]\n"
        "  remote:\n"
        "    type: subprocess\n"
        "    command: [python, second.py]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigValidationError, match="包含重复名称 'remote'"):
        ConfigLoader().load(config_dir)


def test_harness_rejects_non_loopback_http(config_dir):
    _write_harness_config(
        config_dir,
        {"type": "http", "base_url": "http://harness.example", "allow_insecure_http": True},
    )
    with pytest.raises(ConfigValidationError, match="必须使用 HTTPS"):
        ConfigLoader().load(config_dir)


def test_harness_loopback_http_requires_explicit_opt_in(config_dir):
    _write_harness_config(config_dir, {"type": "http", "base_url": "http://127.0.0.1:8080"})
    with pytest.raises(ConfigValidationError, match="allow_insecure_http"):
        ConfigLoader().load(config_dir)


def test_harness_loopback_http_explicit_opt_in_ok(config_dir):
    _write_harness_config(
        config_dir,
        {"type": "http", "base_url": "http://localhost:8080", "allow_insecure_http": True},
    )
    assert "remote" in ConfigLoader().load(config_dir).harness_backends


def test_harness_production_https_rejects_disabled_verify(config_dir):
    _write_harness_config(
        config_dir,
        {"type": "http", "base_url": "https://harness.example", "tls": {"verify": False}},
    )
    with pytest.raises(ConfigValidationError, match="不得关闭 TLS 校验"):
        ConfigLoader().load(config_dir)


def test_harness_auth_requires_nonempty_environment_variable(config_dir, monkeypatch):
    monkeypatch.setenv("HARNESS_SIGNING_KEY", "   ")
    _write_harness_config(
        config_dir,
        {
            "type": "http",
            "base_url": "https://harness.example",
            "auth": {"type": "hmac_sha256", "key_env": "HARNESS_SIGNING_KEY", "key_id": "key-1"},
        },
    )
    with pytest.raises(ConfigValidationError, match="未设置或为空"):
        ConfigLoader().load(config_dir)


def test_harness_mtls_cert_and_key_must_be_paired(config_dir):
    _write_harness_config(
        config_dir,
        {
            "type": "http",
            "base_url": "https://harness.example",
            "tls": {"client_cert_file": "client.pem"},
        },
    )
    with pytest.raises(ConfigValidationError, match="必须成对配置"):
        ConfigLoader().load(config_dir)


def test_harness_tls_files_must_exist(config_dir):
    _write_harness_config(
        config_dir,
        {
            "type": "http",
            "base_url": "https://harness.example",
            "tls": {"ca_file": str(config_dir / "missing-ca.pem")},
        },
    )
    with pytest.raises(ConfigValidationError, match="TLS 文件 ca_file 不存在或不可读"):
        ConfigLoader().load(config_dir)


def test_harness_rejects_invalid_input_schema_type(config_dir):
    _write_harness_config(
        config_dir,
        {"type": "subprocess", "command": ["python", "runner.py"]},
        {"harness": "remote", "input_schema": {"type": "invalid"}},
    )
    with pytest.raises(ConfigValidationError, match="不是合法 JSON Schema"):
        ConfigLoader().load(config_dir)


def test_harness_rejects_structurally_invalid_input_schema(config_dir):
    _write_harness_config(
        config_dir,
        {"type": "subprocess", "command": ["python", "runner.py"]},
        {
            "harness": "remote",
            "input_schema": {"type": "object", "required": "not-an-array"},
        },
    )
    with pytest.raises(ConfigValidationError, match="不是合法 JSON Schema"):
        ConfigLoader().load(config_dir)


def test_harness_loads_docker_backend(config_dir):
    _write_harness_config(
        config_dir,
        {"type": "docker", "image": "example/harness:latest"},
    )
    config = ConfigLoader().load(config_dir)
    assert config.harness_backends["remote"].type == "docker"
    assert config.harness_backends["remote"].image == "example/harness:latest"


def test_harness_loads_isolated_subprocess_backend(config_dir):
    _write_harness_config(
        config_dir,
        {"type": "isolated_subprocess", "python_path": "python"},
    )
    config = ConfigLoader().load(config_dir)
    assert config.harness_backends["remote"].type == "isolated_subprocess"
    assert config.harness_backends["remote"].python_path == "python"
