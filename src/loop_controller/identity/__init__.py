"""可信身份控制平面（v0.20.0）。

负责把外部凭证（JWT / mTLS 证书 / 静态 token）验证并映射为内部 AgentIdentity。
"""

from __future__ import annotations

from loop_controller.identity.models import AgentIdentity, IdentityCredential
from loop_controller.identity.mtls import MTLSIdentityProvider
from loop_controller.identity.provider import IdentityProvider
from loop_controller.identity.static import ConfigIdentityProvider

__all__ = [
    "AgentIdentity",
    "ConfigIdentityProvider",
    "IdentityCredential",
    "IdentityProvider",
    "MTLSIdentityProvider",
]
