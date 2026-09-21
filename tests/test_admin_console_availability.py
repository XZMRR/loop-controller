"""治理台页面可用性红线：真实配置装配下三个管理端点必须 200。

回归对象：浏览器人工点验发现 RBAC 绑定页 503（rbac_unavailable）、策略生命周期页
503（policy_delivery_unavailable）、A2A 治理页 502（内核不可达）。三处根因都在
``ConfigLoader`` + ``build_runtime`` 的真实装配路径上，而手工构造 Runtime 的
单测（如 test_rbac_server.py）绕过了这条路径，因此 CI 全绿仍漏到人工验收。

本文件走真实装配（复制仓库 config → ConfigLoader.load → build_controller），
把"页面背后的端点可用"固化为回归红线。policy_delivery 的 P0 启动门禁在本测试
中通过 fake opa 二进制 + 测试环境变量满足（门禁语义由 config_loader 专项测试
覆盖，此处关注装配与端点）。
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from loop_controller.controller import build_controller
from loop_controller.infra.config_loader import ConfigLoader
from loop_controller.server import build_app
from tests.conftest import write_trusted_local_harness_config

REPO_ROOT = Path(__file__).resolve().parent.parent

ADMIN_KEY = "console-redline-admin-key"

# 模块导入期捕获原始实现：conftest 的 autouse fixture 会替换类属性，
# 运行期再从 __dict__ 取到的是被 patch 后的版本。
_ORIGINAL_WITH_LOCAL_OVERRIDE = ConfigLoader.__dict__["_with_local_override"].__func__


@pytest.fixture
def console_workdir(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    shutil.copytree(REPO_ROOT / "config", root / "config")
    shutil.copytree(REPO_ROOT / "policies", root / "policies")
    (root / "data").mkdir()
    # P0 门禁要求 opa_binary 是存在的文件；装配期不会执行它（惰性），fake 即可。
    (root / "tools").mkdir()
    (root / "tools" / "opa.exe").write_bytes(b"fake-opa-for-availability-test")

    # 启用 policy_delivery：全部使用相对路径（相对项目根解析），验证页面装配。
    (root / "config" / "policy_delivery.yaml").write_text(
        """
policy_delivery:
  enabled: true
  data_dir: "./data"
  state_db_path: "./data/state.db"
  opa_binary: "tools/opa.exe"
  bundle_name: "loop-controller"
  required_instance_ids: ["opa-local-1"]
  status_ttl_seconds: 60
  admin_token_env: "LOOP_CONTROLLER_API_KEY"
  bundle_token_env: "LOOP_CONTROLLER_BUNDLE_TOKEN"
  status_token_env: "LOOP_CONTROLLER_OPA_STATUS_TOKEN"
""",
        encoding="utf-8",
    )
    # 启用 Go 内核桥接，指向不可达端口：可达性降级必须是 200 + reachable=false，
    # 而不是 502/503（页面 fail-safe 语义的红线）。
    (root / "config" / "go_kernel.yaml").write_text(
        """
go_kernel:
  enabled: true
  base_url: "http://127.0.0.1:1"
  timeout: 0.2
  development: true
  control_token_env: "LC_A2A_CONTROL_TOKEN"
  control_initiator: "console-redline"
  control_tenant: "tenant-a"
""",
        encoding="utf-8",
    )
    # 精简 MCP 与 harness 配置：本测试只验管理端点，不触发工具执行。
    (root / "config" / "mcp_servers.yaml").write_text(
        """
servers:
  email_mock:
    command: ["python", "-m", "loop_controller.mocks.email_server"]
    transport: stdio

tool_mapping:
  web_search:  {server: email_mock, mcp_name: web_search, cost_per_call: 200}
  send_email:  {server: email_mock, mcp_name: send_email, cost_per_call: 800}
""",
        encoding="utf-8",
    )
    (root / "config" / "profiles.yaml").write_text(
        """
profiles:
  - profile_id: research_assistant_v1
    description: 研究助手岗位说明书
    max_budget_token: 100000
    max_budget_payment: 0.0
    session_block_threshold: 2
    tools:
      web_search:
        allowed: true
        max_calls_per_task: 10
      send_email:
        allowed: true
        require_approval: true
        allowed_args:
          to: ["*@company.com"]
        max_calls_per_task: 1
