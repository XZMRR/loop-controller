"""Docker Harness backend（v0.32.0）。

通过 ``docker run`` 启动一次性容器执行工具；默认 ``--network none``。
要求目标镜像内包含兼容 Harness 协议 v2 的 runner。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from loop_controller.executors.harness_models import DockerBackendConfig
from loop_controller.executors.harness_protocol import (
    HarnessContext,
    HarnessExecuteRequest,
    HarnessExecuteResponse,
    HarnessSandbox,
)
from loop_controller.models import ToolResult

logger = logging.getLogger(__name__)


class DockerHarnessBackend:
    """Docker 容器化 Harness backend。"""

    def __init__(self, config: DockerBackendConfig) -> None:
        self.config = config

    async def start(self) -> None:
        """检查 docker CLI 可用。"""
        proc = await asyncio.create_subprocess_exec(
            "docker", "--version",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode != 0:
            raise RuntimeError("docker CLI 不可用")

    async def stop(self) -> None:
        return

    async def check_health(self) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "docker", "info",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0

    def _build_command(self) -> list[str]:
        """构造 ``docker run`` 命令。"""
        cmd = [
            "docker", "run", "--rm", "-i",
            "--network", self.config.network_mode or "none",
        ]
        for key, value in self.config.env.items():
            cmd.extend(["-e", f"{key}={value}"])
        for mount in self.config.mounts:
            source = mount.get("source", "")
            target = mount.get("target", "")
            read_only = ":ro" if mount.get("read_only", False) else ""
            if source and target:
                cmd.extend(["-v", f"{source}:{target}{read_only}"])
        cmd.append(self.config.image)
        return cmd

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: Any,
        sandbox: Any,
    ) -> ToolResult:
        from loop_controller.executors.harness_executor import _HTTPHarnessClient

        request = HarnessExecuteRequest(
            tool=tool_name,
            arguments=arguments,
            context=HarnessContext(
                call_id=context.call_id,
                task_id=context.task_id,
                agent_id=context.agent_id,
                user_id=context.user_id,
                session_id=context.session_id,
                tenant_id=context.tenant_id,
            ),
            sandbox=HarnessSandbox.model_validate(sandbox.model_dump()) if sandbox is not None else HarnessSandbox(),
        )
        cmd = self._build_command()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        input_bytes = request.model_dump_json().encode("utf-8")
        try:
            async with asyncio.timeout(sandbox.timeout_seconds if sandbox else 30):
                stdout, stderr = await proc.communicate(input_bytes)
        except TimeoutError:
            proc.kill()
            return _HTTPHarnessClient._error_result(
                context, tool_name, "Docker 容器执行超时", "harness_timeout",
            )
        try:
            response = HarnessExecuteResponse.model_validate_json(stdout.decode("utf-8", errors="replace"))
        except Exception as exc:
            logger.warning("Docker Harness 返回非法 JSON: %s", exc)
            return _HTTPHarnessClient._error_result(
                context, tool_name, f"非法响应: {stdout!r}", "harness_invalid_response",
            )

        if response.status == "success" and response.effective_sandbox is None:
            return _HTTPHarnessClient._error_result(
                context,
                tool_name,
                "Harness 响应缺少 effective_sandbox 回执",
                "harness_sandbox_attestation_missing",
            )
        if response.status == "success" and not _HTTPHarnessClient._sandbox_matches(
            request.sandbox, response.effective_sandbox
        ):
            return _HTTPHarnessClient._error_result(
                context,
                tool_name,
                "Harness 实际生效沙箱与请求不一致",
                "harness_sandbox_violation",
                {
                    "requested_sandbox": request.sandbox.model_dump(mode="json"),
                    "effective_sandbox": response.effective_sandbox.model_dump(mode="json")
                    if response.effective_sandbox
                    else None,
                },
            )
        metadata = dict(response.metadata)
        if response.evidence is not None:
            metadata["harness_evidence"] = response.evidence.model_dump(mode="json")
        return ToolResult(
            call_id=context.call_id,
            task_id=context.task_id,
            tool_name=tool_name,
            status=response.status,
            content=response.content,
            error_code=response.error_code,
            metadata=metadata,
        )
