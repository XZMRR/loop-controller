"""策略引擎（§6.4）.

``OPAPolicyEngine`` 通过 HTTP 查询本地 OPA sidecar，**任何异常路径
（连接失败、超时、非 2xx、返回缺 verdict）统一返回 deny**——fail-closed
逻辑集中在一处，不散落于 try/except。

``build_policy_input`` 是 Python ↔ Rego 的唯一契约点（§6.3 的 input schema），
改 schema 只改这里。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import httpx

from loop_controller.models import ActionProposal, Agent, CapabilityProfile, RiskProfile

FAIL_CLOSED_DENY = {
    "verdict": "deny",
    "reason": "policy engine unavailable",
    "policy_hits": ["fail_closed"],
}


@runtime_checkable
class PolicyEngine(Protocol):
    """策略引擎接口（全链路 async，禁止同步阻塞调用）。"""

    async def evaluate(self, package: str, input_doc: dict[str, Any]) -> dict[str, Any]:
        """评估输入文档，返回策略判定结果.

        Returns:
            至少包含 verdict 和 reason 的字典。
        """
        ...


class OPAPolicyEngine:
    """调用本地 OPA HTTP 服务的策略引擎.

    使用 OPA REST API：``POST /v1/data/<package>``（包名以 ``/`` 分隔）。
    需要先启动 OPA：``opa run --server --bundle policies/``。
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8181", timeout: float = 2.0) -> None:
        """初始化.

        Args:
            base_url: OPA HTTP 服务地址。
            timeout: 请求超时（秒）。
        """
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def evaluate(self, package: str, input_doc: dict[str, Any]) -> dict[str, Any]:
        """通过 HTTP 调用 OPA；任何异常一律 fail-closed 返回 deny."""
        url = f"{self._base_url}/v1/data/{package.replace('.', '/')}"
        try:
            # trust_env=False：访问本地 OPA 必须绕过系统代理（代理会返回 502）
            async with httpx.AsyncClient(trust_env=False, timeout=self._timeout) as client:
                resp = await client.post(url, json={"input": input_doc})
                resp.raise_for_status()
                body = resp.json()
        except Exception:  # noqa: BLE001 - fail-closed 统一兜底
            return dict(FAIL_CLOSED_DENY)

        result = body.get("result", {})
        if isinstance(result, dict) and "result" in result:
            result = result["result"]
        decision = result.get("decision") if isinstance(result, dict) else None
        if not isinstance(decision, dict) or decision.get("verdict") not in (
            "allow", "deny", "modify", "require_approval",
        ):
            return dict(FAIL_CLOSED_DENY)
        return decision


def build_policy_input(
    proposal: ActionProposal,
    agent: Agent,
    profile: CapabilityProfile,
    session_risk: RiskProfile | None = None,
    context_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 Rego input 文档（§6.3 的 JSON schema，唯一契约点）.

    ``task_context`` 由 ``build_governance_context`` 生成的治理上下文填充，
    ``description`` 原文不进（防 prompt injection 借道策略引擎）。
    v1.2 扩展 ``session_risk``，供 default.rego 的 session_risk_gate 使用。
    v0.3.0 扩展 ``context_meta``，保留上下文构造元数据供审计与策略使用。
    """
    doc: dict[str, Any] = {
        "tool_name": proposal.tool_name,
        "arguments": proposal.arguments,
        "risk_level": proposal.risk_level,
        "risk_tags": proposal.risk_tags,
        "task_context": proposal.task_context,
        "action": {
            "combination_risk_tags": proposal.combination_risk_tags,
            "combination_risk_score": proposal.combination_risk_score,
            "authority_token_ids": proposal.authority_token_ids,
        },  # v0.10.0/0.11.0：能力组合风险 + 动态权限令牌进入 Rego input
        "agent": {
            "agent_id": agent.agent_id,
            "owner_id": agent.owner_id,
        },
        "profile": {
            "tools": {
                name: {
                    "require_approval": perm.require_approval,
                    "allowed_args": perm.allowed_args,
                    "denied_args": perm.denied_args,
                }
                for name, perm in profile.tools.items()
            }
        },
    }
    if session_risk is not None:
        doc["session_risk"] = {
            "score": session_risk.cumulative_risk_score,
            "threshold": profile.session_risk_threshold,
            "denied_count": session_risk.denied_count,
            "recent_tags": list(session_risk.recent_tags),
            "session_id": session_risk.session_id,
        }
    if context_meta is not None:
        doc["context_meta"] = context_meta
    return doc