""",
        encoding="utf-8",
    )
    write_trusted_local_harness_config(root / "config", ["web_search", "send_email"])
    return root


@pytest.fixture
async def console_client(
    console_workdir: Path, opa_server: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[TestClient]:
    monkeypatch.setenv("LOOP_CONTROLLER_API_KEY", ADMIN_KEY)
    monkeypatch.setenv("LOOP_CONTROLLER_BUNDLE_TOKEN", "console-redline-bundle-token")
    monkeypatch.setenv("LOOP_CONTROLLER_OPA_STATUS_TOKEN", "console-redline-status-token")
    monkeypatch.setenv("LC_A2A_CONTROL_TOKEN", "console-redline-control-token")

    config = ConfigLoader().load(console_workdir / "config", opa_base_url=opa_server)
    controller = await build_controller(
        config, opa_url=opa_server, env_extra={"PYTHONPATH": str(REPO_ROOT / "src")}
    )
    await controller.start()
    try:
        app = build_app(controller, api_key=ADMIN_KEY, configure_logs=False)
        yield TestClient(app)
    finally:
        await controller.aclose()


def _auth() -> dict[str, str]:
    return {"x-api-key": ADMIN_KEY}


class TestRbacBindingsPage:
    def test_bindings_available_with_enforcement_disabled(
        self, console_client: TestClient
    ) -> None:
        """红线：enforcement=disabled（仓库默认）时绑定管理必须可用（200）。

        回归 v0.55 浏览器实测 503 rbac_unavailable：rbac_store 曾与 enforcement
        耦合，disabled 时 store 为 None。
        """
        response = console_client.get("/v1/admin/rbac/bindings", headers=_auth())
        assert response.status_code == 200, response.text
        response = console_client.get("/v1/admin/rbac/grants", headers=_auth())
        assert response.status_code == 200, response.text

    def test_bindings_require_auth(self, console_client: TestClient) -> None:
        assert console_client.get("/v1/admin/rbac/bindings").status_code == 401


class TestPolicyLifecyclePage:
    def test_candidates_available_with_delivery_enabled(
        self, console_client: TestClient
    ) -> None:
        """红线：policy_delivery 启用装配后候选端点必须可用（200）。

        回归 v0.55 浏览器实测 503 policy_delivery_unavailable：功能从未在真实
        装配下启用过。
        """
        response = console_client.get("/v1/admin/policy/candidates", headers=_auth())
        assert response.status_code == 200, response.text

    def test_status_and_audit_available(self, console_client: TestClient) -> None:
        """策略生命周期页还加载实例加载状态与操作审计。"""
        response = console_client.get("/v1/admin/policy/status", headers=_auth())
        assert response.status_code == 200, response.text
        response = console_client.get("/v1/admin/policy/audit", headers=_auth())
        assert response.status_code == 200, response.text


class TestA2aConsolePage:
    def test_status_degrades_fail_safe_not_502(self, console_client: TestClient) -> None:
        """红线：内核不可达时必须返回 200 + reachable=false（fail-safe 降级）。

        回归 v0.55 浏览器实测 502：带认证内核下 ping() 未携带认证头被判不可达。
        认证头行为由 test_go_kernel_bridge 覆盖，此处锁定降级语义。
        """
        response = console_client.get("/v1/admin/a2a/status", headers=_auth())
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["enabled"] is True
        assert body["reachable"] is False

    def test_agents_view_available(self, console_client: TestClient) -> None:
        response = console_client.get("/v1/admin/a2a/agents", headers=_auth())
        assert response.status_code == 200, response.text


class TestLocalOverride:
    def test_local_yaml_preferred(self, tmp_path: Path) -> None:
        """*.local.yaml 存在时优先，仓库样例保持安全默认值。"""
        base = tmp_path / "go_kernel.yaml"
        local = tmp_path / "go_kernel.local.yaml"
        base.write_text("go_kernel:\n  enabled: false\n", encoding="utf-8")
        assert _ORIGINAL_WITH_LOCAL_OVERRIDE(base) == base
        local.write_text("go_kernel:\n  enabled: true\n", encoding="utf-8")
        assert _ORIGINAL_WITH_LOCAL_OVERRIDE(base) == local
