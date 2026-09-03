"""Python bridge to the Go interaction governance kernel (v0.39.0)."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from loop_controller.go_kernel_bridge import (
    A2AMessage,
    AgentCard,
    AgentEntrypoint,
    DelegationRequest,
    GoKernelBridge,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _go_bin() -> str:
    """Return the 'go' executable or raise if unavailable."""
    found = shutil.which("go")
    if found:
        return found
    raise RuntimeError("go executable not found in PATH")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _AllowIIGEHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        body = json.dumps(
            {
                "allowed": True,
                "decision_id": "test-decision",
                "task_id": payload.get("task_id", ""),
                "reason": "test IIGE allow",
                "protocol_version": "0.39.0",
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def kernel_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Start the Go kernel as a subprocess for the duration of the module tests."""
    port = _free_port()
    interaction_port = _free_port()
    url = f"http://127.0.0.1:{port}"
    interaction_server = ThreadingHTTPServer(
        ("127.0.0.1", interaction_port), _AllowIIGEHandler
    )
    interaction_thread = threading.Thread(target=interaction_server.serve_forever, daemon=True)
    interaction_thread.start()
    go_root = REPO_ROOT / "go"
    db_path = tmp_path_factory.mktemp("go-kernel") / "a2a.db"
    proc = subprocess.Popen(
        [
            _go_bin(),
            "run",
            "./cmd/kernel",
            "-addr",
            f":{port}",
            "-db",
            str(db_path),
            "-interaction-url",
            f"http://127.0.0.1:{interaction_port}",
        ],
        cwd=go_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    deadline = time.perf_counter() + 15.0
    while time.perf_counter() < deadline:
        try:
            resp = httpx.get(f"{url}/health", timeout=1.0)
            if resp.status_code == 200:
                break
        except httpx.RequestError:
            pass
        time.sleep(0.2)
    else:
        proc.terminate()
        proc.wait(timeout=5.0)
        raise RuntimeError("Go kernel did not start in time")

    yield url

    proc.terminate()
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5.0)
    interaction_server.shutdown()
    interaction_server.server_close()
    interaction_thread.join(timeout=2.0)


@pytest.fixture
def bridge(kernel_url: str) -> GoKernelBridge:
    return GoKernelBridge(base_url=kernel_url)


@pytest.mark.asyncio
async def test_register_agent_and_request_delegation(bridge: GoKernelBridge) -> None:
    card = AgentCard(
        agent_id="executor-agent",
        name="Executor Agent",
        entrypoint=AgentEntrypoint("http", "http://executor-agent:8080"),
        capabilities=["delegate_execution"],
    )
    assert await bridge.register_agent(card)

    req = DelegationRequest(
        request_id="req-1",
        initiator_agent_id="planner-agent",
        target_agent_id="executor-agent",
        tool_name="query_sales",
        arguments={"month": "2026-08"},
        session_id="session-1",
        risk_level="critical",
    )
    resp = await bridge.request_delegation(req)
    assert resp.allowed
    assert resp.task_id
    assert resp.target_entrypoint is not None
    assert resp.target_entrypoint.url == "http://executor-agent:8080"


@pytest.mark.asyncio
async def test_request_delegation_fail_closed_for_unregistered_target(
    bridge: GoKernelBridge,
) -> None:
    req = DelegationRequest(
        request_id="req-2",
        initiator_agent_id="planner-agent",
        target_agent_id="unknown-agent",
        tool_name="query_sales",
    )
    resp = await bridge.request_delegation(req)
    assert not resp.allowed


@pytest.mark.asyncio
async def test_bridge_fail_closed_when_kernel_unreachable() -> None:
    bridge = GoKernelBridge(base_url="http://127.0.0.1:1", timeout=0.5)
    req = DelegationRequest(
        request_id="req-3",
        initiator_agent_id="planner-agent",
        target_agent_id="executor-agent",
        tool_name="query_sales",
    )
    resp = await bridge.request_delegation(req)
    assert not resp.allowed
    assert "unreachable" in resp.reason


@pytest.mark.asyncio
async def test_route_message(bridge: GoKernelBridge) -> None:
    card = AgentCard(
        agent_id="receiver-agent",
        name="Receiver",
    )
    await bridge.register_agent(card)

    msg = A2AMessage(
        message_id="msg-1",
        task_id="task-1",
        from_agent_id="sender-agent",
        to_agent_id="receiver-agent",
        parts=[{"type": "text", "text": "hello"}],
    )
    assert await bridge.route_message(msg)


@pytest.mark.asyncio
async def test_query_task(bridge: GoKernelBridge) -> None:
    card = AgentCard(
        agent_id="executor-agent",
        name="Executor Agent",
        entrypoint=AgentEntrypoint("http", "http://executor-agent:8080"),
        capabilities=["delegate_execution"],
    )
    await bridge.register_agent(card)

    req = DelegationRequest(
        request_id="req-4",
        initiator_agent_id="planner-agent",
        target_agent_id="executor-agent",
        tool_name="query_sales",
        session_id="session-query",
    )
    resp = await bridge.request_delegation(req)
    assert resp.allowed

    task = await bridge.query_task(resp.task_id)
    assert task is not None
    assert task["status"] == "pending"
    assert task["session_id"] == "session-query"
