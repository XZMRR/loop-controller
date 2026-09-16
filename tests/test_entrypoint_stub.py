"""Entrypoint stub（任务执行闭环 + 取消）测试。

覆盖：投递接收与 token 校验、create/accept/start 回调链路、回调失败重试
语义、重复投递幂等、取消通知（token 校验/状态机/幂等/重投防御）。
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
from starlette.testclient import TestClient

from loop_controller.entrypoint_stub import EntrypointStub, create_app

TASK_PAYLOAD = {
    "protocol_version": "0.48.0",
    "task_id": "task-1",
    "session_id": "s-1",
    "initiator_agent_id": "planner",
    "target_agent_id": "research-agent",
    "tool_name": "analyze_sales",
    "arguments": {"region": "APAC"},
    "delegation_token": "delegation-token-1",
}


def _recording_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _kernel_handler(calls: list[str], *, create_code: int = 201, accept_code: int = 200, start_code: int = 200):
    """模拟内核 entrypoint 端点的 recording handler。"""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/a2a/v1/entrypoint/tasks":
            return httpx.Response(create_code, json={"status": "pending"})
        if request.url.path == "/a2a/v1/entrypoint/tasks/task-1/accept":
            assert request.headers["Authorization"] == "Bearer delegation-token-1"
            return httpx.Response(accept_code, json={"status": "accepted"})
        if request.url.path == "/a2a/v1/entrypoint/tasks/task-1/start":
            return httpx.Response(start_code, json={"status": "running"})
        if request.url.path == "/a2a/v1/entrypoint/tasks/task-1/results":
            return httpx.Response(200, json={"status": "recorded"})
        return httpx.Response(404)

    return handler


def test_create_drives_create_accept_start_callbacks() -> None:
    calls: list[str] = []
    stub = EntrypointStub("http://kernel:8080", client=_recording_client(_kernel_handler(calls)))
    status, body = stub.handle_create(dict(TASK_PAYLOAD), "delegation-token-1")

    assert status == 201
    assert body["task_id"] == "task-1"
    assert calls == [
        "/a2a/v1/entrypoint/tasks",
        "/a2a/v1/entrypoint/tasks/task-1/accept",
        "/a2a/v1/entrypoint/tasks/task-1/start",
    ]
    assert "task-1" in stub.driven_tasks


def test_create_rejects_token_mismatch() -> None:
    stub = EntrypointStub("http://kernel:8080", client=_recording_client(lambda r: httpx.Response(500)))
    status, body = stub.handle_create(dict(TASK_PAYLOAD), "wrong-token")
    assert status == 403
    assert body["error"] == "token_scope_mismatch"


def test_create_callback_conflict_is_treated_as_registered() -> None:
    calls: list[str] = []
    stub = EntrypointStub(
        "http://kernel:8080",
        client=_recording_client(_kernel_handler(calls, create_code=409)),
    )
    status, _ = stub.handle_create(dict(TASK_PAYLOAD), "delegation-token-1")

    assert status == 201
    # create 409（任务已被接受/启动）后仍继续 accept/start
    assert calls == [
        "/a2a/v1/entrypoint/tasks",
        "/a2a/v1/entrypoint/tasks/task-1/accept",
        "/a2a/v1/entrypoint/tasks/task-1/start",
    ]


def test_accept_conflict_is_treated_as_already_accepted() -> None:
    calls: list[str] = []
    stub = EntrypointStub(
        "http://kernel:8080",
        client=_recording_client(_kernel_handler(calls, accept_code=409)),
    )
    status, _ = stub.handle_create(dict(TASK_PAYLOAD), "delegation-token-1")

    assert status == 201
    assert calls == [
        "/a2a/v1/entrypoint/tasks",
        "/a2a/v1/entrypoint/tasks/task-1/accept",
        "/a2a/v1/entrypoint/tasks/task-1/start",
    ]


def test_create_callback_failure_returns_502_without_marking_driven() -> None:
    calls: list[str] = []
    stub = EntrypointStub(
        "http://kernel:8080",
        client=_recording_client(_kernel_handler(calls, create_code=500)),
    )
    status, body = stub.handle_create(dict(TASK_PAYLOAD), "delegation-token-1")

    assert status == 502
    assert body["error"] == "create_callback_failed"
    assert "task-1" not in stub.driven_tasks


def test_start_failure_returns_502_without_marking_driven() -> None:
    calls: list[str] = []
    stub = EntrypointStub(
        "http://kernel:8080",
        client=_recording_client(_kernel_handler(calls, start_code=503)),
    )
    status, body = stub.handle_create(dict(TASK_PAYLOAD), "delegation-token-1")

    assert status == 502
    assert body["error"] == "start_callback_failed"
    # 未标记已驱动：内核 dispatcher 退避重投后会重试 create/accept/start
    assert "task-1" not in stub.driven_tasks


def test_duplicate_create_is_idempotent() -> None:
    calls: list[str] = []
    stub = EntrypointStub("http://kernel:8080", client=_recording_client(_kernel_handler(calls)))
    first_status, _ = stub.handle_create(dict(TASK_PAYLOAD), "delegation-token-1")
    second_status, second_body = stub.handle_create(dict(TASK_PAYLOAD), "delegation-token-1")

    assert first_status == 201
    assert second_status == 200
    assert second_body["idempotent"] is True
    assert len(calls) == 3  # create + accept + start 只发生一次


def test_no_auto_start_skips_start_callback() -> None:
    calls: list[str] = []
    stub = EntrypointStub(
        "http://kernel:8080",
        auto_accept=True,
        auto_start=False,
        client=_recording_client(_kernel_handler(calls)),
    )
    status, _ = stub.handle_create(dict(TASK_PAYLOAD), "delegation-token-1")

    assert status == 201
    assert calls == [
        "/a2a/v1/entrypoint/tasks",
        "/a2a/v1/entrypoint/tasks/task-1/accept",
    ]


def test_app_routes_end_to_end() -> None:
    calls: list[str] = []
    app = create_app("http://kernel:8080", client=_recording_client(_kernel_handler(calls)))
    client = TestClient(app)

    resp = client.post(
        "/a2a/v1/entrypoint/tasks",
        json=TASK_PAYLOAD,
        headers={"Authorization": "Bearer delegation-token-1"},
    )
    assert resp.status_code == 201

    # 重复投递（内核 dispatcher 重试）幂等
    retry = client.post(
        "/a2a/v1/entrypoint/tasks",
        json=TASK_PAYLOAD,
        headers={"Authorization": "Bearer delegation-token-1"},
    )
    assert retry.status_code == 200
    assert retry.json()["idempotent"] is True

    bad = client.post(
        "/a2a/v1/entrypoint/tasks",
        json=TASK_PAYLOAD,
        headers={"Authorization": "Bearer other"},
    )
    assert bad.status_code == 403

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["driven_tasks"] == ["task-1"]

    assert calls == [
        "/a2a/v1/entrypoint/tasks",
        "/a2a/v1/entrypoint/tasks/task-1/accept",
        "/a2a/v1/entrypoint/tasks/task-1/start",
    ]


def test_app_rejects_invalid_json() -> None:
    app = create_app("http://kernel:8080")
    client = TestClient(app)
    resp = client.post(
        "/a2a/v1/entrypoint/tasks",
        content=b"{not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert json.loads(resp.content)["error"] == "invalid_json"


def _driven_stub(calls: list[str] | None = None) -> EntrypointStub:
    stub = EntrypointStub(
        "http://kernel:8080",
        client=_recording_client(_kernel_handler(calls if calls is not None else [])),
    )
    status, _ = stub.handle_create(dict(TASK_PAYLOAD), "delegation-token-1")
    assert status == 201
    return stub


def test_cancel_marks_task_cancelled_and_undriven() -> None:
    stub = _driven_stub()
    status, body = stub.handle_cancel("task-1", "delegation-token-1")

    assert status == 200
    assert body == {"task_id": "task-1", "status": "cancelled"}
    assert stub.task_status["task-1"] == "cancelled"
    assert "task-1" not in stub.driven_tasks


def test_cancel_rejects_token_mismatch_and_unknown_task() -> None:
    stub = _driven_stub()
    status, body = stub.handle_cancel("task-1", "wrong-token")
    assert status == 403
    assert body["error"] == "token_scope_mismatch"

    status, _ = stub.handle_cancel("task-unknown", "delegation-token-1")
    assert status == 403


def test_cancel_is_idempotent_after_terminal() -> None:
    stub = _driven_stub()
    first_status, first_body = stub.handle_cancel("task-1", "delegation-token-1")
    second_status, second_body = stub.handle_cancel("task-1", "delegation-token-1")

    assert first_status == 200
    assert first_body["status"] == "cancelled"
    # 已终态且 token 已清除：第二次取消无法通过 token 校验（与未登记一致）
    assert second_status == 403


def test_create_redispatch_after_cancel_is_not_redriven() -> None:
    calls: list[str] = []
    stub = _driven_stub(calls)
    calls.clear()
    stub.handle_cancel("task-1", "delegation-token-1")

    status, body = stub.handle_create(dict(TASK_PAYLOAD), "delegation-token-1")

    assert status == 200
    assert body["status"] == "cancelled"
    assert body["idempotent"] is True
    assert calls == []  # 不再回调 accept/start
    assert "task-1" not in stub.driven_tasks


def test_cancel_partially_accepted_task_without_start() -> None:
    calls: list[str] = []
    stub = EntrypointStub(
        "http://kernel:8080",
        auto_accept=True,
        auto_start=False,
        client=_recording_client(_kernel_handler(calls)),
    )
    status, _ = stub.handle_create(dict(TASK_PAYLOAD), "delegation-token-1")
    assert status == 201
    assert stub.task_status["task-1"] == "accepted"

    cancel_status, body = stub.handle_cancel("task-1", "delegation-token-1")
    assert cancel_status == 200
    assert body["status"] == "cancelled"


def test_app_cancel_route_end_to_end() -> None:
    calls: list[str] = []
    app = create_app("http://kernel:8080", client=_recording_client(_kernel_handler(calls)))
    client = TestClient(app)

    resp = client.post(
        "/a2a/v1/entrypoint/tasks",
        json=TASK_PAYLOAD,
        headers={"Authorization": "Bearer delegation-token-1"},
    )
    assert resp.status_code == 201
    assert client.get("/health").json()["task_status"] == {"task-1": "running"}

    cancel = client.post(
        "/a2a/v1/entrypoint/tasks/task-1/cancel",
        json={"protocol_version": "0.48.0"},
        headers={"Authorization": "Bearer delegation-token-1"},
    )
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelled"

    bad = client.post(
        "/a2a/v1/entrypoint/tasks/task-1/cancel",
        json={"protocol_version": "0.48.0"},
        headers={"Authorization": "Bearer other"},
    )
    assert bad.status_code == 403

    health = client.get("/health").json()
    assert health["task_status"] == {"task-1": "cancelled"}
    assert health["driven_tasks"] == []


def _remote_payload() -> dict[str, Any]:
    payload = dict(TASK_PAYLOAD)
    payload["execution_mode"] = "remote_results"
    return payload


def _remote_client(tool_status: str = "allow", tool_result: Any = None, tool_error: str = "") -> httpx.Client:
    """内核回调打桩 + 治理层 tool-call 打桩的顺序处理器。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v1/govern/tool-call" in url:
            body = json.loads(request.content.decode())
            assert body["agent_id"] == "research-agent"
            assert body["user_id"] == "planner"
            assert body["tool_name"] == "analyze_sales"
            assert request.headers.get("Authorization") == "Bearer workload-token"
            if tool_status == "allow":
                return httpx.Response(200, json={"status": "allow", "result": tool_result})
            return httpx.Response(200, json={"status": tool_status, "error_code": tool_error})
        return _kernel_handler(calls)(request)

    return _recording_client(handler)


