"""FastAPI 服务接入示例（v0.32.0）。

演示如何在 FastAPI 路由上使用治理装饰器。
启动：
    uvicorn examples.agent_sdk.fastapi_service:app --reload
"""

from __future__ import annotations

from typing import Any

try:
    from fastapi import FastAPI, Request
except ImportError:
    FastAPI = None  # type: ignore[misc,assignment]
    Request = None  # type: ignore[misc,assignment]

from loop_controller import GovernanceRuntime
from loop_controller.integrations.fastapi import governed_route

if FastAPI is None:
    app: Any = None
else:
    app = FastAPI()

    @app.post("/run-tool")
    @governed_route(tool_name="run_tool")
    async def run_tool(request: Request) -> dict[str, Any]:
        """治理后的工具执行入口。"""
        return {"ok": True}


async def startup_event() -> None:
    if app is not None:
        await GovernanceRuntime.from_config(
            "config",
            agent_id="demo_agent",
            user_id="demo_user",
        )
