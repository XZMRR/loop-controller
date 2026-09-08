"""身份管理兼容层（v0.20.0 前位于 loop_controller.infra.identity）。

新版身份 Provider 已迁移到 loop_controller.identity，本文件保留为兼容入口。
"""

from __future__ import annotations

from loop_controller.identity import (
    AgentIdentity,
    ConfigIdentityProvider,
    IdentityCredential,
    IdentityProvider,
)

__all__ = [
    "AgentIdentity",
    "ConfigIdentityProvider",
    "IdentityCredential",
    "IdentityProvider",
]
