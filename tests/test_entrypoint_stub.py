"""Entrypoint stub（任务执行闭环 + 取消）测试。

覆盖：投递接收与 token 校验、create/accept/start 回调链路、回调失败重试
语义、重复投递幂等、取消通知（token 校验/状态机/幂等/重投防御）。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import httpx
from starlette.testclient import TestClient

from loop_controller.entrypoint_stub import EntrypointStub, create_app
from loop_controller.execution_security_constants import EXECUTION_RECEIPT_V1
from loop_controller.go_kernel_bridge import CURRENT_PROTOCOL_VERSION

TASK_PAYLOAD = {
    "protocol_version": CURRENT_PROTOCOL_VERSION,
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


def _kernel_handler(
    calls: list[str], *, create_code: int = 201, accept_code: int = 200, start_code: int = 200
):
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
    stub = EntrypointStub(
        "http://kernel:8080", client=_recording_client(lambda r: httpx.Response(500))
    )
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
    assert second_status == 200
    assert second_body == {"task_id": "task-1", "status": "cancelled"}


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
        json={"protocol_version": CURRENT_PROTOCOL_VERSION},
        headers={"Authorization": "Bearer delegation-token-1"},
    )
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelled"

    bad = client.post(
        "/a2a/v1/entrypoint/tasks/task-1/cancel",
        json={"protocol_version": CURRENT_PROTOCOL_VERSION},
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


def _remote_client(
    tool_status: str = "allow", tool_result: Any = None, tool_error: str = ""
) -> httpx.Client:
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
    assert stub._task_tokens["task-1"] == "delegation-token-1"


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


def _redelegate_payload() -> dict[str, Any]:
    payload = _remote_payload()
    payload["tool_name"] = "calculate_checksum"
    payload["arguments"] = {"path": "data/kb/report.txt"}
    payload["allow_redelegation"] = True
    payload["budget"] = {"token_count": 1000, "payment_amount": 0}
    return payload


def _redelegate_client(denied_reason: str = "") -> tuple[httpx.Client, dict[str, Any]]:
    """内核打桩：delegations 受理 + 子任务轮询 + results 记录。"""
    calls: list[str] = []
    recorded: dict[str, Any] = {}
    child_polls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/a2a/v1/delegations"):
            body = json.loads(request.content.decode())
            recorded["delegation"] = body
            assert request.headers.get("Authorization") == "Bearer agent-control-token"
            if denied_reason:
                return httpx.Response(
                    403, json={"allowed": False, "verdict": "deny", "reason": denied_reason}
                )
            return httpx.Response(
                200, json={"allowed": True, "verdict": "allow", "task_id": "child-1"}
            )
        if url.endswith("/a2a/v1/tasks/child-1"):
            assert request.headers.get("Authorization") == "Bearer agent-control-token"
            child_polls["count"] += 1
            if child_polls["count"] < 2:
                return httpx.Response(200, json={"task_id": "child-1", "status": "running"})
            return httpx.Response(
                200,
                json={
                    "task_id": "child-1",
                    "status": "completed",
                    "outcome": {"result": {"checksum": "ab12"}},
                    "consumed_budget": {"token_count": 9},
                },
            )
        if url.endswith("/results"):
            recorded["results"] = json.loads(request.content.decode())
        return _kernel_handler(calls)(request)

    return _recording_client(handler), recorded


def _redelegate_stub(client: httpx.Client) -> EntrypointStub:
    return EntrypointStub(
        "http://kernel:8080",
        agent_id="research-agent",
        control_token="agent-control-token",
        redelegate={"calculate_checksum": "specialist-agent"},
        client=client,
    )


def _wait_status(stub: EntrypointStub, expected: str) -> None:
    deadline = time.time() + 5
    while stub.task_status.get("task-1") != expected and time.time() < deadline:
        time.sleep(0.01)
    assert stub.task_status["task-1"] == expected


def test_redelegate_dispatches_child_and_aggregates_result() -> None:
    client, recorded = _redelegate_client()
    stub = _redelegate_stub(client)
    status, _ = stub.handle_create(_redelegate_payload(), "delegation-token-1")
    assert status == 201

    _wait_status(stub, "completed")
    delegation = recorded["delegation"]
    assert delegation["initiator_agent_id"] == "research-agent"
    assert delegation["target_agent_id"] == "specialist-agent"
    assert delegation["tool_name"] == "calculate_checksum"
    assert delegation["parent_task_id"] == "task-1"
    assert delegation["allowed_tools"] == ["calculate_checksum"]
    assert delegation["budget"]["token_count"] == 1000
    results = recorded["results"]
    assert results["status"] == "completed"
    assert results["consumed_budget"] == {"token_count": 9}
    assert results["outcome"]["child_task_id"] == "child-1"
    assert results["outcome"]["child_outcome"] == {"result": {"checksum": "ab12"}}


def test_clamp_consumed_caps_at_task_budget() -> None:
    payload = {"budget": {"token_count": 10, "payment_amount": 0}}
    assert EntrypointStub._clamp_consumed({"token_count": 9}, payload) == {"token_count": 9}
    assert EntrypointStub._clamp_consumed({"token_count": 99}, payload) == {"token_count": 10}
    # 无预算信封（cap=0）时任何自报消耗都会被钳到 0，保证结算不被 409 拒绝
    assert EntrypointStub._clamp_consumed({"token_count": 5}, {}) == {"token_count": 0}
    assert EntrypointStub._clamp_consumed(None, payload) == {"token_count": 0}


def test_redelegate_child_denied_reports_failed() -> None:
    client, recorded = _redelegate_client(denied_reason="scope denied")
    stub = _redelegate_stub(client)
    status, _ = stub.handle_create(_redelegate_payload(), "delegation-token-1")
    assert status == 201

    _wait_status(stub, "failed")
    results = recorded["results"]
    assert results["status"] == "failed"
    assert results["error_code"] == "child_delegation_denied"


def test_redelegate_without_permission_reports_failed() -> None:
    calls: list[str] = []
    stub = _redelegate_stub(_recording_client(_kernel_handler(calls)))
    payload = _redelegate_payload()
    payload["allow_redelegation"] = False
    status, _ = stub.handle_create(payload, "delegation-token-1")
    assert status == 201

    _wait_status(stub, "failed")
    assert [c for c in calls if "delegations" in c] == []


def test_concurrent_create_runs_callbacks_and_tool_once() -> None:
    entered = threading.Event()
    release = threading.Event()
    executed = threading.Event()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a2a/v1/entrypoint/tasks":
            entered.set()
            assert release.wait(5)
        if request.url.path == "/v1/govern/tool-call":
            executed.set()
            return httpx.Response(200, json={"status": "allow", "result": "ok"})
        return _kernel_handler(calls)(request)

    stub = EntrypointStub(
        "http://kernel:8080", tool_url="http://govern:8000", client=_recording_client(handler)
    )
    results: list[tuple[int, dict[str, Any]]] = []
    worker = threading.Thread(
        target=lambda: results.append(stub.handle_create(_remote_payload(), "delegation-token-1"))
    )
    worker.start()
    try:
        assert entered.wait(5)
        duplicate = stub.handle_create(_remote_payload(), "delegation-token-1")
        assert duplicate == (200, {"status": "accepted", "task_id": "task-1", "idempotent": True})
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert results[0][0] == 201
    assert executed.wait(5)
    _wait_status(stub, "completed")
    assert calls.count("/a2a/v1/entrypoint/tasks") == 1
    assert calls.count("/a2a/v1/entrypoint/tasks/task-1/accept") == 1
    assert calls.count("/a2a/v1/entrypoint/tasks/task-1/start") == 1
    assert calls.count("/a2a/v1/entrypoint/tasks/task-1/results") == 1


def test_cancel_during_remote_tool_prevents_results() -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/govern/tool-call":
            entered.set()
            assert release.wait(5)
            finished.set()
            return httpx.Response(200, json={"status": "allow", "result": "ok"})
        return _kernel_handler(calls)(request)

    stub = EntrypointStub(
        "http://kernel:8080", tool_url="http://govern:8000", client=_recording_client(handler)
    )
    assert stub.handle_create(_remote_payload(), "delegation-token-1")[0] == 201
    try:
        assert entered.wait(5)
        assert stub.handle_cancel("task-1", "delegation-token-1") == (
            200,
            {"task_id": "task-1", "status": "cancelled"},
        )
    finally:
        release.set()
    assert finished.wait(5)
    time.sleep(0.05)
    assert not any("results" in call for call in calls)
    assert stub.task_status["task-1"] == "cancelled"


def test_entrypoint_cli_uses_environment_tokens(monkeypatch) -> None:
    import uvicorn

    from loop_controller import cli, entrypoint_stub

    parser = cli._build_parser()
    args = parser.parse_args(["entrypoint-stub", "--kernel-url", "http://kernel:8080"])
    assert not hasattr(args, "tool_token")
    assert not hasattr(args, "control_token")
    for option in ("--tool-token", "--control-token"):
        assert option not in parser.format_help()
    monkeypatch.setenv("LOOP_CONTROLLER_ENTRYPOINT_TOOL_TOKEN", "workload-secret")
    monkeypatch.setenv("LOOP_CONTROLLER_ENTRYPOINT_CONTROL_TOKEN", "control-secret")
    captured: dict[str, Any] = {}

    def fake_create_app(url: str, **kwargs: Any) -> object:
        captured.update(url=url, **kwargs)
        return object()

    monkeypatch.setattr(entrypoint_stub, "create_app", fake_create_app)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
    assert cli._cmd_entrypoint_stub(args) == 0
    assert captured["tool_token"] == "workload-secret"
    assert captured["control_token"] == "control-secret"


def _strict_remote_payload() -> dict[str, Any]:
    payload = _remote_payload()
    payload.update(
        {
            "request_id": "req-1",
            "interaction_id": "int-1",
            "decision_id": "dec-1",
            "tenant_id": "tenant-1",
            "target_workload_id": "workload-research",
            "target_instance_id": "instance-1",
        }
    )
    return payload


def test_remote_results_strict_sends_binding_fields_and_receipt() -> None:
    """strict 语义：tool-call 携带全部绑定字段，results 回报透传 execution_receipt。"""
    calls: list[str] = []
    recorded: dict[str, Any] = {}
    receipt = {"type": "controller_execution_record", "receipt_id": "rcpt-1"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/govern/tool-call":
            recorded["tool_call"] = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "status": "allow",
                    "result": {"summary": "ok"},
                    "execution_receipt": receipt,
                    "terminal_status": "success",
                },
            )
        if request.url.path.endswith("/results"):
            recorded["results"] = json.loads(request.content.decode())
        return _kernel_handler(calls)(request)

    stub = EntrypointStub(
        "http://kernel:8080",
        tool_url="http://govern:8000",
        tool_token="workload-token",
        client=_recording_client(handler),
    )
    assert stub.handle_create(_strict_remote_payload(), "delegation-token-1")[0] == 201
    deadline = time.time() + 5
    while stub.task_status.get("task-1") != "completed" and time.time() < deadline:
        time.sleep(0.01)
    assert stub.task_status["task-1"] == "completed"
    tool_call = recorded["tool_call"]
    assert tool_call["request_id"] == "req-1"
    assert tool_call["interaction_id"] == "int-1"
    assert tool_call["decision_id"] == "dec-1"
    assert tool_call["tenant_id"] == "tenant-1"
    assert tool_call["target_workload_id"] == "workload-research"
    assert tool_call["target_instance_id"] == "instance-1"
    assert tool_call["call_id"] == "call-task-1"
    assert tool_call["task_id"] == "task-1"
    assert tool_call["delegation_token"] == "delegation-token-1"
    assert tool_call["required_security_capabilities"] == [EXECUTION_RECEIPT_V1]
    results = recorded["results"]
    assert results["execution_receipt"] == receipt
    assert results["terminal_status"] == "success"


def test_redelegate_strict_requires_workload_mapping() -> None:
    """strict 语义：payload 带 tenant/workload 绑定时必须配置子委托 workload 映射。"""
    calls: list[str] = []
    stub = _redelegate_stub(_recording_client(_kernel_handler(calls)))
    payload = _redelegate_payload()
    payload["tenant_id"] = "tenant-1"
    payload["target_workload_id"] = "workload-research"
    assert stub.handle_create(payload, "delegation-token-1")[0] == 201
    _wait_status(stub, "failed")
    assert [c for c in calls if "delegations" in c] == []


def test_redelegate_strict_passes_tenant_and_workload_binding() -> None:
    """strict 语义：子委托 body 继承 tenant_id 并携带映射的 target_workload_id。"""
    client, recorded = _redelegate_client()
    stub = EntrypointStub(
        "http://kernel:8080",
        agent_id="research-agent",
        control_token="agent-control-token",
        redelegate={"calculate_checksum": "specialist-agent"},
        redelegate_workloads={"specialist-agent": "workload-specialist"},
        client=client,
    )
    payload = _redelegate_payload()
    payload["tenant_id"] = "tenant-1"
    payload["target_workload_id"] = "workload-research"
    assert stub.handle_create(payload, "delegation-token-1")[0] == 201
    _wait_status(stub, "completed")
    delegation = recorded["delegation"]
    assert delegation["tenant_id"] == "tenant-1"
    assert delegation["target_workload_id"] == "workload-specialist"
