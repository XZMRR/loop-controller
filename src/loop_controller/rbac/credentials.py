"""静态角色凭证解析（v0.52）。

复用 v0.49 approval_auth 的 principal + token_env 模式：凭证不落盘，
token 经 hmac.compare_digest 常量时间比较；轮换 = 替换环境变量值，旧 token 即时失效。
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass

from loop_controller.rbac.models import AuthenticatedPrincipal


@dataclass(frozen=True)
class StaticCredential:
    principal: str
    tenant_id: str | None
    token_env: str
    roles: tuple[str, ...] = ()


class StaticCredentialResolver:
    """从 rbac.yaml 静态凭证表解析 AuthenticatedPrincipal。"""

    def __init__(
        self,
        credentials: tuple[StaticCredential, ...],
        environ: dict[str, str] | None = None,
    ) -> None:
        self._credentials = credentials
        self._environ = environ if environ is not None else dict(os.environ)

    def resolve(
        self, principal: str | None, token: str | None
    ) -> AuthenticatedPrincipal | None:
        """fail-closed：任何不匹配/缺失一律返回 None。token 不合法长度提前拒绝。"""
        if not principal or not token:
            return None
        for cred in self._credentials:
            if cred.principal != principal:
                continue
            expected = self._environ.get(cred.token_env)
            if not expected:
                continue
            if len(token) != len(expected):
                continue
            if hmac.compare_digest(token.encode(), expected.encode()):
                return AuthenticatedPrincipal(
                    principal_id=cred.principal,
                    tenant_id=cred.tenant_id,
                    roles=cred.roles,
                    auth_method="static-credential",
                )
        return None
