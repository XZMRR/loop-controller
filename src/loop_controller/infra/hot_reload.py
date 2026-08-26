"""HTTP 工具规格与 Secret 热更新调度器（v0.22.0）。

- 默认使用 asyncio 轮询（不依赖 watchdog）；
- 检测到变化后重新加载 HTTP 工具规格与 secret 文件；
- 更新失败时保留旧配置并记录告警。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from loop_controller.executors.http_executor import HTTPExecutor
from loop_controller.infra.config_loader import ConfigLoader
from loop_controller.secrets import SecretBroker

logger = logging.getLogger(__name__)


class HotReloader:
    """HTTP 工具配置与 secret 文件热更新器。"""

    def __init__(
        self,
        *,
        config_dir: str | Path,
        config_loader: ConfigLoader,
        http_executor: HTTPExecutor,
        secret_broker: SecretBroker,
        http_tool_names: set[str] | None = None,
        poll_interval_seconds: float = 30.0,
        enabled: bool = True,
    ) -> None:
        self._config_dir = Path(config_dir)
        self._loader = config_loader
        self._http_executor = http_executor
        self._secret_broker = secret_broker
        # 与 Runtime 共享的可变集合；热更新 HTTP 工具后同步刷新。
        self._http_tool_names = http_tool_names
        self._poll_interval = poll_interval_seconds
        self._enabled = enabled
        self._task: asyncio.Task[Any] | None = None
        self._stop_event = asyncio.Event()
        self._snapshots: dict[Path, float] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动热更新轮询任务。"""
        if not self._enabled or self._task is not None:
            return
        self._snapshot()
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop(), name="hot_reloader")

    async def stop(self) -> None:
        """停止热更新轮询任务。"""
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    # ------------------------------------------------------------------
    # 轮询与检测
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval,
                )
            except TimeoutError:
                await self._check_once()

    async def _check_once(self) -> None:
        if not self._changed():
            return
        logger.info("检测到 HTTP 工具或 secret 配置变化，触发热更新")
        await self._reload()
        self._snapshot()

    def _snapshot(self) -> None:
        self._snapshots.clear()
        for path in self._watched_paths():
            if path.exists():
                self._snapshots[path] = path.stat().st_mtime

    def _changed(self) -> bool:
        current_paths = set(self._watched_paths())
        # 新增或删除文件
        if set(self._snapshots) != current_paths:
            return True
        for path, previous in self._snapshots.items():
            if not path.exists():
                return True
            if path.stat().st_mtime != previous:
                return True
        return False

    def _watched_paths(self) -> list[Path]:
        paths: list[Path] = []
        http_tools = self._config_dir / "http_tools.yaml"
        if http_tools.exists():
            paths.append(http_tools)
        secrets_yaml = self._config_dir / "secrets.yaml"
        if secrets_yaml.exists():
            paths.append(secrets_yaml)
        # 监控 secret 后端 base_path 下的所有 .json 文件；优先使用 backend 声明的路径。
        secrets_dir = self._secrets_dir()
        if secrets_dir is not None and secrets_dir.exists():
            paths.extend(sorted(secrets_dir.rglob("*.json")))
        return paths

    def _secrets_dir(self) -> Path | None:
        """从 SecretBroker backend 提取 secret 文件根目录；非文件后端返回 None。"""
        backend = self._secret_broker
        if hasattr(backend, "base_path"):
            return Path(backend.base_path)  # type: ignore[attr-defined]
        # 兜底：兼容旧配置
        default = self._config_dir.parent / "secrets"
        return default if default.exists() else None

    # ------------------------------------------------------------------
    # 重新加载
    # ------------------------------------------------------------------

    async def _reload(self) -> None:
        """原子更新 secret 与 HTTP 工具规格；失败保留旧配置。"""
        try:
            await self._secret_broker.reload()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Secret Broker 热更新失败，保留旧 secret：%s", exc)

        try:
            new_specs = self._loader.reload_http_tools(self._config_dir)
            self._http_executor.update_tool_specs(new_specs)
            if self._http_tool_names is not None:
                self._http_tool_names.clear()
                self._http_tool_names.update(new_specs.keys())
            logger.info("HTTP 工具规格热更新完成，共 %d 个工具", len(new_specs))
        except Exception as exc:  # noqa: BLE001
            logger.warning("HTTP 工具规格热更新失败，保留旧配置：%s", exc)
