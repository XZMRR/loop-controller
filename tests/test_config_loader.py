"""config_loader 单元测试.

覆盖 §4.1 全部 8 条启动校验（每条一个正例 + 一个反例）：
1. profile_id 存在性；2. 工具映射存在性；3. default.rego + OPA 试查询；
4. 日志目录可写；5. glob 编译；6. 正则编译；7. approver 存在性；
8. HMAC key 存在性与格式（P0）。
"""

from __future__ import annotations

from pathlib import Path
import os
import shutil
import socket
import subprocess
import time

import httpx
import pytest
import yaml

from loop_controller.infra.config_loader import ConfigLoader, ConfigValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent
OPA_BIN = Path(os.environ.get("OPA_PATH", REPO_ROOT / "tools" / "opa.exe"))

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
        "rules": [{"tool_name": "send_email", "approver": "zhang_manager", "behavior": "approve"}],
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

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [str(OPA_BIN), "run", "--server", "--bundle", str(policy_dir), "--addr", f"127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    ready = False
    while time.time() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=1, trust_env=False).status_code == 200:
                ready = True
                break
        except Exception:
            time.sleep(0.3)
    if not ready:
        proc.terminate()
        pytest.skip("OPA 无法启动，跳过依赖 OPA 的用例")
    yield base_url
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_load_valid_config(config_dir):
    config = ConfigLoader().load(config_dir)  # 无 opa_base_url，跳过校验 3
    assert "researcher_001" in config.agents
    assert config.agents["researcher_001"].profile_id == "research_assistant_v1"
    assert "research_assistant_v1" in config.profiles
    assert config.users["zhang_manager"] == "张经理"
    assert set(config.profiles["research_assistant_v1"].tools) == {
        "read_file", "write_file", "web_search", "send_email",
    }
    assert config.tool_mapping["send_email"].mcp_name == "send_email"
    assert config.approval.default == "zhang_manager"


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

import dataclasses


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
