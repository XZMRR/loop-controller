"""热更新测试（v0.22.0）。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
import yaml

from loop_controller.executors.http_client import HTTPClient
from loop_controller.executors.http_executor import HTTPExecutor
from loop_controller.infra.config_loader import ConfigLoader
from loop_controller.infra.hot_reload import HotReloader
from loop_controller.secrets import FileSecretBackend


@pytest.fixture
def reload_setup(tmp_path: Path) -> tuple[Path, HTTPExecutor, FileSecretBackend, HotReloader, set[str]]:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "global").mkdir()

    # 初始 http_tools.yaml
    http_tools = config_dir / "http_tools.yaml"
    http_tools.write_text(
        yaml.safe_dump(
            {
                "tools": {
                    "jira": {
                        "base_url": "https://api.example.com",
                        "method": "POST",
                        "path": "/issues",
                        "body_template": {"title": "{title}"},
                        "allowed_hosts": ["api.example.com"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    # 初始 secret
    secret_file = secrets_dir / "global" / "api_key.json"
    secret_file.write_text('{"value": "v1"}', encoding="utf-8")

    secrets_yaml = config_dir / "secrets.yaml"
    secrets_yaml.write_text(
        yaml.safe_dump(
            {
                "backend": {"type": "file", "base_path": str(secrets_dir)},
                "hot_reload": {"enabled": True, "poll_interval_seconds": 0.1},
            }
        ),
        encoding="utf-8",
    )

    loader = ConfigLoader()
    specs = loader.reload_http_tools(config_dir)
    broker = FileSecretBackend(secrets_dir)
    client = HTTPClient()
    executor = HTTPExecutor(client, specs, secret_broker=broker)
    http_tool_names = set(specs.keys())

    reloader = HotReloader(
        config_dir=config_dir,
        config_loader=loader,
        http_executor=executor,
        secret_broker=broker,
        http_tool_names=http_tool_names,
        poll_interval_seconds=0.1,
        enabled=True,
    )
    return config_dir, executor, broker, reloader, http_tool_names


@pytest.mark.asyncio
async def test_hot_reload_updates_http_tool_spec(
    reload_setup: tuple[Path, HTTPExecutor, FileSecretBackend, HotReloader, set[str]],
) -> None:
    config_dir, executor, _broker, reloader, _names = reload_setup
    await reloader.start()
    try:
        assert "jira" in executor._tool_specs
        assert executor._tool_specs["jira"].path == "/issues"

        new_yaml = {
            "tools": {
                "jira": {
                    "base_url": "https://api.example.com",
                    "method": "GET",
                    "path": "/tickets",
                    "allowed_hosts": ["api.example.com"],
                }
            }
        }
        (config_dir / "http_tools.yaml").write_text(
            yaml.safe_dump(new_yaml), encoding="utf-8"
        )

        # 轮询间隔 0.1s，最多等 1s
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if executor._tool_specs["jira"].path == "/tickets":
                break
            await asyncio.sleep(0.05)
        assert executor._tool_specs["jira"].path == "/tickets"
    finally:
        await reloader.stop()


@pytest.mark.asyncio
async def test_hot_reload_reloads_secret(
    reload_setup: tuple[Path, HTTPExecutor, FileSecretBackend, HotReloader, set[str]],
) -> None:
    config_dir, _executor, broker, reloader, _names = reload_setup
    await reloader.start()
    try:
        from loop_controller.secrets import SecretRef

        assert (await broker.get(SecretRef(name="api_key"))).value == "v1"

        secret_file = Path(broker._base) / "global" / "api_key.json"
        secret_file.write_text('{"value": "v2"}', encoding="utf-8")

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            value = await broker.get(SecretRef(name="api_key"))
            if value is not None and value.value == "v2":
                break
            await asyncio.sleep(0.05)
        assert (await broker.get(SecretRef(name="api_key"))).value == "v2"
    finally:
        await reloader.stop()


@pytest.mark.asyncio
async def test_hot_reload_failure_keeps_old_config(
    reload_setup: tuple[Path, HTTPExecutor, FileSecretBackend, HotReloader, set[str]],
) -> None:
    config_dir, executor, _broker, reloader, names = reload_setup
    await reloader.start()
    try:
        original_path = executor._tool_specs["jira"].path
        assert "jira" in names

        # 写入非法 YAML
        (config_dir / "http_tools.yaml").write_text("tools: [", encoding="utf-8")

        await asyncio.sleep(0.3)
        assert executor._tool_specs["jira"].path == original_path
        assert "jira" in names
    finally:
        await reloader.stop()


@pytest.mark.asyncio
async def test_hot_reload_syncs_http_tool_names(
    reload_setup: tuple[Path, HTTPExecutor, FileSecretBackend, HotReloader, set[str]],
) -> None:
    """HTTP 工具热更新后，共享的 http_tool_names 集合应同步更新。"""
    config_dir, executor, _broker, reloader, names = reload_setup
    await reloader.start()
    try:
        assert "jira" in names
        assert "confluence" not in names

        new_yaml = {
            "tools": {
                "confluence": {
                    "base_url": "https://api.example.com",
                    "method": "GET",
                    "path": "/wiki",
                    "allowed_hosts": ["api.example.com"],
                }
            }
        }
        (config_dir / "http_tools.yaml").write_text(
            yaml.safe_dump(new_yaml), encoding="utf-8"
        )

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if "confluence" in names and "jira" not in names:
                break
            await asyncio.sleep(0.05)
        assert "confluence" in names
        assert "jira" not in names
    finally:
        await reloader.stop()
