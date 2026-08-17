"""策略存储（§4.3）.

Rego 策略文件的持久化与版本标识：
- 存储形态：本地目录 ``policies/``，文件名即策略名；
- 版本号 = 内容哈希：对目录内所有 ``.rego`` 文件**按文件名排序**后连接内容
  取 SHA-256 前 12 位（排序不可省，否则版本号不稳定）。
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class PolicyStore(Protocol):
    """策略存储接口."""

    def policy_path(self, name: str) -> str:
        """返回 rego 文件路径."""
        ...

    def current_version(self) -> str:
        """全部策略文件内容连接后的 SHA-256 前 12 位."""
        ...

    def list_policies(self) -> list[str]:
        """列出策略名（不含 .rego 后缀）。"""
        ...


class FilePolicyStore:
    """基于本地 ``policies/`` 目录的策略存储实现。"""

    def __init__(self, policy_dir: str | Path) -> None:
        """初始化.

        Args:
            policy_dir: Rego 策略文件目录。
        """
        self._dir = Path(policy_dir)

    def policy_path(self, name: str) -> str:
        """返回 ``<policy_dir>/<name>.rego`` 的路径."""
        return str(self._dir / f"{name}.rego")

    def list_policies(self) -> list[str]:
        """列出目录内全部 ``.rego`` 文件名（不含后缀），按文件名排序."""
        if not self._dir.exists():
            return []
        return sorted(p.stem for p in self._dir.glob("*.rego"))

    def current_version(self) -> str:
        """对全部 ``.rego`` 文件按文件名排序后连接内容，取 SHA-256 前 12 位."""
        hasher = sha256()
        for name in self.list_policies():
            hasher.update((name + "\n").encode("utf-8"))
            hasher.update(self._dir.joinpath(f"{name}.rego").read_bytes())
        return hasher.hexdigest()[:12]
