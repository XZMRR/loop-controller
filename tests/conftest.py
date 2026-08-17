"""pytest 共享 fixture：会话级 OPA server.

启动仓库 ``policies/`` 目录下的真实策略（--bundle），供各测试模块复用。
"""

from __future__ import annotations

from pathlib import Path
import socket
import subprocess
import time

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
OPA_BIN = REPO_ROOT / "tools" / "opa.exe"
POLICIES_DIR = REPO_ROOT / "policies"


@pytest.fixture(scope="session")
def opa_server() -> str:
    """启动一个加载仓库 policies/ 的 OPA server，返回 base_url。"""
    if not OPA_BIN.exists():
        pytest.skip("OPA 未安装，跳过依赖 OPA 的用例")

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [str(OPA_BIN), "run", "--server", "--bundle", str(POLICIES_DIR), "--addr", f"127.0.0.1:{port}"],
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
