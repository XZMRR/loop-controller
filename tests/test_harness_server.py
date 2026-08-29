"""v0.27 参考 Harness 服务安全契约测试。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import httpx
import pytest

from examples.contrib.harness import harness_server


@pytest.fixture(autouse=True)
def reset_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_AUTH_TYPE", "none")
    monkeypatch.delenv("HARNESS_API_KEY", raising=False)
    monkeypatch.delenv("HARNESS_ALLOWED_ENV", raising=False)
    harness_server._nonce_store = harness_server._NonceStore(capacity=10)


def _payload(tool: str = "echo", arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "tool": tool,
        "arguments": arguments if arguments is not None else {"text": "hello"},
        "context": {"call_id": "c1", "task_id": "t1", "agent_id": "a1", "user_id": "u1"},
        "sandbox": {"timeout_seconds": 1, "max_output_bytes": 1024},
    }


async def _post(
    payload: dict[str, Any], headers: dict[str, str] | None = None
) -> httpx.Response:
    transport = httpx.ASGITransport(app=harness_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/harness/v2/execute",
            headers={"x-harness-protocol-version": "2", **(headers or {})},
            content=json.dumps(payload, separators=(",", ":")),
        )


def _hmac_headers(body: bytes, nonce: str, timestamp: int | None = None) -> dict[str, str]:
    timestamp_text = str(timestamp if timestamp is not None else int(time.time()))
    canonical = "\n".join(
        (
            "POST",
            "/harness/v2/execute",
            "2",
            "test-key",
            timestamp_text,
            nonce,
            hashlib.sha256(body).hexdigest(),
        )
    )
    signature = base64.b64encode(
        hmac.new(b"secret", canonical.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "x-harness-protocol-version": "2",
        "x-harness-key-id": "test-key",
        "x-harness-timestamp": timestamp_text,
        "x-harness-nonce": nonce,
        "x-harness-signature": signature,
    }


@pytest.mark.asyncio
async def test_protocol_and_tool_schema_are_strict() -> None:
    transport = httpx.ASGITransport(app=harness_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unsupported = await client.post(
            "/harness/v2/execute",
            json=_payload(),
            headers={"x-harness-protocol-version": "0"},
        )
    assert unsupported.json()["error_code"] == "harness_protocol_unsupported"

    invalid = await _post(_payload(arguments={"text": 1, "unexpected": True}))
    assert invalid.status_code == 400
    assert invalid.json()["error_code"] == "harness_invalid_request"


@pytest.mark.asyncio
async def test_api_key_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_AUTH_TYPE", "api_key")
    monkeypatch.setenv("HARNESS_API_KEY", "server-secret")
    missing = await _post(_payload())
    failed = await _post(_payload(), {"x-harness-api-key": "wrong"})
    success = await _post(_payload(), {"x-harness-api-key": "server-secret"})
    assert missing.json()["error_code"] == "harness_auth_required"
    assert failed.json()["error_code"] == "harness_auth_failed"
    assert success.json()["status"] == "success"


@pytest.mark.asyncio
async def test_hmac_raw_body_signature_and_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_AUTH_TYPE", "hmac_sha256")
    monkeypatch.setenv("HARNESS_KEY_ID", "test-key")
    monkeypatch.setenv("HARNESS_SIGNING_KEY", "secret")
    body = json.dumps(_payload(), separators=(",", ":")).encode()
    headers = _hmac_headers(body, "nonce-1")
    transport = httpx.ASGITransport(app=harness_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/harness/v2/execute", headers=headers, content=body)
        replay = await client.post("/harness/v2/execute", headers=headers, content=body)
        changed = await client.post("/harness/v2/execute", headers=_hmac_headers(body, "nonce-2"), content=body + b" ")
    assert first.status_code == 200
    assert replay.json()["error_code"] == "harness_replay_detected"
    assert changed.json()["error_code"] == "harness_auth_failed"


@pytest.mark.asyncio
async def test_hmac_rejects_expired_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_AUTH_TYPE", "hmac_sha256")
    monkeypatch.setenv("HARNESS_KEY_ID", "test-key")
    monkeypatch.setenv("HARNESS_SIGNING_KEY", "secret")
    body = json.dumps(_payload(), separators=(",", ":")).encode()
    transport = httpx.ASGITransport(app=harness_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/harness/v2/execute",
            headers=_hmac_headers(body, "old", int(time.time()) - 120),
            content=body,
        )
    assert response.json()["error_code"] == "harness_auth_failed"


class _FakeProcess:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", *, blocked: bool = False) -> None:
        self.stdout = harness_server.asyncio.StreamReader()
        self.stderr = harness_server.asyncio.StreamReader()
        if not blocked:
            self.stdout.feed_data(stdout)
            self.stdout.feed_eof()
            self.stderr.feed_data(stderr)
            self.stderr.feed_eof()
        self.returncode: int | None = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@pytest.mark.asyncio
async def test_output_limit_terminates_process(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(stdout=b"a" * 800, stderr=b"b" * 800)

    async def create(*args: Any, **kwargs: Any) -> _FakeProcess:
        return proc

    monkeypatch.setattr(harness_server.asyncio, "create_subprocess_exec", create)
    response = await _post(_payload("shell", {"command": "echo", "args": []}))
    assert response.json()["error_code"] == "harness_output_limit_exceeded"
    assert proc.killed
    assert proc.returncode is not None


@pytest.mark.asyncio
async def test_timeout_terminates_and_reaps_process(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(blocked=True)

    async def create(*args: Any, **kwargs: Any) -> _FakeProcess:
        return proc

    class ImmediateTimeout:
        async def __aenter__(self) -> None:
            raise TimeoutError

        async def __aexit__(self, *args: Any) -> None:
            return None

    monkeypatch.setattr(harness_server.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(harness_server.asyncio, "timeout", lambda _: ImmediateTimeout())
    response = await _post(_payload("shell", {"command": "echo", "args": []}))
    assert response.json()["error_code"] == "harness_timeout"
    assert proc.killed
    assert proc.returncode is not None


@pytest.mark.asyncio
async def test_cancellation_terminates_and_reaps_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _FakeProcess(blocked=True)

    async def create(*args: Any, **kwargs: Any) -> _FakeProcess:
        return proc

    monkeypatch.setattr(harness_server.asyncio, "create_subprocess_exec", create)
    task = harness_server.asyncio.create_task(
        harness_server._execute_shell(
            {"command": "echo", "args": []},
            type(
                "Sandbox",
                (),
                {
                    "timeout_seconds": 30,
                    "max_output_bytes": 1024,
                    "network_policy": "deny_all",
                    "allowed_hosts": [],
                    "file_policy": "deny_all",
                    "allowed_paths": [],
                    "readonly_paths": [],
                    "env_whitelist": [],
                    "process_policy": "deny_all",
                    "allowed_commands": [],
                    "evidence_capture": "none",
                    "resource_limits": None,
                },
            )(),
        )
    )
    await harness_server.asyncio.sleep(0)
    task.cancel()

    with pytest.raises(harness_server.asyncio.CancelledError):
        await task
    assert proc.killed
    assert proc.returncode is not None


@pytest.mark.asyncio
async def test_sandbox_fail_closed_and_minimal_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsupported_payload = _payload("shell", {"command": "echo", "args": []})
    unsupported_payload["sandbox"]["network_policy"] = "allow_list"
    unsupported_payload["sandbox"]["allowed_hosts"] = ["example.com"]
    unsupported = await _post(unsupported_payload)
    assert unsupported.json()["error_code"] == "harness_sandbox_unsupported"

    captured: dict[str, Any] = {}
    proc = _FakeProcess(stdout=b"ok")

    async def create(*args: Any, **kwargs: Any) -> _FakeProcess:
        captured["env"] = kwargs["env"]
        return proc

    monkeypatch.setenv("HARNESS_ALLOWED_ENV", "SAFE_VALUE")
    monkeypatch.setenv("SAFE_VALUE", "allowed")
    monkeypatch.setenv("HOST_SECRET", "must-not-leak")
    monkeypatch.setattr(harness_server.asyncio, "create_subprocess_exec", create)
    payload = _payload("shell", {"command": "echo", "args": []})
    payload["sandbox"]["env_whitelist"] = ["SAFE_VALUE"]
    success = await _post(payload)
    assert success.json()["status"] == "success"
    assert captured["env"]["SAFE_VALUE"] == "allowed"
    assert "HOST_SECRET" not in captured["env"]

    payload["sandbox"]["env_whitelist"] = ["HOST_SECRET"]
    denied = await _post(payload)
    assert denied.json()["error_code"] == "harness_sandbox_violation"


@pytest.mark.asyncio
async def test_health_does_not_expose_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_SIGNING_KEY", "never-return-this")
    transport = httpx.ASGITransport(app=harness_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.json() == {"status": "ok", "protocol_version": "2"}
    assert "never-return-this" not in response.text
