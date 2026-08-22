"""LoopController 测试公共辅助。

v0.14.0 起核心产品入口只有 ``LoopController``；本模块为依赖 OPA/配置的
端到端测试提供快捷构造方式。
"""

from __future__ import annotations

import os
from pathlib import Path

from loop_controller.controller import LoopController, build_controller

REPO_ROOT = Path(__file__).resolve().parent.parent


def env_extra() -> dict[str, str]:
    """确保 MCP 子进程能找到项目源码。"""
    return {"PYTHONPATH": str(REPO_ROOT / "src")}


def _set_hmac_key() -> None:
    os.environ.setdefault(
        "LOOP_CONTROLLER_AUDIT_HMAC_KEY",
        "a" * 64,
    )


async def controller_for(
    workdir: Path,
    opa_url: str,
    *,
    extra_env: dict[str, str] | None = None,
) -> LoopController:
    """从临时工作目录构造并启动 LoopController。

    Args:
        extra_env: 除 PYTHONPATH 外额外注入给 MCP 子进程的环境变量。
    """
    from loop_controller.infra.config_loader import ConfigLoader

    _set_hmac_key()
    config = ConfigLoader().load(workdir / "config", opa_base_url=opa_url)
    mcp_env = env_extra()
    if extra_env is not None:
        mcp_env.update(extra_env)
    controller = await build_controller(config, opa_url=opa_url, env_extra=mcp_env)
    await controller.start()
    return controller
