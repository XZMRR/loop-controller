"""Python bridge to the Go interaction governance kernel (v0.54.0)."""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from loop_controller.go_kernel_bridge import (
    CURRENT_PROTOCOL_VERSION,
    A2AMessage,
    AgentCard,
    AgentEntrypoint,
    DelegationRequest,
    EventCursorExpiredError,
    EventCursorFutureError,
    EventCursorInvalidError,
    EventSequenceGapError,
    EventSequenceOutOfOrderError,
    GoKernelBridge,
    GoKernelProtocolError,
    UnsupportedEventSchemaError,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.asyncio
async def test_readiness_parses_ready_and_not_ready() -> None:
    responses = [
        httpx.Response(
            200,
            json={
                "protocol_version": "0.54.0",
                "schema_version": "p54-09.v1",
                "status": "ready",
                "checks": {},
                "reasons": [],
                "observed_at": "2026-09-14T00:00:00Z",
            },
        ),
        httpx.Response(
            503,
            json={
                "protocol_version": "0.54.0",
                "schema_version": "p54-09.v1",
                "status": "not_ready",
                "checks": {"database": {"status": "failed"}},
                "reasons": ["database_unavailable"],
                "observed_at": "2026-09-14T00:00:00Z",
            },
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ready"
        return responses.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = GoKernelBridge(base_url="http://kernel", client=client)
        assert (await bridge.readiness())["status"] == "ready"
        assert (await bridge.readiness())["reasons"] == ["database_unavailable"]


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
                "verdict": "allow",
                "decision_id": "test-decision",
                "task_id": payload.get("task_id", ""),
                "effective_args": payload.get("arguments", {}),
                "reason": "test IIGE allow",
                "protocol_version": "0.53.0",
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
def kernel_url(tmp_path_factory: pytest.TempPathFactory) -> Generator[str, None, None]:
    """Start the Go kernel as a subprocess for the duration of the module tests."""
    port = _free_port()
    interaction_port = _free_port()
    url = f"http://127.0.0.1:{port}"
    interaction_server = ThreadingHTTPServer(("127.0.0.1", interaction_port), _AllowIIGEHandler)
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
            "-development",
            "-allow-http",
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
        allowed_tools=["query_sales", "send_email"],
        allowed_capabilities=["read_sales"],
        allow_redelegation=True,
    )
    assert req.to_dict()["allowed_tools"] == ["query_sales", "send_email"]
    assert req.to_dict()["allowed_capabilities"] == ["read_sales"]
    assert req.to_dict()["allow_redelegation"] is True
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
async def test_bridge_v054_status_and_polymorphic_response_contracts() -> None:
    statuses: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a2a/v1/agents":
            return httpx.Response(
                201,
                json={
                    "protocol_version": CURRENT_PROTOCOL_VERSION,
                    "agent_id": "agent-b",
                },
            )
        if request.url.path == "/a2a/v1/messages":
            return httpx.Response(
                400,
                json={
                    "protocol_version": CURRENT_PROTOCOL_VERSION,
                    "accepted": False,
                    "reason": "agent not found",
                },
            )
        statuses.append(202)
        return httpx.Response(
            202,
            json={
                "protocol_version": CURRENT_PROTOCOL_VERSION,
                "allowed": False,
                "verdict": "require_approval",
                "approval_id": "approval-1",
                "interaction_id": "interaction-1",
                "task_id": "task-1",
                "reason": "approval required",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = GoKernelBridge(base_url="http://kernel", client=client)
        assert await bridge.register_agent(AgentCard(agent_id="agent-b"))
        assert not await bridge.route_message(
            A2AMessage(
                message_id="message-1",
                task_id="task-1",
                from_agent_id="agent-a",
                to_agent_id="missing",
            )
        )
        delegated = await bridge.request_delegation(
            DelegationRequest(
                request_id="request-1",
                initiator_agent_id="agent-a",
                target_agent_id="agent-b",
                tool_name="read_file",
            )
        )
    assert statuses == [202]
    assert delegated.approval_id == "approval-1"
    assert delegated.interaction_id == "interaction-1"


@pytest.mark.asyncio
async def test_cancel_task_posts_reason_and_returns_task() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/a2a/v1/tasks/task-cancel/cancel"
        assert request.headers["Authorization"] == "Bearer control-token"
        assert json.loads(request.content) == {
            "protocol_version": CURRENT_PROTOCOL_VERSION,
            "reason": "no longer needed",
        }
        return httpx.Response(
            200,
            json={
                "protocol_version": CURRENT_PROTOCOL_VERSION,
                "task_id": "task-cancel",
                "status": "cancelled",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = GoKernelBridge(
            base_url="http://kernel", client=client, control_token="control-token"
        )
        result = await bridge.cancel_task(
            "task-cancel", "no longer needed", delegation_token="delegation-token"
        )

    assert result == {
        "protocol_version": CURRENT_PROTOCOL_VERSION,
        "task_id": "task-cancel",
        "status": "cancelled",
    }


@pytest.mark.asyncio
async def test_cancel_task_returns_none_when_kernel_rejects() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"code": "invalid_status_transition"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = GoKernelBridge(base_url="http://kernel", client=client)
        assert await bridge.cancel_task("task-running") is None


@pytest.mark.asyncio
async def test_request_delegation_keeps_require_approval_approval_id() -> None:
    """202 Accepted 应解析为 require_approval 并保留 approval_id（对账/代审批入口）。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/a2a/v1/delegations"
        return httpx.Response(
            202,
            json={
                "allowed": False,
                "verdict": "require_approval",
                "approval_id": "approval-abc",
                "interaction_id": "ix-1",
                "decision_id": "decision-1",
                "reason": "delegation requires owner approval",
                "protocol_version": CURRENT_PROTOCOL_VERSION,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = GoKernelBridge(base_url="http://kernel", client=client)
        resp = await bridge.request_delegation(
            DelegationRequest(
                request_id="req-1",
                initiator_agent_id="planner",
                target_agent_id="executor",
                tool_name="query_sales",
            )
        )

    assert resp.allowed is False
    assert resp.verdict == "require_approval"
    assert resp.approval_id == "approval-abc"
    assert "go_kernel_rejected" not in resp.reason


@pytest.mark.asyncio
async def test_request_delegation_surfaces_kernel_deny_reason() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"code": "denied", "reason": "delegation requires owner approval"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = GoKernelBridge(base_url="http://kernel", client=client)
        resp = await bridge.request_delegation(
            DelegationRequest(
                request_id="req-2",
                initiator_agent_id="planner",
                target_agent_id="executor",
                tool_name="query_sales",
            )
        )

    assert resp.allowed is False
    assert resp.reason == "delegation requires owner approval"


@pytest.mark.asyncio
async def test_list_delegation_approvals_uses_control_token() -> None:
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization", "")
        assert request.url.path == "/a2a/v1/delegation-approvals"
        assert request.url.params["status"] == "pending"
        return httpx.Response(
            200,
            json={
                "protocol_version": "0.54.0",
                "approvals": [{"approval_id": "a1", "status": "pending"}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = GoKernelBridge(base_url="http://kernel", client=client, token="control-token")
        approvals = await bridge.list_delegation_approvals(status="pending")

    assert captured["auth"] == "Bearer control-token"
    assert approvals == [{"approval_id": "a1", "status": "pending"}]


@pytest.mark.asyncio
async def test_list_tasks_strictly_validates_tasks_array() -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization", "")
        captured["root_only"] = request.url.params.get("root_only")
        return httpx.Response(
            200,
            json={
                "protocol_version": CURRENT_PROTOCOL_VERSION,
                "tasks": [{"task_id": "task-1", "tenant_id": "tenant-a"}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = GoKernelBridge(base_url="http://kernel", client=client, token="control-token")
        tasks = await bridge.list_tasks(root_only=True)

    assert captured == {"auth": "Bearer control-token", "root_only": "true"}
    assert tasks == [{"task_id": "task-1", "tenant_id": "tenant-a"}]

    for invalid in (None, {}, ["not-an-object"]):
        async def invalid_handler(
            request: httpx.Request, payload: Any = invalid
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={"protocol_version": CURRENT_PROTOCOL_VERSION, "tasks": payload},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(invalid_handler)) as client:
            bridge = GoKernelBridge(base_url="http://kernel", client=client)
            with pytest.raises(GoKernelProtocolError):
                await bridge.list_tasks()


@pytest.mark.asyncio
async def test_query_task_sends_control_token() -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization", "")
        assert request.url.path == "/a2a/v1/tasks/task-1"
        return httpx.Response(
            200,
            json={
                "protocol_version": "0.54.0",
                "task_id": "task-1",
                "status": "accepted",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = GoKernelBridge(base_url="http://kernel", client=client, token="control-token")
        task = await bridge.query_task("task-1")

    assert captured["auth"] == "Bearer control-token"
    assert task is not None
    assert task["status"] == "accepted"


@pytest.mark.asyncio
async def test_approve_delegation_posts_with_approver_token() -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization", "")
        captured["body"] = json.loads(request.content)
        assert request.url.path == "/a2a/v1/delegation-approvals/approval-1/approve"
        return httpx.Response(
            200,
            json={
                "protocol_version": "0.54.0",
                "approval_id": "approval-1",
                "request_id": "req-1",
                "decision_id": "decision-1",
                "request_hash": "h",
                "initiator_agent_id": "planner",
                "target_agent_id": "executor",
                "status": "consumed",
                "task_id": "task-9",
                "expires_at": "2026-09-15T16:00:00Z",
                "created_at": "2026-09-15T15:00:00Z",
                "updated_at": "2026-09-15T15:05:00Z",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = GoKernelBridge(
            base_url="http://kernel", client=client, approval_token="approver-token"
        )
        consumed = await bridge.approve_delegation("approval-1", request_id="req-1", reason="ok")

    assert captured["auth"] == "Bearer approver-token"
    assert captured["body"] == {"request_id": "req-1", "reason": "ok"}
    assert consumed is not None
    assert consumed.status == "consumed"
    assert consumed.task_id == "task-9"


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


def _event(sequence: int, event_id: str, **extra: object) -> dict[str, object]:
    return {
        "protocol_version": CURRENT_PROTOCOL_VERSION,
        "schema_version": 1,
        "sequence": sequence,
        "event_id": event_id,
        "task_id": "task-stream",
        "event_type": "task_updated",
        "payload": {},
        "published_at": "2026-09-14T00:00:00Z",
        **extra,
    }


@pytest.mark.asyncio
async def test_stream_parses_multiline_data_heartbeat_retry_and_unknown_fields() -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        body = (
            ": heartbeat\n"
            "retry: 1\n"
            "unknown: ignored\n\n"
            "id: opaque:cursor/one\n"
            "event: task_updated\n"
            'data: {"schema_version":1,"sequence":1,"event_id":"ev-1",\n'
            'data: "task_id":"task-stream"}\n\n'
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = GoKernelBridge(base_url="https://kernel", client=client)
        stream = bridge.stream_task("task-stream", cursor="previous:opaque", include_sse=True)
        assert await anext(stream) == (
            {
                "schema_version": 1,
                "sequence": 1,
                "event_id": "ev-1",
                "task_id": "task-stream",
            },
            "opaque:cursor/one",
            "task_updated",
        )
        await stream.aclose()
    assert requests == 1


@pytest.mark.asyncio
async def test_stream_reconnects_with_cursor_and_deduplicates_replay() -> None:
    headers: list[httpx.Headers] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers)
        if len(headers) == 1:
            body = f"id: cursor-one\ndata: {json.dumps(_event(1, 'ev-1'))}\n\n"
        else:
            body = (
                f"id: cursor-one\ndata: {json.dumps(_event(1, 'ev-1'))}\n\n"
                f"id: cursor-two\ndata: {json.dumps(_event(2, 'ev-2'))}\n\n"
            )
        return httpx.Response(200, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = GoKernelBridge(
            base_url="https://kernel",
            client=client,
            headers={"X-Control-Tenant": "tenant-a"},
            control_token="secret",
            min_reconnect_delay=0.001,
            max_reconnect_delay=0.002,
        )
        stream = bridge.stream_task("task-stream")
        assert (await anext(stream))["event_id"] == "ev-1"
        assert (await asyncio.wait_for(anext(stream), 0.1))["event_id"] == "ev-2"
        await stream.aclose()

    assert headers[0].get("Last-Event-ID") is None
    assert headers[1]["Last-Event-ID"] == "cursor-one"
    assert all(h["Authorization"] == "Bearer secret" for h in headers)
    assert all(h["X-Control-Tenant"] == "tenant-a" for h in headers)


@pytest.mark.asyncio
async def test_stream_reconnects_after_initial_service_unavailable() -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            text=f"id: cursor-one\ndata: {json.dumps(_event(1, 'ev-1'))}\n\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = GoKernelBridge(
            base_url="http://kernel",
            client=client,
            min_reconnect_delay=0.001,
            max_reconnect_delay=0.001,
        )
        stream = bridge.stream_task("task-stream")
        assert (await asyncio.wait_for(anext(stream), 0.1))["event_id"] == "ev-1"
        await stream.aclose()

    assert requests == 2


@pytest.mark.asyncio
async def test_stream_preserves_cursor_across_service_unavailable() -> None:
    headers: list[httpx.Headers] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers)
        if len(headers) == 1:
            return httpx.Response(
                200,
                text=f"id: cursor-one\ndata: {json.dumps(_event(1, 'ev-1'))}\n\n",
            )
        if len(headers) == 2:
            return httpx.Response(503)
        return httpx.Response(
            200,
            text=f"id: cursor-two\ndata: {json.dumps(_event(2, 'ev-2'))}\n\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = GoKernelBridge(
            base_url="http://kernel",
            client=client,
            min_reconnect_delay=0.001,
            max_reconnect_delay=0.001,
        )
        stream = bridge.stream_task("task-stream")
        assert (await anext(stream))["event_id"] == "ev-1"
        assert (await asyncio.wait_for(anext(stream), 0.1))["event_id"] == "ev-2"
        await stream.aclose()

    assert len(headers) == 3
    assert headers[1]["Last-Event-ID"] == "cursor-one"
    assert headers[2]["Last-Event-ID"] == "cursor-one"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
async def test_stream_does_not_reconnect_after_non_retryable_status(status: int) -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        stream = GoKernelBridge(base_url="http://kernel", client=client).stream_task("task-stream")
        with pytest.raises(httpx.HTTPStatusError):
            await anext(stream)

    assert requests == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "error"),
    [
        ([_event(1, "ev-1"), _event(3, "ev-3")], EventSequenceGapError),
        ([_event(2, "ev-2"), _event(1, "ev-1")], EventSequenceOutOfOrderError),
        ([_event(1, "ev-1", schema_version=2)], UnsupportedEventSchemaError),
    ],
)
async def test_stream_rejects_invalid_sequence_and_schema(
    events: list[dict[str, object]], error: type[Exception]
) -> None:
    body = "".join(
        f"id: cursor-{i}\ndata: {json.dumps(event)}\n\n" for i, event in enumerate(events)
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    ) as client:
        stream = GoKernelBridge(base_url="http://kernel", client=client).stream_task("task-stream")
        if len(events) > 1:
            await anext(stream)
        with pytest.raises(error):
            await anext(stream)
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "error"),
    [
        (410, "event_cursor_expired", EventCursorExpiredError),
        (400, "event_cursor_invalid", EventCursorInvalidError),
        (400, "event_cursor_future", EventCursorFutureError),
    ],
)
async def test_cursor_errors_are_stable_and_not_retried(
    status: int, code: str, error: type[Exception]
) -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(status, json={"code": code, "error": code})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        stream = GoKernelBridge(base_url="http://kernel", client=client).stream_task("task-stream")
        with pytest.raises(error):
            await anext(stream)
    assert requests == 1


@pytest.mark.asyncio
async def test_invalid_json_does_not_advance_cursor_and_aclose_stops_reconnect() -> None:
    headers: list[httpx.Headers] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers)
        if len(headers) == 1:
            return httpx.Response(200, text="id: bad-cursor\ndata: {invalid\n\n")
        return httpx.Response(200, text=f"id: good\ndata: {json.dumps(_event(1, 'ev-1'))}\n\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = GoKernelBridge(
            base_url="http://kernel",
            client=client,
            min_reconnect_delay=0.001,
            max_reconnect_delay=0.001,
        )
        stream = bridge.stream_task("task-stream")
        assert (await asyncio.wait_for(anext(stream), 0.1))["event_id"] == "ev-1"
        assert headers[1].get("Last-Event-ID") is None
        await stream.aclose()
        await asyncio.sleep(0.005)
    assert len(headers) == 2
