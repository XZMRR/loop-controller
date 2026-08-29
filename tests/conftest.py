"""pytest 共享 fixture：会话级 OPA server.

启动仓库 ``policies/`` 目录下的真实策略（--bundle），供各测试模块复用。
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICIES_DIR = REPO_ROOT / "policies"


def resolve_opa_bin(repo_root: Path = REPO_ROOT) -> Path:
    """按 OPA_PATH 环境变量 -> tools/opa -> tools/opa.exe 顺序解析 OPA 可执行文件路径。"""
    env = os.environ.get("OPA_PATH")
    if env:
        return Path(env)
    for name in ("opa", "opa.exe"):
        candidate = repo_root / "tools" / name
        if candidate.exists():
            return candidate
    return repo_root / "tools" / "opa"


def write_trusted_local_harness_config(
    config_dir: Path | str,
    trusted_local_tools: list[str] | None = None,
) -> None:
    """为测试临时配置写入默认 trusted_local 的 harness 执行策略。

    v0.31.0 默认策略为 harness_required；测试若未配置 Harness backend，
    需显式声明 default_mode: trusted_local 并列出允许本地执行的工具。
    """
    config_path = Path(config_dir)
    tools = trusted_local_tools or []
    lines = [
        "execution:",
        "  default_mode: trusted_local",
    ]
    if tools:
        lines.append("trusted_local_tools:")
        lines.extend(f"  - {tool}" for tool in tools)
    (config_path / "harness_tools.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")

# P0 HMAC：为全部测试自动注入一个 32 字节测试 key，避免默认 hmac-sha256 模式启动失败。
# 该 key 仅用于测试，不进入任何日志/审计内容。
TEST_AUDIT_HMAC_KEY = "a" * 64  # 64 hex chars = 32 bytes


@pytest.fixture(autouse=True)
def _set_default_audit_hmac_key(monkeypatch):
    monkeypatch.setenv("LOOP_CONTROLLER_AUDIT_HMAC_KEY", TEST_AUDIT_HMAC_KEY)


@pytest.fixture(scope="session")
def opa_server() -> str:
    """启动一个加载仓库 policies/ 的 OPA server，返回 base_url。"""
    opa_bin = resolve_opa_bin()
    if not opa_bin.exists():
        pytest.skip("OPA 未安装，跳过依赖 OPA 的用例")

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [str(opa_bin), "run", "--server", "--bundle", str(POLICIES_DIR), "--addr", f"127.0.0.1:{port}"],
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
