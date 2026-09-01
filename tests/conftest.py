"""pytest 共享 fixture：会话级 OPA server.

启动仓库 ``policies/`` 目录下的真实策略（--bundle），供各测试模块复用。
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

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
        lines.append("  trusted_local_tools:")
        lines.extend(f"    - {tool}" for tool in tools)
    (config_path / "harness_tools.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")

# P0 HMAC：为全部测试自动注入一个 32 字节测试 key，避免默认 hmac-sha256 模式启动失败。
# 该 key 仅用于测试，不进入任何日志/审计内容。
TEST_AUDIT_HMAC_KEY = "a" * 64  # 64 hex chars = 32 bytes


@pytest.fixture(autouse=True)
def _set_default_audit_hmac_key(monkeypatch):
    monkeypatch.setenv("LOOP_CONTROLLER_AUDIT_HMAC_KEY", TEST_AUDIT_HMAC_KEY)


def _terminate_proc(proc: subprocess.Popen[Any]) -> None:
    """温和终止 OPA 子进程，必要时强制 kill。"""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _find_opa_listening_port(proc: subprocess.Popen[Any]) -> int | None:
    """通过 psutil 探测 OPA 子进程实际绑定的 TCP 监听端口。

    OPA 启动时使用 ``--addr 127.0.0.1:0``，由操作系统分配端口；
    这里从子进程的网络连接表中读取实际端口，避免预先分配端口再
    启动 OPA 之间的 TOCTOU 窗口。
    """
    try:
        import psutil
    except ImportError:
        return None
    try:
        p = psutil.Process(proc.pid)
    except psutil.NoSuchProcess:
        return None
    for conn in p.connections(kind="inet"):
        if conn.status != psutil.CONN_LISTEN:
            continue
        laddr = conn.laddr
        if laddr.ip in ("127.0.0.1", "::1", "::"):
            return int(laddr.port)
    return None


def _start_opa(
    opa_bin: Path,
    policy_dir: Path = POLICIES_DIR,
    max_attempts: int = 5,
) -> tuple[subprocess.Popen[Any], str]:
    """尝试启动 OPA，自动处理端口探测与二进制无效等启动失败。"""
    for attempt in range(max_attempts):
        try:
            proc = subprocess.Popen(
                [
                    str(opa_bin),
                    "run",
                    "--server",
                    "--bundle",
                    str(policy_dir),
                    "--addr",
                    "127.0.0.1:0",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, PermissionError) as exc:
            logger = logging.getLogger(__name__)
            logger.warning("OPA 二进制无法启动: %s", exc)
            pytest.skip("OPA 二进制无法启动，跳过依赖 OPA 的用例")

        # 探测 OPA 实际绑定的端口（TOCTOU-free：端口由 OPA 自己申请）
        port: int | None = None
        port_deadline = time.time() + 20
        while time.time() < port_deadline and port is None:
            if proc.poll() is not None:
                break
            port = _find_opa_listening_port(proc)
            if port is None:
                time.sleep(0.1)

        if port is None:
            _terminate_proc(proc)
            if attempt < max_attempts - 1:
                time.sleep(0.5)
            continue

        base_url = f"http://127.0.0.1:{port}"
        ready = False
        health_deadline = time.time() + 20
        while time.time() < health_deadline:
            if proc.poll() is not None:
                break
            try:
                if httpx.get(f"{base_url}/health", timeout=1, trust_env=False).status_code == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.3)

        if ready:
            return proc, base_url

        _terminate_proc(proc)
        if attempt < max_attempts - 1:
            time.sleep(0.5)

    pytest.skip("OPA 无法启动，跳过依赖 OPA 的用例")


@pytest.fixture(scope="session")
def opa_server() -> str:
    """启动一个加载仓库 policies/ 的 OPA server，返回 base_url。"""
    opa_bin = resolve_opa_bin()
    if not opa_bin.exists():
        pytest.skip("OPA 未安装，跳过依赖 OPA 的用例")

    proc, base_url = _start_opa(opa_bin)
    yield base_url
    _terminate_proc(proc)
