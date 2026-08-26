"""Docker 容器执行后端示例（v0.25.0）。

演示如何通过 Docker 启动一次性容器执行工具。需要安装 docker SDK：
  pip install docker

本文件为占位/示例实现；生产环境应结合 harness_server.py 镜像使用。
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any


def execute_in_docker(
    tool: str,
    arguments: dict[str, Any],
    *,
    image: str = "loop-controller/harness:latest",
    network_mode: str | None = "none",
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """启动一次性 Docker 容器执行工具调用。"""
    try:
        import docker
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Docker SDK 未安装，请执行：pip install docker"
        ) from exc

    client = docker.from_env()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"tool": tool, "args": arguments}, f)
        input_path = f.name

    try:
        container = client.containers.run(
            image,
            ["/input.json"],
            network_mode=network_mode,
            mounts=[
                docker.types.Mount(
                    target="/input.json", source=input_path, type="bind", read_only=True
                )
            ],
            remove=True,
            detach=False,
            stdout=True,
            stderr=True,
        )
        return json.loads(container.decode("utf-8"))
    except json.JSONDecodeError:
        return {
            "status": "error",
            "error_code": "docker_output_not_json",
            "content": container.decode("utf-8", errors="replace"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error_code": "docker_error", "content": str(exc)}
    finally:
        os.unlink(input_path)


if __name__ == "__main__":
    print(execute_in_docker("shell", {"command": "echo", "args": ["hello"]}))
