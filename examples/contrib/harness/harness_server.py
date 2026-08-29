"""参考 Harness 服务器（v0.27.0 安全契约）。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from loop_controller.executors.harness_protocol import (
    HARNESS_EXECUTE_PATH,
    HARNESS_PROTOCOL_VERSION,
    HarnessEvidence,
    HarnessExecuteRequest,
    HarnessExecuteResponse,
)

logger = logging.getLogger(__name__)
_MAX_NONCES = 10_000
_MINIMUM_ENV_NAMES = ("SYSTEMROOT", "WINDIR")
_ALLOWED_SHELL_COMMANDS: dict[str, list[str]] = {
    "echo": ["echo"],
    "ls": ["ls"],
    "pwd": ["pwd"],
}


class _NonceStore:
    def __init__(self, capacity: int = _MAX_NONCES) -> None:
        self._capacity = capacity
        self._entries: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._lock = asyncio.Lock()

    async def add(self, key_id: str, nonce: str, expires_at: float) -> bool:
        now = time.time()
        async with self._lock:
            while self._entries:
                first_key = next(iter(self._entries))
                if self._entries[first_key] > now:
                    break
                self._entries.popitem(last=False)
            key = (key_id, nonce)
            if key in self._entries:
                return False
            self._entries[key] = expires_at
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)
            return True


_nonce_store = _NonceStore()


def _error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        HarnessExecuteResponse(
            status="error", error_code=code, content=message
        ).model_dump(mode="json"),
        status_code=status_code,
    )


def _auth_type() -> str:
    return os.environ.get("HARNESS_AUTH_TYPE", "none").strip().lower()


def _required_header(request: Request, name: str) -> str | None:
    value = request.headers.get(name)
    return value.strip() if value and value.strip() else None


async def _authenticate(request: Request, raw_body: bytes) -> JSONResponse | None:
    auth_type = _auth_type()
    if auth_type == "none":
        return None

    if auth_type == "api_key":
        supplied = _required_header(request, "x-harness-api-key")
        expected = os.environ.get("HARNESS_API_KEY")
        if supplied is None:
            return _error("harness_auth_required", "缺少 Harness 认证", 401)
        if not expected or not hmac.compare_digest(supplied, expected):
            return _error("harness_auth_failed", "Harness 认证失败", 401)
        return None

    if auth_type != "hmac_sha256":
        logger.error("Harness 认证配置无效")
        return _error("harness_auth_failed", "Harness 认证不可用", 503)

    names = (
        "x-harness-key-id",
        "x-harness-timestamp",
        "x-harness-nonce",
        "x-harness-signature",
    )
    values = [_required_header(request, name) for name in names]
    if any(value is None for value in values):
        return _error("harness_auth_required", "缺少 Harness 认证", 401)
    key_id, timestamp_text, nonce, supplied_signature = values
    assert key_id and timestamp_text and nonce and supplied_signature

    expected_key_id = os.environ.get("HARNESS_KEY_ID")
    key = os.environ.get("HARNESS_SIGNING_KEY")
    if not expected_key_id or not key or not hmac.compare_digest(key_id, expected_key_id):
        return _error("harness_auth_failed", "Harness 认证失败", 401)
    try:
        timestamp = int(timestamp_text)
        skew = int(os.environ.get("HARNESS_MAX_CLOCK_SKEW_SECONDS", "60"))
    except ValueError:
        return _error("harness_auth_failed", "Harness 认证失败", 401)
    if skew < 1 or abs(time.time() - timestamp) > skew:
        return _error("harness_auth_failed", "Harness 认证失败", 401)

    body_hash = hashlib.sha256(raw_body).hexdigest()
    canonical = "\n".join(
        (
            request.method,
            request.url.path,
            HARNESS_PROTOCOL_VERSION,
            key_id,
            timestamp_text,
            nonce,
            body_hash,
        )
    )
    expected_signature = base64.b64encode(
        hmac.new(key.encode(), canonical.encode(), hashlib.sha256).digest()
    ).decode("ascii")
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return _error("harness_auth_failed", "Harness 认证失败", 401)
    if not await _nonce_store.add(key_id, nonce, timestamp + skew):
        return _error("harness_replay_detected", "Harness 请求已被使用", 409)
    return None


def _validate_keys(value: Any, allowed: set[str], name: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是对象")
    extras = set(value) - allowed
    if extras:
        raise ValueError(f"{name} 包含未知字段")


def _validate_request_shape(payload: Any) -> None:
    _validate_keys(payload, {"tool", "arguments", "context", "sandbox"}, "请求")
    _validate_keys(
        payload.get("context"),
        {"call_id", "task_id", "agent_id", "user_id", "session_id", "tenant_id"},
        "context",
    )
    _validate_keys(
        payload.get("sandbox", {}),
        {
            "timeout_seconds",
            "max_output_bytes",
            "network_policy",
            "allowed_hosts",
            "file_policy",
            "allowed_paths",
            "readonly_paths",
            "env_whitelist",
            "process_policy",
            "allowed_commands",
            "evidence_capture",
            "resource_limits",
        },
        "sandbox",
    )


def _validate_echo(arguments: dict[str, Any]) -> None:
    _validate_keys(arguments, {"text"}, "echo arguments")
    if "text" not in arguments or not isinstance(arguments["text"], str):
        raise ValueError("echo.text 必须是字符串")


def _validate_shell(arguments: dict[str, Any]) -> None:
    _validate_keys(arguments, {"command", "args"}, "shell arguments")
    if not isinstance(arguments.get("command"), str):
        raise ValueError("shell.command 必须是字符串")
    args = arguments.get("args", [])
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise ValueError("shell.args 必须是字符串数组")


async def _execute_echo(
    arguments: dict[str, Any], sandbox: Any
) -> HarnessExecuteResponse:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return HarnessExecuteResponse(
        status="success",
        content={"echo": arguments["text"]},
        effective_sandbox=sandbox,
        evidence=HarnessEvidence(
            started_at=now,
            finished_at=now,
        ),
        metadata={"tool": "echo"},
    )


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        proc.kill()
    await proc.wait()


def _minimal_environment(env_whitelist: list[str]) -> dict[str, str]:
    configured = {
        name.strip()
        for name in os.environ.get("HARNESS_ALLOWED_ENV", "").split(",")
        if name.strip()
    }
    requested = set(env_whitelist)
    if not requested <= configured:
        raise ValueError("请求了服务端未允许的环境变量")
    names = requested | {name for name in _MINIMUM_ENV_NAMES if name in os.environ}
    return {name: os.environ[name] for name in names if name in os.environ}


async def _read_stream(
    stream: asyncio.StreamReader,
    output: bytearray,
    limit: int,
    lock: asyncio.Lock,
    exceeded: asyncio.Event,
    proc: asyncio.subprocess.Process,
) -> None:
    while chunk := await stream.read(4096):
        async with lock:
            remaining = limit - len(output)
            if len(chunk) > remaining:
                output.extend(chunk[: max(remaining, 0)])
                exceeded.set()
                if proc.returncode is None:
                    proc.kill()
                return
            output.extend(chunk)


async def _execute_shell(arguments: dict[str, Any], sandbox: Any) -> HarnessExecuteResponse:
    from datetime import UTC, datetime

    command = arguments["command"].strip()
    allowed = _ALLOWED_SHELL_COMMANDS.get(command)
    if allowed is None:
        return HarnessExecuteResponse(
            status="error",
            error_code="harness_sandbox_violation",
            content="命令不在允许列表中",
        )
    if (
        sandbox.network_policy != "deny_all"
        or sandbox.file_policy != "deny_all"
        or sandbox.process_policy != "deny_all"
    ):
        return HarnessExecuteResponse(
            status="error",
            error_code="harness_sandbox_unsupported",
            content="参考 Harness 无法执行请求的网络或文件系统沙箱约束",
        )
    try:
        env = _minimal_environment(sandbox.env_whitelist)
    except ValueError:
        return HarnessExecuteResponse(
            status="error",
            error_code="harness_sandbox_violation",
            content="请求的环境变量不在服务端允许列表中",
        )

    started_at = datetime.now(UTC)
    proc = await asyncio.create_subprocess_exec(
        *allowed,
        *arguments.get("args", []),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    assert proc.stdout is not None and proc.stderr is not None
    output = bytearray()
    exceeded = asyncio.Event()
    lock = asyncio.Lock()
    readers = [
        asyncio.create_task(_read_stream(stream, output, sandbox.max_output_bytes, lock, exceeded, proc))
        for stream in (proc.stdout, proc.stderr)
    ]
    try:
        async with asyncio.timeout(sandbox.timeout_seconds):
            await asyncio.gather(*readers)
            await proc.wait()
    except TimeoutError:
        for reader in readers:
            reader.cancel()
        await _terminate(proc)
        await asyncio.gather(*readers, return_exceptions=True)
        return HarnessExecuteResponse(
            status="error", error_code="harness_timeout", content="命令执行超时"
        )
    except asyncio.CancelledError:
        for reader in readers:
            reader.cancel()
        await _terminate(proc)
        await asyncio.gather(*readers, return_exceptions=True)
        raise

    if exceeded.is_set():
        await _terminate(proc)
        return HarnessExecuteResponse(
            status="error",
            error_code="harness_output_limit_exceeded",
            content="命令输出超过限制并已终止",
        )
    finished_at = datetime.now(UTC)
    return HarnessExecuteResponse(
        status="success" if proc.returncode == 0 else "error",
        content={"output": output.decode("utf-8", errors="replace"), "returncode": proc.returncode},
        effective_sandbox=sandbox,
        evidence=HarnessEvidence(
            started_at=started_at,
            finished_at=finished_at,
            exit_code=proc.returncode,
        ),
        metadata={"tool": "shell"},
    )


_TOOL_REGISTRY: dict[
    str, tuple[Callable[[dict[str, Any]], None], Callable[[dict[str, Any], Any], Any]]
] = {
    "echo": (_validate_echo, _execute_echo),
    "shell": (_validate_shell, _execute_shell),
}


async def _handle_execute(request: Request) -> JSONResponse:
    if request.headers.get("x-harness-protocol-version") != HARNESS_PROTOCOL_VERSION:
        return _error("harness_protocol_unsupported", "不支持的 Harness 协议版本", 400)

    raw_body = await request.body()
    auth_error = await _authenticate(request, raw_body)
    if auth_error is not None:
        return auth_error
    try:
        payload = await request.json()
        _validate_request_shape(payload)
        req = HarnessExecuteRequest.model_validate(payload)
    except Exception:  # noqa: BLE001
        return _error("harness_invalid_request", "请求格式非法", 400)

    registered = _TOOL_REGISTRY.get(req.tool)
    if registered is None:
        return _error("harness_tool_not_found", "Harness 不支持该工具", 404)
    validator, executor = registered
    try:
        validator(req.arguments)
    except ValueError:
        return _error("harness_invalid_request", "工具参数不符合 schema", 400)

    result = await executor(req.arguments, req.sandbox)
    return JSONResponse(result.model_dump(mode="json"))


async def _handle_health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "protocol_version": HARNESS_PROTOCOL_VERSION})


app = Starlette(
    routes=[
        Route(HARNESS_EXECUTE_PATH, _handle_execute, methods=["POST"]),
        Route("/health", _handle_health, methods=["GET"]),
    ],
)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("HARNESS_PORT", "9000"))
    uvicorn.run(app, host="127.0.0.1", port=port)
