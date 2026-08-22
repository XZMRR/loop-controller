"""适配器共享辅助：把 LoopController 的 GovernanceResult 转成自然语言。"""

from __future__ import annotations

from loop_controller.models import GovernanceResult


def format_governance_result(result: GovernanceResult) -> str:
    """把治理结果转成给 Agent 阅读的自然语言字符串。

    - ``allow``：返回工具执行结果。
    - ``require_approval``：提示 Agent 需要人工审批，并给出 request_id。
    - ``deny`` / ``error`` / ``blocked``：返回原因，让 Agent 决定是否重试或终止。
    """
    if result.status == "require_approval":
        return (
            f"[requires approval] request_id={result.request_id}. "
            f"Approve via 'lc approvals approve {result.request_id}', then retry."
        )

    if result.status == "deny":
        return f"[denied] {result.reason}"

    if result.status == "error":
        return f"[error] {result.error_code}: {result.reason}"

    if result.status == "blocked":
        return f"[blocked] {result.error_code}: {result.reason}"

    return str(result.content if result.content is not None else "")