def test_remote_results_executes_tool_and_reports_completed() -> None:
    stub = EntrypointStub(
        "http://kernel:8080",
        tool_url="http://govern:8000",
        tool_token="workload-token",
        client=_remote_client(tool_result={"summary": "ok"}),
    )
    payload = _remote_payload()
    status, _ = stub.handle_create(payload, "delegation-token-1")
    assert status == 201

    deadline = time.time() + 5
    while stub.task_status.get("task-1") != "completed" and time.time() < deadline:
        time.sleep(0.01)
    assert stub.task_status["task-1"] == "completed"
    # results 已回调且内核确认后 token 已清除
    assert "task-1" not in stub._task_tokens


def test_remote_results_tool_denied_reports_failed() -> None:
    stub = EntrypointStub(
        "http://kernel:8080",
        tool_url="http://govern:8000",
        tool_token="workload-token",
        client=_remote_client(tool_status="deny", tool_error="policy_denied"),
    )
    status, _ = stub.handle_create(_remote_payload(), "delegation-token-1")
    assert status == 201

    deadline = time.time() + 5
    while stub.task_status.get("task-1") != "failed" and time.time() < deadline:
        time.sleep(0.01)
    assert stub.task_status["task-1"] == "failed"


def test_remote_results_without_tool_url_skips_execution() -> None:
    calls: list[str] = []
    stub = EntrypointStub(
        "http://kernel:8080",
        client=_recording_client(_kernel_handler(calls)),
    )
    status, _ = stub.handle_create(_remote_payload(), "delegation-token-1")
    assert status == 201
    # 未配置 tool_url：不执行、不回调 results，任务停留在 running
    assert stub.task_status["task-1"] == "running"
    assert [c for c in calls if "results" in c] == []
