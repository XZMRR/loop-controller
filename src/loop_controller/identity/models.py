"""身份认证相关模型。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AgentIdentity(BaseModel):
    """经 Loop Controller 验证后的 Agent 身份。"""

    model_config = ConfigDict(frozen=True)

    agent_id: str
    user_id: str
    harness_id: str | None = None
    profile_id: str
    issued_at: datetime = Field(default_factory=_utc_now)
    expires_at: datetime | None = None


class IdentityCredential(BaseModel):
    """外部调用方呈现的身份凭证。"""

    model_config = ConfigDict(frozen=True)

    token: str | None = None          # JWT / static token
    cert_cn: str | None = None        # mTLS 证书 CN
    cert_sans: list[str] = Field(default_factory=list)  # mTLS 证书 SAN
    cert_subject: str | None = None   # 完整证书 subject（备用）

    def as_dict(self) -> dict[str, Any]:
        """用于日志/审计时脱敏后的凭证摘要。"""
        return {
            "has_token": self.token is not None,
            "cert_cn": self.cert_cn,
            "cert_sans": self.cert_sans,
            "cert_subject": self.cert_subject,
        }
